---
name: skew
description: 스큐 진단과 해결 - 테이블 PI 스큐, 조인/집계 키 쏠림, NULL·기본값, 스풀 부족(2646), DBQL CPU 스큐
category: 분산
findings: [CPU_SKEW, IO_SKEW, TABLE_SKEW]
---
# 스큐 (Skew)

병렬 처리는 **가장 느린 AMP** 가 끝나야 끝난다. 스풀 한도도 AMP 별로 나뉘므로(사용자 스풀 ÷ AMP 수)
한 AMP 에 몰리면 전체 스풀이 남아도 `2646 No more spool space` 가 난다.

## 진단 쿼리
```sql
-- 테이블 스큐 (AMP 별 용량)
SELECT DatabaseName, TableName, SUM(CurrentPerm) AS CurrentPerm,
       CAST((1 - AVG(CurrentPerm) / NULLIFZERO(MAX(CurrentPerm))) * 100 AS DECIMAL(5,2)) AS SkewPct
FROM DBC.TableSizeV WHERE DatabaseName = 'DB' AND TableName = 'TBL' GROUP BY 1, 2;
-- PI 값이 AMP 에 퍼지는 모양
SELECT HASHAMP(HASHBUCKET(HASHROW(PI_COL))) AS amp_no, COUNT(*) FROM DB.TBL GROUP BY 1 ORDER BY 2 DESC;
-- 조인/집계 키 상위 값
SELECT TOP 20 KEY_COL, COUNT(*) FROM DB.TBL GROUP BY 1 ORDER BY 2 DESC;
```
현장 경험칙: SkewPct 10~20% 이상 조사, 50% 이상 심각. DBQL CPU 스큐 2 이상 주의, 5 이상 심각.

## TP-SKEW-01 테이블 PI 스큐
- 원인: PI 카디널리티가 낮거나 특정 값(NULL, 0, 기본 코드)이 많음
- 조치(C): 고유값이 많은 컬럼(조합)으로 PI 변경 (primary-index-design 스킬). 재생성 필요.

## TP-SKEW-02 조인 키 쏠림 (재분배 후 스풀 스큐)
- 신호: DBQL CPU 스큐 높음 + EXPLAIN 에서 해당 키로 `redistributed`
- 조치(A): 쏠린 값만 분리 처리 → 쏠린 값 쪽은 상대 테이블 해당 행을 복제 조인, 나머지는 일반 조인 → UNION ALL.
- 조치(B): 조인 컬럼 통계 수집 → 옵티마이저가 쏠린 값을 알면 PRPD(부분 재분배·부분 복제)를 자동 적용할 수 있다
  (버전/설정 의존).

## TP-SKEW-03 NULL·기본값 쏠림
- 동등 INNER JOIN 에서 NULL 은 매칭되지 않으므로 조인 전 `WHERE k IS NOT NULL` 은 결과를 바꾸지 않는다
  (OUTER JOIN 의 보존 테이블에는 적용 금지 - 결과가 달라짐).
- 0, -1, 'UNKNOWN' 같은 기본값은 매칭될 수 있으므로 업무 의미를 확인하고 TP-SKEW-02 방식으로 분리.

## TP-SKEW-04 집계·윈도우 키 쏠림
- 낮은 카디널리티 GROUP BY / PARTITION BY 는 몇 개 AMP 에 몰린다. PARTITION BY 없는 윈도우 함수는 **한 AMP** 에서
  처리된다 (aggregation-olap 스킬 TP-AGG-03).

## TP-SKEW-05 volatile table PI 를 잘못 잡음
- `CREATE VOLATILE TABLE ... AS (...) WITH DATA` 에서 PI 를 생략하면 첫 컬럼이 PI 가 되어 스큐가 생기기 쉽다.
- 조치(A): 다음 단계 조인 키 중 분산이 좋은 컬럼으로 `PRIMARY INDEX (...)` 명시.
