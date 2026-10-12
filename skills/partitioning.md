---
name: partitioning
description: PPI 파티션 제거(partition elimination) 확보, 파티션 미사용 전체 스캔 해소, 파티션 추가 설계·마이그레이션
category: 접근경로
findings: [PARTITION_NOT_ELIMINATED, FULL_SCAN_LARGE, NON_SARGABLE]
---
# 파티셔닝 (PPI)

PPI 테이블은 각 AMP 안에서 행을 파티션별로 묶어 저장한다. EXPLAIN 에 `n partitions of DB.T` /
`a single partition of DB.T` 가 보이면 파티션 제거가 적용된 것이고, `all-rows scan` 만 보이면 미적용이다.

## TP-PART-01 파티션 컬럼 조건 누락
- 신호: 파티션 테이블(카탈로그 PARTITION BY)에 `all-rows scan`, SQL 에 파티션 컬럼 조건 없음
- 조치(A): 업무상 기간이 정해져 있으면 파티션 컬럼 범위 조건 추가 → **결과가 달라질 수 있으므로 가정으로 명시**.
  조인 상대 쪽에만 기간 조건이 있으면 같은 기간 조건을 이 테이블에도 복제(동치 조건 전파)할 수 있는지 검토.

## TP-PART-02 파티션 컬럼에 함수/형변환
- 신호: 파티션 컬럼이 CAST/SUBSTR/EXTRACT/TRUNC 안에 있음, 문자열 상수와 비교
- 조치(A): predicates 스킬 TP-PRED-01/02 방식으로 컬럼을 그대로 두고 상수 쪽을 범위로.

## TP-PART-03 OR 로 섞인 파티션 조건
- 신호: `WHERE (SALE_DT >= ... AND ...) OR other_col = ...`
- 조치(A): 파티션 조건이 모든 OR 가지에 적용되게 재구성하거나 UNION ALL 분리.

## TP-PART-04 동적 파티션 제거(DPE)에 의존
- 신호: 파티션 테이블이 다른 테이블과 조인으로만 범위가 정해짐
- 조치(A): 가능하면 상수 범위 조건을 직접 추가 (DPE 는 보장되지 않음).

## TP-PART-05 파티션 추가 제안 (설계, 등급 C)
- 신호: 파티션 없는 대용량 테이블이 날짜 범위 조건으로 반복 조회되며 `all-rows scan`
- 설계: 조회 단위(일/월)에 맞춘 RANGE_N. 너무 잘게 나누면 비효율. 범위 밖 값용 `NO RANGE` 고려.
- 트레이드오프: PI 에 파티션 컬럼이 없으면 PI 동등 조회 시 모든 파티션을 확인해야 한다.
```sql
CREATE MULTISET TABLE SALES_DB.SALES_TXN_NEW AS SALES_DB.SALES_TXN WITH NO DATA
PRIMARY INDEX (CUST_ID)
PARTITION BY RANGE_N(SALE_DT BETWEEN DATE '2020-01-01' AND DATE '2030-12-31' EACH INTERVAL '1' MONTH, NO RANGE);
```
- 마이그레이션: 데이터가 있는 테이블의 PI/파티션은 ALTER 로 바로 바꿀 수 없으므로
  1) 새 테이블 생성 2) `INSERT INTO new SELECT * FROM old;` 3) 건수 검증 4) `RENAME TABLE` 교체
  5) 통계 재수집(`COLUMN (PARTITION)`, 파티션 컬럼 포함) 6) 뷰·권한·적재 배치 영향 확인. DBA 검토 필수.
- 기존 RANGE_N 범위 확장은 `ALTER TABLE ... MODIFY PRIMARY INDEX ... ADD RANGE ...` 형태 (버전 확인).
- 검증: 테스트 환경에서 새 테이블로 EXPLAIN → `n partitions of` 확인.
