import json
from pathlib import Path

from tdagent.agent import Agent, build_agent
from tdagent.config import AgentConfig, Config
from tdagent.llm import LLMReply, ToolCall, parse_text_tool_calls

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    """Plays back canned replies and records what the agent sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self.replies.pop(0)


def call(name, **args):
    return LLMReply(tool_calls=[ToolCall(id=f"c{name}", name=name, arguments=json.dumps(args))])


def answer(sql):
    return LLMReply(content=f"## 결과\n```sql\n{sql}\n```\n설명: 테스트")


def make_agent(replies, **agent_cfg):
    cfg = Config(base_dir=ROOT)
    for k, v in agent_cfg.items():
        setattr(cfg.agent, k, v)
    model = ScriptedModel(replies)
    events = []
    agent = build_agent(cfg, model=model, on_event=lambda k, d: events.append(k))
    return agent, model, events


GOOD = ("SELECT c.CUST_NM, SUM(t.SALE_AMT) AS amt FROM SALES_DB.SALES_TXN t "
        "JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID "
        "WHERE t.SALE_DT >= ADD_MONTHS(CURRENT_DATE, -1) GROUP BY 1")


def test_tool_loop_then_answer():
    agent, model, events = make_agent([
        call("search_schema", query="고객 매출"),
        call("describe_table", table="SALES_DB.SALES_TXN"),
        answer(GOOD),
    ])
    result = agent.run("지난달 고객별 매출")
    assert result.sql == GOOD and result.validation.ok
    tool_msgs = [m for m in model.calls[-1]["messages"] if m["role"] == "tool"]
    assert "SALES_TXN" in tool_msgs[0]["content"] and "PRIMARY INDEX (CUST_ID)" in tool_msgs[1]["content"]
    assert events.count("tool_call") == 2


def test_invalid_sql_triggers_fix_round():
    agent, model, events = make_agent([
        answer("SELECT CUST_NM FROM SALES_DB.CUSTOMER LIMIT 10"),
        answer("SELECT TOP 10 CUST_NM FROM SALES_DB.CUSTOMER"),
    ])
    result = agent.run("고객 10명")
    assert "TOP 10" in result.sql and result.validation.ok
    assert "fix_round" in events
    feedback = model.calls[1]["messages"][-1]["content"]
    assert "[자동 검증]" in feedback and "LIMIT" in feedback


def test_fix_rounds_are_bounded():
    bad = answer("SELECT CUST_NAME FROM SALES_DB.CUSTOMER")
    agent, model, _ = make_agent([bad, bad, bad, bad], max_fix_rounds=2)
    result = agent.run("고객 이름")
    assert not result.validation.ok and len(model.calls) == 3


def test_users_own_sql_is_not_looped():
    user_sql = "SELECT CUST_NM FROM SALES_DB.CUSTOMER LIMIT 10"
    agent, model, _ = make_agent([answer(user_sql)])
    result = agent.run(f"이 쿼리 분석해줘\n```sql\n{user_sql}\n```")
    assert len(model.calls) == 1 and not result.validation.ok


def test_repeated_calls_are_cut_and_budget_forces_answer():
    same = call("search_schema", query="x")
    agent, model, _ = make_agent([same, same, same, answer("SELECT 1")], max_steps=3)
    result = agent.run("아무거나")
    assert "반복" in model.calls[-1]["messages"][-2]["content"]  # third identical call refused
    assert model.calls[-1]["tools"] == []  # forced final answer without tools
    assert result.sql == "SELECT 1"


def test_bad_tool_arguments_are_reported_back():
    agent, model, _ = make_agent([
        LLMReply(tool_calls=[ToolCall(id="c1", name="describe_table", arguments="{broken")]),
        LLMReply(tool_calls=[ToolCall(id="c2", name="drop_table", arguments="{}")]),
        LLMReply(content="질문을 좀 더 구체적으로 알려주세요."),
    ])
    result = agent.run("?")
    tool_outputs = [m["content"] for m in model.calls[-1]["messages"] if m["role"] == "tool"]
    assert tool_outputs[0].startswith("ERROR: 인자 JSON") and "알 수 없는 도구" in tool_outputs[1]
    assert result.sql == "" and result.validation is None


def test_history_keeps_only_final_answers():
    agent, model, _ = make_agent([call("search_schema", query="고객"), answer(GOOD), answer(GOOD)])
    agent.run("첫 요청")
    agent.run("할인금액도 추가해줘")
    sent = model.calls[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]


def test_text_tool_call_fallback():
    content, calls = parse_text_tool_calls(
        'ok <tool_call>{"name": "search_schema", "arguments": {"query": "매출"}}</tool_call>')
    assert content == "ok" and calls[0].name == "search_schema"
    assert json.loads(calls[0].arguments) == {"query": "매출"}


def test_system_prompt_contains_schema_and_knowledge():
    agent, _, _ = make_agent([])
    assert "SALES_DB.SALES_TXN" in agent.system_prompt
    assert "순매출" in agent.system_prompt
    assert "run_query" not in agent.system_prompt
