"""Command line interface.

    tdagent chat                       대화형 (기본)
    tdagent ask "지난달 고객별 매출 TOP 10"
    tdagent ask "할인율 컬럼 추가해줘" --file q.sql
    tdagent analyze q.sql              쿼리 분석/개선
    tdagent check q.sql                LLM 없이 린트 (+ EXPLAIN)
    tdagent sync-schema --db SALES_DB  DBC 에서 스키마 카탈로그 생성
    tdagent doctor                     LLM / Teradata 연결 확인
    tdagent web                        로컬 웹 UI (브라우저)
    tdagent tune q.sql --time 30       튜닝 (목표: 예상 시간 30% 단축) 까지 반복
    tdagent tune q.sql --explain e.txt 붙여넣은 EXPLAIN 으로 진단 (DB 미연결)
    tdagent dbql --days 1 --by skew    DBQL 고부하 쿼리
    tdagent skills                     튜닝 포인트 카탈로그
    tdagent explain-check top50.txt    추출한 실제 EXPLAIN 들로 파서·탐지 점검
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .agent import AgentResult, build_agent
from .config import Config, load_config
from .schema import SchemaStore
from .teradata import TeradataClient, TeradataError
from .tools import Toolbox

console = Console()


def _event_printer(verbose: bool):
    def on_event(kind: str, data: dict) -> None:
        if kind == "tool_call":
            args = data["arguments"]
            try:
                args = ", ".join(f"{k}={v!r}"[:120] for k, v in json.loads(args).items())
            except Exception:
                pass
            console.print(f"[dim]→ {data['name']}({args})[/dim]")
        elif kind == "tool_result" and verbose:
            console.print(Panel(Text(data["output"][:2000]), title=data["name"], border_style="dim"))
        elif kind == "fix_round":
            console.print(f"[yellow]↻ 자동 검증 실패 → 재작성 요청 ({data['round']}회차)[/yellow]")
    return on_event


def _print_result(result: AgentResult) -> None:
    console.print()
    console.print(Markdown(result.answer))
    v = result.validation
    if v is not None:
        style = "green" if v.ok and not v.warnings else ("yellow" if v.ok else "red")
        console.print(Panel(Text(v.render()), title="자동 검증", border_style=style))
    console.print(f"[dim]({result.steps} steps)[/dim]")


def _with_sql(request: str, sql_path: str | None) -> str:
    if not sql_path:
        return request
    sql = Path(sql_path).read_text(encoding="utf-8")
    return f"{request}\n\n```sql\n{sql.strip()}\n```"


def _run_once(cfg: Config, message: str, verbose: bool) -> int:
    agent = build_agent(cfg, on_event=_event_printer(verbose))
    with console.status("생각 중..."):
        result = agent.run(message)
    _print_result(result)
    return 0 if result.validation is None or result.validation.ok else 1


def _read_input() -> str | None:
    try:
        line = console.input("[bold cyan]td>[/bold cyan] ")
    except (EOFError, KeyboardInterrupt):
        return None
    if line.strip() != '"""':
        return line
    lines = []
    while True:
        try:
            nxt = console.input("[dim]...[/dim] ")
        except (EOFError, KeyboardInterrupt):
            return None
        if nxt.strip() == '"""':
            return "\n".join(lines)
        lines.append(nxt)


CHAT_HELP = """\
요청을 입력하세요. 예) 최근 3개월 등급별 고객 수와 평균 매출
  \"\"\"        여러 줄 입력 시작/끝 (SQL 붙여넣기)
  /file 경로 [요청]  SQL 파일을 첨부해서 요청 (예: /file q.sql 성능 분석해줘)
  /sql       마지막으로 생성된 SQL 만 출력
  /reset     대화 기록 초기화
  /exit      종료"""


def cmd_chat(cfg: Config, verbose: bool) -> int:
    agent = build_agent(cfg, on_event=_event_printer(verbose))
    console.print(Panel(CHAT_HELP, title=f"Teradata SQL Agent · {cfg.llm.model}"
                        f" · 테이블 {len(agent.toolbox.store)}개"
                        f"{' · EXPLAIN 사용' if agent.toolbox.td else ''}"))
    last_sql = ""
    while True:
        text = _read_input()
        if text is None or text.strip() in ("/exit", "/quit"):
            return 0
        text = text.strip()
        if not text:
            continue
        if text == "/reset":
            agent.reset()
            console.print("[dim]대화 기록을 초기화했습니다.[/dim]")
            continue
        if text == "/sql":
            console.print(Text(last_sql) if last_sql else "[dim](없음)[/dim]")
            continue
        if text.startswith("/file"):
            parts = text.split(maxsplit=2)
            if len(parts) < 2 or not Path(parts[1]).exists():
                console.print("[red]사용법: /file 경로 [요청][/red]")
                continue
            text = _with_sql(parts[2] if len(parts) > 2 else "이 쿼리를 분석해줘.", parts[1])
        try:
            with console.status("생각 중..."):
                result = agent.run(text)
        except KeyboardInterrupt:
            console.print("[yellow]중단됨[/yellow]")
            continue
        except Exception as e:  # keep the session alive on LLM/network errors
            console.print(Text(f"오류: {e}", style="red"))
            continue
        _print_result(result)
        last_sql = result.sql or last_sql


def cmd_check(cfg: Config, path: str) -> int:
    sql = Path(path).read_text(encoding="utf-8")
    store = SchemaStore.load_dir(cfg.path(cfg.agent.schema_dir))
    td = TeradataClient(cfg.teradata) if cfg.teradata.enabled else None
    v = Toolbox(store, td).validate(sql)
    console.print(Panel(Text(v.render()), title=path, border_style="green" if v.ok else "red"))
    return 0 if v.ok else 1


def cmd_sync_schema(cfg: Config, databases: list[str] | None, out: str | None) -> int:
    dbs = databases or cfg.teradata.databases
    if not dbs:
        console.print("[red]--db 또는 config 의 teradata.databases 를 지정하세요.[/red]")
        return 2
    td = TeradataClient(cfg.teradata)
    try:
        with console.status(f"{', '.join(dbs)} 메타데이터 수집 중..."):
            store = td.sync_schema(dbs)
    except TeradataError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    finally:
        td.close()
    target = Path(out) if out else cfg.path(cfg.agent.schema_dir) / "catalog.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    store.save_json(target)
    console.print(f"[green]{len(store)}개 테이블/뷰를 {target} 에 저장했습니다.[/green]")
    return 0


def cmd_web(config_path: str, port: int, no_browser: bool) -> int:
    try:
        import streamlit  # noqa: F401
    except ImportError:
        console.print("[red]웹 UI 에는 streamlit 이 필요합니다: pip install -e \".[web]\"[/red]")
        return 2
    app = Path(__file__).with_name("webapp.py")
    env = dict(os.environ, TDAGENT_CONFIG=str(Path(config_path).resolve()))
    cmd = [sys.executable, "-m", "streamlit", "run", str(app),
           "--server.address", "127.0.0.1",  # this PC only: the app holds DB credentials
           "--server.port", str(port),
           "--server.headless", "true" if no_browser else "false",
           "--browser.gatherUsageStats", "false",
           "--client.toolbarMode", "minimal"]
    console.print(f"[green]http://127.0.0.1:{port} 에서 실행합니다. 종료: Ctrl+C[/green]")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


def cmd_tune(cfg: Config, args) -> int:
    from .tuning.findings import SEVERITY_ICON
    from .tuning.goal import Goal
    from .tuning.session import TuningSession, final_metrics, metrics_table

    sql = Path(args.file).read_text(encoding="utf-8")
    explain_text = Path(args.explain).read_text(encoding="utf-8") if args.explain else None
    agent = build_agent(cfg)
    td = agent.toolbox.td

    def on_event(kind: str, data: dict) -> None:
        if kind == "status":
            console.print(f"[dim]… {data['text']}[/dim]")
        elif kind == "baseline":
            c = data["measurement"].counts
            console.print(f"원본: 🔴 {c['critical']} 🟠 {c['warning']} 🔵 {c['info']}")
        elif kind == "attempt":
            a = data["attempt"]
            console.print(Text(f"시도 {a.n}: [{a.status}] {a.reason}"))

    goal = Goal(no_product_join=not args.allow_product_join, no_partition_miss=True,
                no_large_duplication=args.no_dup, no_critical=args.no_critical,
                time_reduction_pct=args.time, note=args.note)
    session = TuningSession(agent.model, agent.toolbox.store, agent.toolbox.skills, cfg.tuning, td=td,
                            on_event=on_event)
    r = session.run(sql, goal, explain_text=explain_text, max_iterations=args.max_iter,
                    analyze_only=args.analyze_only, result_check=args.result_check)
    console.print()
    for f in r.original.findings:
        console.print(Text(f"{SEVERITY_ICON[f.severity]} {f.code} {f.title} ({', '.join(f.tuning_points)}): {f.detail}"))
    console.print(Markdown(metrics_table(r.original.metrics, final_metrics(r))))
    unverified = next((a for a in r.attempts if a.status == "unverified"), None)
    if unverified:
        console.print("[yellow]EXPLAIN 으로 검증하지 못한 재작성 제안:[/yellow]")
        console.print(Text(unverified.sql))
    console.print(Markdown(r.report_md))
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(r.report_md, encoding="utf-8")
        if r.best_attempt:
            (out / "tuned.sql").write_text(r.best.sql + "\n", encoding="utf-8")
        elif unverified:
            (out / "suggested_unverified.sql").write_text(unverified.sql + "\n", encoding="utf-8")
        if r.recommendations.stats:
            (out / "collect_stats.sql").write_text("\n".join(r.recommendations.stats) + "\n", encoding="utf-8")
        console.print(f"[green]{out}/ 에 저장했습니다.[/green]")
    style = "green" if r.goal_met else "yellow"
    console.print(f"[{style}]{'목표 달성' if r.goal_met else '목표 미달성'} · {r.stop_reason}[/{style}]")
    return 0 if r.goal_met or args.analyze_only else 1


def cmd_dbql(cfg: Config, args) -> int:
    import datetime as dt

    from .tuning import dbql

    if not cfg.teradata.enabled:
        console.print("[red]Teradata 연결이 필요합니다 (teradata.enabled = true).[/red]")
        return 2
    td = TeradataClient(cfg.teradata)
    end = dt.datetime.now()
    f = dbql.DBQLFilter(start=(end - dt.timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S"),
                        end=end.strftime("%Y-%m-%d %H:%M:%S"), user=args.user or "",
                        min_cpu=args.min_cpu, order=args.by, top=args.top)
    try:
        rows = dbql.top_queries(td, cfg.dbql, f)
        for r in rows:
            console.print(Text(f"{r['QueryID']} {r['UserName']:<12} CPU {r['AMPCPUTime']:>10} "
                               f"스큐 {r.get('cpu_skew', '-'):>5} PJI {r.get('pji', '-'):>6} UII {r.get('uii', '-'):>6} "
                               f"| {str(r.get('SqlPreview') or '')[:70]}"))
        if args.save and rows:
            r = rows[0] if args.save == "top" else next((x for x in rows if str(x["QueryID"]) == args.save), None)
            if r is None:
                console.print("[red]해당 QueryID 가 목록에 없습니다.[/red]")
                return 1
            path = Path(f"query_{r['QueryID']}.sql")
            path.write_text(dbql.full_sql(td, cfg.dbql, r["ProcID"], r["QueryID"]) + "\n", encoding="utf-8")
            console.print(f"[green]{path} 저장 → tdagent tune {path}[/green]")
    except TeradataError as e:
        console.print(Text(str(e), style="red"))
        return 1
    finally:
        td.close()
    return 0


def cmd_explain_check(cfg: Config, args) -> int:
    from .tuning.corpus import check, load_extract
    from .tuning.findings import SEVERITY_ICON, from_explain, sort_findings
    from .tuning.sqlcheck import analyze_sql

    plans = load_extract(args.file)
    cov = check(plans)
    store = SchemaStore.load_dir(cfg.path(cfg.agent.schema_dir))
    severity_count: dict[str, int] = {}
    for p in plans:
        if p.parsed is None:
            if args.verbose_plans:
                console.print(Text(f"[{p.n}] EXPLAIN 실패: {p.error[:160]}", style="yellow"))
            continue
        findings = sort_findings(from_explain(p.parsed, cfg.tuning) + analyze_sql(p.sql, store))
        for f in findings:
            severity_count[f.code] = severity_count.get(f.code, 0) + 1
        if args.verbose_plans:
            t = p.parsed.total_time
            console.print(Text(f"[{p.n}] 단계 {len(p.parsed.steps)} · 예상 {t if t is not None else '-'}s "
                               f"({p.parsed.total_time_source or '없음'}) · {p.meta.get('User', '')}", style="bold"))
            console.print(Text(p.parsed.summary()))
            for f in findings:
                if f.severity != "info":
                    console.print(Text(f"  {SEVERITY_ICON[f.severity]} {f.code}: {f.detail[:140]}"))
    console.print(f"계획 {cov.plans}개 파싱, EXPLAIN 실패 {cov.failed}개")
    for k, v in sorted(cov.counts.items()):
        console.print(Text(f"  {k:<40} {v}"))
    console.print("탐지 결과:")
    for k, v in sorted(severity_count.items(), key=lambda x: -x[1]):
        console.print(Text(f"  {k:<28} {v}"))
    if cov.problems:
        console.print(f"[yellow]파서가 놓친 부분 {len(cov.problems)}건:[/yellow]")
        for prob in cov.problems[:50]:
            console.print(Text("  " + prob))
        return 1
    console.print("[green]놓친 문구 없음[/green]")
    return 0


def cmd_skills(cfg: Config, query: str) -> int:
    from .skills import SkillLibrary

    lib = SkillLibrary.load_dir(cfg.path(cfg.tuning.skills_dir))
    for tp in lib.tuning_points().values():
        line = f"{tp.id:<12} {tp.title}  [{tp.skill}]  {tp.signal[:80]}"
        if not query or query.lower() in (line + tp.text).lower():
            console.print(Text(line))
    return 0


def cmd_doctor(cfg: Config) -> int:
    from .llm import OpenAICompatModel

    ok = True
    console.print(f"LLM: {cfg.llm.base_url} / {cfg.llm.model}")
    try:
        reply = OpenAICompatModel(cfg.llm).chat(
            [{"role": "user", "content": "Reply with exactly: OK"}], [])
        console.print(f"  [green]응답: {reply.content[:80]!r}[/green]")
    except Exception as e:
        ok = False
        console.print(f"  [red]실패: {e}[/red]")
    store = SchemaStore.load_dir(cfg.path(cfg.agent.schema_dir))
    console.print(f"스키마 카탈로그: {len(store)}개 ({cfg.path(cfg.agent.schema_dir)})")
    if cfg.teradata.enabled:
        td = TeradataClient(cfg.teradata)
        try:
            td.explain("SELECT 1")
            console.print(f"Teradata: [green]{cfg.teradata.host} 접속 OK[/green]")
        except TeradataError as e:
            ok = False
            console.print(f"Teradata: [red]{e}[/red]")
        finally:
            td.close()
    else:
        console.print("Teradata: 비활성 (정적 검사 + 카탈로그 검사만 사용)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tdagent", description="Teradata SQL agent (local open-weight LLM)")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true", help="도구 결과까지 출력")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat")
    a = sub.add_parser("ask")
    a.add_argument("request")
    a.add_argument("-f", "--file", help="요청에 첨부할 SQL 파일")
    an = sub.add_parser("analyze")
    an.add_argument("file")
    an.add_argument("--note", default="", help="추가 요청 (예: '실행 시간이 30분 걸림')")
    ck = sub.add_parser("check")
    ck.add_argument("file")
    ss = sub.add_parser("sync-schema")
    ss.add_argument("--db", nargs="+")
    ss.add_argument("--out")
    sub.add_parser("doctor")
    t = sub.add_parser("tune", help="튜닝 목표 달성까지 재작성 반복")
    t.add_argument("file")
    t.add_argument("--explain", help="EXPLAIN 결과 파일 (DB 미연결 진단)")
    t.add_argument("--time", type=float, default=0, help="예상 시간 단축 목표 %%")
    t.add_argument("--no-dup", action="store_true", help="대용량 복제 제거도 목표에 포함")
    t.add_argument("--no-critical", action="store_true", help="critical 0건을 목표에 포함")
    t.add_argument("--allow-product-join", action="store_true", help="product join 제거를 목표에서 제외")
    t.add_argument("--note", default="", help="추가 목표 (모델에게 전달)")
    t.add_argument("--max-iter", type=int)
    t.add_argument("--analyze-only", action="store_true", help="재작성 없이 진단만")
    t.add_argument("--result-check", action="store_true", help="결과 동일성 검증 (쿼리 실행 필요)")
    t.add_argument("--out", help="report.md / tuned.sql / collect_stats.sql 저장 폴더")
    d = sub.add_parser("dbql", help="DBQL 고부하 쿼리 조회")
    d.add_argument("--days", type=float, default=1)
    d.add_argument("--user")
    d.add_argument("--min-cpu", type=float, default=10)
    d.add_argument("--by", choices=["cpu", "io", "impact", "skew", "spool"], default="cpu")
    d.add_argument("--top", type=int, default=20)
    d.add_argument("--save", help="QueryID 또는 'top' - 전체 SQL 을 파일로 저장")
    ec = sub.add_parser("explain-check", help="실제 EXPLAIN 추출본으로 파서·탐지 점검")
    ec.add_argument("file")
    ec.add_argument("--plans", dest="verbose_plans", action="store_true", help="계획별 요약과 탐지 결과 출력")
    k = sub.add_parser("skills", help="튜닝 포인트 카탈로그")
    k.add_argument("query", nargs="?", default="")
    w = sub.add_parser("web")
    w.add_argument("--port", type=int, default=8501)
    w.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")

    args = p.parse_args(argv)
    if args.cmd == "web":
        return cmd_web(args.config, args.port, args.no_browser)
    cfg = load_config(args.config)

    if args.cmd in (None, "chat"):
        return cmd_chat(cfg, args.verbose)
    if args.cmd == "ask":
        return _run_once(cfg, _with_sql(args.request, args.file), args.verbose)
    if args.cmd == "analyze":
        request = "이 쿼리가 무엇을 하는지 설명하고, 버그와 성능 문제를 찾아 개선된 쿼리를 제안해줘. " + args.note
        return _run_once(cfg, _with_sql(request.strip(), args.file), args.verbose)
    if args.cmd == "check":
        return cmd_check(cfg, args.file)
    if args.cmd == "sync-schema":
        return cmd_sync_schema(cfg, args.db, args.out)
    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "tune":
        return cmd_tune(cfg, args)
    if args.cmd == "dbql":
        return cmd_dbql(cfg, args)
    if args.cmd == "explain-check":
        return cmd_explain_check(cfg, args)
    if args.cmd == "skills":
        return cmd_skills(cfg, args.query)
    return 2


if __name__ == "__main__":
    sys.exit(main())
