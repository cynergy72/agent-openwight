---
name: explain-reading
description: Teradata EXPLAIN 문구 해석 사전 - AMP 범위, 접근 경로, 스풀 배치, 조인 방식, 신뢰도, 병목 단계
category: 진단
findings: [HOTSPOT_STEP]
always_tuning: true
---
# EXPLAIN 해석

EXPLAIN 은 실행하지 않고 실행 계획만 보여준다. 예상 행 수·시간은 **상대 비용** (같은 쿼리 버전끼리 비교용).

## 문구 사전
| 문구 | 의미 | 판단 |
|---|---|---|
| `single-AMP ... by way of the unique primary index` | UPI 로 1행 | 최상 |
| `by way of the primary index` | NUPI 접근 | 좋음 |
| `by way of unique index # n` / `index # n` | USI / NUSI | 선택도 높으면 좋음 |
| `by way of an all-rows scan` | 전체 스캔 | 대용량이면 튜닝 대상 |
| `n partitions of` / `a single partition of` | 파티션 제거 | 좋음 |
| `built locally on the AMPs` | 스풀 이동 없음 | 좋음 |
| `redistributed by the hash code of (...)` | AMP 간 재분배 | 행이 많으면 비용 |
| `duplicated on all AMPs` | 전체 AMP 복제 | 큰 스풀이면 위험 |
| `joined using a nested join` | 인덱스로 소수 행 조인 | 최상 |
| `joined using a merge join` | 해시 정렬 후 병합 | 대량 조인 표준 |
| `joined using a hash join` | 작은 쪽 해시 테이블 | 작은+큰 조인에 유리 |
| `joined using a product join` | 모든 행 × 모든 행 | 한쪽이 아주 작을 때만 정상 |
| `exclusion ... join` / `inclusion ... join` | NOT IN·NOT EXISTS / IN·EXISTS | |
| `estimated with high confidence` | 통계/PI 기반 추정 | 신뢰 |
| `estimated with low confidence` | 일부 통계 | 주의 |
| `estimated with no confidence` | 통계 없음 | 통계 수집 대상 |
| `with a condition of ("...")` | 잔여 조건 | 컬럼에 CAST/SUBSTR 보이면 sargable 위반 |
| `Aggregate Intermediate Results are computed globally` | 전역 집계 | 그룹 키 재분배 |
| `STAT FUNCTION step` | 윈도우 함수 | PARTITION BY 키로 재분배 |
| `We execute the following steps in parallel` | 병렬 단계 | |
| `lock ... for access` | 접근 잠금 (dirty read) | 조회 쿼리 권장 |

## Teradata 17/20 에서 자주 보이는 문구 (실제 계획에서 확인)
| 문구 | 의미 | 판단 |
|---|---|---|
| `This request is eligible for incremental planning and execution (IPE) ... static plan` | IPE 대상이지만 비용 기준 미달로 정적 계획 사용 | 정보 |
| `DB.A in view V` | 뷰 V 안에서 별칭 A 로 참조된 테이블 (실제 테이블명은 뷰 정의 확인) | 뷰 정의를 함께 본다 |
| `The size is estimated with ... confidence to be n rows` | 이 단계 최종 결과 스풀의 크기 (스풀 번호 생략형) | |
| `(n rows (b bytes))` 에서 b/n 이 매우 큼 | 행이 넓다 (예: 행당 100KB 이상) | 불필요 컬럼·넓은 VARCHAR 제거 (TP-RW-01) |
| `redistributed by hash code to all AMPs` / `assumed to be redistributed by value` | 키를 표시하지 않는 재분배 (윈도우·집계 내부) | 행 수로 판단 |
| `redistributed by the hash code of ('상수') to few AMPs` | 상수/저카디널리티 키 → 소수 AMP 로 몰림 | 스큐 (TP-SKEW-06) |
| `left/right outer joined using ...` | OUTER JOIN 실행 방식 | |
| `with condition(s) used for non-matching on right table` | OUTER JOIN 의 비매칭 처리 조건 | |
| `single partition hash join` / `dynamic hash join` | 해시 조인 변형 (작은 쪽을 메모리 해시) | 대체로 양호 |
| `row id join` | 인덱스로 얻은 row id 로 기본 테이블 접근 | 양호 |
| `all partitions of T` | 파티션 테이블의 모든 파티션 접근 (제거 없음) | PI 에 파티션 컬럼이 없으면 흔함 |
| `4294967296 partitions of T` 처럼 매우 큰 수 | 사실상 전체 파티션 | 파티션 제거 아님 |
| `by way of the hash value of "..."` | 해시값으로 특정 AMP 의 행 접근 | 양호 |
| `built locally on that AMP` | 단일 AMP 안에서 처리 | 양호 |
| `This step is used to retrieve the TOP n rows` | TOP n 처리 단계 | |
| `-> As the result of statement 1.` (총 예상 시간 없음) | 일부 계획은 총 시간을 표시하지 않음 | 단계별 시간 합으로 비교 |
| `:?` (인덱스 조건 안) | 파라미터 마커 | 값에 따라 계획이 달라질 수 있음 |
| `We do an all-AMPs MERGE step into T from Spool n` | INSERT-SELECT 적재 단계 | 대상 PI·빈 테이블 여부 확인 (TP-ETL-01) |
| `We do an INSERT step into T` | 단건 INSERT | |
| `by way of full table deletion` | 빠른 전체 삭제 (DELETE ALL) | 양호 (TP-ETL-04) |
| `We spoil the parser's dictionary cache` / `We flush the DISKSPACE caches` | DDL/DML 후 캐시 처리 | 정보 |
| `executing table operator TD_SYSFNLIB.QGINITIATORIMPORT` | QueryGrid 원격 조회 | 원격 계획 블록 확인 (querygrid 스킬) |
| `< BEGIN EXPLAIN FOR REMOTE QUERY --> ... <-- END ... >` | 원격 시스템의 실행 계획 | 원격 SQL·전체 스캔·추정 행 수 확인 |
| `BEGIN RECOMMENDED STATS FOR FINAL PLAN / OTHER PLANS ->` | HELPSTATS 통계 권고 (신뢰도 표시) | 선별 적용 (TP-STAT-06) |

## TP-EXP-01 병목 단계
- 신호: 한 단계의 예상 시간이 총 예상 시간의 절반 이상
- 조치: 그 단계의 입력 스풀(행 수·출처), 조인 방식, 신뢰도부터 본다. 나머지 단계 튜닝은 효과가 작다.
- 검증: 해당 단계 예상 시간 감소, 총 예상 시간 감소
