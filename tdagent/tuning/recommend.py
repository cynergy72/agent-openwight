"""Deterministic recommendations the report is built on: statistics to collect
and design candidates (partition / PI). The model explains and prioritizes
them; it does not invent them."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schema import SchemaStore
from .explain import HelpStat, ParsedExplain
from .findings import Finding
from .sqlcheck import predicate_columns


@dataclass
class Recommendations:
    stats: list[str] = field(default_factory=list)        # COLLECT STATISTICS statements to apply first
    stats_optional: list[str] = field(default_factory=list)  # lower value / higher cost: review before collecting
    stats_reasons: dict[str, str] = field(default_factory=dict)
    partition_candidates: list[tuple[str, str]] = field(default_factory=list)  # (table, date column)
    pi_candidates: list[tuple[str, str]] = field(default_factory=list)         # (table, columns)

    def render(self) -> str:
        lines = []
        if self.stats:
            lines.append("통계 수집 후보:")
            lines += [f"- {s}  -- {self.stats_reasons.get(s, '')}" for s in self.stats]
        if self.stats_optional:
            lines.append("통계 선택 검토 (우선순위 낮음 / 수집 비용 큼 / DBA 영역):")
            lines += [f"- {s}  -- {self.stats_reasons.get(s, '')}" for s in self.stats_optional]
        if self.partition_candidates:
            lines.append("파티션 추가 후보 (테이블, 범위 조건 날짜 컬럼):")
            lines += [f"- {t} / {c}" for t, c in self.partition_candidates]
        if self.pi_candidates:
            lines.append("PI 검토 후보 (테이블, 조인 키):")
            lines += [f"- {t} / {c}" for t, c in self.pi_candidates]
        return "\n".join(lines) or "(없음)"


WIDE_STATS = 5  # multi-column statistics with this many columns cost a lot for little gain in most plans


def _triage(h: HelpStat) -> tuple[bool, str]:
    """(apply first?, reason) for one HELPSTATS recommendation."""
    where = "최종 계획" if h.section != "other" else "다른 계획 후보"
    reason = f"HELPSTATS · {h.confidence or '?'} · {where}"
    if h.summary_stats:
        return True, reason + " · SUMMARY 통계 (테이블 행 수, 수집 비용 작음)"
    if h.table.upper().startswith("DBC."):
        return False, reason + " · 시스템 딕셔너리 (DBA 작업)"
    if len(h.columns) >= WIDE_STATS:
        return False, reason + f" · {len(h.columns)}개 컬럼 조합 (수집 비용 큼, 효과 제한적)"
    if h.section == "other" or h.confidence.lower() in ("medium", "low"):
        return False, reason
    return True, reason + (f" · {len(h.columns)}개 컬럼 조합" if len(h.columns) > 1 else "")


def _stat(table: str, cols: str) -> str:
    return f"COLLECT STATISTICS COLUMN ({cols}) ON {table};"


def build(sql: str, parsed: ParsedExplain | None, findings: list[Finding], store: SchemaStore) -> Recommendations:
    rec = Recommendations()

    def add_stat(stmt: str, why: str, optional: bool = False):
        key = stmt.upper()
        if all(s.upper() != key for s in rec.stats + rec.stats_optional):
            (rec.stats_optional if optional else rec.stats).append(stmt)
            rec.stats_reasons[stmt] = why

    if parsed is not None:
        details = parsed.helpstats_detail or [HelpStat(statement=h) for h in parsed.helpstats]
        for h in details:
            first, why = _triage(h)
            add_stat(h.statement if h.statement.endswith(";") else h.statement + ";", why, optional=not first)

    weak_tables = {t.lower() for f in findings if f.code in ("STATS_NO_CONFIDENCE", "STATS_LOW_CONFIDENCE",
                                                             "DUPLICATE_LARGE", "PRODUCT_JOIN") for t in f.tables}
    preds = predicate_columns(sql, store)
    for table, cols in preds.items():
        t = store.find(table)
        weak = table.lower() in weak_tables
        for c in sorted(cols["join"]):
            if weak:
                add_stat(_stat(table, c), "조인 컬럼 (추정 신뢰도 낮은 단계)")
        for c in sorted(cols["filter"] - cols["join"]):
            if weak:
                add_stat(_stat(table, c), "필터 컬럼 (추정 신뢰도 낮은 단계)")
        if weak and t is not None and t.partitioned_by:
            add_stat(_stat(table, "PARTITION"), "파티션 테이블")

    scanned = {t.lower() for f in findings if f.code == "FULL_SCAN_LARGE" for t in f.tables}
    for table, cols in preds.items():
        t = store.find(table)
        if t is None or t.partitioned_by or table.lower() not in scanned:
            continue
        for c in sorted(cols["range"]):
            col = next((x for x in t.columns if x.name.upper() == c), None)
            if col and (col.type or "").upper().startswith(("DATE", "TIMESTAMP")):
                rec.partition_candidates.append((table, c))

    for f in findings:
        if f.code == "TABLE_SKEW":
            for table in f.tables:
                keys = sorted(preds.get(table, {}).get("join", set()))
                rec.pi_candidates.append((table, ", ".join(keys) or "(조인 키 없음 - 분산 좋은 컬럼 검토)"))
        if f.code == "REDISTRIBUTE_LARGE" and parsed is not None:
            step = parsed.step(f.step)
            for table in f.tables:
                if step and step.redistribute_keys:
                    rec.pi_candidates.append((table, step.redistribute_keys))
    rec.pi_candidates = list(dict.fromkeys(rec.pi_candidates))
    rec.partition_candidates = list(dict.fromkeys(rec.partition_candidates))
    return rec
