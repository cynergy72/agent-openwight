---
name: join-product-join
description: Product join 의 원인별 튜닝 포인트 (조건 누락, OR 조인, 비동등/기간 조인, 함수 조인, 통계 오판)
category: 조인
findings: [PRODUCT_JOIN, PRODUCT_JOIN_SUSPECT, COMMA_JOIN, OR_JOIN, NONEQUI_JOIN, FUNCTION_JOIN]
---
# Product Join

모든 행 × 모든 행을 비교한다. 한쪽이 수 행~수백 행이면 정상일 수 있지만, 양쪽이 크면 가장 비싼 연산이다.
EXPLAIN `joined using a product join` 의 입력 스풀 예상 행 수를 먼저 확인한다.

## TP-JOIN-01 조인 조건 누락 / 콤마 조인
- 신호: `product join` + `join condition of ("(1=1)")` 또는 조건 없음. SQL 에 `FROM a, b` 콤마 조인.
- 원인: WHERE 에서 조인 조건을 빠뜨림. 테이블이 3개 이상일 때 흔함.
- 조치(A): 명시적 `JOIN ... ON` 으로 바꾸고 누락 조건 추가. 의도된 cross join 이면 `CROSS JOIN` 으로 명시.
- 검증: product join 사라짐, 결과 행 수 급감(누락이었다면 원본 결과가 틀렸던 것 → 사용자 확인 필요).

## TP-JOIN-02 OR 조인
- 신호: `ON a.k1 = b.k1 OR a.k2 = b.k2` → product join
- 조치(A): 조건별 동등 조인으로 분리 후 합치기.
```sql
SELECT a.id, b.nm FROM db.a a JOIN db.b b ON a.k1 = b.k1
UNION            -- 두 조건을 동시에 만족하는 행 중복 제거. 중복이 불가능하면 UNION ALL
SELECT a.id, b.nm FROM db.a a JOIN db.b b ON a.k2 = b.k2;
```
- 주의: 원본은 한 쌍(a행,b행)이 1번 나오므로 UNION ALL 은 중복을 만들 수 있다. 출력에 키가 없으면 UNION 도 원본 중복을 없앨 수 있다 → 결과 동일성 확인.

## TP-JOIN-03 비동등 / 기간 조인
- 신호: `ON a.dt BETWEEN b.start_dt AND b.end_dt`, `<`, `LIKE`
- 조치(A): 작은 쪽을 먼저 최대한 필터. 기간 조인은 달력 테이블로 일자 단위 동등 조인 변환을 검토
  (`JOIN calendar c ON c.dt BETWEEN b.start_dt AND b.end_dt` 로 b 를 일자로 펼친 뒤 `a.dt = c.dt` 동등 조인).
  가능한 동등 조건(예: 같은 고객)이 있으면 반드시 함께 둔다 → 동등 조건으로 해시 조인/병합 조인 후 기간은 잔여 조건.

## TP-JOIN-04 함수/표현식 조인
- 신호: `ON SUBSTR(a.cd,1,3) = b.cd`, `ON CAST(a.id AS VARCHAR(10)) = b.id`, `ON a.x || a.y = b.z`
- 조치(A): 변환값을 파생테이블 컬럼으로 미리 만든 뒤 그 컬럼으로 동등 조인. 반복되면 (C) 테이블에 변환 컬럼 추가.

## TP-JOIN-05 통계 오판으로 인한 product join
- 신호: product join 입력 스풀이 `no/low confidence` 로 작게 추정됐는데 실제로는 큼 (DBQL PJI ≥ 3)
- 조치(B): 조인/필터 컬럼 통계 수집 (statistics 스킬). 조치 후 merge/hash join 으로 바뀌는지 EXPLAIN 비교.
