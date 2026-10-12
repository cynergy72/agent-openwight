from pathlib import Path

import pytest

from tdagent.config import DBQLConfig, TuningConfig
from tdagent.llm import LLMReply
from tdagent.schema import SchemaStore
from tdagent.skills import SkillLibrary
from tdagent.teradata import TeradataError
from tdagent.tuning import dbql, recommend
from tdagent.tuning.explain import parse_explain, parse_time, plan_metrics
from tdagent.tuning.findings import FINDING_INFO, from_dbql, from_explain, from_table_skew
from tdagent.tuning.goal import Goal, evaluate, is_better
from tdagent.tuning.prompts import parse_rewrite_reply
from tdagent.tuning.session import TuningSession
from tdagent.tuning.sqlcheck import analyze_sql, result_check_sql, semantic_warnings

ROOT = Path(__file__).resolve().parents[1]
BAD = (ROOT / "tests/fixtures/explain_bad.txt").read_text()
GOOD = (ROOT / "tests/fixtures/explain_good.txt").read_text()
STORE = SchemaStore.load_dir(ROOT / "schema")
SKILLS = SkillLibrary.load_dir(ROOT / "skills")
PART = {"SALES_DB.SALES_TXN"}

ORIG = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t, SALES_DB.CUSTOMER c
WHERE CAST(t.SALE_DT AS CHAR(7)) = '2024-01' AND t.CUST_ID = c.CUST_ID
GROUP BY 1"""
GOOD_SQL = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID
WHERE t.SALE_DT >= DATE '2024-01-01' AND t.SALE_DT < DATE '2024-02-01'
GROUP BY 1"""


def reply(sql, changes="- [TP-PART-02] 범위 조건"):
    return f"<analysis>TP-PART-02</analysis>\n<rewrite>\n{sql}\n</rewrite>\n<changes>{changes}</changes>\n<assumptions></assumptions>"


class FakeTD:
    def __init__(self, allow_execute=False, diff=(0, 0)):
        self.explained = []
        self.cfg = type("C", (), {"allow_execute": allow_execute})()
        self.diff = list(diff)

    def explain(self, sql):
        self.explained.append(sql)
        if "BROKEN" in sql:
            raise TeradataError("[Error 3807] Object 'BROKEN' does not exist.")
        return BAD if "CAST(" in sql else GOOD

    def table_skew(self, name):
        return 35.0 if "CUSTOMER" in name else 2.0

    def describe(self, name):
        return None

    def count_query(self, sql):
        return self.diff.pop(0)


class Model:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMReply(content=r)


def session(replies, td=None, **cfg):
    model = Model(replies)
    return TuningSession(model, SchemaStore.load_dir(ROOT / "schema"), SKILLS, TuningConfig(**cfg), td=td), model


# ------------------------------------------------------------------ explain
def test_parse_bad_plan():
    p = parse_explain(BAD)
    assert p.total_time == 4320
    assert [s.label for s in p.steps if s.op] == ["4", "5"]
    s4, s5 = p.step("4"), p.step("5")
    assert s4.geography == "duplicated" and s4.confidence == "no" and s4.est_rows == 24_000_000
    assert s5.join_method == "product" and s5.est_time == 4320 and s5.est_bytes == 5_880_000_000
    assert "CAST" in s5.conditions[0] and " OR " in s5.join_condition
    assert len(p.helpstats) == 3 and p.helpstats[2].endswith("ON SALES_DB.SALES_TXN;")
    m = plan_metrics(p, PART)
    assert m["product_joins"] == 1 and m["partition_misses"] == 1 and m["no_confidence_steps"] == 2


def test_parse_good_plan_parallel_steps():
    p = parse_explain(GOOD)
    assert p.total_time == 0.81 and p.helpstats == []
    s = p.step("3.1")
    assert s.scans[0].partitions == "1" and not s.scans[0].full_scan
    assert p.step("3.2").geography == "redistributed" and "CUST_ID" in p.step("3.2").redistribute_keys
    assert p.step("4").join_method == "merge"
    assert plan_metrics(p, PART)["partition_misses"] == 0


@pytest.mark.parametrize("text,sec", [("0.85 seconds", 0.85), ("1 hour and 12 minutes", 4320),
                                      ("2 minutes and 3 seconds", 123), ("00:01:23.5", 83.5), ("?", None)])
def test_parse_time(text, sec):
    assert parse_time(text) == sec


def test_explain_findings():
    codes = {f.code: f for f in from_explain(parse_explain(BAD), TuningConfig(), PART)}
    for c in ("PRODUCT_JOIN", "DUPLICATE_LARGE", "PARTITION_NOT_ELIMINATED", "FULL_SCAN_LARGE",
              "STATS_NO_CONFIDENCE", "STATS_RECOMMENDED", "HOTSPOT_STEP", "NON_SARGABLE"):
        assert c in codes, c
    assert codes["PRODUCT_JOIN"].severity == "critical" and codes["PRODUCT_JOIN"].step == "5"
    good = {f.code for f in from_explain(parse_explain(GOOD), TuningConfig(), PART)}
    assert not good & {"PRODUCT_JOIN", "PARTITION_NOT_ELIMINATED", "STATS_NO_CONFIDENCE"}


def test_dbql_and_table_findings():
    m = {"AMPCPUTime": 5000, "TotalIOCount": 100000, "MaxAMPCPUTime": 150, "NumOfActiveAMPs": 200,
         "MaxAMPIO": 500}
    f = {x.code: x for x in from_dbql(m, TuningConfig())}
    assert f["CPU_SKEW"].severity == "critical" and "6.0" in f["CPU_SKEW"].detail
    assert "PRODUCT_JOIN_SUSPECT" in f and "UNNECESSARY_IO" not in f and "IO_SKEW" not in f
    assert from_table_skew("DB.T", 10.0, TuningConfig()) == []
    assert from_table_skew("DB.T", 60.0, TuningConfig())[0].severity == "critical"


# ------------------------------------------------------------------ sql patterns
def codes(sql):
    return {f.code for f in analyze_sql(sql, STORE)}


def test_sql_pattern_detection():
    assert "OR_JOIN" in codes("SELECT 1 FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c "
                              "ON t.CUST_ID = c.CUST_ID OR t.PROD_ID = c.CUST_ID")
    assert "OR_JOIN" in codes("SELECT 1 FROM SALES_DB.SALES_TXN t, SALES_DB.CUSTOMER c "
                              "WHERE t.CUST_ID = c.CUST_ID OR t.PROD_ID = c.CUST_ID")
    assert "NONEQUI_JOIN" in codes("SELECT 1 FROM db.a a JOIN db.b b ON a.dt BETWEEN b.s AND b.e")
    assert "FUNCTION_JOIN" in codes("SELECT 1 FROM db.a a JOIN db.b b ON SUBSTR(a.cd,1,3) = b.cd")
    assert "IMPLICIT_CONVERSION" in codes("SELECT 1 FROM SALES_DB.CUSTOMER WHERE REGION_CD = 11")
    assert "IMPLICIT_CONVERSION" not in codes("SELECT 1 FROM SALES_DB.CUSTOMER WHERE REGION_CD = '11'")
    assert "OUTER_WHERE_FILTER" in codes("SELECT a.x FROM db.a a LEFT JOIN db.b b ON a.k = b.k WHERE b.c = 1")
    assert "OUTER_WHERE_FILTER" not in codes("SELECT a.x FROM db.a a LEFT JOIN db.b b ON a.k = b.k WHERE b.k IS NULL")
    assert "UNUSED_OUTER_JOIN" in codes("SELECT a.x FROM db.a a LEFT JOIN db.b b ON a.k = b.k")
    assert "WINDOW_NO_PARTITION" in codes("SELECT ROW_NUMBER() OVER (ORDER BY x) FROM db.t")
    assert "WINDOW_NO_PARTITION" not in codes("SELECT ROW_NUMBER() OVER (PARTITION BY k ORDER BY x) FROM db.t")
    assert "SCALAR_SUBQUERY" in codes("SELECT a.x, (SELECT b.n FROM db.b b WHERE b.k = a.k) FROM db.a a")
    assert "CORRELATED_SUBQUERY" in codes("SELECT * FROM db.t a WHERE a.v = (SELECT MAX(b.v) FROM db.t b WHERE b.k = a.k)")
    assert "NOT_IN_SUBQUERY" in codes("SELECT 1 FROM db.a WHERE k NOT IN (SELECT k FROM db.b)")
    assert "LARGE_IN_LIST" in codes("SELECT 1 FROM db.a WHERE k IN (" + ",".join(map(str, range(150))) + ")")
    assert "UPDATE_JOIN" in codes("UPDATE t FROM db.s s SET x = s.x WHERE t.k = s.k")
    unused = analyze_sql("WITH t AS (SELECT CUST_ID, SALE_AMT, QTY FROM SALES_DB.SALES_TXN) "
                         "SELECT SUM(t.SALE_AMT) FROM t", STORE)
    assert any(f.code == "UNUSED_COLUMNS" and "qty" in f.detail.lower() and "cust_id" in f.detail.lower()
               for f in unused)


def test_partition_column_function_is_critical():
    f = [x for x in analyze_sql("SELECT 1 FROM SALES_DB.SALES_TXN WHERE CAST(SALE_DT AS CHAR(7)) = '2024-01'", STORE)
         if x.code == "NON_SARGABLE"]
    assert f and f[0].severity == "critical"


def test_semantic_guards():
    dropped = ORIG.replace("AND t.CUST_ID = c.CUST_ID", "AND (t.CUST_ID = c.CUST_ID OR t.PROD_ID = c.CUST_ID)")
    assert "PROD_ID" in semantic_warnings(dropped, GOOD_SQL)[0]
    assert semantic_warnings(ORIG, GOOD_SQL) == []
    assert "OUTER" in " ".join(semantic_warnings("SELECT a.x FROM db.a a LEFT JOIN db.b b ON a.k = b.k",
                                                 "SELECT a.x FROM db.a a JOIN db.b b ON a.k = b.k"))
    assert result_check_sql(ORIG, GOOD_SQL)[0].startswith("SELECT COUNT(*) FROM (")
    assert result_check_sql("SELECT a FROM t ORDER BY 1", "SELECT a FROM t") is None


# ------------------------------------------------------------------ goal / recs / dbql
def test_goal_and_better():
    base = plan_metrics(parse_explain(BAD), PART)
    good = plan_metrics(parse_explain(GOOD), PART)
    crit = evaluate(Goal(time_reduction_pct=50), base, base, 3, 1_000_000)
    assert not any(c.met for c in crit)
    crit = evaluate(Goal(time_reduction_pct=50, no_large_duplication=True), base, good, 0, 1_000_000)
    assert all(c.met for c in crit) and "단축" in crit[-1].detail
    assert is_better(good, 0, 3, base, 3, 5)[0]
    assert not is_better(base, 3, 5, good, 0, 3)[0]
    assert not is_better(dict(good, product_joins=1), 0, 3, good, 0, 3)[0]


def test_recommendations():
    p = parse_explain(BAD)
    f = from_explain(p, TuningConfig(), PART) + from_table_skew("SALES_DB.CUSTOMER", 40.0, TuningConfig())
    r = recommend.build(ORIG, p, f, STORE)
    assert r.stats[0] == "COLLECT STATISTICS COLUMN (CUST_ID) ON SALES_DB.CUSTOMER;"
    assert "COLLECT STATISTICS COLUMN (PARTITION) ON SALES_DB.SALES_TXN;" in r.stats
    assert ("SALES_DB.CUSTOMER", "CUST_ID") in r.pi_candidates
    assert len(r.stats) == len({s.upper() for s in r.stats})


def test_partition_candidate_for_unpartitioned_scan():
    store = SchemaStore.load_dir(ROOT / "schema")
    store.find("SALES_DB.SALES_TXN").partitioned_by = ""
    p = parse_explain(BAD)
    f = from_explain(p, TuningConfig(scan_rows=1), set())
    r = recommend.build(GOOD_SQL, p, f, store)
    assert ("SALES_DB.SALES_TXN", "SALE_DT") in r.partition_candidates


def test_dbql_queries():
    class TD:
        def __init__(self):
            self.calls = []

        def query(self, sql, params):
            self.calls.append((sql, params))
            if "SqlTextInfo FROM" in sql:
                return ["SqlTextInfo"], [("SELECT a ",), ("FROM t",)]
            return [], [(1, 2, "ETL", "2026-10-11", 100.0, 1000, 5.0, 10, 100, 1e9, "SELECT a")]

    td = TD()
    rows = dbql.top_queries(td, DBQLConfig(), dbql.DBQLFilter("2026-10-11 00:00:00", "2026-10-12 00:00:00",
                                                              user="ETL", order="impact"))
    assert rows[0]["UserName"] == "ETL" and rows[0]["cpu_skew"] == 5.0 and rows[0]["pji"] == 100.0
    assert "MaxAMPCPUTime * l.NumOfActiveAMPs DESC" in td.calls[0][0] and td.calls[0][1][-1] == "ETL"
    assert dbql.full_sql(td, DBQLConfig(), 2, 1) == "SELECT a FROM t"
    with pytest.raises(ValueError):
        dbql.top_queries_sql(DBQLConfig(log_table="x; DROP"), dbql.DBQLFilter("a", "b"))


# ------------------------------------------------------------------ skills ↔ code consistency
def test_every_finding_maps_to_existing_tuning_points_and_skill():
    tps = SKILLS.tuning_points()
    assert len(SKILLS) >= 19 and len(tps) >= 70
    for code, (_, sev, tp_ids, grade) in FINDING_INFO.items():
        assert sev in ("critical", "warning", "info") and grade in "ABC"
        for tp in tp_ids:
            assert tp in tps, f"{code} -> {tp} missing in skills/"
        assert any(code in s.findings for s in SKILLS.skills.values()), f"no skill lists {code}"
    for tp in tps.values():
        assert tp.title and tp.skill


def test_parse_rewrite_reply_variants():
    r = parse_rewrite_reply("<analysis>x</analysis><rewrite>\n```sql\nSELECT 1;\n```\n</rewrite><changes>- a</changes>")
    assert r["rewrite"] == "SELECT 1" and r["changes"] == "- a"
    assert parse_rewrite_reply("<rewrite>NONE</rewrite>")["rewrite"] == "NONE"
    assert parse_rewrite_reply("설명\n```sql\nSELECT 2\n```")["rewrite"] == "SELECT 2"
    assert parse_rewrite_reply("<rewrite>\nSELECT 3\n<changes>- b")["rewrite"] == "SELECT 3"


# ------------------------------------------------------------------ the loop
def test_loop_reaches_goal_and_skips_invalid_attempts():
    td = FakeTD()
    s, model = session([reply(GOOD_SQL + " LIMIT 10"), reply(GOOD_SQL.replace("AS amt", "AS total")),
                        reply(GOOD_SQL), "## 요약\n완료"], td=td)
    dbq = {"AMPCPUTime": 5000, "TotalIOCount": 100000, "MaxAMPCPUTime": 60, "NumOfActiveAMPs": 200, "MaxAMPIO": 600}
    r = s.run(ORIG, Goal(time_reduction_pct=50), dbql=dbq)
    assert [a.status for a in r.attempts] == ["invalid", "invalid", "accepted"]
    assert "LIMIT" in r.attempts[0].reason and "출력 컬럼" in r.attempts[1].reason
    assert r.goal_met and r.stop_reason == "목표 달성" and r.best_attempt == 3 and r.best.sql == GOOD_SQL
    assert {"CPU_SKEW", "TABLE_SKEW", "PRODUCT_JOIN"} <= {f.code for f in r.original.findings}
    assert r.report_md.startswith("## 요약")
    prompt = model.calls[0][1]["content"]
    assert "TP-PART-02" in prompt and "SALES_DB.SALES_TXN (TABLE)" in prompt and "병목 단계 5" in prompt
    assert "이전 시도 결과" in model.calls[2][1]["content"] and "LIMIT" in model.calls[2][1]["content"]
    evidence = model.calls[-1][1]["content"]
    assert "채택된 SQL (시도 3)" in evidence and "COLLECT STATISTICS COLUMN (CUST_ID) ON SALES_DB.CUSTOMER" in evidence


def test_dropped_condition_is_rejected_and_fed_back():
    dropped = ORIG.replace("AND t.CUST_ID = c.CUST_ID", "AND (t.CUST_ID = c.CUST_ID OR t.PROD_ID = c.CUST_ID)")
    s, model = session([reply(GOOD_SQL), reply(GOOD_SQL.replace("GROUP BY 1", "GROUP BY 1 HAVING SUM(t.SALE_AMT) > 0")),
                        "report"], td=FakeTD(), patience=2)
    r = s.run(dropped, Goal())
    assert [a.status for a in r.attempts] == ["rejected", "rejected"]
    assert "결과 보존 의심" in r.attempts[0].reason and "PROD_ID" in r.attempts[0].reason
    assert r.best_attempt is None and not r.goal_met and "연속 개선 없음" in r.stop_reason
    assert "PROD_ID" in model.calls[1][1]["content"]


def test_result_check_rejects_different_results_and_confirms_same():
    s, _ = session([reply(GOOD_SQL), "report"], td=FakeTD(allow_execute=True, diff=(3, 0)), max_iterations=1)
    r = s.run(ORIG, Goal(), result_check=True)
    assert r.attempts[0].status == "rejected" and r.attempts[0].result_check == "differs"
    assert "원본에만 3행" in r.attempts[0].reason
    s, _ = session([reply(GOOD_SQL), "report"], td=FakeTD(allow_execute=True, diff=(0, 0)))
    r = s.run(ORIG, Goal(), result_check=True)
    assert r.attempts[0].status == "accepted" and "결과 동일" in r.attempts[0].reason


def test_explain_failure_and_none_reply():
    s, _ = session([reply(GOOD_SQL.replace("SALES_DB.CUSTOMER", "SALES_DB.BROKEN")), "<rewrite>NONE</rewrite>",
                    "report"], td=FakeTD())
    r = s.run(ORIG, Goal())
    assert r.attempts[0].status == "invalid" and "3807" in r.attempts[0].reason
    assert r.attempts[1].status == "none" and "더 이상" in r.stop_reason


def test_offline_mode_gives_one_unverified_suggestion():
    s, model = session([reply(GOOD_SQL), "report"], td=None)
    r = s.run(ORIG, Goal(), explain_text=BAD)
    assert not r.verified and len(r.attempts) == 1 and r.attempts[0].status == "unverified"
    assert r.original.has_plan and "PRODUCT_JOIN" in {f.code for f in r.original.findings}
    assert not r.goal_met and "검증할 수 없어" in r.stop_reason


def test_analyze_only_and_report_fallback():
    s, model = session([RuntimeError("LLM down")], td=FakeTD())
    r = s.run(ORIG, Goal(), analyze_only=True)
    assert r.attempts == [] and r.stop_reason == "진단만 수행"
    assert "## 요약" in r.report_md and "LLM 오류" in r.report_md and "COLLECT STATISTICS" in r.report_md


def test_already_meeting_goal_skips_loop():
    s, model = session(["report"], td=FakeTD())
    r = s.run(GOOD_SQL, Goal())
    assert r.attempts == [] and r.stop_reason == "원본이 이미 목표를 충족" and r.goal_met
