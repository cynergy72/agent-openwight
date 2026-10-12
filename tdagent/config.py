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
class TuningConfig:
    skills_dir: str = "skills"
    max_iterations: int = 5       # rewrite attempts per tuning run
    patience: int = 2             # stop after this many attempts without improvement
    skill_chars: int = 7000       # tuning-point knowledge budget per prompt
    helpstats: bool = True        # DIAGNOSTIC HELPSTATS ON FOR SESSION before EXPLAIN
    # thresholds for EXPLAIN findings (estimated rows)
    dup_rows: int = 1_000_000
    redistribute_rows: int = 10_000_000
    scan_rows: int = 1_000_000
    spool_rows: int = 100_000_000
    spool_bytes: int = 1_000_000_000  # a single spool this large (wide rows) is worth trimming
    stats_min_rows: int = 10_000      # no-confidence estimates below this are not worth reporting
    # thresholds for DBQL / table findings
    cpu_skew_warn: float = 2.0
    cpu_skew_crit: float = 5.0
    pji_warn: float = 3.0
    uii_warn: float = 3.0
    table_skew_warn: float = 20.0
    table_skew_crit: float = 50.0


@dataclass
class DBQLConfig:
    log_table: str = "DBC.DBQLogTbl"   # PDCR sites: e.g. PDCRINFO.DBQLogTbl_Hst
    sql_table: str = "DBC.DBQLSqlTbl"  # e.g. PDCRINFO.DBQLSqlTbl_Hst


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    teradata: TeradataConfig = field(default_factory=TeradataConfig)
    tuning: TuningConfig = field(default_factory=TuningConfig)
    dbql: DBQLConfig = field(default_factory=DBQLConfig)
    base_dir: Path = field(default_factory=Path.cwd)
    source: Path = field(default_factory=lambda: Path("config.toml"))  # file to save settings to

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
        tuning=_fill(TuningConfig, data.get("tuning", {})),
        dbql=_fill(DBQLConfig, data.get("dbql", {})),
        base_dir=path.resolve().parent if path.exists() else Path.cwd(),
        source=path.resolve(),
    )
    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            setattr(getattr(cfg, section), key, os.environ[env])
    td = cfg.teradata
    if not td.password:  # TD_PASSWORD / config file win over the OS credential store
        from .credentials import get_password

        td.password = get_password(td.host, td.user) or ""
    return cfg


SAVED_TERADATA_KEYS = ("enabled", "host", "user", "logmech", "databases", "allow_execute", "max_rows")


def save_teradata_settings(path: Path, td: TeradataConfig) -> None:
    """Write the [teradata] section, keeping comments and other sections.
    The password is never written (it lives in the OS credential store);
    a password already in the file is removed so it can't shadow the new one."""
    import tomlkit

    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    if "teradata" not in doc:
        doc.add("teradata", tomlkit.table())
    section = doc["teradata"]
    for key in SAVED_TERADATA_KEYS:
        section[key] = getattr(td, key)
    if "password" in section:
        del section["password"]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
