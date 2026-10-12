---
name: dbql
description: DBQL 로 고부하 쿼리 선정과 지표 해석 - CPU·IO·스큐·Impact CPU·PJI·UII·스풀, SQL 텍스트 복원
category: 진단
findings: [CPU_SKEW, IO_SKEW, PRODUCT_JOIN_SUSPECT, UNNECESSARY_IO, LARGE_SPOOL]
---
# DBQL (Database Query Log)

## 테이블 (로깅 설정에 따라 다름)
- `DBC.DBQLogTbl` : 쿼리당 1행 - QueryID, ProcID, UserName, StartTime, FirstRespTime, AMPCPUTime, TotalIOCount,
  MaxAMPCPUTime, MaxAMPIO, NumOfActiveAMPs, SpoolUsage, ErrorCode, QueryBand ...
- `DBC.DBQLSqlTbl` : SQL 전문. 길면 여러 행 → `SqlRowNo` 순으로 이어 붙임.
- `DBC.DBQLObjTbl`(사용 객체), `DBC.DBQLStepTbl`(단계 로깅 시), `DBC.DBQLExplainTbl`(EXPLAIN 로깅 시)
- 뷰 `DBC.QryLogV`, `DBC.QryLogSQLV`. 이력 보관(PDCR) 환경은 `PDCRINFO.DBQLogTbl_Hst` 등 사이트별 테이블.

## TP-DBQL-01 튜닝 대상 선정
- 총 부하 기준: `AMPCPUTime` 합계가 큰 쿼리 (같은 SQL 이 자주 실행되면 실행 횟수 × CPU)
- 체감 부하 기준: `Impact CPU = MaxAMPCPUTime × NumOfActiveAMPs` (스큐까지 반영)

## TP-DBQL-02 CPU/IO 스큐
- `CPU 스큐 = MaxAMPCPUTime × NumOfActiveAMPs / AMPCPUTime` (1.0 = 균등). 2 이상 주의, 5 이상 심각 → skew 스킬
- IO 도 같은 방식 (`MaxAMPIO`, `TotalIOCount`).

## TP-DBQL-03 PJI (Product Join Indicator)
- `PJI = AMPCPUTime × 1000 / TotalIOCount`. IO 대비 CPU 가 크다 → 3 이상이면 product join 등 CPU 집약 연산 의심 (현장 경험칙)

## TP-DBQL-04 UII (Unnecessary IO Indicator)
- `UII = TotalIOCount / (AMPCPUTime × 1000)`. CPU 대비 IO 가 크다 → 3 이상이면 전체 스캔·불필요한 IO 의심 (현장 경험칙)
  → partitioning, predicates, indexes 스킬

## TP-DBQL-05 스풀
- `SpoolUsage` 가 크면 sql-rewrite TP-RW-01, spool-locking 스킬.

## 주의
- DBQL SQL 이 volatile table 을 참조하면 지금 세션에 그 테이블이 없어 EXPLAIN 이 실패한다 → 앞선 CREATE VOLATILE 문을 함께 확인.
- 매크로/프로시저 호출(EXEC, CALL)은 내부 SQL 을 따로 찾아야 한다.
- 애플리케이션 SQL 의 `?` 파라미터 마커는 그대로 EXPLAIN 할 수 없어 더미 값을 바인딩한다. 실제 값의 선택도에 따라
  계획이 달라질 수 있으므로, 중요한 쿼리는 대표 값을 넣어 다시 EXPLAIN 한다.
- EXPLAIN 도 대상 객체의 SELECT 권한이 필요하다 (Error 3523). DBQL/PDCR 이력 테이블처럼 권한이 제한된 객체는 실패할 수 있다.
- 실행마다 동시 부하에 따라 지표 편차가 있다.
