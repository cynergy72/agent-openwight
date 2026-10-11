"""Local web UI (Streamlit). Start with `tdagent web`.

Left: chat with the agent. Right: the "current SQL" - the target of the next
modify/analyze request - with copy, edit, validate, EXPLAIN, diff and run.
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # executed as a script by `streamlit run`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st

from tdagent import credentials
from tdagent.agent import Agent, build_agent
from tdagent.config import TeradataConfig, load_config, save_teradata_settings
from tdagent.sql_lint import explain_hints, normalize_sql
from tdagent.teradata import TeradataClient, TeradataError

st.set_page_config(page_title="Teradata SQL Agent", page_icon="🗄️", layout="wide")

ss = st.session_state


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def _init_state() -> None:
    if "agent" not in ss:
        ss.cfg = load_config(os.environ.get("TDAGENT_CONFIG", "config.toml"))
        ss.agent = build_agent(ss.cfg)
    defaults = {
        "messages": [],       # {"role", "content", "steps", "validation", "attached"}
        "current_sql": "",
        "prev_sql": "",
        "validation": None,   # rendered text of the latest validation
        "validation_ok": None,
        "explain": None,      # (plan text, hints) or error str
        "run_result": None,   # (cols, rows) or error str
        "edit_mode": False,
        "sql_editor": "",
        "attach_sql": True,
    }
    for k, v in defaults.items():
        ss.setdefault(k, v)
    _init_conn_form()


def _rebuild_agent(keep_history: bool = True) -> None:
    """Rebuild after settings/schema changes, keeping the model and chat."""
    old: Agent = ss.agent
    if old.toolbox.td:
        old.toolbox.td.close()
    new = build_agent(ss.cfg, model=old.model)
    if keep_history:
        new.history = old.history
    ss.agent = new


def _set_sql(sql: str) -> None:
    """Replace the current SQL (keeps the previous one for the diff tab)."""
    if normalize_sql(sql) != normalize_sql(ss.current_sql):
        ss.prev_sql = ss.current_sql
    ss.current_sql = sql
    ss.sql_editor = sql
    ss.explain = None
    ss.run_result = None


def _sync_editor() -> None:
    ss.current_sql = ss.sql_editor
    ss.validation = None
    ss.explain = None


def _enter_edit() -> None:
    if ss.edit_mode:
        ss.sql_editor = ss.current_sql


def _new_query() -> None:
    ss.agent.reset()
    ss.messages = []
    ss.current_sql = ss.prev_sql = ss.sql_editor = ""
    ss.validation = ss.validation_ok = ss.explain = ss.run_result = None


def _validate() -> None:
    v = ss.agent.toolbox.validate(ss.current_sql)
    ss.validation, ss.validation_ok = v.render(), v.ok


def _explain() -> None:
    try:
        plan = ss.agent.toolbox.td.explain(ss.current_sql)
        ss.explain = (plan, explain_hints(plan))
    except TeradataError as e:
        ss.explain = str(e)


def _run() -> None:
    try:
        ss.run_result = ss.agent.toolbox.td.run_select(ss.current_sql)
    except TeradataError as e:
        ss.run_result = str(e)


# --------------------------------------------------------------------------- #
# Agent call
# --------------------------------------------------------------------------- #

def _describe_event(kind: str, data: dict) -> str | None:
    if kind == "tool_call":
        try:
            args = ", ".join(f"{k}={v!r}"[:100] for k, v in json.loads(data["arguments"]).items())
        except Exception:
            args = data["arguments"][:100]
        return f"🔧 `{data['name']}`({args})"
    if kind == "fix_round":
        return f"↻ 자동 검증 실패 → 재작성 요청 ({data['round']}회차)"
    return None


def _ask(agent: Agent, prompt: str) -> None:
    attached = bool(ss.attach_sql and ss.current_sql.strip()
                    and normalize_sql(ss.current_sql) not in normalize_sql(prompt))
    message = prompt + (f"\n\n현재 SQL:\n```sql\n{ss.current_sql.strip()}\n```" if attached else "")
    ss.messages.append({"role": "user", "content": prompt, "attached": attached})
    with st.chat_message("user"):
        st.markdown(prompt)
        if attached:
            st.caption("📎 현재 SQL 첨부됨")

    steps: list[str] = []
    with st.chat_message("assistant"):
        status = st.status("에이전트 작업 중...", expanded=True)

        def on_event(kind: str, data: dict) -> None:
            line = _describe_event(kind, data)
            if line:
                steps.append(line)
                status.markdown(line)

        agent.on_event = on_event
        try:
            result = agent.run(message)
        except Exception as e:  # LLM server down, timeout ...
            status.update(label="오류", state="error")
            st.error(f"LLM 호출 실패: {e}")
            ss.messages.append({"role": "assistant", "content": f"⚠️ LLM 호출 실패: {e}", "steps": steps})
            return
        finally:
            agent.on_event = lambda kind, data: None

        status.update(label=f"완료 · {result.steps} steps", state="complete", expanded=False)
        st.markdown(result.answer)

    v = result.validation
    if result.sql:
        _set_sql(result.sql)
        if ss.edit_mode:
            ss.sql_editor = result.sql
    if v is not None:
        ss.validation, ss.validation_ok = v.render(), v.ok
    ss.messages.append({"role": "assistant", "content": result.answer, "steps": steps,
                        "validation": None if v is None else ("통과" if v.ok else "실패")})


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

def _sidebar() -> None:
    cfg, toolbox = ss.cfg, ss.agent.toolbox
    with st.sidebar:
        st.header("Teradata SQL Agent")
        st.markdown(f"**모델** `{cfg.llm.model}`  \n`{cfg.llm.base_url}`")
        st.markdown(f"**스키마** 테이블 {len(toolbox.store)}개")
        if toolbox.td:
            st.markdown(f"**Teradata** `{cfg.teradata.host}`  \n"
                        f"EXPLAIN 사용 · 실행 {'허용' if toolbox.allow_execute else '안 함'}")
        else:
            st.markdown("**Teradata** 미연결 (정적 검사 + 카탈로그 검사)")
        st.divider()
        st.button("🆕 새 쿼리 (대화 초기화)", on_click=_new_query, width="stretch")
        if st.button("🔁 스키마·지식 다시 읽기", width="stretch",
                     help="schema/, knowledge/ 폴더를 수정한 뒤 누르세요. 대화도 초기화됩니다."):
            _rebuild_agent(keep_history=False)
            _new_query()
            st.rerun()
        _connection_panel()
        st.caption("요청 입력창에서 Shift+Enter 로 줄바꿈, SQL 을 그대로 붙여넣어도 됩니다.")


# --------------------------------------------------------------------------- #
# Teradata connection settings
# --------------------------------------------------------------------------- #

LOGMECHS = ["TD2", "LDAP", "TDNEGO", "KRB5"]


def _init_conn_form() -> None:
    if "conn_host" in ss:
        return
    td = ss.cfg.teradata
    ss.conn_enabled = td.enabled
    ss.conn_host = td.host
    ss.conn_user = td.user
    ss.conn_password = ""  # never pre-filled; empty means "keep the current one"
    ss.conn_save_pw = True
    ss.conn_logmech = td.logmech or "TD2"
    ss.conn_databases = ", ".join(td.databases)
    ss.conn_allow_execute = td.allow_execute
    ss.conn_max_rows = td.max_rows
    ss.conn_msg = None
    ss.conn_saved_cache = {}


def _has_saved_password(host: str, user: str) -> bool:
    key = (host, user)
    if key not in ss.conn_saved_cache:  # keyring lookups can be slow / prompt; ask once
        ss.conn_saved_cache[key] = credentials.get_password(host, user) is not None
    return ss.conn_saved_cache[key]


def _form_config() -> TeradataConfig:
    """TeradataConfig from the form. An empty password field keeps the current
    password (session or stored) for the same host/user."""
    cur = ss.cfg.teradata
    host, user = ss.conn_host.strip(), ss.conn_user.strip()
    password = ss.conn_password
    if not password:
        same = (host, user) == (cur.host, cur.user)
        password = (cur.password if same else "") or credentials.get_password(host, user) or ""
    return TeradataConfig(
        enabled=ss.conn_enabled, host=host, user=user, password=password,
        logmech=ss.conn_logmech,
        databases=[d.strip() for d in ss.conn_databases.split(",") if d.strip()],
        allow_execute=ss.conn_allow_execute, max_rows=int(ss.conn_max_rows),
    )


def _test_connection() -> None:
    td = _form_config()
    if not (td.host and td.user and td.password):
        ss.conn_msg = ("error", "Host, User, 비밀번호가 필요합니다.")
        return
    client = TeradataClient(td)
    try:
        ss.conn_msg = ("success", f"접속 성공 · Teradata {client.ping()}"
                       + ("" if ss.conn_password == "" else " · '저장'을 눌러야 적용됩니다"))
    except TeradataError as e:
        ss.conn_msg = ("error", str(e))
    finally:
        client.close()


def _save_connection() -> None:
    td = _form_config()
    entered = ss.conn_password
    notes, level = [], "success"
    if ss.conn_save_pw:
        if entered:
            ok, err = credentials.set_password(td.host, td.user, entered)
            if ok:
                notes.append("비밀번호를 OS 자격 증명 저장소에 저장했습니다.")
            else:
                level = "warning"
                notes.append(f"비밀번호를 저장하지 못했습니다 ({err}). 이번 실행 동안만 기억합니다.")
    elif _has_saved_password(td.host, td.user):
        credentials.delete_password(td.host, td.user)
        notes.append("저장된 비밀번호를 삭제했습니다. 이번 실행 동안만 기억합니다.")

    try:
        save_teradata_settings(ss.cfg.source, td)
        notes.insert(0, f"{ss.cfg.source.name} 에 저장했습니다 (비밀번호 제외).")
    except Exception as e:
        level = "error"
        notes.insert(0, f"설정 파일 저장 실패: {e}")
    ss.cfg.teradata = td
    ss.conn_saved_cache = {}
    ss.conn_password = ""
    _rebuild_agent(keep_history=True)
    ss.conn_msg = (level, " ".join(notes))


def _sync_schema() -> None:
    td, dbs = ss.agent.toolbox.td, ss.cfg.teradata.databases
    try:
        with st.spinner(f"{', '.join(dbs)} 메타데이터를 가져오는 중..."):
            store = td.sync_schema(dbs)
        target = ss.cfg.path(ss.cfg.agent.schema_dir) / "catalog.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store.save_json(target)
        _rebuild_agent(keep_history=True)
        ss.conn_msg = ("success", f"테이블/뷰 {len(store)}개를 {target.name} 에 저장했습니다.")
    except TeradataError as e:
        ss.conn_msg = ("error", f"스키마 가져오기 실패: {e}")


def _connection_panel() -> None:
    td = ss.cfg.teradata
    with st.expander("🔌 Teradata 접속 설정", expanded=not td.host):
        if ss.conn_msg:
            level, text = ss.conn_msg
            getattr(st, level)(text)
        st.toggle("Teradata 사용 (EXPLAIN 검증)", key="conn_enabled")
        st.text_input("Host", key="conn_host", placeholder="tdprod.example.com")
        st.text_input("User", key="conn_user")

        host, user = ss.conn_host.strip(), ss.conn_user.strip()
        if _has_saved_password(host, user):
            hint = "저장됨 · 변경 시에만 입력"
        elif td.password and (host, user) == (td.host, td.user):
            hint = "이번 실행만 기억 · 변경 시 입력"
        else:
            hint = ""
        st.text_input("Password", key="conn_password", type="password", placeholder=hint)
        st.checkbox("비밀번호 저장 (OS 자격 증명 저장소)", key="conn_save_pw",
                    help="Windows 자격 증명 관리자 / macOS 키체인에 저장합니다. 설정 파일에는 저장하지 않습니다. "
                         "체크를 해제하고 저장하면 저장된 비밀번호를 삭제합니다.")

        options = LOGMECHS if ss.conn_logmech in LOGMECHS else [ss.conn_logmech, *LOGMECHS]
        st.selectbox("인증 방식 (logmech)", options, key="conn_logmech")
        st.text_input("데이터베이스 (쉼표로 구분)", key="conn_databases",
                      help="스키마 가져오기 대상", placeholder="SALES_DB, CUST_DB")
        st.checkbox("SELECT 실행 허용 (읽기 전용)", key="conn_allow_execute")
        st.number_input("실행 시 최대 행 수", min_value=1, max_value=10000, key="conn_max_rows")

        c = st.columns(2)
        c[0].button("연결 테스트", on_click=_test_connection, width="stretch")
        c[1].button("저장", on_click=_save_connection, type="primary", width="stretch")
        if st.button("📥 스키마 가져오기", width="stretch",
                     disabled=not (ss.agent.toolbox.td and td.databases),
                     help="저장된 접속 정보로 DBC 에서 테이블·컬럼·코멘트·PI 를 가져와 schema/catalog.json 에 저장"):
            _sync_schema()
            st.rerun()


def _chat_column() -> None:
    history = st.container(height=640, border=False)
    with history:
        if not ss.messages:
            st.info("예) 지난달 채널별 순매출과 거래 건수\n\n"
                    "예) (SQL 붙여넣고) 이 쿼리 왜 느린지 분석해줘\n\n"
                    "예) 고객명 컬럼 추가하고 VIP 등급만 보여줘  ← 오른쪽 '현재 SQL' 을 수정")
        for m in ss.messages:
            with st.chat_message(m["role"]):
                if m.get("steps"):
                    with st.expander(f"작업 과정 ({len(m['steps'])})"):
                        st.markdown("\n\n".join(m["steps"]))
                st.markdown(m["content"])
                if m.get("attached"):
                    st.caption("📎 현재 SQL 첨부됨")
                if m.get("validation"):
                    st.caption(f"자동 검증: {m['validation']}")

    if ss.current_sql.strip():
        st.toggle("현재 SQL 을 요청에 포함 (수정/분석할 때)", key="attach_sql")
    prompt = st.chat_input("무엇을 도와드릴까요? SQL 을 붙여넣어도 됩니다")
    if prompt:
        with history:
            _ask(ss.agent, prompt)


def _sql_column() -> None:
    toolbox = ss.agent.toolbox
    head = st.columns([3, 2])
    head[0].subheader("현재 SQL")
    head[1].toggle("✏️ 직접 편집", key="edit_mode", on_change=_enter_edit)

    if ss.edit_mode:
        st.text_area("SQL", key="sql_editor", height=320, on_change=_sync_editor,
                     label_visibility="collapsed",
                     placeholder="SQL 을 붙여넣거나 직접 작성하세요. Ctrl+Enter 로 반영됩니다.")
    elif ss.current_sql.strip():
        st.code(ss.current_sql, language="sql", line_numbers=True)  # has a copy button
    else:
        st.caption("아직 SQL 이 없습니다. 왼쪽에서 요청하거나 '직접 편집'으로 붙여넣으세요.")

    has_sql = bool(ss.current_sql.strip())
    b = st.columns(4)
    b[0].button("✅ 검증", on_click=_validate, disabled=not has_sql, width="stretch")
    b[1].button("🔍 EXPLAIN", on_click=_explain, disabled=not (has_sql and toolbox.td),
                width="stretch", help=None if toolbox.td else "config 에서 teradata.enabled = true 필요")
    b[2].button("▶ 실행", on_click=_run, disabled=not (has_sql and toolbox.allow_execute),
                width="stretch",
                help=None if toolbox.allow_execute else "config 에서 teradata.allow_execute = true 필요")
    b[3].download_button("💾 .sql", data=ss.current_sql, file_name="query.sql", mime="text/plain",
                         disabled=not has_sql, width="stretch")

    tabs = st.tabs(["검증 결과", "변경 비교", "EXPLAIN", "실행 결과"])
    with tabs[0]:
        if ss.validation:
            (st.success if ss.validation_ok else st.error)("통과" if ss.validation_ok else "실패")
            st.code(ss.validation, language=None)
        else:
            st.caption("'검증'을 누르거나 에이전트가 SQL 을 만들면 결과가 표시됩니다.")
    with tabs[1]:
        if ss.prev_sql.strip() and has_sql:
            diff = difflib.unified_diff(ss.prev_sql.splitlines(), ss.current_sql.splitlines(),
                                        "이전", "현재", lineterm="")
            st.code("\n".join(diff) or "(변경 없음)", language="diff")
        else:
            st.caption("SQL 이 수정되면 이전 버전과의 차이가 표시됩니다.")
    with tabs[2]:
        if isinstance(ss.explain, str):
            st.error(ss.explain)
        elif ss.explain:
            plan, hints = ss.explain
            for h in hints:
                st.markdown(f"- {h}")
            with st.expander("EXPLAIN 원문"):
                st.code(plan, language=None)
        else:
            st.caption("Teradata 에 연결되어 있으면 'EXPLAIN'으로 실행 계획을 볼 수 있습니다.")
    with tabs[3]:
        if isinstance(ss.run_result, str):
            st.error(ss.run_result)
        elif ss.run_result:
            cols, rows = ss.run_result
            st.dataframe([dict(zip(cols, r)) for r in rows], width="stretch")
            st.caption(f"{len(rows)}행 (최대 {ss.cfg.teradata.max_rows}행)")
        else:
            st.caption("실행이 허용된 경우 읽기 전용 SELECT 결과를 미리 볼 수 있습니다.")


def main() -> None:
    _init_state()
    _sidebar()
    left, right = st.columns([1, 1], gap="large")
    with left:
        _chat_column()
    with right:
        _sql_column()


main()
