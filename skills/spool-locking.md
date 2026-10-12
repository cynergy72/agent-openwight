---
name: spool-locking
description: 리소스·동시성 - 대용량 스풀, 스풀 부족(2646), 조회 잠금(LOCKING FOR ACCESS), TOP/SAMPLE 로 탐색 비용 줄이기
category: 리소스
findings: [LARGE_SPOOL, NO_ACCESS_LOCK]
---
# 스풀과 잠금

## TP-RES-01 대용량 스풀
- 신호: EXPLAIN 스풀 예상 행 수·bytes 가 큼, DBQL SpoolUsage 큼
- 조치(A): 불필요 컬럼 제거(TP-RW-01), 조인 전 필터·집계(TP-JOIN-14), 넓은 문자열은 마지막 단계에서 조인.

## TP-RES-02 스풀 부족 (2646 No more spool space)
- 스풀 한도는 AMP 별(사용자 스풀 ÷ AMP 수). 총 스풀이 남아도 스큐로 한 AMP 가 넘치면 실패한다.
- 조치: 스큐 해결(skew 스킬)이 우선. 스풀 한도 증설은 근본 해결이 아니다.

## TP-RES-03 조회 쿼리 잠금
- 신호: 조회 쿼리가 `for read` 잠금 → 적재(쓰기)와 서로 대기
- 조치(A): 조회는 `LOCKING ROW FOR ACCESS SELECT ...` (적재 중인 데이터를 읽을 수 있음 = dirty read 허용 시).
  보통 조회용 뷰에 LOCKING FOR ACCESS 가 정의돼 있으므로 뷰를 통해 조회.

## TP-RES-04 탐색 쿼리 비용
- 데이터 확인용 쿼리는 `SAMPLE n` 또는 `TOP n` (ORDER BY 없는 TOP 은 정렬 없이 빠름) 사용.
- `SELECT COUNT(*)` 대신 통계/DBC.TableSizeV 로 규모 파악.
