"""Command line interface.

    tdagent chat                       대화형 (기본)
    tdagent ask "지난달 고객별 매출 TOP 10"
    tdagent ask "할인율 컬럼 추가해줘" --file q.sql
    tdagent analyze q.sql              쿼리 분석/개선
    tdagent check q.sql                LLM 없이 린트 (+ EXPLAIN)
    tdagent sync-schema --db SALES_DB  DBC 에서 스키마 카탈로그 생성
    tdagent doctor                     LLM / Teradata 연결 확인
"""

from __future__ import annotations

import argparse
import json
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

    args = p.parse_args(argv)
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
