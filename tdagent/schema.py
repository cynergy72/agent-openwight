"""Local schema catalog.

Small models cannot hold a whole warehouse in context, so the agent searches
this catalog with a tool and only sees the tables it asks for.

Sources (both in `schema_dir`):
  * *.json  - written by `tdagent sync-schema` from DBC views
  * *.sql   - hand-written / exported DDL (CREATE TABLE/VIEW, COMMENT ON)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: bool = True
    comment: str = ""


@dataclass
class Table:
    database: str
    name: str
    kind: str = "T"  # T=table, V=view
    comment: str = ""
    columns: list[Column] = field(default_factory=list)
    primary_index: list[str] = field(default_factory=list)
    partitioned_by: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.database}.{self.name}" if self.database else self.name

    def render(self) -> str:
        kind = "VIEW" if self.kind == "V" else "TABLE"
        lines = [f"{self.full_name} ({kind})" + (f" -- {self.comment}" if self.comment else "")]
        if self.primary_index:
            lines.append(f"  PRIMARY INDEX ({', '.join(self.primary_index)})")
        if self.partitioned_by:
            lines.append(f"  PARTITION BY {self.partitioned_by}")
        for c in self.columns:
            null = "" if c.nullable else " NOT NULL"
            note = f" -- {c.comment}" if c.comment else ""
            lines.append(f"  {c.name} {c.type or '?'}{null}{note}")
        return "\n".join(lines)

    def summary(self) -> str:
        cols = ", ".join(c.name for c in self.columns[:12])
        more = f" 외 {len(self.columns) - 12}개" if len(self.columns) > 12 else ""
        comment = f" -- {self.comment}" if self.comment else ""
        return f"{self.full_name}{comment}\n    컬럼: {cols}{more}"


_TOKEN = re.compile(r"[0-9A-Za-z_가-힣]+")
_HANGUL = re.compile(r"[가-힣]")


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in _TOKEN.findall(text.lower()):
        if len(t) < 2:
            continue
        out.add(t)
        # crude particle stripping for Korean: "매출을" -> "매출"
        if _HANGUL.search(t) and len(t) >= 3:
            out.add(t[:-1])
        if "_" in t:
            out.update(p for p in t.split("_") if len(p) >= 2)
    return out


class SchemaStore:
    def __init__(self, tables: list[Table] | None = None):
        self.tables: dict[str, Table] = {}
        for t in tables or []:
            self.add(t)

    # ------------------------------------------------------------------ io
    def add(self, table: Table) -> None:
        self.tables[table.full_name.lower()] = table

    @classmethod
    def load_dir(cls, directory: str | Path) -> "SchemaStore":
        store = cls()
        d = Path(directory)
        if not d.exists():
            return store
        for p in sorted(d.glob("*.json")):
            store.load_json(p)
        for p in sorted(d.glob("*.sql")):
            store.load_ddl(p.read_text(encoding="utf-8"))
        return store

    def load_json(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for t in data.get("tables", []):
            cols = [Column(**c) for c in t.pop("columns", [])]
            self.add(Table(columns=cols, **t))

    def save_json(self, path: str | Path) -> None:
        data = {"tables": [asdict(t) for t in self.tables.values()]}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def load_ddl(self, ddl: str) -> None:
        try:
            statements = sqlglot.parse(ddl, read="teradata")
        except Exception:
            statements = []
            for chunk in re.split(r";\s*\n", ddl):
                try:
                    statements.extend(sqlglot.parse(chunk, read="teradata"))
                except Exception:
                    continue
        comments: list[exp.Comment] = []
        for st in statements:
            if isinstance(st, exp.Create):
                table = _table_from_create(st)
                if table:
                    self.add(table)
            elif isinstance(st, exp.Comment):
                comments.append(st)
        for c in comments:
            self._apply_comment(c)

    def _apply_comment(self, c: exp.Comment) -> None:
        text = c.expression.name if c.expression else ""
        target = c.this
        if isinstance(target, exp.Column):
            t = self.find(f"{target.args.get('db') or ''}.{target.table}".strip("."))
            if t:
                for col in t.columns:
                    if col.name.lower() == target.name.lower():
                        col.comment = text
        elif isinstance(target, exp.Table):
            t = self.find(f"{target.db}.{target.name}" if target.db else target.name)
            if t:
                t.comment = text

    # -------------------------------------------------------------- lookup
    def find(self, name: str) -> Table | None:
        key = name.strip().strip('"').lower()
        if key in self.tables:
            return self.tables[key]
        matches = [t for k, t in self.tables.items() if k.split(".")[-1] == key.split(".")[-1]]
        return matches[0] if len(matches) == 1 else None

    def search(self, query: str, limit: int = 8) -> list[Table]:
        q = _tokens(query)
        if not q:
            return []
        scored: list[tuple[int, Table]] = []
        for t in self.tables.values():
            name = t.full_name.lower()
            comment = t.comment.lower()
            score = 0
            for tok in q:
                if tok in name:
                    score += 5
                if comment and tok in comment:
                    score += 4
                col_hits = sum(
                    1 for c in t.columns
                    if tok in c.name.lower() or (c.comment and tok in c.comment.lower())
                )
                score += min(col_hits, 3) * 2
            if score:
                scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], x[1].full_name))
        return [t for _, t in scored[:limit]]

    def overview(self, limit: int = 40) -> str:
        names = sorted(self.tables.values(), key=lambda t: t.full_name)
        lines = [f"- {t.full_name}" + (f": {t.comment}" if t.comment else "") for t in names[:limit]]
        if len(names) > limit:
            lines.append(f"- ... 외 {len(names) - limit}개 (search_schema 로 검색)")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.tables)


def _table_from_create(st: exp.Create) -> Table | None:
    kind = (st.args.get("kind") or "").upper()
    target = st.this
    if kind == "TABLE" and isinstance(target, exp.Schema):
        tbl = target.this
        columns = []
        for cd in target.expressions:
            if not isinstance(cd, exp.ColumnDef):
                continue
            col = Column(name=cd.name, type=cd.args["kind"].sql("teradata") if cd.args.get("kind") else "")
            for cons in cd.args.get("constraints") or []:
                k = cons.args.get("kind")
                if isinstance(k, exp.NotNullColumnConstraint) and not k.args.get("allow_null"):
                    col.nullable = False
                elif isinstance(k, (exp.CommentColumnConstraint, exp.TitleColumnConstraint)) and not col.comment:
                    col.comment = k.this.name if k.this else ""
            columns.append(col)
        pi, part = [], ""
        for idx in st.args.get("indexes") or []:
            if idx.args.get("primary"):
                params = idx.args.get("params")
                if params:
                    pi = [o.this.name if isinstance(o, exp.Ordered) else o.name
                          for o in params.args.get("columns") or []]
                    pb = params.args.get("partition_by")
                    if pb:
                        part = ", ".join(p.sql("teradata") for p in pb)
        return Table(database=tbl.db, name=tbl.name, kind="T", columns=columns,
                     primary_index=pi, partitioned_by=part)
    if kind == "VIEW" and isinstance(target, exp.Table):
        query = st.expression
        cols = [Column(name=n) for n in getattr(query, "named_selects", []) if n and n != "*"]
        return Table(database=target.db, name=target.name, kind="V", columns=cols)
    return None
