"""Tuning goal: what "done" means, checked in code against EXPLAIN metrics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Goal:
    no_product_join: bool = True
    no_partition_miss: bool = True
    no_large_duplication: bool = False
    no_critical: bool = False           # every critical finding gone
    time_reduction_pct: float = 0.0     # e.g. 30 = estimated time at most 70% of baseline; 0 = not worse
    note: str = ""                      # free-text extra goal, passed to the model only

    def describe(self) -> str:
        parts = []
        if self.no_product_join:
            parts.append("product join 제거")
        if self.no_partition_miss:
            parts.append("파티션 테이블 전체 스캔 제거")
        if self.no_large_duplication:
            parts.append("대용량 복제 제거")
        if self.no_critical:
            parts.append("심각(critical) 튜닝 포인트 0건")
        parts.append(f"예상 시간 {self.time_reduction_pct:g}% 이상 단축" if self.time_reduction_pct
                     else "예상 시간 악화 없음")
        if self.note:
            parts.append(f"추가: {self.note}")
        return ", ".join(parts)


@dataclass
class Criterion:
    name: str
    met: bool
    detail: str


def evaluate(goal: Goal, baseline: dict, current: dict, n_critical: int, dup_rows: int) -> list[Criterion]:
    """Criteria that can be judged from the metrics. Unknown values (no EXPLAIN) count as unmet."""
    out = []

    def known(k):
        return current.get(k) is not None

    if goal.no_product_join:
        v = current.get("product_joins")
        out.append(Criterion("product join 제거", known("product_joins") and v == 0, f"product join {v}개"))
    if goal.no_partition_miss:
        v = current.get("partition_misses")
        out.append(Criterion("파티션 전체 스캔 제거", known("partition_misses") and v == 0, f"파티션 미적용 스캔 {v}개"))
    if goal.no_large_duplication:
        v = current.get("max_duplicated_rows")
        out.append(Criterion("대용량 복제 제거", known("max_duplicated_rows") and v < dup_rows,
                             f"최대 복제 {v or 0:,}행"))
    if goal.no_critical:
        out.append(Criterion("critical 0건", n_critical == 0, f"critical {n_critical}건"))
    b, c = baseline.get("total_time"), current.get("total_time")
    if b is None or c is None:
        out.append(Criterion("예상 시간", False, "EXPLAIN 예상 시간 없음"))
    else:
        limit = b * (1 - goal.time_reduction_pct / 100)
        label = f"예상 시간 {goal.time_reduction_pct:g}% 단축" if goal.time_reduction_pct else "예상 시간 악화 없음"
        change = (1 - c / b) * 100 if b else 0.0
        trend = f"{change:.0f}% 단축" if change >= 0 else f"{-change:.0f}% 증가"
        out.append(Criterion(label, c <= limit + 1e-9, f"{c:g}s (기준 {b:g}s, {trend}, 목표 ≤ {limit:g}s)"))
    return out


def is_better(cand: dict, cand_critical: int, cand_warning: int,
              best: dict, best_critical: int, best_warning: int) -> tuple[bool, str]:
    """Is the candidate plan an improvement over the current best?"""
    ct, bt = cand.get("total_time"), best.get("total_time")
    if cand.get("product_joins", 0) > best.get("product_joins", 0):
        return False, "product join 증가"
    if cand_critical != best_critical:
        if cand_critical < best_critical and (ct is None or bt is None or ct <= bt * 1.5):
            return True, f"critical {best_critical}→{cand_critical}"
        return False, f"critical {best_critical}→{cand_critical}" if cand_critical > best_critical \
            else "critical 은 줄었지만 예상 시간이 크게 증가"
    if ct is not None and bt is not None:
        if ct < bt * 0.97:
            return True, f"예상 시간 {bt:g}s→{ct:g}s"
        if ct > bt * 1.03:
            return False, f"예상 시간 증가 {bt:g}s→{ct:g}s"
    if cand_warning < best_warning:
        return True, f"warning {best_warning}→{cand_warning}"
    return False, "개선 없음"
