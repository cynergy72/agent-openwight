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

## TP-EXP-01 병목 단계
- 신호: 한 단계의 예상 시간이 총 예상 시간의 절반 이상
- 조치: 그 단계의 입력 스풀(행 수·출처), 조인 방식, 신뢰도부터 본다. 나머지 단계 튜닝은 효과가 작다.
- 검증: 해당 단계 예상 시간 감소, 총 예상 시간 감소
