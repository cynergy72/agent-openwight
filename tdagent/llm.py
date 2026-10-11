"""OpenAI-compatible chat client (Ollama, vLLM, llama.cpp server, LM Studio...).

Normalizes responses into `LLMReply` and recovers tool calls that some local
models emit as plain text (e.g. `<tool_call>{...}</tool_call>`) when the
server's chat template does not parse them.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .config import LLMConfig


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, parsed by the agent so errors can be fed back


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


_TEXT_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_text_tool_calls(content: str) -> tuple[str, list[ToolCall]]:
    calls = []
    for raw in _TEXT_TOOL_CALL.findall(content):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", {}))
        if name:
            calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name,
                                  arguments=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)))
    if calls:
        content = _TEXT_TOOL_CALL.sub("", content)
    return content.strip(), calls


class OpenAICompatModel:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout)

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            tools=tools or None,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            extra_body=self.cfg.extra_body or None,
        )
        msg = resp.choices[0].message
        content = _THINK.sub("", msg.content or "").strip()
        calls = [ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:8]}", name=tc.function.name,
                          arguments=tc.function.arguments or "{}")
                 for tc in (msg.tool_calls or [])]
        if not calls:
            content, calls = parse_text_tool_calls(content)
        return LLMReply(content=content, tool_calls=calls)
