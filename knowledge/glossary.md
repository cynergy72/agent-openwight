<!-- 팀의 업무 용어와 규칙을 적어두세요. 작은 모델일수록 이 정보가 정확도를 크게 좌우합니다. -->
- 매출 = SALES_TXN.SALE_AMT 합계. "순매출" = SALE_AMT - DISC_AMT.
- 활성 고객 = 최근 90일 이내 거래가 1건 이상 있는 고객.
- 월 기준은 판매일자(SALE_DT)의 달력 월. 이번 달 = TRUNC(CURRENT_DATE, 'MM') 부터 오늘까지.
- 큰 거래 테이블(SALES_TXN)은 항상 SALE_DT 범위 조건을 넣는다 (파티션 제거).
