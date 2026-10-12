---
name: querygrid
description: QueryGrid 원격 조회 튜닝 - 원격 실행 계획 읽기, 조건·컬럼 pushdown, 원격 전체 스캔, 원격 결과와 로컬 조인
category: 접근경로
findings: [REMOTE_QUERY, REMOTE_FULL_TRANSFER]
---
# QueryGrid 원격 조회

QueryGrid 로 원격 시스템(Oracle, Hadoop, 다른 Teradata 등)의 테이블을 읽으면 EXPLAIN 에 이렇게 나온다:
`We do an all-AMPs RETRIEVE step executing table operator TD_SYSFNLIB.QGINITIATORIMPORT ...`
그 안의 `< BEGIN EXPLAIN FOR REMOTE QUERY --> ... <-- END EXPLAIN FOR REMOTE QUERY >` 블록이 **원격 시스템의 실행 계획**이다.
`Query steps:` 아래가 원격으로 실제 보내지는 SQL 이다. 커넥터·버전에 따라 기능(pushdown 범위, pass-through 문법)이 다르므로
적용 전에 사용하는 커넥터 문서를 확인한다.

## TP-QG-01 원격 조회 구조 확인
- 신호: `executing table operator TD_SYSFNLIB.QGINITIATORIMPORT`, `BEGIN EXPLAIN FOR REMOTE QUERY`
- 확인할 것: (1) `Query steps` 의 원격 SQL 에 WHERE 조건과 필요한 컬럼만 들어갔는가 (2) 원격 계획의 접근 방식
  (Oracle 이면 `Operation=TABLE ACCESS, Options=FULL` 은 전체 스캔) (3) Teradata 쪽 예상 행 수 신뢰도
- Teradata 는 원격 테이블 행 수를 모르므로 `no confidence` 로 추정한다 (원격 계획의 Cardinality 와 크게 다를 수 있음).

## TP-QG-02 필요한 컬럼만 가져오기
- 신호: 원격 SQL 이 모든 컬럼을 선택 (`SELECT *` 로 외부 테이블 조회)
- 조치(A): 필요한 컬럼만 SELECT. 원격에서 Teradata 로 네트워크 전송되는 데이터 양이 그대로 줄어든다.

## TP-QG-03 조건을 원격으로 내려보내기 (pushdown)
- 신호: 원격 계획이 전체 스캔이고 Filter/Access 조건이 비어 있음, 원격 SQL 에 WHERE 가 없음
- 원인: 조건이 없거나, 원격에서 평가할 수 없는 함수·형변환이 들어가 Teradata 쪽에서 거르게 됨
- 조치(A): 외부 테이블 컬럼에 대한 조건을 단순 비교(=, 범위, IN)로 써서 원격 SQL 로 넘어가게 한다.
  날짜 조건은 원격 컬럼 타입 그대로 비교한다. 적용 후 `Query steps` 의 원격 SQL 에 WHERE 가 생겼는지 확인.
- 조치(A, 커넥터 지원 시): 원격 쪽에서 집계·필터를 끝내도록 pass-through 형태
  `SELECT ... FROM FOREIGN TABLE (원격 SQL)@서버 AS t` 를 검토 (커넥터별 지원 여부 확인).
- 조치(C): 원격 테이블에 조건 컬럼 인덱스/파티션.

## TP-QG-04 원격 결과와 로컬 대용량 테이블 조인
- 신호: 원격 조회 결과 스풀이 `no confidence` 인데 다음 단계에서 로컬 대용량 테이블과 조인 (복제·product join 위험)
- 조치(A, 다중 문장): 원격 결과를 volatile table 에 먼저 적재 → 조인 키를 PI 로, `COLLECT STATISTICS` 후 조인.
  옵티마이저가 실제 행 수를 알게 되어 조인 계획이 안정된다.

## TP-QG-05 같은 원격 데이터의 반복 조회
- 신호: 같은 외부 테이블을 여러 쿼리·배치에서 반복 조회 (DBQL 에서 같은 QGINITIATORIMPORT 패턴이 반복)
- 조치(C): 자주 쓰는 원격 데이터는 주기 적재(ETL)로 로컬 테이블에 두고 조회한다. 실시간성이 필요한 경우만 원격 조회.
