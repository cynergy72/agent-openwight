---
name: join-geography
description: 조인 시 행 이동(재분배·복제) 줄이기 - PI 정렬, 조인 전 필터·집계, 조인 컬럼 타입 일치
category: 조인
findings: [REDISTRIBUTE_LARGE, DUPLICATE_LARGE, JOIN_CONVERSION]
---
# 조인 행 이동 (Geography)

행은 PI 해시값으로 AMP 에 분산돼 있다. 조인하려면 같은 키 값의 행이 같은 AMP 에 있어야 하므로,
조인 키가 PI 와 다르면 **재분배**(redistribute)하거나 작은 쪽을 **전체 복제**(duplicate)한다.

## TP-JOIN-11 대용량 재분배
- 신호: `redistributed by the hash code of (...)` + 큰 예상 행 수
- 원인: 조인 키 ≠ PI (양쪽 다 다르면 양쪽 모두 이동)
- 조치(A): 조인 전에 행을 줄인다 - 필터를 파생테이블 안으로, N 쪽을 조인 키로 **먼저 집계**(pre-aggregation).
- 조치(A): 같은 키로 여러 번 조인하는 ETL 은 조인 키를 PI 로 한 volatile table 에 한 번 담아 재사용.
- 조치(C): 핵심 조인 경로라면 PI 를 조인 키로 정렬 검토 (primary-index-design 스킬).
- 검증: 재분배 단계의 행 수 감소 또는 `built locally` 로 변경.

## TP-JOIN-12 대용량 복제
- 신호: `duplicated on all AMPs` + 수백만 행 이상
- 원인: 대부분 통계 부재로 작은 테이블이라고 오판. 복제량 = 스풀 × AMP 수.
- 조치(B): 복제되는 쪽의 조인/필터 컬럼 통계 수집. (A): 복제 전 필터 강화, 필요한 컬럼만.
- 검증: 복제 단계가 재분배로 바뀌거나 복제 행 수 감소.

## TP-JOIN-13 조인 컬럼 타입·문자셋 불일치
- 신호: 조인 조건에 `CAST(...)`, `TRANSLATE(...)`, 문자셋 변환이 보임. 카탈로그상 양쪽 타입이 다름
  (예: INTEGER vs VARCHAR, LATIN vs UNICODE, DECIMAL 자릿수 상이).
- 원인: 타입이 다르면 해시값이 달라 같은 값도 다른 AMP 에 있다 → 변환 + 재분배.
- 조치(A): 작은 쪽을 큰 쪽 타입으로 변환한 파생테이블로 조인 (큰 테이블 컬럼은 그대로).
- 조치(C): 테이블 설계에서 조인 컬럼 타입·문자셋 통일.

## TP-JOIN-14 조인 순서·중간 결과 축소
- 신호: 조인 후 GROUP BY 로 크게 줄어드는 쿼리, 조인 결과 스풀이 입력보다 훨씬 큼(1:N:N 폭증)
- 조치(A): 1:N 관계의 N 쪽들을 각각 먼저 집계한 뒤 조인 (N×N 폭증 방지).
```sql
-- Before: 주문 × 결제 가 고객 단위로 N×N 폭증
SELECT o.CUST_ID, SUM(o.AMT), SUM(p.PAY_AMT)
FROM db.ORD o JOIN db.PAY p ON o.CUST_ID = p.CUST_ID GROUP BY 1;   -- 합계가 부풀려진 버그이기도 함
-- After
SELECT o.CUST_ID, o.AMT, p.PAY_AMT
FROM (SELECT CUST_ID, SUM(AMT) AS AMT FROM db.ORD GROUP BY 1) o
JOIN (SELECT CUST_ID, SUM(PAY_AMT) AS PAY_AMT FROM db.PAY GROUP BY 1) p ON o.CUST_ID = p.CUST_ID;
```
  (Before 는 합계가 중복 집계되는 버그일 가능성이 높다 → 결과가 달라지므로 사용자에게 알린다)
