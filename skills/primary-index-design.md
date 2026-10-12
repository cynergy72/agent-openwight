---
name: primary-index-design
description: PI 설계 - 분산·접근·조인 정렬 기준, UPI/NUPI/NoPI, SET 테이블 중복 체크 비용, PI 컬럼 변경 비용
category: 설계
findings: [TABLE_SKEW, SET_TABLE_DML, REDISTRIBUTE_LARGE]
---
# Primary Index 설계 (등급 C)

PI 는 (1) 행 분산 (2) 단일 AMP 접근 (3) 조인 시 행 이동 여부를 동시에 결정한다. 바꾸려면 테이블 재생성이 필요하다.

## TP-PI-01 PI 선택 기준
1. **분산**: 고유값이 충분히 많은 컬럼(조합). NULL·기본값이 많은 컬럼 금지.
2. **접근**: 자주 동등 조건으로 조회되는 컬럼.
3. **조인**: 가장 크고 자주 하는 조인의 키와 맞추면 재분배가 사라진다.
4. **안정성**: 자주 UPDATE 되지 않는 컬럼 (PI 값이 바뀌면 행이 다른 AMP 로 이동).
- 다중 컬럼 PI 는 모든 컬럼 동등 조건이 있어야 해시 접근이 된다 (일부만 있으면 못 씀).

## TP-PI-02 SET 테이블의 중복 행 검사
- 신호: `CREATE SET TABLE` + NUPI + 같은 PI 값 행이 많은 테이블에 INSERT SELECT 가 느림
- 원인: SET 테이블은 중복 행을 허용하지 않아, 같은 row hash 의 모든 행과 비교한다 (NUPI 값당 행이 많을수록 급증).
- 조치(C): 중복 행이 업무상 불가능하거나 상관없으면 MULTISET 으로. 유일성이 필요하면 UPI/USI 로 보장
  (인덱스로 검사하므로 저렴). (A) 적재 SQL 에서 미리 중복 제거.

## TP-PI-03 NoPI 테이블
- 적재 스테이징용: 해시 없이 균등 분산, 적재가 빠름. 대신 PI 접근이 없고 조인 시 항상 이동.

## TP-PI-04 PI 변경 마이그레이션
- 새 PI 로 테이블 생성 → INSERT SELECT → 건수 검증 → RENAME → 통계 재수집 → 뷰·권한·배치 영향 확인.
- 변경 전 후보 PI 의 분산 확인:
```sql
SELECT HASHAMP(HASHBUCKET(HASHROW(NEW_PI_COL1, NEW_PI_COL2))) AS amp_no, COUNT(*)
FROM DB.TBL GROUP BY 1 ORDER BY 2 DESC;
```
