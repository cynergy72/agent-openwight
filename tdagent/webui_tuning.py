"""Tuning and knowledge pages of the web UI (imported by webapp.py)."""

from __future__ import annotations

import datetime as dt
import difflib

import streamlit as st

from tdagent.teradata import TeradataError
from tdagent.tuning import dbql
from tdagent.tuning.findings import SEVERITY_ICON
from tdagent.tuning.goal import Goal
from tdagent.tuning.session import Measurement, TuningReport, TuningSession

ss = st.session_state

SOURCES = ["SQL 붙여넣기", "DBQL 에서 찾기", "EXPLAIN 붙여넣기 (DB 미연결)"]
STATUS_BADGE = {"accepted": "✅ 채택", "rejected": "↩️ 기각", "invalid": "⛔ 무효", "unverified": "❔ 미검증 제안",
                "none": "⏹ 재작성 없음"}


def _init() -> None:
    defaults = {
        "tune_source": SOURCES[0], "tune_sql": "", "tune_explain": "", "tune_dbql": None, "use_dbql": True,
        "dbql_rows": None, "dbql_error": "", "tune_report": None,
        "g_no_pj": True, "g_no_part": True, "g_no_dup": False, "g_no_crit": False,
        "g_time_pct": 30, "g_max_iter": max(1, min(10, ss.cfg.tuning.max_iterations)), "g_note": "", "g_result_check": False,
    }
    for k, v in defaults.items():
        ss.setdefault(k, v)


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def _load_dbql_query(row: dict) -> None:
    td = ss.agent.toolbox.td
    try:
        text = dbql.full_sql(td, ss.cfg.dbql, row["ProcID"], row["QueryID"])
    except TeradataError as e:
        ss.dbql_error = f"SQL 텍스트 조회 실패: {e}"
        return
    ss.tune_sql = text or str(row.get("SqlPreview") or "")
    ss.tune_dbql = {k: row.get(k) for k in dbql.METRIC_COLS if k != "SqlPreview"}
    ss.tune_report = None


def _dbql_panel() -> None:
    td = ss.agent.toolbox.td
    if td is None:
        st.info("DBQL 조회에는 Teradata 연결이 필요합니다. 사이드바 🔌 접속 설정에서 연결하세요.")
        return
    with st.form("dbql_form"):
        c = st.columns([2, 1.3, 1, 1.2, 0.8])
        today = dt.date.today()
        period = c[0].date_input("기간", value=(today - dt.timedelta(days=1), today))
        user = c[1].text_input("사용자", placeholder="전체")
        min_cpu = c[2].number_input("최소 CPU(s)", min_value=0.0, value=10.0, step=10.0)
        order = c[3].selectbox("정렬", list(dbql.ORDER_LABELS), format_func=dbql.ORDER_LABELS.get)
        top = c[4].number_input("건수", min_value=5, max_value=200, value=20, step=5)
        submitted = st.form_submit_button("🔎 고부하 쿼리 조회")
    if submitted:
        start, end = (period if isinstance(period, (list, tuple)) and len(period) == 2 else (period, period))
        f = dbql.DBQLFilter(start=f"{start} 00:00:00", end=f"{end + dt.timedelta(days=1)} 00:00:00",
                            user=user, min_cpu=min_cpu, order=order, top=int(top))
        try:
            with st.spinner("DBQL 조회 중..."):
                ss.dbql_rows, ss.dbql_error = dbql.top_queries(td, ss.cfg.dbql, f), ""
        except (TeradataError, ValueError) as e:
            ss.dbql_rows, ss.dbql_error = None, str(e)
    if ss.dbql_error:
        st.error(ss.dbql_error)
    rows = ss.dbql_rows
    if rows is None:
        return
    if not rows:
        st.caption("조건에 맞는 쿼리가 없습니다.")
        return
    table = [{"QueryID": r["QueryID"], "사용자": r["UserName"], "시작": r["StartTime"], "CPU(s)": r["AMPCPUTime"],
              "IO": r["TotalIOCount"], "CPU 스큐": r.get("cpu_skew"), "PJI": r.get("pji"), "UII": r.get("uii"),
              "SQL": (str(r.get("SqlPreview") or ""))[:80]} for r in rows]
    st.dataframe(table, width="stretch", hide_index=True)
    labels = [f"{r['QueryID']} · {r['UserName']} · CPU {r['AMPCPUTime']} · 스큐 {r.get('cpu_skew', '-')}" for r in rows]
    c = st.columns([4, 1])
    pick = c[0].selectbox("튜닝할 쿼리", range(len(rows)), format_func=lambda i: labels[i], key="dbql_pick",
                          label_visibility="collapsed")
    c[1].button("불러오기", on_click=_load_dbql_query, args=(rows[pick],), width="stretch")


def _goal_panel(allow_result_check: bool) -> None:
    with st.expander("🎯 튜닝 목표 (달성할 때까지 반복)", expanded=True):
        c = st.columns(4)
        c[0].checkbox("product join 제거", key="g_no_pj")
        c[1].checkbox("파티션 전체 스캔 제거", key="g_no_part")
        c[2].checkbox("대용량 복제 제거", key="g_no_dup")
        c[3].checkbox("critical 0건", key="g_no_crit")
        c = st.columns([2, 1, 3])
        c[0].slider("예상 시간 단축 목표 (%)", 0, 90, step=5, key="g_time_pct",
                    help="0 이면 '악화되지 않음'만 확인")
        c[1].number_input("최대 시도", 1, 10, key="g_max_iter")
        c[2].text_input("추가 목표 (모델에게 전달)", key="g_note", placeholder="예) 스풀 사용량 최소화, 불필요 컬럼 제거")
        st.checkbox("결과 동일성 검증 (원본·재작성 쿼리를 실제 실행해 EXCEPT ALL 비교)", key="g_result_check",
                    disabled=not allow_result_check,
                    help=None if allow_result_check else "Teradata 연결 + SELECT 실행 허용이 필요합니다")


def _goal() -> Goal:
    return Goal(no_product_join=ss.g_no_pj, no_partition_miss=ss.g_no_part, no_large_duplication=ss.g_no_dup,
                no_critical=ss.g_no_crit, time_reduction_pct=float(ss.g_time_pct), note=ss.g_note.strip())


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def _run(analyze_only: bool) -> None:
    agent = ss.agent
    sql = ss.tune_sql.strip()
    explain_text = ss.tune_explain.strip() if ss.tune_source == SOURCES[2] else None
    status = st.status("튜닝 진행 중...", expanded=True)

    def on_event(kind: str, data: dict) -> None:
        if kind == "status":
            status.update(label=data["text"])
        elif kind == "baseline":
            m: Measurement = data["measurement"]
            c = m.counts
            t = m.metrics.get("total_time")
            status.write(f"원본: 🔴 {c['critical']} · 🟠 {c['warning']} · 🔵 {c['info']}"
                         + (f" · 예상 {t:g}s" if t is not None else "")
                         + (f" · EXPLAIN 실패: {m.explain_error}" if m.explain_error else ""))
        elif kind == "attempt":
            a = data["attempt"]
            status.write(f"시도 {a.n}: {STATUS_BADGE.get(a.status, a.status)} — {a.reason}")

    session = TuningSession(agent.model, agent.toolbox.store, agent.toolbox.skills, ss.cfg.tuning,
                            td=agent.toolbox.td, on_event=on_event)
    try:
        report = session.run(sql, _goal(), dbql=ss.tune_dbql if (ss.tune_dbql and ss.use_dbql) else None,
                             explain_text=explain_text or None, max_iterations=int(ss.g_max_iter),
                             analyze_only=analyze_only, result_check=bool(ss.g_result_check))
    except Exception as e:  # LLM server down etc.
        status.update(label="오류", state="error")
        st.error(f"튜닝 실패: {e}")
        return
    status.update(label=f"완료 · {report.stop_reason}", state="complete", expanded=False)
    ss.tune_report = report


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

def _fmt(v, unit=""):
    if v is None:
        return "-"
    return f"{v:,.2f}{unit}" if isinstance(v, float) else f"{v:,}{unit}"


def _metrics(r: TuningReport) -> None:
    b, a = r.original.metrics, r.best.metrics
    tiles = [("예상 시간", "total_time", "s"), ("Product join", "product_joins", ""),
             ("파티션 미적용 스캔", "partition_misses", ""), ("통계 없음 단계", "no_confidence_steps", "")]
    cols = st.columns(len(tiles) + 1)
    for col, (label, key, unit) in zip(cols, tiles):
        before, after = b.get(key), a.get(key)
        delta = None
        if before is not None and after is not None and r.best is not r.original:
            delta = (f"{(after - before) / before * 100:+.0f}%" if key == "total_time" and before
                     else f"{after - before:+,}")
        col.metric(label, _fmt(after, unit), delta, delta_color="inverse",
                   help=f"원본 {_fmt(before, unit)}")
    cb, ca = r.original.counts["critical"], r.best.counts["critical"]
    cols[-1].metric("critical 튜닝 포인트", ca, f"{ca - cb:+}" if r.best is not r.original else None,
                    delta_color="inverse", help=f"원본 {cb}")


def _finding_resolved(f, best: Measurement) -> bool:
    return not any(g.code == f.code and (g.tables == f.tables or not f.tables) for g in best.findings)


def _findings_tab(r: TuningReport) -> None:
    tps = ss.agent.toolbox.skills.tuning_points()
    if not r.original.findings:
        st.success("탐지된 튜닝 포인트가 없습니다.")
    for f in r.original.findings:
        with st.container(border=True):
            done = r.best is not r.original and _finding_resolved(f, r.best)
            head = f"{SEVERITY_ICON[f.severity]} **{f.title}** `{f.code}`" + (f" · 단계 {f.step}" if f.step else "")
            head += f" · 등급 {f.grade}" + (" · ✅ 해결" if done else "")
            st.markdown(head)
            st.markdown(f.detail)
            if f.evidence:
                st.code(f.evidence[:600], language=None)
            for tp_id in f.tuning_points:
                tp = tps.get(tp_id)
                if tp is not None:
                    with st.expander(f"📘 {tp.id} {tp.title}  ·  {tp.skill}"):
                        st.markdown(tp.text)


def _attempts_tab(r: TuningReport) -> None:
    if not r.attempts:
        st.caption(f"재작성 시도 없음 ({r.stop_reason})")
    for a in r.attempts:
        with st.container(border=True):
            m = a.measurement
            t = m.metrics.get("total_time") if m else None
            st.markdown(f"**시도 {a.n}** · {STATUS_BADGE.get(a.status, a.status)} · {a.reason}"
                        + (f" · 예상 {t:g}s" if t is not None else ""))
            if a.analysis:
                st.markdown(f"🧭 {a.analysis}")
            if a.changes:
                st.markdown(a.changes)
            if a.assumptions:
                st.warning("가정: " + a.assumptions)
            for w in a.warnings:
                st.warning("⚠ " + w)
            if a.result_check:
                st.caption(f"결과 비교: {a.result_check}")
            if a.sql and a.sql != "NONE":
                with st.expander("SQL"):
                    st.code(a.sql, language="sql")


def _sql_tab(r: TuningReport) -> None:
    final = r.best.sql if r.best_attempt else next((a.sql for a in r.attempts if a.status == "unverified"), "")
    if not final:
        st.info("채택된 재작성이 없습니다. 아래 권고(통계·설계)를 먼저 검토하세요.")
        return
    if not r.best_attempt:
        st.warning("EXPLAIN 으로 검증하지 못한 제안입니다. DB 에서 EXPLAIN 으로 확인 후 사용하세요.")
    st.code(final, language="sql", line_numbers=True)
    diff = difflib.unified_diff(r.original.sql.splitlines(), final.splitlines(), "원본", "튜닝", lineterm="")
    with st.expander("원본 대비 변경"):
        st.code("\n".join(diff), language="diff")
    st.download_button("💾 튜닝 SQL 저장", final, file_name="tuned.sql", mime="text/plain")


def _recs_tab(r: TuningReport) -> None:
    rec = r.recommendations
    if rec.stats:
        st.markdown("**통계 수집 (등급 B)**")
        st.code("\n".join(rec.stats), language="sql")
        st.dataframe([{"문장": s, "근거": rec.stats_reasons.get(s, "")} for s in rec.stats], hide_index=True,
                     width="stretch")
        st.download_button("💾 통계 스크립트 저장", "\n".join(rec.stats) + "\n", file_name="collect_stats.sql",
                           mime="text/plain")
    if rec.stats_optional:
        with st.expander(f"선택 검토 통계 {len(rec.stats_optional)}건 (우선순위 낮음 · 수집 비용 큼 · DBA 영역)"):
            st.code("\n".join(rec.stats_optional), language="sql")
            st.dataframe([{"문장": s, "근거": rec.stats_reasons.get(s, "")} for s in rec.stats_optional],
                         hide_index=True, width="stretch")
    if rec.partition_candidates:
        st.markdown("**파티션 추가 후보 (등급 C)**")
        for t, c in rec.partition_candidates:
            st.markdown(f"- `{t}` — 범위 조건 날짜 컬럼 `{c}` 로 RANGE_N 검토 (리포트의 DDL/마이그레이션 참고)")
    if rec.pi_candidates:
        st.markdown("**PI 검토 후보 (등급 C)**")
        for t, c in rec.pi_candidates:
            st.markdown(f"- `{t}` — 조인/재분배 키 `{c}`")
    if not (rec.stats or rec.stats_optional or rec.partition_candidates or rec.pi_candidates):
        st.caption("코드가 계산한 권고 후보가 없습니다.")


def _explain_tab(r: TuningReport) -> None:
    cols = st.columns(2)
    for col, (label, m) in zip(cols, [("원본", r.original), ("최종", r.best)]):
        with col:
            st.markdown(f"**{label}**")
            if m.explain_error:
                st.error(m.explain_error)
            if m.parsed is not None:
                st.code(m.parsed.summary(), language=None)
                with st.expander("EXPLAIN 원문"):
                    st.code(m.explain_text, language=None)
            elif not m.explain_error:
                st.caption("EXPLAIN 없음")


def _render_report(r: TuningReport) -> None:
    st.divider()
    if r.goal_met:
        how = f"시도 {r.best_attempt} 채택" if r.best_attempt else r.stop_reason
        st.success(f"🎯 목표 달성 · {how}")
    elif r.stop_reason == "진단만 수행":
        st.info("🔍 진단 결과")
    else:
        st.warning(f"목표 미달성 · {r.stop_reason}")
    if not r.verified:
        st.info("Teradata 에 연결되지 않아 재작성 결과를 EXPLAIN 으로 검증하지 못했습니다.")
    for note in r.original.notes:
        st.caption(f"ℹ️ {note}")
    if r.original.explain_error:
        st.error(f"원본 EXPLAIN 실패: {r.original.explain_error}")
    _metrics(r)
    if r.criteria and r.stop_reason != "진단만 수행":
        st.markdown(" · ".join(f"{'✅' if c.met else '❌'} {c.name} ({c.detail})" for c in r.criteria))
    tabs = st.tabs(["튜닝 포인트", "반복 과정", "최종 SQL", "권고 스크립트", "리포트", "EXPLAIN"])
    with tabs[0]:
        _findings_tab(r)
    with tabs[1]:
        _attempts_tab(r)
    with tabs[2]:
        _sql_tab(r)
    with tabs[3]:
        _recs_tab(r)
    with tabs[4]:
        st.markdown(r.report_md)
        st.download_button("💾 리포트 저장 (.md)", r.report_md, file_name="tuning_report.md", mime="text/markdown")
    with tabs[5]:
        _explain_tab(r)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def render_tuning() -> None:
    _init()
    toolbox = ss.agent.toolbox
    st.subheader("🔧 쿼리 튜닝")
    st.radio("튜닝 대상", SOURCES, key="tune_source", horizontal=True, label_visibility="collapsed")
    if ss.tune_source == SOURCES[1]:
        _dbql_panel()
    st.text_area("SQL", key="tune_sql", height=240, placeholder="튜닝할 SQL 을 붙여넣으세요 (SELECT, INSERT-SELECT 등)")
    if ss.tune_dbql:
        d = ss.tune_dbql
        ratios = dbql.dbql_ratios(d)
        st.checkbox(f"DBQL 지표 사용 · QueryID {d.get('QueryID')} · CPU {d.get('AMPCPUTime')}s · "
                    f"스큐 {ratios.get('cpu_skew', 0):.1f} · PJI {ratios.get('pji', 0):.1f} · UII {ratios.get('uii', 0):.2f}",
                    key="use_dbql")
    if ss.tune_source == SOURCES[2]:
        st.text_area("EXPLAIN 결과", key="tune_explain", height=200,
                     placeholder="Teradata Studio / BTEQ 에서 EXPLAIN 실행 결과를 붙여넣으세요")
    _goal_panel(allow_result_check=bool(toolbox.td and toolbox.allow_execute))

    has_sql = bool(ss.tune_sql.strip())
    c = st.columns([1, 1, 3])
    analyze = c[0].button("🔍 진단만", disabled=not has_sql, width="stretch")
    tune = c[1].button("🚀 목표 달성까지 튜닝", type="primary", disabled=not has_sql, width="stretch")
    mode = "EXPLAIN 검증" if toolbox.td else ("붙여넣은 EXPLAIN 으로 진단" if ss.tune_source == SOURCES[2]
                                             else "DB 미연결: SQL 패턴만 진단")
    c[2].caption(f"{mode} · 스킬 {len(toolbox.skills)}개 · 튜닝 포인트 {len(toolbox.skills.tuning_points())}개")
    if analyze or tune:
        _run(analyze_only=analyze)
    if ss.tune_report is not None:
        _render_report(ss.tune_report)


def render_knowledge() -> None:
    skills = ss.agent.toolbox.skills
    st.subheader("📚 튜닝 지식 (스킬)")
    st.caption(f"skills/ 폴더의 마크다운 {len(skills)}개 · 튜닝 포인트 {len(skills.tuning_points())}개. "
               "파일을 고친 뒤 사이드바 '스키마·지식 다시 읽기'로 반영됩니다.")
    q = st.text_input("검색", key="kb_search", placeholder="예) product join, 파티션, 스큐, NOT IN").strip().lower()
    by_cat: dict[str, list] = {}
    for s in skills.skills.values():
        by_cat.setdefault(s.category or "기타", []).append(s)
    for cat, items in by_cat.items():
        shown = [s for s in items if not q or q in (s.name + s.description + s.body).lower()]
        if not shown:
            continue
        st.markdown(f"#### {cat}")
        for s in shown:
            tps = s.tuning_points()
            with st.expander(f"{s.name} — {s.description}" + (f"  ({len(tps)} TP)" if tps else "")):
                st.markdown(s.body)
