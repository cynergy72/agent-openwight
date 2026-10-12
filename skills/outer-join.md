---
name: outer-join
description: OUTER JOIN 함정과 튜닝 - WHERE 의 안쪽 테이블 조건, ON 절 오해, 불필요한 OUTER JOIN 제거
category: 조인
findings: [OUTER_WHERE_FILTER, UNUSED_OUTER_JOIN]
---
# OUTER JOIN

`a LEFT JOIN b` 에서 a 는 보존(outer) 테이블, b 는 안쪽(inner, NULL 채움) 테이블이다.

## TP-OJ-01 WHERE 절에 안쪽 테이블 조건
- 신호: `LEFT JOIN b ... WHERE b.col = 'X'` (IS NULL 조건 제외)
- 의미: b 가 없는 행은 b.col 이 NULL 이라 WHERE 에서 탈락 → 사실상 INNER JOIN. 옵티마이저도 INNER 로 바꾼다.
- 조치: 의도가 INNER 면 `JOIN` 으로 명시 (가독성·옵티마이저 선택지 확대).
  의도가 "b 가 없어도 a 는 유지" 라면 조건을 ON 절로 옮겨야 하며 **결과가 달라진다** → 버그 가능성으로 사용자에게 보고.

## TP-OJ-02 ON 절의 보존 테이블 조건 오해
- 신호: `a LEFT JOIN b ON a.k = b.k AND a.type = 'X'`
- 의미: a.type 조건은 a 행을 거르지 않는다 (매칭 여부만 바꿈). 필터 의도라면 WHERE 로 옮겨야 하며 결과가 달라진다.
- 조치: 의도 확인 후 보고. 성능상 a 를 거르는 필터는 WHERE 에 두는 것이 스캔량을 줄인다.

## TP-OJ-03 불필요한 OUTER JOIN
- 신호: LEFT JOIN 한 테이블의 컬럼을 SELECT/WHERE/GROUP BY 어디에서도 쓰지 않음, 그리고 조인 키가 b 에서 유일
- 의미: b 가 결과에 영향이 없다 (b 키가 유일할 때만. 유일하지 않으면 행이 늘어날 수 있음).
- 조치(A): 조인 제거. b 키 유일성(UPI/PK)을 카탈로그로 확인하고 가정으로 명시.

## TP-OJ-04 OUTER JOIN 과 재분배
- 보존 테이블이 크고 안쪽이 작으면 작은 쪽 복제가 유리하다. 안쪽 테이블은 조인 전에 필터해 둔다 (파생테이블 안 WHERE).
