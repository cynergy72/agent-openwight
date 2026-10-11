"""Tools the model can call, plus `validate()` which the agent loop also runs
on its own (the model's claim that a query is fine is never trusted)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .schema import SchemaStore
from .sql_lint import Issue, catalog_issues, explain_hints, lint_sql
from .teradata import TeradataClient, TeradataError


@dataclass
class Validation:
    issues: list[Issue] = field(default_factory=list)
    explain_ok: bool | None = None  # None = EXPLAIN not run
    explain_error: str = ""
    hints: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors and self.explain_ok is not False

    def render(self) -> str:
        lines = [f"결과: {'통과' if self.ok else '실패'} (오류 {len(self.errors) + (self.explain_ok is False)}, "
                 f"경고 {len(self.warnings)})"]
        lines += [f"- {i}" for i in self.errors + self.warnings]
        if self.explain_ok is True:
            lines.append("- EXPLAIN: 성공 (Teradata 가 문법/객체/권한을 확인함)")
        elif self.explain_ok is False:
            lines.append(f"- [error:explain] Teradata EXPLAIN 실패: {self.explain_error}")
        if self.hints:
            lines.append("플랜 힌트:")
            lines += [f"- {h}" for h in self.hints]
        return "\n".join(lines)


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


class Toolbox:
    def __init__(self, store: SchemaStore, td: TeradataClient | None = None,
                 allow_execute: bool = False, output_chars: int = 6000):
        self.store = store
        self.td = td
        self.allow_execute = allow_execute and td is not None
        self.output_chars = output_chars

    # ---------------------------------------------------------- specs
    def specs(self) -> list[dict]:
        specs = [
            _fn("search_schema", "Search tables and columns by keywords (Korean or English business terms, "
                "table or column names). Returns matching tables with their columns.",
                {"query": {"type": "string", "description": "keywords, e.g. '고객 매출 월별'"}}, ["query"]),
            _fn("describe_table", "Get full definition of one table: columns, types, primary index, comments.",
                {"table": {"type": "string", "description": "Database.Table"}}, ["table"]),
            _fn("validate_sql", "Check a Teradata SQL query for dialect errors and problems"
                + (" and run EXPLAIN on the real database" if self.td else "") + ".",
                {"sql": {"type": "string"}}, ["sql"]),
        ]
        if self.allow_execute:
            specs.append(_fn("run_query", "Run a read-only SELECT and return at most a few rows. "
                             "Use for checking code values or sanity checks only.",
                             {"sql": {"type": "string"}}, ["sql"]))
        return specs

    # ---------------------------------------------------------- dispatch
    def call(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"ERROR: 인자 JSON 을 해석할 수 없습니다 ({e}). 올바른 JSON 객체로 다시 호출하세요."
        handler = getattr(self, f"tool_{name}", None)
        if handler is None or (name == "run_query" and not self.allow_execute):
            return f"ERROR: 알 수 없는 도구 '{name}'. 사용 가능: {', '.join(s['function']['name'] for s in self.specs())}"
        try:
            out = handler(**args)
        except TypeError as e:
            return f"ERROR: 잘못된 인자: {e}"
        except TeradataError as e:
            return f"ERROR: {e}"
        if len(out) > self.output_chars:
            out = out[: self.output_chars] + "\n...(생략됨)"
        return out

    # ---------------------------------------------------------- tools
    def tool_search_schema(self, query: str) -> str:
        if not len(self.store):
            return ("스키마 카탈로그가 비어 있습니다. 사용자에게 테이블 이름을 물어보거나 "
                    "<TABLE_NAME> 같은 placeholder 를 쓰세요.")
        hits = self.store.search(query)
        if not hits:
            return f"'{query}' 와 일치하는 테이블이 없습니다. 다른 키워드(영문 약어, 동의어)로 다시 검색하세요."
        if len(hits) <= 3:
            return "\n\n".join(t.render() for t in hits)
        return "\n".join(t.summary() for t in hits) + "\n\n(자세한 정의는 describe_table 사용)"

    def tool_describe_table(self, table: str) -> str:
        t = self.store.find(table)
        if t is None and self.td is not None:
            t = self.td.describe(table)
            if t is not None:
                self.store.add(t)
        if t is None:
            return f"'{table}' 테이블을 찾을 수 없습니다. search_schema 로 검색하세요."
        return t.render()

    def tool_validate_sql(self, sql: str) -> str:
        return self.validate(sql).render()

    def tool_run_query(self, sql: str) -> str:
        cols, rows = self.td.run_select(sql)
        if not rows:
            return "결과 0행"
        lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
        return "\n".join(lines) + f"\n({len(rows)}행, 최대 {self.td.cfg.max_rows}행까지만 표시)"

    # ---------------------------------------------------------- validation
    def validate(self, sql: str) -> Validation:
        v = Validation(issues=lint_sql(sql))
        if self.td is not None:
            try:
                plan = self.td.explain(sql)
                v.explain_ok = True
                v.hints = explain_hints(plan)
            except TeradataError as e:
                if self.td.connected:
                    v.explain_ok = False
                    v.explain_error = str(e)
                else:  # DB unreachable: not the query's fault, fall back to the catalog
                    v.issues.append(Issue("warning", "explain_skipped", f"EXPLAIN 을 실행하지 못했습니다: {e}"))
                    v.issues.extend(catalog_issues(sql, self.store))
        else:
            v.issues.extend(catalog_issues(sql, self.store))
        return v
