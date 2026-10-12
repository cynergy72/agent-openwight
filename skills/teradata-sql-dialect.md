---
name: teradata-sql-dialect
description: Teradata SQL dialect rules (TOP/QUALIFY, dates, strings, casts, volatile tables) - always applied when writing SQL
always: true
always_tuning: true
---
Teradata SQL dialect - follow these strictly:
- Row limit: `SELECT TOP 10 ...` or `SAMPLE 10`. NEVER use LIMIT / OFFSET / FETCH FIRST.
- Top-N per group / dedup: `QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY d DESC) = 1`.
- Date literal: `DATE '2024-01-31'`. Timestamp literal: `TIMESTAMP '2024-01-31 10:00:00'`.
- Current: CURRENT_DATE, CURRENT_TIMESTAMP (never NOW(), GETDATE(), SYSDATE).
- Date math: `d + 7`, `d - 1`, `ADD_MONTHS(d, -3)`, `d + INTERVAL '1' DAY`, `date1 - date2` = days (INTEGER).
  Month start: `TRUNC(d, 'MM')`. Month end: `LAST_DAY(d)`. Parts: `EXTRACT(YEAR FROM d)`.
- Char -> date: `CAST('20240131' AS DATE FORMAT 'YYYYMMDD')`. Date -> char: `CAST(CAST(d AS FORMAT 'YYYYMMDD') AS CHAR(8))`.
- Strings: `a || b`, SUBSTR(s, start, len), TRIM(s), INDEX(s, 'x') or POSITION('x' IN s), OREPLACE(s, 'a', 'b'), UPPER/LOWER.
- NULLs: COALESCE, ZEROIFNULL(x), NULLIFZERO(x), NULLIF. No IFNULL / ISNULL.
- Casts: CAST(x AS INTEGER / DECIMAL(18,2) / VARCHAR(100)). No `::`.
- Identifiers: double quotes only if needed. No backticks. Always qualify tables as Database.Table.
- No ORDER BY inside subqueries / derived tables / views (unless with TOP).
- Window aggregates with ORDER BY: always write the frame, e.g.
  `SUM(x) OVER (PARTITION BY k ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`.
- Temp tables: `CREATE VOLATILE TABLE vt AS (SELECT ...) WITH DATA PRIMARY INDEX (k) ON COMMIT PRESERVE ROWS;`
- Upsert: MERGE INTO ... USING ... ON ... WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...
- Prefer `NOT EXISTS` over `NOT IN (subquery)`.
Performance (Teradata is MPP, data is hashed to AMPs by the PRIMARY INDEX):
- Join on PI columns when possible; joins on non-PI columns cause redistribution or duplication.
- Filter on the partition column with plain range predicates (no functions on the column) so partitions are eliminated.
- Avoid product joins (missing join conditions) and SELECT *.
- `LOCKING ROW FOR ACCESS` before SELECT avoids blocking behind writers (dirty read).
