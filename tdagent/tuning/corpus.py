"""Check the EXPLAIN parser against a batch of real plans.

Input is the extract format produced from DBQL (one block per query):

    ====...
    [3/46] QueryID=... ProcID=... User=... NumSteps=42 AMPCPUTime=0.336 Start=...
    ----...
    -- SQL
    SELECT ...
    ----...
    -- NOTE ...            (optional)
    -- EXPLAIN
      1) First, we lock ...         or   !! EXPLAIN FAILED: [Error 3523] ...

A file with a bare EXPLAIN (no headers) is treated as one plan.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .explain import ParsedExplain, parse_explain

_QUOTED = re.compile(r'"[^"]*"')


@dataclass
class Plan:
    n: int
    meta: dict
    sql: str
    explain: str
    error: str = ""
    parsed: ParsedExplain | None = None


@dataclass
class Coverage:
    plans: int = 0
    failed: int = 0
    counts: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def load_extract(path: str | Path) -> list[Plan]:
    text = Path(path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n=+\n(?=\[\d+/\d+\])", "\n" + text)
    if len(blocks) == 1:
        return [Plan(n=1, meta={}, sql="", explain=text.strip())]
    plans = []
    for b in blocks[1:]:
        head = b.splitlines()[0]
        m = re.match(r"\[(\d+)/\d+\]\s*(.*)", head)
        meta = dict(kv.split("=", 1) for kv in re.findall(r"(\w+=\S+)", m.group(2))) if m else {}
        sql = re.search(r"-- SQL\n(.*?)\n-{10,}\n", b, re.S)
        ex = re.search(r"-- EXPLAIN\n(.*)", b, re.S)
        explain = re.sub(r"(?m)^#.*$", "", ex.group(1)).strip("\n") if ex else ""
        error = ""
        if explain.lstrip().startswith("!!"):
            error, explain = explain.strip().lstrip("!").strip(), ""
        plans.append(Plan(n=int(m.group(1)) if m else len(plans) + 1, meta=meta,
                          sql=sql.group(1).strip() if sql else "", explain=explain, error=error))
    return plans


def check(plans: list[Plan]) -> Coverage:
    """Parse every plan and look for phrases the parser did not turn into structure."""
    cov = Coverage()
    for p in plans:
        if p.error or not p.explain:
            cov.failed += 1
            continue
        cov.plans += 1
        p.parsed = parse_explain(p.explain)
        cov.counts["total_time_" + (p.parsed.total_time_source or "missing")] += 1
        for s in p.parsed.steps:
            masked = _QUOTED.sub('""', s.text)
            cov.counts["steps"] += 1
            where = f"[{p.n}] 단계 {s.label}"
            if re.search(r"\bwe do an? ", masked, re.I):
                cov.counts["operation_steps"] += 1
                if not s.op:
                    cov.problems.append(f"{where}: 연산 종류를 못 읽음")
            if re.search(r"joined using", masked, re.I):
                cov.counts["joins"] += 1
                cov.counts[f"join:{s.join_type} {s.join_method}".strip()] += 1
                if not s.join_method:
                    cov.problems.append(f"{where}: 조인 방식을 못 읽음")
            if re.search(r"redistributed by", masked, re.I):
                cov.counts["redistributions"] += 1
                if s.geography != "redistributed":
                    cov.problems.append(f"{where}: 재분배를 못 읽음")
            if re.search(r"duplicated on all AMPs", masked, re.I):
                cov.counts["duplications"] += 1
            if re.search(r"estimated with \w+(?: join)? confidence", masked, re.I) and s.est_rows is None:
                cov.problems.append(f"{where}: 예상 행 수를 못 읽음")
            if re.search(r"estimated time for this step", masked, re.I) and s.est_time is None:
                cov.problems.append(f"{where}: 단계 예상 시간을 못 읽음")
            for sc in s.scans:
                cov.counts["table_accesses"] += 1
                cov.counts[f"access:{sc.access or '?'}"] += 1
                if not sc.access:
                    cov.problems.append(f"{where}: {sc.table} 접근 경로를 못 읽음")
    return cov
