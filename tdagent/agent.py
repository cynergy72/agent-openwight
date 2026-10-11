"""The agent loop.

Design choices for small (~30B) local models:
  * Few, simple tools; schema is searched, never dumped.
  * Every final SQL is validated in code; errors are fed back automatically
    (bounded by `max_fix_rounds`) instead of trusting the model.
  * Repeated identical tool calls are cut off; a step budget forces an answer.
  * Only the user's request and the final answer of past turns are kept as
    history, so the context stays short across a long chat session.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import AgentConfig, Config
from .llm import ChatModel, LLMReply, OpenAICompatModel
from .prompts import build_system_prompt
from .schema import SchemaStore
from .sql_lint import extract_sql_blocks, normalize_sql
from .teradata import TeradataClient
from .tools import Toolbox, Validation

EventHandler = Callable[[str, dict], None]


@dataclass
class AgentResult:
    answer: str
    sql: str = ""
    validation: Validation | None = None
    steps: int = 0


class Agent:
    def __init__(self, model: ChatModel, toolbox: Toolbox, cfg: AgentConfig, system_prompt: str,
                 on_event: EventHandler | None = None):
        self.model = model
        self.toolbox = toolbox
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.on_event = on_event or (lambda kind, data: None)
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history.clear()

    def run(self, user_input: str) -> AgentResult:
        messages = [{"role": "system", "content": self.system_prompt},
                    *self.history[-2 * self.cfg.history_turns:],
                    {"role": "user", "content": user_input}]
        tools = self.toolbox.specs()
        seen: Counter = Counter()
        fix_rounds = 0

        for step in range(1, self.cfg.max_steps + 1):
            reply = self.model.chat(messages, tools)

            if reply.tool_calls:
                self._run_tools(reply, messages, seen)
                continue

            if not reply.content.strip():
                messages.append({"role": "user", "content": "최종 답을 작성하세요."})
                continue

            result = self._finish(user_input, reply.content, step)
            v = result.validation
            if v is not None and not v.ok and fix_rounds < self.cfg.max_fix_rounds \
                    and not self._is_users_own_sql(user_input, result.sql):
                fix_rounds += 1
                self.on_event("fix_round", {"round": fix_rounds, "validation": v.render()})
                messages.append({"role": "assistant", "content": reply.content})
                messages.append({"role": "user", "content":
                                 "[자동 검증] 최종 쿼리에 오류가 있습니다:\n" + v.render()
                                 + "\n\n오류를 모두 고친 뒤 같은 형식으로 전체 답을 다시 작성하세요."})
                continue
            return self._remember(user_input, result)

        # step budget exhausted: force an answer without tools
        messages.append({"role": "user", "content":
                         "단계 한도에 도달했습니다. 도구 없이 지금까지의 정보로 최종 답을 작성하세요."})
        reply = self.model.chat(messages, [])
        return self._remember(user_input, self._finish(user_input, reply.content, self.cfg.max_steps + 1))

    # ------------------------------------------------------------ helpers
    def _run_tools(self, reply: LLMReply, messages: list[dict], seen: Counter) -> None:
        messages.append({
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments}}
                           for tc in reply.tool_calls],
        })
        for tc in reply.tool_calls:
            key = (tc.name, tc.arguments.strip())
            seen[key] += 1
            self.on_event("tool_call", {"name": tc.name, "arguments": tc.arguments})
            if seen[key] > 2:
                out = ("ERROR: 같은 호출을 반복하고 있습니다. 이미 받은 결과를 활용해 다음 단계로 "
                       "넘어가거나 최종 답을 작성하세요.")
            else:
                out = self.toolbox.call(tc.name, tc.arguments)
            self.on_event("tool_result", {"name": tc.name, "output": out})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

    def _finish(self, user_input: str, answer: str, steps: int) -> AgentResult:
        blocks = extract_sql_blocks(answer)
        sql = blocks[-1] if blocks else ""
        validation = self.toolbox.validate(sql) if sql else None
        if validation is not None:
            self.on_event("validation", {"sql": sql, "validation": validation.render()})
        return AgentResult(answer=answer.strip(), sql=sql, validation=validation, steps=steps)

    @staticmethod
    def _is_users_own_sql(user_input: str, sql: str) -> bool:
        """An ANALYZE answer may echo the user's query unchanged; don't loop on
        errors that were already in the user's SQL."""
        return bool(sql) and normalize_sql(sql) in normalize_sql(user_input)

    def _remember(self, user_input: str, result: AgentResult) -> AgentResult:
        self.history += [{"role": "user", "content": user_input},
                         {"role": "assistant", "content": result.answer}]
        self.history = self.history[-2 * self.cfg.history_turns:]
        return result


def load_knowledge(directory: Path, max_chars: int = 8000) -> str:
    if not directory.exists():
        return ""
    parts = []
    for p in sorted(directory.glob("*.md")):
        parts.append(f"### {p.stem}\n{p.read_text(encoding='utf-8').strip()}")
    text = "\n\n".join(parts)
    return text[:max_chars]


def build_agent(cfg: Config, model: ChatModel | None = None,
                on_event: EventHandler | None = None) -> Agent:
    store = SchemaStore.load_dir(cfg.path(cfg.agent.schema_dir))
    td = TeradataClient(cfg.teradata) if cfg.teradata.enabled else None
    toolbox = Toolbox(store, td, allow_execute=cfg.teradata.allow_execute,
                      output_chars=cfg.agent.tool_output_chars)
    prompt = build_system_prompt(
        knowledge=load_knowledge(cfg.path(cfg.agent.knowledge_dir)),
        schema_overview=store.overview() if len(store) else "",
        explain_enabled=td is not None,
        execute_enabled=toolbox.allow_execute,
    )
    return Agent(model or OpenAICompatModel(cfg.llm), toolbox, cfg.agent, prompt, on_event)
