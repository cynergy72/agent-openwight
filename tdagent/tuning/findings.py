"""Tuning findings: what was detected, the evidence, and which tuning points
(TP-xxx ids in skills/*.md) it maps to.

Detection here is deterministic (EXPLAIN text, DBQL numbers, table sizes);
SQL-pattern detection lives in sqlcheck.py. The LLM never decides *whether*
a problem exists - it only explains and fixes what was detected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import TuningConfig
from .explain import ParsedExplain

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}

# code: (title, default severity, tuning points, action grade A=rewrite B=stats C=design)
FINDING_INFO: dict[str, tuple[str, str, list[str], str]] = {
    # EXPLAIN
    "PRODUCT_JOIN": ("Product join", "critical", ["TP-JOIN-01", "TP-JOIN-05"], "A"),
    "DUPLICATE_LARGE": ("대용량 전체 AMP 복제", "warning", ["TP-JOIN-12", "TP-STAT-01"], "B"),
    "REDISTRIBUTE_LARGE": ("대용량 재분배", "warning", ["TP-JOIN-11", "TP-JOIN-14"], "A"),
    "JOIN_CONVERSION": ("조인 컬럼 형변환", "warning", ["TP-JOIN-13", "TP-TYPE-01"], "A"),
    "PARTITION_NOT_ELIMINATED": ("파티션 테이블 전체 스캔 (파티션 제거 미적용)", "critical",
                                 ["TP-PART-01", "TP-PART-02"], "A"),
    "FULL_SCAN_LARGE": ("대용량 전체 스캔 (파티션 없음)", "warning", ["TP-PART-05", "TP-IDX-01"], "C"),
    "STATS_NO_CONFIDENCE": ("통계 없음 (no confidence)", "warning", ["TP-STAT-01", "TP-STAT-05"], "B"),
    "STATS_LOW_CONFIDENCE": ("통계 부족 (low confidence)", "info", ["TP-STAT-02"], "B"),
    "STATS_RECOMMENDED": ("옵티마이저 통계 권고 (HELPSTATS)", "info", ["TP-STAT-03"], "B"),
    "LARGE_SPOOL": ("대용량 스풀", "warning", ["TP-RES-01", "TP-RW-01"], "A"),
    "SKEWED_REDISTRIBUTION": ("소수 AMP 로 재분배 (스큐)", "warning", ["TP-SKEW-06", "TP-SKEW-02"], "A"),
    "HOTSPOT_STEP": ("병목 단계", "info", ["TP-EXP-01"], "A"),
    # SQL patterns (sqlcheck.py)
    "COMMA_JOIN": ("콤마 조인 / 조인 조건 누락 위험", "warning", ["TP-JOIN-01"], "A"),
    "OR_JOIN": ("OR 조인 조건", "warning", ["TP-JOIN-02"], "A"),
    "NONEQUI_JOIN": ("비동등(범위) 조인", "warning", ["TP-JOIN-03"], "A"),
    "FUNCTION_JOIN": ("함수/표현식 조인", "warning", ["TP-JOIN-04"], "A"),
    "NON_SARGABLE": ("조건 컬럼에 함수 사용", "warning", ["TP-PRED-01", "TP-PART-02"], "A"),
    "IMPLICIT_CONVERSION": ("암시적 형변환 비교", "warning", ["TP-PRED-02"], "A"),
    "LIKE_LEADING_WILDCARD": ("선행 와일드카드 LIKE", "info", ["TP-PRED-03"], "A"),
    "OR_FILTER": ("서로 다른 컬럼의 OR 조건", "info", ["TP-PRED-04"], "A"),
    "LARGE_IN_LIST": ("대형 IN 리스트", "info", ["TP-PRED-05"], "A"),
    "OUTER_WHERE_FILTER": ("OUTER JOIN 안쪽 테이블 조건이 WHERE 에 있음", "warning", ["TP-OJ-01"], "A"),
    "UNUSED_OUTER_JOIN": ("사용하지 않는 OUTER JOIN 테이블", "info", ["TP-OJ-03"], "A"),
    "NOT_IN_SUBQUERY": ("NOT IN (서브쿼리)", "warning", ["TP-SUB-01"], "A"),
    "CORRELATED_SUBQUERY": ("상관 서브쿼리", "info", ["TP-SUB-02"], "A"),
    "SCALAR_SUBQUERY": ("SELECT 절 스칼라 서브쿼리", "warning", ["TP-SUB-03"], "A"),
    "UNUSED_COLUMNS": ("쓰지 않는 컬럼을 중간 결과에 적재", "warning", ["TP-RW-01"], "A"),
    "SELECT_STAR": ("SELECT *", "info", ["TP-RW-01"], "A"),
    "REPEATED_SCAN": ("같은 테이블 반복 참조", "info", ["TP-RW-03"], "A"),
    "UNION_DISTINCT": ("UNION (중복 제거)", "info", ["TP-RW-04"], "A"),
    "DISTINCT_LARGE": ("SELECT DISTINCT", "info", ["TP-AGG-01"], "A"),
    "WINDOW_NO_PARTITION": ("PARTITION BY 없는 윈도우 함수", "critical", ["TP-AGG-03", "TP-SKEW-04"], "A"),
    "UPDATE_JOIN": ("UPDATE-조인 (MERGE 검토)", "info", ["TP-ETL-03"], "A"),
    "DELETE_ALL_CANDIDATE": ("전체 삭제 DELETE", "info", ["TP-ETL-04"], "A"),
    "NO_ACCESS_LOCK": ("조회 쿼리 잠금 (LOCKING FOR ACCESS 없음)", "info", ["TP-RES-03"], "A"),
    # DBQL / table metrics
    "CPU_SKEW": ("CPU 스큐 (DBQL)", "warning", ["TP-DBQL-02", "TP-SKEW-02", "TP-SKEW-03"], "A"),
    "IO_SKEW": ("IO 스큐 (DBQL)", "warning", ["TP-DBQL-02"], "A"),
    "PRODUCT_JOIN_SUSPECT": ("Product join 의심 (DBQL PJI)", "warning", ["TP-DBQL-03", "TP-JOIN-05"], "A"),
    "UNNECESSARY_IO": ("불필요한 IO 의심 (DBQL UII)", "warning", ["TP-DBQL-04", "TP-PRED-01"], "A"),
    "TABLE_SKEW": ("테이블 데이터 스큐", "warning", ["TP-SKEW-01", "TP-PI-01"], "C"),
}


@dataclass
class Finding:
    code: str
    detail: str
    evidence: str = ""
    step: str = ""
    tables: list[str] = field(default_factory=list)
    source: str = "explain"           # explain | sql | dbql | table
    severity: str = ""

    def __post_init__(self):
        if not self.severity:
            self.severity = FINDING_INFO[self.code][1]

    @property
    def title(self) -> str:
        return FINDING_INFO[self.code][0]

    @property
    def tuning_points(self) -> list[str]:
        return FINDING_INFO[self.code][2]

    @property
    def grade(self) -> str:
        return FINDING_INFO[self.code][3]

    def key(self) -> tuple:
        return (self.code, self.step, tuple(self.tables), self.detail)

    def render(self) -> str:
        where = f" [단계 {self.step}]" if self.step else ""
        tps = ", ".join(self.tuning_points)
        line = f"{SEVERITY_ICON[self.severity]} {self.code} {self.title}{where} ({tps}, 등급 {self.grade}): {self.detail}"
        if self.evidence:
            line += f"\n   근거: {self.evidence[:300]}"
        return line


def sort_findings(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code, f.step)):
        if f.key() not in seen:
            seen.add(f.key())
            out.append(f)
    return out


def count_by_severity(findings: list[Finding]) -> dict[str, int]:
    out = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        out[f.severity] += 1
    return out


# --------------------------------------------------------------------------- #
# EXPLAIN
# --------------------------------------------------------------------------- #

_FUNC_ON_COL = re.compile(r"\b(CAST|SUBSTR|SUBSTRING|TRIM|EXTRACT|TRUNC|OREPLACE|UPPER|LOWER|COALESCE)\s*\(\s*\(*\s*"
                          r"(?:(?:BOTH|LEADING|TRAILING)\s+FROM\s+)?(?:YEAR\s+FROM\s+|MONTH\s+FROM\s+|DAY\s+FROM\s+)?"
                          r"[A-Za-z_][\w$#]*(?:\s+in view\s+[\w$#]+)?\.[A-Za-z_]", re.I)


def _product_join(s, share: float) -> Finding:
    cmp = s.product_comparisons
    rows = s.est_rows or 0
    inputs = " × ".join("?" if r is None else f"{r:,}" for r in s.input_rows) or "?"
    detail = f"{s.join_type + ' ' if s.join_type not in ('', 'inner') else ''}{s.join_method} join, " \
             f"입력 {inputs}행 → 결과 {rows:,}행"
    if s.small_product_join and share < 0.2:
        return Finding("PRODUCT_JOIN", detail + " (한쪽이 아주 작아 대개 정상)", evidence=s.join_condition or s.summary(),
                       step=s.label, tables=s.lineage, severity="info")
    big = (cmp is not None and cmp >= 1_000_000_000) or (cmp is None and rows >= 100_000) or share >= 0.2
    return Finding("PRODUCT_JOIN", detail, evidence=s.join_condition or s.summary(), step=s.label,
                   tables=s.lineage, severity="critical" if big else "warning")


def from_explain(p: ParsedExplain, cfg: TuningConfig, partitioned: set[str] | None = None) -> list[Finding]:
    partitioned = {t.lower() for t in partitioned or set()}
    out: list[Finding] = []
    total = p.total_time or 0
    no_conf, low_conf = [], []

    for s in p.steps:
        rows = s.est_rows or 0
        share = (s.est_time or 0) / total if total else 0
        if "product" in s.join_method:
            out.append(_product_join(s, share))
        if s.geography == "duplicated" and rows >= cfg.dup_rows:
            out.append(Finding("DUPLICATE_LARGE", f"{rows:,}행을 모든 AMP 로 복제 ({s.confidence or '?'} confidence)",
                               evidence=s.summary(), step=s.label, tables=s.lineage))
        if s.geography == "redistributed" and rows >= cfg.redistribute_rows:
            out.append(Finding("REDISTRIBUTE_LARGE", f"{rows:,}행 재분배, 키: {s.redistribute_keys or '(내부 키)'}",
                               evidence=s.summary(), step=s.label, tables=s.lineage))
        if s.redistribute_target == "few":
            out.append(Finding("SKEWED_REDISTRIBUTION",
                               f"재분배 결과가 소수 AMP 로 몰립니다 (키: {s.redistribute_keys or '?'}, {rows:,}행)",
                               evidence=s.summary(), step=s.label, tables=s.lineage,
                               severity="critical" if rows >= cfg.redistribute_rows else
                               "warning" if rows >= cfg.stats_min_rows else "info"))
        if s.join_condition and re.search(r"\b(CAST|TRANSLATE)\s*\(", s.join_condition, re.I):
            out.append(Finding("JOIN_CONVERSION", "조인 조건에 형변환이 있습니다",
                               evidence=s.join_condition, step=s.label, tables=s.lineage))
        for sc in s.scans:
            if not sc.full_scan:
                continue
            if sc.table.lower() in partitioned:
                out.append(Finding("PARTITION_NOT_ELIMINATED",
                                   f"{sc.label} 를 파티션 제거 없이 전체 스캔"
                                   + (" (모든 파티션)" if sc.partitions else ""),
                                   evidence=s.summary(), step=s.label, tables=[sc.table]))
            elif rows >= cfg.scan_rows:
                out.append(Finding("FULL_SCAN_LARGE", f"{sc.label} 전체 스캔, 결과 {rows:,}행 예상",
                                   evidence=s.summary(), step=s.label, tables=[sc.table]))
        if any(sc.full_scan for sc in s.scans):
            for cond in s.conditions:
                if _FUNC_ON_COL.search(cond):
                    out.append(Finding("NON_SARGABLE", "전체 스캔 단계의 조건에서 컬럼이 함수로 감싸져 있습니다",
                                       evidence=cond[:400], step=s.label, tables=s.tables))
                    break
        if s.confidence == "no" and (rows >= cfg.stats_min_rows or share >= 0.1):
            no_conf.append(s)
        elif s.confidence == "low" and rows >= cfg.scan_rows:
            low_conf.append(s)
        if rows >= cfg.spool_rows or (s.est_bytes or 0) >= cfg.spool_bytes:
            width = f", 행당 약 {s.est_bytes // max(rows, 1):,} bytes" if s.est_bytes else ""
            out.append(Finding("LARGE_SPOOL", f"스풀 {rows:,}행"
                               + (f" / {s.est_bytes:,} bytes{width}" if s.est_bytes else ""),
                               evidence=s.summary(), step=s.label, tables=s.lineage))

    for code, steps in (("STATS_NO_CONFIDENCE", no_conf), ("STATS_LOW_CONFIDENCE", low_conf)):
        if steps:
            tables = list(dict.fromkeys(t for st in steps for t in st.lineage))
            listed = ", ".join(f"{st.label}({st.est_rows or 0:,}행)" for st in steps[:8])
            more = f" 외 {len(steps) - 8}개" if len(steps) > 8 else ""
            out.append(Finding(code, f"{len(steps)}개 단계가 통계 없이/부족하게 추정됨: {listed}{more}",
                               evidence="\n".join(st.summary() for st in steps[:5]), tables=tables))

    hot = p.hotspot
    if hot and total and hot.est_time and hot.est_time / total >= 0.5 and len(p.steps) > 2:
        out.append(Finding("HOTSPOT_STEP", f"전체 예상 시간의 {hot.est_time / total:.0%} 가 이 단계",
                           evidence=hot.summary(), step=hot.label, tables=hot.lineage))
    if p.helpstats:
        out.append(Finding("STATS_RECOMMENDED", f"옵티마이저가 통계 {len(p.helpstats)}건을 권고",
                           evidence="\n".join(p.helpstats[:10])))
    return out


# --------------------------------------------------------------------------- #
# DBQL / table metrics
# --------------------------------------------------------------------------- #

def dbql_ratios(m: dict) -> dict:
    """Derived DBQL indicators from a DBQLogTbl row (keys as column names)."""
    def num(k):
        v = m.get(k)
        return float(v) if v is not None else None

    cpu, io = num("AMPCPUTime"), num("TotalIOCount")
    max_cpu, max_io, amps = num("MaxAMPCPUTime"), num("MaxAMPIO"), num("NumOfActiveAMPs")
    out = {}
    if cpu and max_cpu is not None and amps:
        out["cpu_skew"] = max_cpu * amps / cpu
        out["impact_cpu"] = max_cpu * amps
    if io and max_io is not None and amps:
        out["io_skew"] = max_io * amps / io
    if cpu and io:
        out["pji"] = cpu * 1000 / io
        out["uii"] = io / (cpu * 1000)
    return out


def from_dbql(m: dict, cfg: TuningConfig) -> list[Finding]:
    r = dbql_ratios(m)
    out = []
    if (sk := r.get("cpu_skew")) and sk >= cfg.cpu_skew_warn:
        out.append(Finding("CPU_SKEW", f"CPU 스큐 {sk:.1f} (Impact CPU {r['impact_cpu']:,.0f}s)",
                           evidence=f"MaxAMPCPUTime={m.get('MaxAMPCPUTime')}, AMPCPUTime={m.get('AMPCPUTime')}, "
                                    f"NumOfActiveAMPs={m.get('NumOfActiveAMPs')}",
                           source="dbql", severity="critical" if sk >= cfg.cpu_skew_crit else "warning"))
    if (sk := r.get("io_skew")) and sk >= cfg.cpu_skew_warn:
        out.append(Finding("IO_SKEW", f"IO 스큐 {sk:.1f}", source="dbql",
                           evidence=f"MaxAMPIO={m.get('MaxAMPIO')}, TotalIOCount={m.get('TotalIOCount')}",
                           severity="critical" if sk >= cfg.cpu_skew_crit else "warning"))
    if (v := r.get("pji")) and v >= cfg.pji_warn:
        out.append(Finding("PRODUCT_JOIN_SUSPECT", f"PJI {v:.1f} (≥ {cfg.pji_warn:g})", source="dbql",
                           evidence=f"AMPCPUTime={m.get('AMPCPUTime')}, TotalIOCount={m.get('TotalIOCount')}"))
    if (v := r.get("uii")) and v >= cfg.uii_warn:
        out.append(Finding("UNNECESSARY_IO", f"UII {v:.1f} (≥ {cfg.uii_warn:g})", source="dbql",
                           evidence=f"AMPCPUTime={m.get('AMPCPUTime')}, TotalIOCount={m.get('TotalIOCount')}"))
    return out


def from_table_skew(table: str, skew_pct: float | None, cfg: TuningConfig) -> list[Finding]:
    if skew_pct is None or skew_pct < cfg.table_skew_warn:
        return []
    return [Finding("TABLE_SKEW", f"{table} AMP 간 용량 스큐 {skew_pct:.1f}%", tables=[table], source="table",
                    severity="critical" if skew_pct >= cfg.table_skew_crit else "warning",
                    evidence="DBC.TableSizeV: (1 - AVG/MAX CurrentPerm) × 100")]
