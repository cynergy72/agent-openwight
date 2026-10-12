---
name: data-types
description: 데이터 타입·문자셋·압축 - 조인/비교 형변환, LATIN vs UNICODE, CHAR vs VARCHAR 행 길이, MVC 압축
category: 설계
findings: [JOIN_CONVERSION, IMPLICIT_CONVERSION, LARGE_SPOOL]
---
# 데이터 타입과 행 크기

## TP-TYPE-01 비교·조인 타입 불일치
- predicates TP-PRED-02(문자 컬럼 vs 숫자 상수), join-geography TP-JOIN-13(조인 컬럼 타입 불일치) 참조.
- 같은 의미의 코드 컬럼은 전사적으로 같은 타입·길이·문자셋으로 통일하는 것이 근본 해결(C).

## TP-TYPE-02 문자셋 (LATIN vs UNICODE)
- UNICODE 는 문자당 2바이트로 LATIN 의 두 배. 영문/숫자 코드 컬럼을 UNICODE 로 두면 스풀·IO 가 커진다.
- LATIN 과 UNICODE 컬럼을 비교·조인하면 변환(TRANSLATE)이 생긴다.
- 조치(C): 코드성 컬럼은 LATIN, 한글 등 다국어 텍스트만 UNICODE.

## TP-TYPE-03 CHAR vs VARCHAR
- CHAR(n) 은 항상 n 길이. 실제 값이 짧은 넓은 CHAR 는 스풀·IO 낭비 → 가변 길이 데이터는 VARCHAR.
- 고정 길이 코드(예: 2자리)는 CHAR 가 적합.

## TP-TYPE-04 압축 (MVC / BLC)
- MVC(Multi-Value Compression): 컬럼별로 자주 나오는 값(최대 255개)을 헤더 비트로 대체 → 행 크기·IO 감소,
  조회 CPU 부담 거의 없음. NULL·기본값·상위 코드값이 많은 컬럼에 효과적.
```sql
CUST_GRADE CHAR(1) COMPRESS ('N','S','G')
```
- BLC(블록 압축)은 저장 공간은 크게 줄지만 읽을 때 CPU 를 쓴다 → 콜드 데이터에 적합.
- 조치(C): 대용량 테이블의 저카디널리티 컬럼에 MVC 권고 (재생성/ALTER 필요, 버전 확인).

## TP-TYPE-05 숫자 정밀도
- DECIMAL 연산 결과 자릿수 초과(오버플로 오류 2616 류) 방지: 큰 합계는 `CAST(SUM(x) AS DECIMAL(38,2))` 등으로 명시.
