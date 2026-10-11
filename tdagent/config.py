"""Configuration loaded from config.toml with environment variable overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3.8:27b"
    api_key: str = "ollama"
    temperature: float = 0.1
    max_tokens: int = 8192
    timeout: float = 300
    # server-specific request fields, e.g. to turn thinking off on vLLM:
    # {"chat_template_kwargs": {"enable_thinking": false}}
    extra_body: dict = field(default_factory=dict)


@dataclass
class AgentConfig:
    max_steps: int = 12
    max_fix_rounds: int = 2
    history_turns: int = 6
    tool_output_chars: int = 6000
    knowledge_dir: str = "knowledge"
    schema_dir: str = "schema"


@dataclass
class TeradataConfig:
    enabled: bool = False
    host: str = ""
    user: str = ""
    password: str = ""
    logmech: str = "TD2"
    databases: list[str] = field(default_factory=list)
    allow_execute: bool = False
    max_rows: int = 50


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    teradata: TeradataConfig = field(default_factory=TeradataConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.base_dir / path


def _fill(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


ENV_OVERRIDES = {
    "TDAGENT_LLM_BASE_URL": ("llm", "base_url"),
    "TDAGENT_LLM_MODEL": ("llm", "model"),
    "TDAGENT_LLM_API_KEY": ("llm", "api_key"),
    "TD_HOST": ("teradata", "host"),
    "TD_USER": ("teradata", "user"),
    "TD_PASSWORD": ("teradata", "password"),
}


def load_config(path: str | Path | None = None) -> Config:
    """Load config.toml (if present) and apply environment overrides."""
    path = Path(path) if path else Path("config.toml")
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)

    cfg = Config(
        llm=_fill(LLMConfig, data.get("llm", {})),
        agent=_fill(AgentConfig, data.get("agent", {})),
        teradata=_fill(TeradataConfig, data.get("teradata", {})),
        base_dir=path.resolve().parent if path.exists() else Path.cwd(),
    )
    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            setattr(getattr(cfg, section), key, os.environ[env])
    return cfg
