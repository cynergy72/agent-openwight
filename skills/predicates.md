---
name: predicates
description: WHERE/ON 조건의 sargability - 컬럼 함수, 암시적 형변환, 선행 와일드카드 LIKE, OR 조건, 대형 IN 리스트
category: 접근경로
findings: [NON_SARGABLE, IMPLICIT_CONVERSION, LIKE_LEADING_WILDCARD, OR_FILTER, LARGE_IN_LIST]
---
# 조건절 (Sargability)

조건의 컬럼 쪽을 그대로 두어야 PI 해시 접근, 파티션 제거, 인덱스, 통계 기반 추정이 가능하다.

## TP-PRED-01 컬럼을 함수로 감쌈
- 신호: `WHERE CAST(SALE_DT AS CHAR(7)) = '2024-01'`, `SUBSTR(col,1,6) = '202401'`, `TRIM(cd) = 'A'`,
  `EXTRACT(YEAR FROM dt) = 2024`, `col + 1 = 10`. EXPLAIN 잔여 조건에 함수가 보임.
- 조치(A): 상수 쪽을 변환. 날짜는 반열린 범위로:
```sql
WHERE SALE_DT >= DATE '2024-01-01' AND SALE_DT < DATE '2024-02-01'
```
  `EXTRACT(YEAR FROM dt) = 2024` → `dt >= DATE '2024-01-01' AND dt < DATE '2025-01-01'`.
  `TRIM(cd) = 'A'` → Teradata 는 CHAR 비교 시 뒤 공백을 무시하므로 보통 `cd = 'A'` 로 충분 (선행 공백은 예외).
- 검증: 해당 테이블 접근이 `n partitions of` / 인덱스 접근으로 바뀜, 신뢰도 상승.

## TP-PRED-02 문자 컬럼과 숫자 상수 비교 (암시적 형변환)
- 신호: 카탈로그상 CHAR/VARCHAR 컬럼을 `WHERE CUST_CD = 123` 처럼 숫자와 비교
- 원인: Teradata 는 문자 ↔ 숫자 비교 시 **문자 쪽을 숫자로 변환**한다 → PI 해시 접근 불가, 전체 스캔,
  숫자가 아닌 값이 있으면 실행 오류(2620/2621 류)까지 발생할 수 있다.
- 조치(A): 상수를 컬럼 타입에 맞춤: `WHERE CUST_CD = '123'`. 반대로 숫자 컬럼은 숫자 상수로.
- 날짜 컬럼과 문자열 비교(`dt = '20240101'`)도 형식 의존적이므로 `DATE '2024-01-01'` 리터럴 사용.

## TP-PRED-03 선행 와일드카드 LIKE
- 신호: `LIKE '%abc'`, `LIKE '%abc%'`
- 의미: 인덱스·통계 추정이 어렵고 전체 스캔. 대안이 없을 때가 많다.
- 조치(A): 다른 선택적 조건(기간, 코드)을 먼저 걸어 스캔량을 줄인다. 반복 패턴이면 (C) 검색용 파생 컬럼.

## TP-PRED-04 서로 다른 컬럼의 OR
- 신호: `WHERE a.x = 1 OR a.y = 2` 가 대용량 테이블에서 전체 스캔
- 조치(A): 각 조건이 인덱스/파티션을 탈 수 있다면 `UNION ALL` 로 분리 (두 조건을 동시에 만족하는 행 중복 주의:
  두 번째 쿼리에 `AND NOT (a.x = 1)` 추가 - x 가 NULL 가능하면 `AND (a.x <> 1 OR a.x IS NULL)`).
- 같은 컬럼의 OR 는 `IN (...)` 으로.

## TP-PRED-05 대형 IN 리스트
- 신호: IN 안에 수백 개 이상의 상수, 또는 매번 바뀌는 긴 목록
- 조치(A): 값을 volatile table 에 넣고 조인 (PI = 해당 값, 통계 수집). 파싱 시간·플랜 품질이 좋아진다.
