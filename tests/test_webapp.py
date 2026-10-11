import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tdagent.agent import build_agent  # noqa: E402
from tdagent.config import Config  # noqa: E402
from tdagent.llm import LLMReply, ToolCall  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "tdagent" / "webapp.py")

SQL1 = ("SELECT c.CUST_NM, SUM(t.SALE_AMT) AS amt FROM SALES_DB.SALES_TXN t "
        "JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID GROUP BY 1")
SQL2 = ("SELECT c.CUST_NM, c.GRADE_CD, SUM(t.SALE_AMT) AS amt FROM SALES_DB.SALES_TXN t "
        "JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID WHERE c.GRADE_CD = 'V' GROUP BY 1, 2")


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


def answer(sql):
    return LLMReply(content=f"```sql\n{sql}\n```\n설명: 테스트")


def start(replies):
    model = ScriptedModel(replies)
    cfg = Config(base_dir=ROOT)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["cfg"] = cfg
    at.session_state["agent"] = build_agent(cfg, model=model)
    at.run()
    assert not at.exception
    return at, model


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_write_then_modify_with_attached_sql():
    at, model = start([
        LLMReply(tool_calls=[ToolCall("c1", "search_schema", json.dumps({"query": "고객 매출"}))]),
        answer(SQL1),
        answer(SQL2),
    ])
    at.chat_input[0].set_value("고객별 매출").run()
    assert not at.exception
    assert at.session_state["current_sql"] == SQL1
    assert at.session_state["validation_ok"] is True
    assert any(c.value == SQL1 and c.language == "sql" for c in at.code)

    at.chat_input[0].set_value("VIP 만, 등급 컬럼 추가").run()
    assert at.session_state["current_sql"] == SQL2
    assert at.session_state["prev_sql"] == SQL1
    sent = model.calls[-1][-1]["content"]
    assert sent.startswith("VIP 만") and "현재 SQL:" in sent and SQL1 in sent
    diff = next(c.value for c in at.code if c.language == "diff")
    assert "+" in diff and "GRADE_CD = 'V'" in diff


def test_manual_edit_validate_and_new_query():
    at, _ = start([])
    at.toggle(key="edit_mode").set_value(True).run()
    at.text_area(key="sql_editor").set_value("SELECT CUST_NM FROM SALES_DB.CUSTOMER LIMIT 5").run()
    assert at.session_state["current_sql"].endswith("LIMIT 5")

    button(at, "✅ 검증").click().run()
    assert at.session_state["validation_ok"] is False
    assert "limit" in at.session_state["validation"]
    assert button(at, "🔍 EXPLAIN").disabled  # no Teradata configured

    button(at, "🆕 새 쿼리 (대화 초기화)").click().run()
    assert at.session_state["current_sql"] == "" and at.session_state["messages"] == []


def test_llm_failure_is_shown_not_crashing():
    class Down:
        def chat(self, messages, tools):
            raise ConnectionError("connection refused")

    cfg = Config(base_dir=ROOT)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["cfg"] = cfg
    at.session_state["agent"] = build_agent(cfg, model=Down())
    at.run()
    at.chat_input[0].set_value("아무거나").run()
    assert not at.exception
    assert any("connection refused" in e.value for e in at.error)
