from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tdagent.agent import build_agent  # noqa: E402
from tdagent.config import Config  # noqa: E402
from tdagent.llm import LLMReply  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "tdagent" / "webapp.py")
BAD = (ROOT / "tests/fixtures/explain_bad.txt").read_text()
GOOD = (ROOT / "tests/fixtures/explain_good.txt").read_text()

ORIG = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t, SALES_DB.CUSTOMER c
WHERE CAST(t.SALE_DT AS CHAR(7)) = '2024-01' AND t.CUST_ID = c.CUST_ID
GROUP BY 1"""
GOOD_SQL = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID
WHERE t.SALE_DT >= DATE '2024-01-01' AND t.SALE_DT < DATE '2024-02-01'
GROUP BY 1"""


class Model:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools):
        return LLMReply(content=self.replies.pop(0))


class FakeTD:
    connected = True

    def __init__(self):
        self.cfg = type("C", (), {"allow_execute": False, "max_rows": 50})()

    def explain(self, sql):
        return BAD if "CAST(" in sql else GOOD

    def table_skew(self, name):
        return 1.0

    def describe(self, name):
        return None

    def close(self):
        pass

    def query(self, sql, params):
        if "SqlTextInfo FROM" in sql:
            return ["SqlTextInfo"], [(ORIG[:40],), (ORIG[40:],)]
        return [], [(111, 222, "ETL_USER", "2026-10-11 02:00:00", 5000.0, 100000, 60.0, 600, 200, 1e10,
                     ORIG[:100])]


def start(replies, td=None):
    cfg = Config(base_dir=ROOT)
    agent = build_agent(cfg, model=Model(replies))
    agent.toolbox.td = td
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["cfg"] = cfg
    at.session_state["agent"] = agent
    at.run()
    assert not at.exception
    return at


def button(at, label):
    return next(b for b in at.button if b.label == label)


def reply(sql):
    return f"<analysis>TP-PART-02</analysis>\n<rewrite>\n{sql}\n</rewrite>\n<changes>- [TP-PART-02] 범위 조건</changes>"


def test_tuning_is_default_and_reaches_goal():
    at = start([reply(GOOD_SQL), "## 요약\n목표 달성 리포트"], td=FakeTD())
    assert at.session_state["mode"] == "🔧 튜닝"
    at.text_area(key="tune_sql").set_value(ORIG).run()
    button(at, "🚀 목표 달성까지 튜닝").click().run()
    assert not at.exception
    r = at.session_state["tune_report"]
    assert r.goal_met and r.best_attempt == 1
    assert any("목표 달성" in s.value for s in at.success)
    assert any(c.value == GOOD_SQL and c.language == "sql" for c in at.code)
    assert any("COLLECT STATISTICS COLUMN (CUST_ID) ON SALES_DB.CUSTOMER;" in c.value for c in at.code)
    assert any(m.label == "Product join" and m.value == "0" for m in at.metric)
    assert any("TP-JOIN-01" in e.label for e in at.expander)  # tuning-point knowledge attached to findings
    assert any("목표 달성 리포트" in m.value for m in at.markdown)


def test_dbql_search_and_load():
    at = start([], td=FakeTD())
    at.radio(key="tune_source").set_value("DBQL 에서 찾기").run()
    button(at, "🔎 고부하 쿼리 조회").click().run()
    assert not at.exception
    rows = at.session_state["dbql_rows"]
    assert rows[0]["QueryID"] == 111 and rows[0]["cpu_skew"] == 2.4
    button(at, "불러오기").click().run()
    assert at.session_state["tune_sql"] == ORIG
    assert at.session_state["tune_dbql"]["AMPCPUTime"] == 5000.0
    assert any("DBQL 지표 사용" in c.label for c in at.checkbox)


def test_offline_explain_paste_diagnosis():
    at = start(["## 요약\n진단"], td=None)
    at.radio(key="tune_source").set_value("EXPLAIN 붙여넣기 (DB 미연결)").run()
    at.text_area(key="tune_sql").set_value(ORIG).run()
    at.text_area(key="tune_explain").set_value(BAD).run()
    button(at, "🔍 진단만").click().run()
    assert not at.exception
    r = at.session_state["tune_report"]
    assert r.stop_reason == "진단만 수행" and r.original.has_plan
    assert {"PRODUCT_JOIN", "DUPLICATE_LARGE", "STATS_RECOMMENDED"} <= {f.code for f in r.original.findings}
    assert any("검증하지 못했습니다" in i.value for i in at.info)


def test_buttons_disabled_without_sql_and_knowledge_page():
    at = start([])
    assert button(at, "🚀 목표 달성까지 튜닝").disabled
    at.radio(key="mode").set_value("📚 튜닝 지식").run()
    assert not at.exception
    skills = [e for e in at.main.expander]
    assert len(skills) >= 19
    at.text_input(key="kb_search").set_value("PRPD").run()
    labels = [e.label for e in at.main.expander]
    assert labels and all("skew" in label for label in labels)
