---
name: etl-dml
description: ETL/DML 튜닝 - 빈 테이블 INSERT SELECT, 대량 UPDATE 대체, MERGE, DELETE ALL, SET 테이블 적재, 트랜잭션 저널
category: ETL
findings: [UPDATE_JOIN, DELETE_ALL_CANDIDATE, SET_TABLE_DML]
---
# ETL / DML

DML 은 변경 행마다 트랜잭션 저널(TJ, 롤백용 이미지)을 남겨 대량 변경 시 비싸다. EXPLAIN 은 INSERT/UPDATE/DELETE/MERGE 에도
쓸 수 있다 (실행하지 않음).

## TP-ETL-01 빈 테이블에 INSERT SELECT
- 빈 테이블 대상 INSERT SELECT 는 행 단위 저널 없이 빠르게 처리된다 (fast path).
  대상과 원천의 PI 가 같으면 AMP 간 이동도 없다.
- 조치(A): 대량 변환은 "빈 대상 테이블 + INSERT SELECT" 구조로. 원천 PI 와 대상 PI 정렬.

## TP-ETL-02 대량 UPDATE → 새 테이블 재생성
- 신호: 테이블 행의 상당 부분(경험칙 수십 % 이상)을 UPDATE
- 조치(A/C): 변경 값을 계산한 SELECT 로 빈 새 테이블에 INSERT SELECT → RENAME 교체. 저널·블록 재작성 비용 절감.

## TP-ETL-03 UPDATE-조인 / UPDATE+INSERT → MERGE
- 신호: `UPDATE t FROM s SET ... WHERE t.k = s.k` 후 존재하지 않는 행 INSERT 를 별도 수행
- 조치(A): 한 번의 `MERGE INTO t USING s ON t.k = s.k WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`.
  ON 절에 대상 테이블 PI 동등 조건이 있을 때 가장 효율적 (버전에 따라 필수 조건).

## TP-ETL-04 전체 삭제
- 신호: `DELETE FROM t WHERE 1=1` 또는 조건 없는 DELETE 를 트랜잭션 중간에 실행
- 조치(A): `DELETE FROM t ALL;` (또는 조건 없는 DELETE) 를 트랜잭션의 마지막 문장으로 → 행 단위 저널 없이 빠름.

## TP-ETL-05 SET 테이블 적재
- primary-index-design TP-PI-02 참조. 대량 적재 대상은 MULTISET + 원천에서 중복 제거가 일반적으로 빠르다.

## TP-ETL-06 volatile table 사용 규칙
- PI 명시(다음 조인 키), `ON COMMIT PRESERVE ROWS`, 적재 후 통계 수집, 필요한 컬럼만.
- 같은 세션에서만 존재하므로 다른 세션의 EXPLAIN 으로는 검증할 수 없다.
