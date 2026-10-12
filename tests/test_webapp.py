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


def start(replies, source=None):
    model = ScriptedModel(replies)
    cfg = Config(base_dir=ROOT)
    if source is not None:
        cfg.source = source
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["cfg"] = cfg
    at.session_state["agent"] = build_agent(cfg, model=model)
    at.session_state["mode"] = "✍️ SQL 작성"
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
    at.session_state["mode"] = "✍️ SQL 작성"
    at.run()
    at.chat_input[0].set_value("아무거나").run()
    assert not at.exception
    assert any("connection refused" in e.value for e in at.error)


# --------------------------------------------------------------------------- #
# Connection settings
# --------------------------------------------------------------------------- #

from tdagent import credentials  # noqa: E402
from tdagent.config import load_config  # noqa: E402
from tdagent.teradata import TeradataClient  # noqa: E402


def fill(at, **values):
    for key, value in values.items():
        if key == "conn_enabled":
            at.toggle(key=key).set_value(value)
        else:
            at.text_input(key=key).set_value(value)
    at.run()


def password_input(at):
    return at.text_input(key="conn_password")


def test_save_connection_stores_password_in_keyring(tmp_path, memory_keyring):
    path = tmp_path / "config.toml"
    at, model = start([], source=path)
    fill(at, conn_enabled=True, conn_host="td.example.com", conn_user="me",
         conn_password="s3cret", conn_databases="SALES_DB, CUST_DB")
    button(at, "저장").click().run()
    assert not at.exception

    assert credentials.get_password("td.example.com", "me") == "s3cret"
    text = path.read_text(encoding="utf-8")
    assert "td.example.com" in text and "s3cret" not in text
    assert load_config(path).teradata.password == "s3cret"  # next start: from keyring
    agent = at.session_state["agent"]
    assert agent.toolbox.td is not None and agent.model is model
    assert password_input(at).value == ""  # field cleared
    assert "저장됨" in password_input(at).placeholder
    assert "real Teradata EXPLAIN" in agent.system_prompt


def test_empty_password_keeps_saved_one_and_new_one_replaces(tmp_path, memory_keyring):
    credentials.set_password("td.example.com", "me", "old")
    path = tmp_path / "config.toml"
    at, _ = start([], source=path)
    fill(at, conn_enabled=True, conn_host="td.example.com", conn_user="me")
    assert "저장됨" in password_input(at).placeholder

    at.number_input(key="conn_max_rows").set_value(20).run()
    button(at, "저장").click().run()
    assert credentials.get_password("td.example.com", "me") == "old"
    assert at.session_state["cfg"].teradata.password == "old"
    assert at.session_state["cfg"].teradata.max_rows == 20

    fill(at, conn_password="new")
    button(at, "저장").click().run()
    assert credentials.get_password("td.example.com", "me") == "new"
    assert at.session_state["cfg"].teradata.password == "new"


def test_unchecking_save_deletes_stored_password(tmp_path, memory_keyring):
    credentials.set_password("td.example.com", "me", "old")
    at, _ = start([], source=tmp_path / "config.toml")
    fill(at, conn_enabled=True, conn_host="td.example.com", conn_user="me")
    at.checkbox(key="conn_save_pw").uncheck().run()
    button(at, "저장").click().run()
    assert credentials.get_password("td.example.com", "me") is None
    assert at.session_state["cfg"].teradata.password == "old"  # still usable this session
    assert "이번 실행만" in password_input(at).placeholder


def test_no_keyring_backend_keeps_password_for_session(tmp_path, no_keyring):
    at, _ = start([], source=tmp_path / "config.toml")
    fill(at, conn_enabled=True, conn_host="h", conn_user="u", conn_password="pw")
    button(at, "저장").click().run()
    assert not at.exception
    assert any("이번 실행 동안만" in w.value for w in at.warning)
    assert at.session_state["cfg"].teradata.password == "pw"


def test_connection_test_uses_form_values(tmp_path, memory_keyring, monkeypatch):
    seen = {}

    def fake_ping(self):
        seen.update(host=self.cfg.host, password=self.cfg.password)
        return "17.20.03.01"

    monkeypatch.setattr(TeradataClient, "ping", fake_ping)
    at, _ = start([], source=tmp_path / "config.toml")
    button(at, "연결 테스트").click().run()
    assert any("필요합니다" in e.value for e in at.error)

    fill(at, conn_host="td.example.com", conn_user="me", conn_password="pw")
    button(at, "연결 테스트").click().run()
    assert seen == {"host": "td.example.com", "password": "pw"}
    assert any("17.20.03.01" in m.value for m in at.success)
    assert not (tmp_path / "config.toml").exists()  # testing does not save
