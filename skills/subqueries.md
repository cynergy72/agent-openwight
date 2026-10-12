---
name: subqueries
description: 서브쿼리 튜닝 - NOT IN 의 NULL 함정, 상관 서브쿼리, SELECT 절 스칼라 서브쿼리, IN/EXISTS
category: 재작성
findings: [NOT_IN_SUBQUERY, CORRELATED_SUBQUERY, SCALAR_SUBQUERY]
---
# 서브쿼리

## TP-SUB-01 NOT IN (서브쿼리)
- 신호: `WHERE k NOT IN (SELECT k FROM ...)`, EXPLAIN `exclusion` 조인, 때로 product join
- 의미: 서브쿼리 결과에 NULL 이 하나라도 있으면 **결과 0건** (3값 논리). 옵티마이저도 NULL 처리 때문에 비싼 계획을 쓴다.
- 조치(A): `NOT EXISTS` 로. 단, 두 쿼리가 같은 결과인 것은 양쪽 컬럼이 NOT NULL 일 때만 (카탈로그 확인).
```sql
WHERE NOT EXISTS (SELECT 1 FROM db.B b WHERE b.k = a.k)
```
  NULL 가능 컬럼이면 결과가 달라질 수 있음을 가정으로 명시 (대개 원본이 버그).

## TP-SUB-02 상관 서브쿼리
- 신호: 서브쿼리 안에서 바깥 테이블 컬럼 참조 (`WHERE b.k = a.k`), 특히 집계 비교
```sql
-- Before: 고객별 최대 금액 행
SELECT * FROM db.T a WHERE AMT = (SELECT MAX(AMT) FROM db.T b WHERE b.CUST_ID = a.CUST_ID);
-- After: 한 번 스캔 + QUALIFY (동률 행 모두 유지하려면 RANK)
SELECT * FROM db.T QUALIFY RANK() OVER (PARTITION BY CUST_ID ORDER BY AMT DESC) = 1;
```
  (원본은 AMT 가 NULL 인 고객을 제외한다 → RANK 버전도 NULL 이 1등이 되지 않게 `WHERE AMT IS NOT NULL` 확인)
- 조치(A): 미리 집계한 파생테이블과 조인하거나 QUALIFY 로 변환.

## TP-SUB-03 SELECT 절 스칼라 서브쿼리
- 신호: `SELECT a.*, (SELECT b.NM FROM db.B b WHERE b.k = a.k) ...`
- 조치(A): `LEFT JOIN` 으로 변환 (b.k 가 유일할 때 동일 결과. 유일하지 않으면 원본은 오류(다중 행)이므로 확인).

## TP-SUB-04 IN / EXISTS
- 대부분 옵티마이저가 같은 계획(inclusion join)을 만든다. 서브쿼리 결과가 크면 조인 키 통계가 중요.
- IN 서브쿼리 안의 DISTINCT 는 불필요 (의미 동일).
