"""Parse Teradata EXPLAIN text into steps and plan metrics.

Calibrated against real Teradata 20.0 EXPLAIN output. Things real plans do
that the parser has to cope with:
  * conditions and index predicates are quoted - `("... TRIM(BOTH FROM T.c) ...")`
    - so table references are only looked for outside quotes
  * tables reached through views read `DB.Alias in view ViewName`
  * spool sizes come as `The size of Spool 5 is estimated ...` or just
    `The size is estimated ...`; single-row PI steps may have no size at all
  * redistribution: `by the hash code of (k1, f(k2))` (nested parens),
    `by hash code to all AMPs`, `assumed to be redistributed by value`,
    and `... to few AMPs` (everything hashed to a constant -> skew)
  * joins: `left outer joined using a merge join`, `single partition hash join`,
    `dynamic hash join`, `row id join`, `inclusion merge join` ...
  * partitions: `n partitions of`, `a single partition of`, `all partitions of`,
    and huge counts (4294967296) that mean "all of them"
  * plans without `The total estimated time is ...` (e.g. `-> As the result of
    statement 1.`) - the total then falls back to the sum of the step times
  * an IPE preamble ("eligible for incremental planning and execution")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME = r'(?:"[^"]+"|[A-Za-z_$#][\w$#]*)'
_TABLE = rf"{_NAME}\.{_NAME}"

_STEP_START = re.compile(r"^(\s*)(\d+)\)\s+(.*)$")
_AMPS_OP = re.compile(r"we do an? (all-AMPs?|single-AMP|group-AMPs?|two-AMP|few-AMPs?)\s+(.+?)\s+"
                      r"(?:step|into|from)\b", re.I)
_SCAN = re.compile(rf"\b(?P<verb>from|joined to)\s+(?:(?P<parts>\d+|a single|all) partitions? of\s+)?"
                   rf"(?P<table>{_TABLE}|Spool \d+)(?:\s+in view\s+(?P<view>{_NAME}(?:\.{_NAME})?))?", re.I)
_BY_WAY = re.compile(r"by way of (an all-rows scan|the unique primary index|the primary index|a RowHash match scan|"
                     r"the hash value of|a traversal of index # ?\d+|unique index # ?\d+|index # ?\d+|[^,.]+)", re.I)
_JOIN = re.compile(r"\b(?:(left|right|full) outer )?joined using an? ([a-z][a-z -]*?) join\b", re.I)
_JOIN_COND = re.compile(r"(?:join condition of|used for non-matching on \w+ table) \(\s*\"(.*?)\"\s*\)", re.I | re.S)
_COND = re.compile(r"with a (?:residual )?condition of \(\s*\"(.*?)\"\s*\)", re.I | re.S)
_REDIST = re.compile(r"redistributed by\b(?: the)?(?: rowkey-based)?(?: (hash code|value)\b(?: of \()?)?", re.I)
_REDIST_TARGET = re.compile(r"\bto (all|few) AMPs\b", re.I)
_EST = re.compile(r"(?:size of Spool (\d+)|The size) is estimated with (high|low|no|index join) confidence to be "
                  r"([\d,]+) rows?(?: \(\s*([\d,]+) bytes\))?", re.I)
_EST_ANY = re.compile(r"estimated with (high|low|no|index join) confidence to be ([\d,]+) rows?(?: \(\s*([\d,]+) bytes\))?",
                      re.I)
_STEP_TIME = re.compile(r"estimated time for this step is (.+?)(?:\.\s|\.$|$)", re.I)
_TOTAL_TIME = re.compile(r"total estimated time is (.+?)(?:\.\s|\.$|$)", re.I | re.M)
_HELPSTATS = re.compile(r"(COLLECT STATISTICS[^\"\n;]*)", re.I)
_SPOOL_IN = re.compile(r"\b(?:from|joined to)\s+Spool (\d+)", re.I)
_SPOOL_OUT = re.compile(r"\b(?:into|placed in|put into|goes into)\s+Spool (\d+)", re.I)
_TOP = re.compile(r"retrieve the TOP (\d+) rows", re.I)
_QUOTED = re.compile(r'"[^"]*"')

CONFIDENCE_ORDER = {"no": 0, "low": 1, "index join": 2, "high": 3}
ALL_PARTITIONS = 65535        # a "partition count" above this is the whole partition space, not elimination
SMALL_PRODUCT = 1_000_000     # product joins below this many row comparisons are normally harmless


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


def _depths(text: str) -> list[int]:
    """Parenthesis depth at every position (expressions such as TRIM(BOTH FROM t.c) sit at depth > 0)."""
    out, d = [], 0
    for ch in text:
        if ch == "(":
            d += 1
        out.append(d)
        if ch == ")":
            d = max(0, d - 1)
    return out


def _balanced(text: str, start: int) -> str:
    """Contents of the parenthesis group opening at text[start-1] == '('."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start: i - 1].strip()


@dataclass
class Scan:
    table: str            # DB.TBL - for view access the alias inside the view (see `view`)
    access: str = ""      # all-rows | primary index | unique primary index | index | rowhash | hash value
    partitions: str = ""  # "12" | "a single" | "all" | "" (not partitioned / not reported)
    view: str = ""

    @property
    def eliminated(self) -> bool:
        """Partition elimination actually narrowed the scan."""
        if not self.partitions or self.partitions == "all":
            return False
        return self.partitions == "a single" or int(self.partitions) <= ALL_PARTITIONS

    @property
    def full_scan(self) -> bool:
        return self.access == "all-rows" and not self.eliminated

    @property
    def label(self) -> str:
        return f"{self.table} (view {self.view})" if self.view else self.table


@dataclass
class Step:
    label: str
    text: str
    amps: str = ""
    op: str = ""
    scans: list[Scan] = field(default_factory=list)
    join_method: str = ""
    join_type: str = ""             # inner | left outer | right outer | full outer
    join_condition: str = ""
    conditions: list[str] = field(default_factory=list)
    geography: str = ""            # local | redistributed | duplicated
    redistribute_keys: str = ""
    redistribute_target: str = ""  # all | few
    confidence: str = ""           # worst confidence in the step
    est_rows: int | None = None
    est_bytes: int | None = None
    est_time: float | None = None
    top_n: int | None = None
    inputs: list[int] = field(default_factory=list)    # spool numbers read
    outputs: list[int] = field(default_factory=list)   # spool numbers written
    input_rows: list[int | None] = field(default_factory=list)  # filled from upstream steps
    lineage: list[str] = field(default_factory=list)  # base tables feeding this step, through spools

    @property
    def tables(self) -> list[str]:
        return [s.table for s in self.scans]

    @property
    def product_comparisons(self) -> int | None:
        """rows(left) x rows(right) when both inputs are known spools."""
        if "product" not in self.join_method:
            return None
        known = [r for r in self.input_rows if r is not None]
        if len(self.input_rows) >= 2 and len(known) == len(self.input_rows):
            prod = 1
            for r in known:
                prod *= max(r, 1)
            return prod
        return None

    @property
    def small_product_join(self) -> bool:
        c = self.product_comparisons
        if c is not None:
            return c < SMALL_PRODUCT
        known = [r for r in self.input_rows if r is not None]
        # one side is a tiny spool and the result is small: a lookup against a handful of rows
        return bool(known) and min(known) <= 100 and (self.est_rows or 0) < 100_000

    def summary(self) -> str:
        bits = [f"{self.label})"]
        if self.amps or self.op:
            bits.append(f"{self.amps} {self.op}".strip())
        for s in self.scans:
            acc = f"{s.partitions} partitions" if s.partitions else (s.access or "?")
            if s.partitions and s.access and s.access != "partitions":
                acc = f"{s.partitions} partitions, {s.access}"
            bits.append(f"{s.label}[{acc}]")
        if self.join_method:
            jt = f"{self.join_type} " if self.join_type and self.join_type != "inner" else ""
            inputs = ""
            if any(r is not None for r in self.input_rows):
                inputs = " (" + " × ".join("?" if r is None else f"{r:,}" for r in self.input_rows) + ")"
            bits.append(f"{jt}{self.join_method} join{inputs}")
        if self.geography:
            geo = self.geography
            if self.redistribute_keys:
                geo += f"({self.redistribute_keys[:80]})"
            if self.redistribute_target == "few":
                geo += " to few AMPs"
            bits.append(geo)
        if self.est_rows is not None:
            bits.append(f"{self.est_rows:,} rows/{self.confidence or '?'} conf")
        if self.top_n is not None:
            bits.append(f"TOP {self.top_n}")
        if self.est_time is not None:
            bits.append(f"{self.est_time:g}s")
        return " · ".join(bits)


@dataclass
class ParsedExplain:
    steps: list[Step]
    total_time: float | None
    helpstats: list[str]
    raw: str
    total_time_source: str = ""     # explain | steps (sum of step times) | ""
    notes: list[str] = field(default_factory=list)
    spool_rows: dict[int, int] = field(default_factory=dict)

    def step(self, label: str) -> Step | None:
        return next((s for s in self.steps if s.label == label), None)

    @property
    def hotspot(self) -> Step | None:
        timed = [s for s in self.steps if s.est_time is not None]
        return max(timed, key=lambda s: s.est_time) if timed else None

    def summary(self) -> str:
        lines = [s.summary() for s in self.steps if s.op or s.scans or s.join_method]
        if self.total_time is not None:
            src = "" if self.total_time_source == "explain" else " (단계 합계 추정)"
            lines.append(f"총 예상 시간: {self.total_time:g}s{src}")
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
    masked = _QUOTED.sub('""', text)   # conditions/index predicates out of the way
    m = _AMPS_OP.search(masked)
    if m:
        st.amps, st.op = m.group(1), m.group(2).strip().upper()
        st.op = re.sub(r"\s+IN TD_\w+$", "", st.op)
    if not re.search(r"\bwe lock\b", masked, re.I) or st.op:
        depth = _depths(masked)
        for m in _SCAN.finditer(masked):
            table = m.group("table")
            if table.lower().startswith("spool") or depth[m.start()] > 0:
                continue
            after = masked[m.end(): m.end() + 200]
            nxt = re.search(r"\b(from|joined to|which is|into)\b", after, re.I)
            window = after[: nxt.start()] if nxt else after
            way = _BY_WAY.search(window)
            access = ""
            if way:
                w = way.group(1).lower()
                access = ("all-rows" if "all-rows" in w else "unique primary index" if "unique primary" in w
                          else "primary index" if "primary index" in w else "rowhash" if "rowhash" in w
                          else "hash value" if "hash value" in w else "index" if "index" in w else w.strip())
            parts = (m.group("parts") or "").lower()
            if parts and not access:
                access = "all-rows"  # "joined to N partitions of T with a condition ..." scans those partitions
            st.scans.append(Scan(table=table.replace('"', ""), access=access, partitions=parts,
                                 view=(m.group("view") or "").replace('"', "")))
    m = _JOIN.search(masked)
    if m:
        st.join_type = f"{m.group(1).lower()} outer" if m.group(1) else "inner"
        st.join_method = m.group(2).strip().lower()
    m = _JOIN_COND.search(text)
    if m:
        st.join_condition = m.group(1)
    st.conditions = _COND.findall(text)
    if re.search(r"duplicated on all AMPs", masked, re.I):
        st.geography = "duplicated"
    m = _REDIST.search(text)
    if m:
        st.geography = "redistributed"
        if m.group(0).rstrip().endswith("("):
            st.redistribute_keys = _balanced(text, m.end())
        tail = text[m.end() + len(st.redistribute_keys):][:80]
        t = _REDIST_TARGET.search(tail)
        st.redistribute_target = t.group(1).lower() if t else ""
    if not st.geography and re.search(r"built locally on (?:the AMPs|that AMP)", masked, re.I):
        st.geography = "local"
    ests = _EST_ANY.findall(masked)
    if ests:
        st.confidence = min((c.lower() for c, _, _ in ests), key=lambda c: CONFIDENCE_ORDER.get(c, 3))
        st.est_rows = max(_int(r) or 0 for _, r, _ in ests)
        sizes = [_int(b) for _, _, b in ests if b]
        st.est_bytes = max(sizes) if sizes else None
    m = _STEP_TIME.search(masked)
    if m:
        st.est_time = parse_time(m.group(1))
    m = _TOP.search(masked)
    if m:
        st.top_n = int(m.group(1))
    st.inputs = [int(n) for n in _SPOOL_IN.findall(masked)]
    st.outputs = [int(n) for n in _SPOOL_OUT.findall(masked)]
    return st


def _spool_sizes(steps: list[Step]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for st in steps:
        masked = _QUOTED.sub('""', st.text)
        for spool, _, rows, _ in _EST.findall(masked):
            if spool:
                sizes[int(spool)] = _int(rows) or 0
            elif st.outputs:  # "The size is estimated ..." refers to the final output of the step
                sizes[st.outputs[-1]] = _int(rows) or 0
    return sizes


def parse_explain(text: str) -> ParsedExplain:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cut = re.search(r"BEGIN RECOMMENDED STATS", text, re.I)
    plan_text = text[: cut.start()] if cut else text
    helpstats = []
    if cut:
        for h in _HELPSTATS.findall(text[cut.start():]):
            stmt = re.sub(r"\s+", " ", h).strip().rstrip(".") + ";"
            if stmt not in helpstats:
                helpstats.append(stmt)
    steps = [_parse_step(label, body) for label, body in _split_steps(plan_text)]
    sizes = _spool_sizes(steps)
    spool_tables: dict[int, list[str]] = {}
    for st in steps:
        lineage = list(dict.fromkeys(st.tables + [t for n in st.inputs for t in spool_tables.get(n, [])]))
        st.lineage = lineage
        for n in st.outputs:
            spool_tables[n] = lineage
        if st.join_method:
            n_base = sum(1 for s in st.scans)
            st.input_rows = [sizes.get(n) for n in st.inputs[:2]] + [None] * min(n_base, max(0, 2 - len(st.inputs)))
    notes = []
    flat = re.sub(r"\s+", " ", plan_text)
    if re.search(r"incremental planning and execution", flat, re.I):
        notes.append("IPE 대상 요청 - " + ("정적 계획" if re.search(r"static plan", flat, re.I) else "동적 계획"))
    m = _TOTAL_TIME.search(flat)
    total, source = (parse_time(m.group(1)), "explain") if m else (None, "")
    if total is None:
        times = [s.est_time for s in steps if s.est_time is not None]
        if times:
            total, source = sum(times), "steps"
    return ParsedExplain(steps=steps, total_time=total, helpstats=helpstats, raw=text,
                         total_time_source=source, notes=notes, spool_rows=sizes)


def plan_metrics(p: ParsedExplain, partitioned: set[str] | None = None) -> dict:
    """Numbers the goal check and comparisons use."""
    partitioned = {t.lower() for t in partitioned or set()}
    product = [s for s in p.steps if "product" in s.join_method]
    full_scans = [(s.label, sc.table) for s in p.steps for sc in s.scans if sc.full_scan]
    part_miss = [(lbl, t) for lbl, t in full_scans if t.lower() in partitioned]
    return {
        "total_time": p.total_time,
        "product_joins": sum(1 for s in product if not s.small_product_join),
        "small_product_joins": sum(1 for s in product if s.small_product_join),
        "full_scans": len(full_scans),
        "partition_misses": len(part_miss),
        "no_confidence_steps": sum(1 for s in p.steps if s.confidence == "no"),
        "low_confidence_steps": sum(1 for s in p.steps if s.confidence == "low"),
        "max_duplicated_rows": max([s.est_rows or 0 for s in p.steps if s.geography == "duplicated"], default=0),
        "max_redistributed_rows": max([s.est_rows or 0 for s in p.steps if s.geography == "redistributed"],
                                      default=0),
        "few_amp_redistributions": sum(1 for s in p.steps if s.redistribute_target == "few"),
        "max_spool_rows": max([s.est_rows or 0 for s in p.steps], default=0),
        "max_spool_bytes": max([s.est_bytes or 0 for s in p.steps], default=0),
        "helpstats": len(p.helpstats),
    }
