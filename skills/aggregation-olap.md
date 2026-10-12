---
name: aggregation-olap
description: 집계·윈도우 함수 튜닝 - DISTINCT vs GROUP BY, 조인 전 집계, PARTITION BY 없는 윈도우, QUALIFY, 윈도우 프레임
category: 재작성
findings: [WINDOW_NO_PARTITION, DISTINCT_LARGE]
---
# 집계와 윈도우(OLAP) 함수

## TP-AGG-01 DISTINCT vs GROUP BY
- 같은 결과지만 처리 방식이 다를 수 있다: GROUP BY 는 AMP 로컬 집계 후 재분배(중복이 많을 때 유리),
  DISTINCT 는 재분배 후 중복 제거(중복이 적을 때 유리). 최신 버전은 옵티마이저가 알아서 고르는 경우가 많다.
- 조치(A): 대용량에서 중복이 많은 컬럼의 DISTINCT 는 GROUP BY 로 바꿔 EXPLAIN 비교. 효과 없으면 원복.

## TP-AGG-02 조인 전 집계 (pre-aggregation)
- join-geography 스킬 TP-JOIN-14 참조. 1:N 의 N 쪽을 먼저 집계하면 재분배·조인 행이 줄어든다.

## TP-AGG-03 PARTITION BY 없는 윈도우 함수
- 신호: `ROW_NUMBER() OVER (ORDER BY ...)`, `SUM(x) OVER ()` 등 PARTITION BY 없음 (STAT FUNCTION 단계)
- 원인: 파티션이 하나이므로 **모든 행이 한 AMP 로 모여** 처리된다 → 극심한 스큐, 스풀 부족(2646).
- 조치(A): 업무상 가능한 PARTITION BY 추가. 전체 순번이 꼭 필요하면 데이터를 먼저 줄이고(필터·집계) 적용,
  상위 N 만 필요하면 `TOP n ... ORDER BY` 또는 `QUALIFY ... <= n`.
- 전체 합계 대비 비율은 먼저 전체 합계를 1행으로 집계해 CROSS JOIN 하는 방식이 더 싸다.

## TP-AGG-04 낮은 카디널리티 PARTITION BY
- 신호: `PARTITION BY 성별`, `PARTITION BY 지역(소수)` + 대용량 → 소수 AMP 에 집중
- 조치(A): 더 세분된 키로 1차 처리 후 상위 집계, 또는 대상 행을 먼저 줄인다.

## TP-AGG-05 QUALIFY 활용
- 윈도우 결과로 거르는 파생테이블 래핑 대신 `QUALIFY ROW_NUMBER() OVER (...) = 1`. 가독성·스풀 단계 감소.

## TP-AGG-06 윈도우 프레임 명시
- ORDER BY 가 있는 집계 윈도우는 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` 등 프레임을 명시
  (버전마다 기본 프레임이 달라 결과가 달라질 수 있음).
