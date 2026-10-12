"""Parse Teradata EXPLAIN text into steps and plan metrics.

The parser is deliberately tolerant: EXPLAIN wording differs a little between
releases, so every attribute is extracted independently with its own pattern
and missing pieces simply stay empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TABLE = r'(?:"[^"]+"|[A-Za-z_$#][\w$#]*)\.(?:"[^"]+"|[A-Za-z_$#][\w$#]*)'

_STEP_START = re.compile(r"^(\s*)(\d+)\)\s+(.*)$")
_AMPS_OP = re.compile(r"we do an? (all-AMPs?|single-AMP|group-AMPs?|two-AMP)\s+(.+?)\s+(?:step|into|from)\b", re.I)
_SCAN = re.compile(
    rf"(?P<verb>from|joined to)\s+(?:(?P<parts>\d+|a single) partitions? of\s+)?(?P<table>{_TABLE}|Spool \d+)", re.I)
_BY_WAY = re.compile(r"by way of (an all-rows scan|the unique primary index|the primary index|a RowHash match scan|"
                     r"a traversal of index # ?\d+|unique index # ?\d+|index # ?\d+|[^,.]+)", re.I)
_JOIN = re.compile(r"joined using an? ([a-z][a-z -]*?) join\b", re.I)
_JOIN_COND = re.compile(r"join condition of \(\s*\"(.*?)\"\s*\)", re.I | re.S)
_COND = re.compile(r"with a (?:residual )?condition of \(\s*\"(.*?)\"\s*\)", re.I | re.S)
_REDIST = re.compile(r"redistributed by (?:the )?(?:rowkey-based )?hash code of \((.*?)\)", re.I)
_EST = re.compile(r"estimated with (high|low|no|index join) confidence to be ([\d,]+) rows?(?: \(([\d,]+) bytes\))?",
                  re.I)
_STEP_TIME = re.compile(r"estimated time for this step is (.+?)(?:\.\s|\.$|$)", re.I)
_TOTAL_TIME = re.compile(r"total estimated time is (.+?)(?:\.\s|\.$|$)", re.I | re.M)
_HELPSTATS = re.compile(r"(COLLECT STATISTICS[^\"\n;]*)", re.I)

CONFIDENCE_ORDER = {"no": 0, "low": 1, "index join": 2, "high": 3}


def parse_time(text: str) -> float | None:
    """'0.85 seconds' | '1 hour and 5 minutes' | '00:01:23.50' -> seconds."""
    t = text.strip().lower()
    m = re.search(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d,]*\.?\d+)\s*(hour|minute|second)s?", t):
        found = True
        total += float(num.replace(",", "")) * {"hour": 3600, "minute": 60, "second": 1}[unit]
    return total if found else None


def _int(s: str | None) -> int | None:
    return int(s.replace(",", "")) if s else None


@dataclass
class Scan:
    table: str            # DB.TBL (base table); spools are skipped
    access: str = ""      # all-rows | primary index | unique primary index | index | rowhash | ...
    partitions: str = ""  # "12" | "a single" when partition elimination applied

    @property
    def full_scan(self) -> bool:
        return self.access == "all-rows" and not self.partitions


@dataclass
class Step:
    label: str
    text: str
    amps: str = ""
    op: str = ""
    scans: list[Scan] = field(default_factory=list)
    join_method: str = ""
    join_condition: str = ""
    conditions: list[str] = field(default_factory=list)
    geography: str = ""            # local | redistributed | duplicated
    redistribute_keys: str = ""
    confidence: str = ""           # worst confidence in the step
    est_rows: int | None = None
    est_bytes: int | None = None
    est_time: float | None = None

    @property
    def tables(self) -> list[str]:
        return [s.table for s in self.scans]

    def summary(self) -> str:
        bits = [f"{self.label})"]
        if self.amps or self.op:
            bits.append(f"{self.amps} {self.op}".strip())
        for s in self.scans:
            acc = f"{s.partitions} partitions" if s.partitions else (s.access or "?")
            bits.append(f"{s.table}[{acc}]")
        if self.join_method:
            bits.append(f"{self.join_method} join")
        if self.geography:
            geo = self.geography + (f"({self.redistribute_keys})" if self.redistribute_keys else "")
            bits.append(geo)
        if self.est_rows is not None:
            bits.append(f"{self.est_rows:,} rows/{self.confidence or '?'} conf")
        if self.est_time is not None:
            bits.append(f"{self.est_time:g}s")
        return " · ".join(bits)


@dataclass
class ParsedExplain:
    steps: list[Step]
    total_time: float | None
    helpstats: list[str]
    raw: str

    def step(self, label: str) -> Step | None:
        return next((s for s in self.steps if s.label == label), None)

    @property
    def hotspot(self) -> Step | None:
        timed = [s for s in self.steps if s.est_time is not None]
        return max(timed, key=lambda s: s.est_time) if timed else None

    def summary(self) -> str:
        lines = [s.summary() for s in self.steps if s.op or s.scans or s.join_method]
        if self.total_time is not None:
            lines.append(f"총 예상 시간: {self.total_time:g}s")
        return "\n".join(lines)


def _split_steps(text: str) -> list[tuple[str, str]]:
    steps: list[list] = []      # [label, indent, lines]
    parents: list[tuple[int, str]] = []  # (indent, label) of steps that announce parallel sub-steps
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _STEP_START.match(line)
        if m:
            indent, num, rest = len(m.group(1)), m.group(2), m.group(3)
            while parents and parents[-1][0] >= indent:
                parents.pop()
            label = f"{parents[-1][1]}.{num}" if parents else num
            steps.append([label, indent, [rest.strip()]])
            if re.search(r"following steps in parallel", rest, re.I):
                parents.append((indent, label))
        elif line.strip().startswith("->"):
            steps.append(["->", 0, [line.strip()[2:].strip()]])
        elif steps:
            steps[-1][2].append(line.strip())
        else:
            steps.append(["0", 0, [line.strip()]])
    return [(label, re.sub(r"\s+", " ", " ".join(lines)).strip()) for label, _, lines in steps]


def _parse_step(label: str, text: str) -> Step:
    st = Step(label=label, text=text)
    m = _AMPS_OP.search(text)
    if m:
        st.amps, st.op = m.group(1), m.group(2).strip().upper()
    for m in _SCAN.finditer(text):
        table = m.group("table")
        if table.lower().startswith("spool"):
            continue
        after = text[m.end(): m.end() + 160]
        nxt = re.search(r"\b(from|joined to|which is|into)\b", after, re.I)
        window = after[: nxt.start()] if nxt else after
        way = _BY_WAY.search(window)
        access = ""
        if way:
            w = way.group(1).lower()
            access = ("all-rows" if "all-rows" in w else "unique primary index" if "unique primary" in w
                      else "primary index" if "primary index" in w else "rowhash" if "rowhash" in w
                      else "index" if "index" in w else w.strip())
        parts = m.group("parts") or ""
        if parts and not access:
            access = "partitions"
        st.scans.append(Scan(table=table.replace('"', ""), access=access, partitions=parts))
    m = _JOIN.search(text)
    if m:
        st.join_method = m.group(1).strip().lower()
    m = _JOIN_COND.search(text)
    if m:
        st.join_condition = m.group(1)
    st.conditions = _COND.findall(text)
    if re.search(r"duplicated on all AMPs", text, re.I):
        st.geography = "duplicated"
    m = _REDIST.search(text)
    if m:
        st.geography, st.redistribute_keys = "redistributed", m.group(1).strip()
    if not st.geography and re.search(r"built locally on the AMPs", text, re.I):
        st.geography = "local"
    ests = _EST.findall(text)
    if ests:
        st.confidence = min((c.lower() for c, _, _ in ests), key=lambda c: CONFIDENCE_ORDER.get(c, 3))
        st.est_rows = max(_int(r) or 0 for _, r, _ in ests)
        sizes = [_int(b) for _, _, b in ests if b]
        st.est_bytes = max(sizes) if sizes else None
    m = _STEP_TIME.search(text)
    if m:
        st.est_time = parse_time(m.group(1))
    return st


def parse_explain(text: str) -> ParsedExplain:
    cut = re.search(r"BEGIN RECOMMENDED STATS", text, re.I)
    plan_text = text[: cut.start()] if cut else text
    helpstats = []
    if cut:
        for h in _HELPSTATS.findall(text[cut.start():]):
            stmt = re.sub(r"\s+", " ", h).strip().rstrip(".") + ";"
            if stmt not in helpstats:
                helpstats.append(stmt)
    steps = [_parse_step(label, body) for label, body in _split_steps(plan_text)]
    m = _TOTAL_TIME.search(re.sub(r"\s+", " ", plan_text))
    total = parse_time(m.group(1)) if m else None
    return ParsedExplain(steps=steps, total_time=total, helpstats=helpstats, raw=text)


def plan_metrics(p: ParsedExplain, partitioned: set[str] | None = None) -> dict:
    """Numbers the goal check and comparisons use."""
    partitioned = {t.lower() for t in partitioned or set()}
    product = [s for s in p.steps if "product" in s.join_method]
    full_scans = [(s.label, sc.table) for s in p.steps for sc in s.scans if sc.full_scan]
    part_miss = [(lbl, t) for lbl, t in full_scans if t.lower() in partitioned]
    return {
        "total_time": p.total_time,
        "product_joins": len(product),
        "full_scans": len(full_scans),
        "partition_misses": len(part_miss),
        "no_confidence_steps": sum(1 for s in p.steps if s.confidence == "no"),
        "low_confidence_steps": sum(1 for s in p.steps if s.confidence == "low"),
        "max_duplicated_rows": max([s.est_rows or 0 for s in p.steps if s.geography == "duplicated"], default=0),
        "max_redistributed_rows": max([s.est_rows or 0 for s in p.steps if s.geography == "redistributed"],
                                      default=0),
        "max_spool_rows": max([s.est_rows or 0 for s in p.steps], default=0),
        "helpstats": len(p.helpstats),
    }
