---
name: sql-rewrite
description: 결과를 보존하는 재작성 원칙과 공통 기법 - 불필요 컬럼 제거, 조기 필터, 반복 스캔 통합, UNION/OR 처리, 단계화
category: 재작성
findings: [UNUSED_COLUMNS, SELECT_STAR, REPEATED_SCAN, UNION_DISTINCT, LARGE_SPOOL]
always_tuning: true
---
# 쿼리 재작성

## 절대 규칙
- 최종 출력 컬럼의 이름·순서·의미, 결과 행이 원본과 같아야 한다.
- 조인 종류(INNER/OUTER), 필터, 집계 기준을 바꾸지 않는다. 바꿔야만 하면 "가정"으로 명시하고 사용자 확인을 요청.
- Teradata 문법만 사용 (TOP/QUALIFY/SAMPLE, DATE 'yyyy-mm-dd', ADD_MONTHS). LIMIT 금지.

## TP-RW-01 불필요 컬럼 제거
- 신호: 파생테이블/CTE 가 바깥에서 쓰지 않는 컬럼을 선택, 서브쿼리 `SELECT *`, 큰 스풀
- 원리: 스풀 크기 = 행 수 × 행 길이. 넓은 VARCHAR/CHAR 를 빼면 재분배·정렬·스풀 모두 줄어든다.
- 조치(A): 안쪽 SELECT 목록을 바깥에서 실제 쓰는 컬럼(조인 키, 필터, 집계, 출력)만 남긴다.
- 검증: 스풀 예상 크기(bytes) 감소. 행 수는 그대로.

## TP-RW-02 조기 필터 / 조건 전파
- 필터를 조인 전 파생테이블 안으로. A.k = B.k 이고 A.k 에 범위 조건이 있으면 B.k 에도 같은 조건을 추가할 수 있다
  (INNER JOIN 일 때만 동치).

## TP-RW-03 같은 대용량 테이블 반복 스캔 → 1회 스캔 조건부 집계
- 신호: 같은 큰 테이블을 서브쿼리 여러 개에서 조건만 바꿔 읽음
```sql
-- Before
SELECT (SELECT SUM(AMT) FROM db.T WHERE TYPE='A'), (SELECT SUM(AMT) FROM db.T WHERE TYPE='B');
-- After
SELECT SUM(CASE WHEN TYPE='A' THEN AMT END), SUM(CASE WHEN TYPE='B' THEN AMT END)
FROM db.T WHERE TYPE IN ('A','B');
```

## TP-RW-04 UNION → UNION ALL
- 신호: UNION (중복 제거 정렬 단계)
- 조치(A): 두 결과 사이에 중복이 생길 수 없을 때만 (서로 배타적인 조건, 구분 컬럼 존재). 근거를 적는다.

## TP-RW-05 다단계 처리 → volatile table 단계화
- 신호: 매우 긴 단일 쿼리, 같은 중간 결과를 여러 번 사용, 중간 결과 추정 오류
- 조치(A, 다중 문장): 중간 결과를 volatile table 로. 다음 조인 키를 PI 로, 적재 후 통계 수집.
```sql
CREATE VOLATILE TABLE VT_SALE AS (
  SELECT CUST_ID, SUM(SALE_AMT) AS AMT FROM SALES_DB.SALES_TXN
  WHERE SALE_DT >= DATE '2024-01-01' AND SALE_DT < DATE '2024-02-01' GROUP BY 1
) WITH DATA PRIMARY INDEX (CUST_ID) ON COMMIT PRESERVE ROWS;
COLLECT STATISTICS COLUMN (CUST_ID) ON VT_SALE;
```
  단일 쿼리 재작성과 구분해서 "단계화 제안"으로 제시한다 (에이전트의 EXPLAIN 검증은 단일 문장 기준).
