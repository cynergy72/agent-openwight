<!-- 검증된 요청 → 쿼리 예시. 팀 스타일을 가르치는 가장 효과적인 방법입니다. -->
요청: 지난달 채널별 매출과 거래 건수
```sql
SELECT  t.CHANNEL_CD
      , SUM(t.SALE_AMT) AS SALE_AMT
      , COUNT(*)        AS TXN_CNT
FROM    SALES_DB.SALES_TXN t
WHERE   t.SALE_DT >= ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), -1)
AND     t.SALE_DT <  TRUNC(CURRENT_DATE, 'MM')
GROUP BY 1
ORDER BY SALE_AMT DESC;
```
