"""The goal loop: measure → rewrite → re-measure until the goal is met,
attempts stop improving, or the attempt budget runs out.

    baseline = measure(original)                     # EXPLAIN + findings (code)
    while goal not met:
        candidate = model(rewrite prompt)            # one call, context prepared by code
        guard(candidate)                             # dialect, same output columns, catalog
        m = measure(candidate)                       # EXPLAIN again
        keep it only if is_better(m, best)           # judged in code, never by the model
    report = model(report prompt) over the evidence  # + deterministic stats/design candidates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import TuningConfig
from ..llm import ChatModel
from ..schema import SchemaStore
from ..skills import SkillLibrary
from ..sql_lint import catalog_issues, lint_sql, normalize_sql
from ..teradata import TeradataClient, TeradataError
from . import recommend
from .explain import ParsedExplain, parse_explain, plan_metrics
from .findings import (SEVERITY_ICON, Finding, count_by_severity, from_dbql, from_explain, from_table_skew,
                       sort_findings)
from .goal import Criterion, Goal, evaluate, is_better
from .prompts import REPORT_SYSTEM, REWRITE_SYSTEM, parse_rewrite_reply
from .sqlcheck import (QueryContext, analyze_sql, output_signature, parse, referenced_partitioned_tables,
                       result_check_sql, semantic_warnings)

EventHandler = Callable[[str, dict], None]


@dataclass
class Measurement:
    sql: str
    explain_text: str = ""
    explain_error: str = ""
    parsed: ParsedExplain | None = None
    metrics: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    dialect_errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return count_by_severity(self.findings)

    @property
    def has_plan(self) -> bool:
        return self.parsed is not None


@dataclass
class Attempt:
    n: int
    sql: str
    analysis: str = ""
    changes: str = ""
    assumptions: str = ""
    status: str = ""     # accepted | rejected | invalid | unverified | none
    reason: str = ""
    measurement: Measurement | None = None
    warnings: list[str] = field(default_factory=list)  # signs the result may differ
    result_check: str = ""                              # "" | same | differs | skipped: ...


@dataclass
class TuningReport:
    goal: Goal
    original: Measurement
    best: Measurement
    best_attempt: int | None
    attempts: list[Attempt]
    criteria: list[Criterion]
    goal_met: bool
    stop_reason: str
    verified: bool
    recommendations: recommend.Recommendations
    report_md: str
    dbql: dict | None = None

    @property
    def improved(self) -> bool:
        return self.best_attempt is not None


class TuningSession:
    def __init__(self, model: ChatModel, store: SchemaStore, skills: SkillLibrary, cfg: TuningConfig,
                 td: TeradataClient | None = None, on_event: EventHandler | None = None):
        self.model = model
        self.store = store
        self.skills = skills
        self.cfg = cfg
        self.td = td
        self.on_event = on_event or (lambda kind, data: None)
        self._skew_cache: dict[str, float | None] = {}

    # ------------------------------------------------------------ measure
    def measure(self, sql: str, explain_text: str | None = None, dbql: dict | None = None,
                table_checks: bool = False) -> Measurement:
        m = Measurement(sql=sql)
        m.dialect_errors = [str(i) for i in lint_sql(sql) if i.level == "error"]
        findings = analyze_sql(sql, self.store)
        partitioned = referenced_partitioned_tables(sql, self.store)

        if explain_text:
            m.explain_text = explain_text
        elif self.td is not None:
            try:
                m.explain_text = self.td.explain(sql)
            except TeradataError as e:
                m.explain_error = str(e)
        if m.explain_text:
            m.parsed = parse_explain(m.explain_text)
            m.metrics = plan_metrics(m.parsed, partitioned)
            findings += from_explain(m.parsed, self.cfg, partitioned)
        if dbql:
            findings += from_dbql(dbql, self.cfg)
        if table_checks and self.td is not None:
            for name in self._base_tables(sql):
                if name not in self._skew_cache:
                    try:
                        self._skew_cache[name] = self.td.table_skew(name)
                    except TeradataError:
                        self._skew_cache[name] = None
                findings += from_table_skew(name, self._skew_cache[name], self.cfg)
        if any(f.code == "NON_SARGABLE" and f.source == "sql" for f in findings):
            findings = [f for f in findings if not (f.code == "NON_SARGABLE" and f.source == "explain")]
        m.findings = sort_findings(findings)
        return m

    def _base_tables(self, sql: str) -> list[str]:
        names = []
        for tree in parse(sql):
            for n in QueryContext(tree, self.store).base_tables:
                if "." in n and n.upper() not in (x.upper() for x in names):
                    names.append(n)
        return names

    # ------------------------------------------------------------ run
    def run(self, sql: str, goal: Goal, dbql: dict | None = None, explain_text: str | None = None,
            max_iterations: int | None = None, analyze_only: bool = False,
            result_check: bool = False) -> TuningReport:
        sql = sql.strip().rstrip(";").strip()
        self.on_event("status", {"text": "원본 쿼리 측정 (EXPLAIN · 튜닝 포인트 탐지)"})
        base = self.measure(sql, explain_text=explain_text, dbql=dbql, table_checks=True)
        self.on_event("baseline", {"measurement": base})

        verified = self.td is not None and base.has_plan
        best, best_attempt = base, None
        attempts: list[Attempt] = []
        budget = max_iterations if max_iterations is not None else self.cfg.max_iterations
        stop_reason = ""

        def crit(m: Measurement) -> list[Criterion]:
            return evaluate(goal, base.metrics, m.metrics, m.counts["critical"], self.cfg.dup_rows)

        criteria = crit(best)
        if analyze_only:
            stop_reason = "진단만 수행"
        elif base.has_plan and all(c.met for c in criteria):
            stop_reason = "원본이 이미 목표를 충족"
        elif not verified:
            budget = min(budget, 1)  # cannot re-measure candidates: one suggestion, clearly marked
        no_improve, seen = 0, {normalize_sql(sql)}

        for n in range(1, budget + 1):
            if stop_reason:
                break
            self.on_event("status", {"text": f"재작성 시도 {n}/{budget}"})
            att = self._attempt(n, sql, best, base, goal, criteria, attempts, verified, seen, result_check)
            attempts.append(att)
            self.on_event("attempt", {"attempt": att})
            if att.status == "none":
                stop_reason = "모델이 더 이상 재작성으로 개선할 방법이 없다고 판단"
                break
            if att.status == "unverified":
                stop_reason = "EXPLAIN 으로 검증할 수 없어 제안 1회로 종료"
                break
            if att.status == "accepted":
                best, best_attempt, no_improve = att.measurement, n, 0
                criteria = crit(best)
                if all(c.met for c in criteria):
                    stop_reason = "목표 달성"
                    break
            elif att.status == "rejected":  # invalid attempts only consume the budget
                no_improve += 1
                if no_improve >= self.cfg.patience:
                    stop_reason = f"{no_improve}회 연속 개선 없음"
                    break
        if not stop_reason:
            stop_reason = f"시도 한도({budget}회) 도달"

        criteria = crit(best)
        goal_met = best.has_plan and all(c.met for c in criteria)
        self.on_event("status", {"text": "권고 · 리포트 작성"})
        recs = recommend.build(best.sql, best.parsed or base.parsed, best.findings + [
            f for f in base.findings if f.source in ("dbql", "table")], self.store)
        if base.parsed is not None and best.parsed is not None and base is not best:
            for h in base.parsed.helpstats:
                if h not in recs.stats:
                    recs.stats.append(h)
                    recs.stats_reasons[h] = "옵티마이저 HELPSTATS 권고 (원본)"
        report = TuningReport(goal=goal, original=base, best=best, best_attempt=best_attempt, attempts=attempts,
                              criteria=criteria, goal_met=goal_met, stop_reason=stop_reason, verified=verified,
                              recommendations=recs, report_md="", dbql=dbql)
        report.report_md = self._report(report)
        self.on_event("done", {"report": report})
        return report

    # ------------------------------------------------------------ attempt
    def _attempt(self, n: int, original: str, best: Measurement, base: Measurement, goal: Goal,
                 criteria: list[Criterion], history: list[Attempt], verified: bool, seen: set,
                 result_check: bool = False) -> Attempt:
        messages = [{"role": "system", "content": REWRITE_SYSTEM},
                    {"role": "user", "content": self._rewrite_context(original, best, base, goal, criteria, history)}]
        reply = parse_rewrite_reply(self.model.chat(messages, []).content)
        att = Attempt(n=n, sql=reply["rewrite"], analysis=reply["analysis"], changes=reply["changes"],
                      assumptions=reply["assumptions"])
        if not att.sql or att.sql == "NONE":
            att.status, att.reason = "none", "재작성 없음"
            return att
        key = normalize_sql(att.sql)
        if key in seen:
            att.status, att.reason = "invalid", "이전에 시도한 SQL 과 같습니다"
            return att
        seen.add(key)

        errors = [str(i) for i in lint_sql(att.sql) if i.level == "error"]
        if errors:
            att.status, att.reason = "invalid", "Teradata 방언 오류: " + "; ".join(errors)
            return att
        want, got = output_signature(original), output_signature(att.sql)
        if want and got and want != got:
            att.status, att.reason = "invalid", f"출력 컬럼이 원본과 다릅니다 (원본 {want[1:]}, 재작성 {got[1:]})"
            return att
        att.warnings = semantic_warnings(original, att.sql)
        if not verified:
            bad = [str(i) for i in catalog_issues(att.sql, self.store) if i.level == "error"]
            if bad:
                att.status, att.reason = "invalid", "; ".join(bad)
                return att
            att.measurement = self.measure(att.sql)
            att.status, att.reason = "unverified", "EXPLAIN 없이 정적 검사만 통과 (실행 계획 미확인)"
            return att

        self.on_event("status", {"text": f"시도 {n}: EXPLAIN 으로 측정"})
        m = self.measure(att.sql)
        att.measurement = m
        if m.explain_error:
            att.status, att.reason = "invalid", f"EXPLAIN 실패: {m.explain_error}"
            return att
        ok, why = is_better(m.metrics, m.counts["critical"], m.counts["warning"],
                            best.metrics, best.counts["critical"], best.counts["warning"])
        if ok and result_check:
            ok, why = self._check_result(original, att, why)
        if ok and att.warnings and att.result_check != "same":
            # plan got better but the query may no longer mean the same thing: don't build on it
            ok, why = False, "결과 보존 의심 (" + "; ".join(att.warnings) + ") - 조건/테이블을 원본과 같게 유지하세요"
        att.status, att.reason = ("accepted" if ok else "rejected"), why
        return att

    def _check_result(self, original: str, att: Attempt, why: str) -> tuple[bool, str]:
        queries = result_check_sql(original, att.sql)
        if queries is None:
            att.result_check = "skipped: WITH/ORDER BY/TOP 이 있거나 단일 SELECT 가 아니라 비교 불가"
            return True, why
        self.on_event("status", {"text": f"시도 {att.n}: 결과 동일성 비교 (두 쿼리 실행)"})
        try:
            only_a, only_b = (self.td.count_query(q) for q in queries)
        except TeradataError as e:
            att.result_check = f"skipped: {e}"
            return True, why
        if only_a or only_b:
            att.result_check = "differs"
            return False, f"결과 불일치 (원본에만 {only_a:,}행, 재작성에만 {only_b:,}행)"
        att.result_check = "same"
        return True, why + " · 결과 동일 확인"

    def _rewrite_context(self, original: str, best: Measurement, base: Measurement, goal: Goal,
                         criteria: list[Criterion], history: list[Attempt]) -> str:
        parts = [f"## 목표\n{goal.describe()}"]
        unmet = [f"- {c.name}: {c.detail}" for c in criteria if not c.met]
        if unmet:
            parts.append("아직 충족되지 않은 기준:\n" + "\n".join(unmet))
        parts.append(f"## 원본 SQL\n```sql\n{original}\n```")
        if best is not base:
            parts.append(f"## 현재 최선 SQL (이것을 개선하세요)\n```sql\n{best.sql}\n```")
        if best.parsed is not None:
            plan = best.parsed.summary()
            hot = best.parsed.hotspot
            parts.append(f"## 현재 실행 계획 요약 (EXPLAIN)\n{plan[:3500]}")
            if hot is not None:
                parts.append(f"병목 단계 {hot.label} 원문: {hot.text[:800]}")
        else:
            parts.append("## 실행 계획\n(EXPLAIN 없음 - SQL 패턴만으로 판단)")
        parts.append("## 탐지된 튜닝 포인트\n" + ("\n".join(f.render() for f in best.findings[:15]) or "(없음)"))
        knowledge = self._tp_knowledge(best.findings)
        if knowledge:
            parts.append("## 관련 튜닝 지식\n" + knowledge)
        tables = self._table_defs(best.sql)
        if tables:
            parts.append("## 테이블 정의\n" + tables)
        if history:
            lines = []
            for a in history:
                lines.append(f"- 시도 {a.n} [{a.status}] {a.reason}" + (f" | 변경: {a.changes[:300]}" if a.changes else ""))
            parts.append("## 이전 시도 결과\n" + "\n".join(lines))
        dialect = self.skills.get("teradata-sql-dialect")
        if dialect:
            parts.append("## Teradata 방언 규칙\n" + dialect.body)
        return "\n\n".join(parts)

    def _tp_knowledge(self, findings: list[Finding]) -> str:
        tps = self.skills.tuning_points()
        out, used, seen = [], 0, set()
        for f in findings:
            if f.severity == "info" and f.grade != "A":
                continue
            for tp_id in f.tuning_points:
                tp = tps.get(tp_id)
                if tp is None or tp_id in seen:
                    continue
                seen.add(tp_id)
                if used + len(tp.text) > self.cfg.skill_chars:
                    return "\n\n".join(out)
                out.append(tp.text)
                used += len(tp.text)
        return "\n\n".join(out)

    def _table_defs(self, sql: str, max_chars: int = 3500) -> str:
        out, used = [], 0
        for name in self._base_tables(sql):
            t = self.store.find(name)
            if t is None and self.td is not None:
                try:
                    t = self.td.describe(name)
                    if t is not None:
                        self.store.add(t)
                except TeradataError:
                    t = None
            if t is None:
                continue
            text = t.render()
            if used + len(text) > max_chars:
                break
            out.append(text)
            used += len(text)
        return "\n\n".join(out)

    # ------------------------------------------------------------ report
    def _report(self, r: TuningReport) -> str:
        evidence = self._report_evidence(r)
        try:
            reply = self.model.chat([{"role": "system", "content": REPORT_SYSTEM},
                                     {"role": "user", "content": evidence}], [])
            if reply.content.strip():
                return reply.content.strip()
        except Exception as e:  # keep the deterministic part even if the model fails
            return fallback_report(r) + f"\n\n> 리포트 작성 중 LLM 오류: {e}"
        return fallback_report(r)

    def _report_evidence(self, r: TuningReport) -> str:
        parts = [f"## 목표\n{r.goal.describe()}",
                 "## 목표 판정\n" + "\n".join(f"- {'✅' if c.met else '❌'} {c.name}: {c.detail}" for c in r.criteria),
                 f"종료 사유: {r.stop_reason}. EXPLAIN 검증: {'예' if r.verified else '아니오'}",
                 "## 지표 (원본 → 최종)\n" + metrics_table(r.original.metrics, final_metrics(r))]
        if r.dbql:
            parts.append("## DBQL 지표\n" + ", ".join(f"{k}={v}" for k, v in r.dbql.items() if k != "SqlPreview"))
        parts.append("## 원본의 튜닝 포인트\n" + "\n".join(f.render() for f in r.original.findings[:25]))
        if r.best is not r.original:
            parts.append("## 최종 SQL 에 남은 튜닝 포인트\n" + ("\n".join(f.render() for f in r.best.findings[:15])
                                                         or "(없음)"))
        att = [f"- 시도 {a.n} [{a.status}] {a.reason}\n  분석: {a.analysis[:300]}\n  변경: {a.changes[:500]}"
               + (f"\n  가정: {a.assumptions[:300]}" if a.assumptions else "")
               + (f"\n  ⚠ 결과 변경 가능성: {'; '.join(a.warnings)}" if a.warnings else "")
               + (f"\n  결과 비교: {a.result_check}" if a.result_check else "") for a in r.attempts]
        parts.append("## 재작성 시도\n" + ("\n".join(att) or "(없음)"))
        if r.best_attempt:
            parts.append(f"## 채택된 SQL (시도 {r.best_attempt})\n```sql\n{r.best.sql}\n```")
        parts.append("## 권고 후보 (코드가 계산)\n" + r.recommendations.render())
        knowledge = self._tp_knowledge(r.original.findings)
        if knowledge:
            parts.append("## 관련 튜닝 지식\n" + knowledge[: self.cfg.skill_chars])
        return "\n\n".join(parts)


def metrics_table(before: dict, after: dict | None) -> str:
    """Before/after plan metrics; only the original column when nothing was accepted."""
    labels = [("total_time", "총 예상 시간(s)"), ("product_joins", "product join"), ("full_scans", "전체 스캔"),
              ("partition_misses", "파티션 미적용 스캔"), ("no_confidence_steps", "no confidence 단계"),
              ("max_duplicated_rows", "최대 복제 행"), ("max_redistributed_rows", "최대 재분배 행"),
              ("max_spool_rows", "최대 스풀 행")]
    def fmt(v):
        return "-" if v is None else (f"{v:,.2f}" if isinstance(v, float) else f"{v:,}")

    rows = ["| 지표 | 원본 | 최종 |", "|---|---|---|"] if after is not None else ["| 지표 | 원본 |", "|---|---|"]
    for k, label in labels:
        b = before.get(k)
        a = after.get(k) if after is not None else None
        if b is None and a is None:
            continue
        rows.append(f"| {label} | {fmt(b)} | {fmt(a)} |" if after is not None else f"| {label} | {fmt(b)} |")
    return "\n".join(rows)


def final_metrics(r: "TuningReport") -> dict | None:
    return r.best.metrics if r.best_attempt else None


def fallback_report(r: TuningReport) -> str:
    lines = ["## 요약", f"- 목표: {r.goal.describe()}",
             f"- 결과: {'목표 달성' if r.goal_met else '목표 미달성'} ({r.stop_reason})", "",
             metrics_table(r.original.metrics, final_metrics(r)), "", "## 튜닝 포인트"]
    lines += [f"- {SEVERITY_ICON[f.severity]} {f.code} {f.title} ({', '.join(f.tuning_points)}): {f.detail}"
              for f in r.original.findings]
    if r.recommendations.stats:
        lines += ["", "## 통계 수집 권고", "```sql", *r.recommendations.stats, "```"]
    return "\n".join(lines)
