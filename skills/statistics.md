---
name: statistics
description: 통계 수집 권고 - 대상 컬럼 우선순위, 문법, HELPSTATS, 신뢰도, 오래된 통계, 다중 컬럼 통계
category: 통계
findings: [STATS_NO_CONFIDENCE, STATS_LOW_CONFIDENCE, STATS_RECOMMENDED, DUPLICATE_LARGE, PRODUCT_JOIN, REMOTE_QUERY]
---
# 통계 (Statistics)

옵티마이저는 통계로 행 수와 값 분포를 추정해 조인 순서·방식·재분배/복제를 결정한다.
**추정이 틀리면 나머지 튜닝도 의미가 없다.** 통계는 등급 B(낮은 위험) 조치다.

## TP-STAT-01 통계 없음 (no confidence)
- 신호: `estimated with no confidence`, 특히 병목 단계·product join·복제 단계의 입력
- 조치(B): 해당 단계 테이블의 조인 컬럼·필터 컬럼부터 수집.

## TP-STAT-02 부분 통계 (low confidence)
- 신호: `estimated with low confidence`
- 조치(B): 복합 조건이면 함께 쓰이는 컬럼의 다중 컬럼 통계, 범위 조건 컬럼 통계.

## TP-STAT-03 HELPSTATS 권고
```sql
DIAGNOSTIC HELPSTATS ON FOR SESSION;
EXPLAIN <쿼리>;  -- 끝에 BEGIN RECOMMENDED STATS -> "COLLECT STATISTICS ..." 목록
```
- 모두 수집할 필요는 없다. 조인·필터 컬럼과 겹치는 것부터.

## TP-STAT-04 오래된 통계
- 신호: 통계는 있는데 추정 행 수가 실제(DBQL 결과 행 수, 테이블 행 수)와 크게 다름. 대량 적재 이후 미수집.
- 조치(B): 재수집 `COLLECT STATISTICS ON db.tbl;`(정의된 통계 전체). 정기 적재 테이블은 적재 직후 재수집 배치.

## TP-STAT-05 수집 대상 우선순위
1. 조인 컬럼 (양쪽) 2. 선택도 높은 WHERE 컬럼 3. NUPI 컬럼 4. 파티션 테이블의 `PARTITION` 과 파티션 컬럼
5. 함께 쓰이는 상관 컬럼 조합(다중 컬럼) 6. 대량 GROUP BY 컬럼 7. NUSI 컬럼

## TP-STAT-06 HELPSTATS 권고 선별
- 신호: `BEGIN RECOMMENDED STATS FOR FINAL PLAN ->` / `FOR OTHER PLANS ->` 블록, 각 권고 끝의 `(High|Medium Confidence)`
- 해석: FINAL PLAN = 지금 선택된 계획의 추정에 쓰일 통계, OTHER PLANS = 다른(더 나을 수 있는) 계획을 고려하게 할 통계.
  권고 수가 많아도 전부 수집하지 않는다.
- 우선 적용: High + FINAL PLAN + 단일 컬럼 또는 2~4개 컬럼 조합 (조인·필터에 실제로 쓰이는 컬럼),
  그리고 `COLLECT SUMMARY STATISTICS ON t` (테이블 행 수만 수집하므로 매우 저렴).
- 선택 검토: Medium/Low, OTHER PLANS, 5개 이상 컬럼 조합(GROUP BY 목록에서 나오는 경우가 많고 수집 비용이 큼),
  `DBC.` 시스템 딕셔너리 테이블(DBA 작업).
- 검증: 수집 후 같은 쿼리 EXPLAIN 에서 해당 단계 신뢰도가 high 로 바뀌고 계획/예상 시간이 달라지는지 확인.

## 문법 (Teradata 14 이상, 버전 확인)
```sql
COLLECT STATISTICS COLUMN (CUST_ID) ON SALES_DB.SALES_TXN;
COLLECT STATISTICS COLUMN (CUST_ID), COLUMN (SALE_DT), COLUMN (PARTITION) ON SALES_DB.SALES_TXN;
COLLECT STATISTICS COLUMN (CUST_ID, PROD_ID) ON SALES_DB.SALES_TXN;              -- 다중 컬럼
COLLECT STATISTICS USING SAMPLE 10 PERCENT COLUMN (CUST_ID) ON SALES_DB.SALES_TXN;  -- 대용량 샘플
COLLECT SUMMARY STATISTICS ON SALES_DB.SALES_TXN;                                 -- 요약 통계(행 수 등, 저렴)
HELP STATISTICS SALES_DB.SALES_TXN;                                               -- 현재 통계
```
- 대용량 테이블 수집은 리소스를 쓰므로 배치 시간에. volatile table 도 적재 후 수집 가능.
- 에이전트는 통계를 직접 수집하지 않는다. 스크립트로 권고하고, 수집 후 EXPLAIN 을 다시 비교한다.
