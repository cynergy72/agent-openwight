from tdagent.sql_lint import (explain_hints, extract_sql_blocks, is_read_only,
                              lint_sql, normalize_sql, referenced_tables)


def codes(sql):
    return {i.code: i.level for i in lint_sql(sql)}


def test_valid_teradata_has_no_errors():
    sql = """SELECT TOP 10 c.CUST_NM, SUM(t.SALE_AMT) AS amt
             FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID
             WHERE t.SALE_DT >= ADD_MONTHS(CURRENT_DATE, -3)
             GROUP BY 1 ORDER BY 2 DESC"""
    assert not [i for i in lint_sql(sql) if i.level == "error"]


def test_foreign_dialect_errors():
    c = codes("SELECT a::int FROM t WHERE b ILIKE 'x%' AND d > NOW() AND IFNULL(e, 0) = 1 LIMIT 10")
    assert {"pg_cast", "ilike", "now", "ifnull", "limit"} <= {k for k, v in c.items() if v == "error"}


def test_rules_ignore_strings_and_comments():
    sql = "SELECT 'use LIMIT 10 here' AS note -- LIMIT 5\nFROM t /* ILIKE */"
    assert "limit" not in codes(sql) and "ilike" not in codes(sql)


def test_warnings():
    assert "product_join" in codes("SELECT a.x FROM t1 a JOIN t2 b")
    assert "comma_join" in codes("SELECT a.x FROM t1 a, t2 b WHERE a.id = b.id")
    assert "non_sargable" in codes("SELECT x FROM t WHERE CAST(ts AS DATE) = DATE '2024-01-01'")
    assert "not_in_subquery" in codes("SELECT x FROM t WHERE id NOT IN (SELECT id FROM u)")
    assert "window_frame" in codes("SELECT SUM(x) OVER (PARTITION BY k ORDER BY d) FROM t")
    assert "window_frame" not in codes(
        "SELECT SUM(x) OVER (PARTITION BY k ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t")
    assert "subquery_order_by" in codes("SELECT * FROM (SELECT a FROM t ORDER BY a) x")
    assert "select_star" in codes("SELECT * FROM t")


def test_read_only_guard():
    assert is_read_only("SELECT a FROM t;")[0]
    assert is_read_only("LOCKING ROW FOR ACCESS SEL a FROM t")[0]
    assert is_read_only("WITH x AS (SELECT 1 AS a) SELECT a FROM x")[0]
    assert is_read_only("SELECT update_dt FROM t WHERE note = 'delete me'")[0]
    assert not is_read_only("DELETE FROM t")[0]
    assert not is_read_only("SELECT 1; DROP TABLE t")[0]
    assert not is_read_only("INSERT INTO t SELECT * FROM u")[0]
    assert not is_read_only("CREATE VOLATILE TABLE v AS (SELECT 1 a) WITH DATA")[0]


def test_extract_and_normalize():
    text = "설명\n```sql\nSELECT 1;\n```\n그리고\n```SQL\nSELECT 2\n```"
    assert extract_sql_blocks(text) == ["SELECT 1;", "SELECT 2"]
    assert extract_sql_blocks("```\nselect 3\n```") == ["select 3"]
    assert normalize_sql("SELECT  a -- c\n FROM t;") == "select a from t"


def test_referenced_tables_excludes_ctes():
    sql = "WITH x AS (SELECT a FROM db.t1) SELECT * FROM x JOIN db.t2 ON 1=1"
    assert sorted(referenced_tables(sql)) == ["db.t1", "db.t2"]


def test_explain_hints():
    plan = """3) We do an all-AMPs RETRIEVE step from 12 partitions of SALES_DB.SALES_TXN by way of an
    all-rows scan with no residual conditions into Spool 2, which is redistributed by the hash code of
    (CUST_ID). The size of Spool 2 is estimated with low confidence to be 1,234 rows. The estimated size of the spool is 1,234 rows
    4) We do an all-AMPs JOIN step ... using a product join ...
    The total estimated time is 0.25 seconds."""
    hints = "\n".join(explain_hints(plan))
    for word in ("Product join", "재분배", "all-rows scan", "low confidence", "파티션 제거", "1,234", "0.25"):
        assert word in hints
