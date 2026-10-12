---
name: indexes
description: 보조 인덱스(USI/NUSI), Join Index, Aggregate Join Index 의 사용 조건과 유지 비용
category: 접근경로
findings: [FULL_SCAN_LARGE]
---
# 인덱스

Teradata 의 기본 접근 경로는 PI 다. 보조 인덱스와 조인 인덱스는 **반복되는 특정 패턴**에만 설계 변경(등급 C)으로 권고한다.

## TP-IDX-01 PI 접근 활용
- 신호: PI 컬럼에 동등 조건을 줄 수 있는데 다른 조건만 사용 → all-AMPs 스캔
- 조치(A): PI 컬럼 전체에 대한 동등 조건(다중 컬럼 PI 면 모든 컬럼)을 넣으면 single-AMP 접근.
  PI 일부 컬럼만 조건이면 해시 접근 불가.

## TP-IDX-02 보조 인덱스 (USI / NUSI)
- USI: 유일값 조회 → two-AMP 접근. NUSI: 선택도가 높을 때(전체 대비 소수 행) all-AMPs 지만 서브테이블만 읽음.
- 옵티마이저는 통계가 있어야 NUSI 를 선택한다 → 인덱스 컬럼 통계 수집 필수.
- 선택도가 낮으면 전체 스캔이 더 싸서 사용되지 않는다 (인덱스를 만들어도 안 쓰일 수 있음).
- 비용: INSERT/UPDATE/DELETE 마다 인덱스 갱신, 저장공간. FastLoad 는 SI 가 있는 테이블에 적재 불가, MultiLoad 는 USI 불가.

## TP-IDX-03 Join Index / Aggregate Join Index
- Join Index(JI): 자주 조인되는 테이블 결과를 미리 저장 → 조인 생략. 단일 테이블 JI 로 **다른 PI** 를 줄 수도 있다
  (조인 키 기준 재분배를 미리 해둔 효과).
- Aggregate Join Index(AJI): 반복되는 집계를 미리 저장 → 요약 쿼리 가속.
- 옵티마이저가 쿼리를 JI 로 커버할 수 있을 때만 자동 사용 (EXPLAIN 에 JI 이름이 나오는지 확인).
- 비용: 기본 테이블 DML 시 자동 유지 → 적재 시간 증가. FastLoad/MultiLoad 제약.

## TP-IDX-04 권고 기준
- 같은 패턴의 쿼리가 하루 수십 회 이상, 또는 핵심 리포트의 SLA 문제일 때만.
- 적재 배치 영향(시간, 유틸리티 제약)을 함께 적는다.
