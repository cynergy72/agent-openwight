"""SQL-pattern tuning findings from the parsed query (sqlglot, Teradata dialect),
plus helpers the recommendations and the rewrite guard share."""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from ..schema import SchemaStore, Table
from ..sql_lint import strip_comments_and_strings
from .findings import Finding

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # EXEC/CALL etc. fall back to Command with a warning

_CMP = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_RANGE = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
_UPDATE_JOIN = re.compile(r"^\s*(update|upd)\s+[\w$#.\"]+(\s+[\w$#]+)?\s+from\b", re.I)
_DELETE_ALL = re.compile(r"^\s*(delete|del)\s+(from\s+)?[\w$#.\"]+(\s+all)?\s*(where\s+1\s*=\s*1)?\s*;?\s*$", re.I)


def parse(sql: str) -> list[exp.Expression]:
    try:
        return [t for t in sqlglot.parse(sql, read="teradata") if t is not None]
    except Exception:
        return []


def _sql(node: exp.Expression) -> str:
    try:
        return node.sql("teradata")[:200]
    except Exception:
        return str(node)[:200]


def _conjuncts(cond: exp.Expression | None) -> list[exp.Expression]:
    if cond is None:
        return []
    cond = cond.unnest()
    if isinstance(cond, exp.And):
        return _conjuncts(cond.left) + _conjuncts(cond.right)
    return [cond]


def _wraps_column(node: exp.Expression | None) -> bool:
    return isinstance(node, (exp.Func, exp.Cast, exp.DPipe, exp.Binary)) and not isinstance(node, exp.Column) \
        and node.find(exp.Column) is not None


def _schema_mapping(store: SchemaStore, tables: list[str]) -> dict:
    mapping: dict = {}
    for name in tables:
        t = store.find(name)
        if t and t.columns and t.database:
            mapping.setdefault(t.database.lower(), {})[t.name.lower()] = {
                c.name.lower(): (c.type or "VARCHAR(100)") for c in t.columns}
    return mapping


class QueryContext:
    """Alias → table resolution for one statement."""

    def __init__(self, tree: exp.Expression, store: SchemaStore):
        self.tree = tree
        self.store = store
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        self.aliases: dict[str, str] = {}
        self.base_tables: list[str] = []
        for t in tree.find_all(exp.Table):
            if not t.name or t.name.lower() in ctes:
                continue
            full = f"{t.db}.{t.name}" if t.db else t.name
            self.base_tables.append(full)
            self.aliases[t.alias_or_name.lower()] = full
            self.aliases.setdefault(t.name.lower(), full)

    def table_of(self, col: exp.Column) -> Table | None:
        if col.table:
            full = self.aliases.get(col.table.lower())
            return self.store.find(full) if full else None
        tables = {t.full_name: t for t in (self.store.find(n) for n in set(self.base_tables)) if t is not None}
        owners = [t for t in tables.values() if any(c.name.lower() == col.name.lower() for c in t.columns)]
        return owners[0] if len(owners) == 1 else None

    def column_type(self, col: exp.Column) -> str:
        t = self.table_of(col)
        if t:
            for c in t.columns:
                if c.name.lower() == col.name.lower():
                    return (c.type or "").upper()
        return ""

    def is_partition_column(self, col: exp.Column) -> bool:
        t = self.table_of(col)
        return bool(t and t.partitioned_by and re.search(rf"\b{re.escape(col.name)}\b", t.partitioned_by, re.I))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def analyze_sql(sql: str, store: SchemaStore | None = None) -> list[Finding]:
    store = store or SchemaStore()
    text = strip_comments_and_strings(sql)
    out: list[Finding] = []
    if _UPDATE_JOIN.search(text):
        out.append(Finding("UPDATE_JOIN", "UPDATE ... FROM 조인 형태입니다. UPDATE+INSERT 를 함께 한다면 MERGE 로 통합 검토",
                           source="sql"))
    if _DELETE_ALL.search(text.strip()):
        out.append(Finding("DELETE_ALL_CANDIDATE", "조건 없는 전체 삭제입니다. DELETE ... ALL 을 트랜잭션 마지막 문장으로",
                           source="sql"))
    for tree in parse(sql):
        out.extend(_analyze_tree(tree, sql, store))
    return out


def _analyze_tree(tree: exp.Expression, sql: str, store: SchemaStore) -> list[Finding]:
    ctx = QueryContext(tree, store)
    out: list[Finding] = []

    for select in tree.find_all(exp.Select):
        out += _join_checks(select, ctx)
        out += _where_checks(select, ctx)
        out += _outer_join_checks(select)
        if select.args.get("distinct"):
            out.append(Finding("DISTINCT_LARGE", "DISTINCT 사용 - 중복이 많으면 GROUP BY 와 EXPLAIN 비교",
                               evidence=_sql(select)[:120], source="sql"))
        for proj in select.expressions:
            node = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(node, exp.Subquery):
                out.append(Finding("SCALAR_SUBQUERY", "SELECT 목록에 스칼라 서브쿼리가 있습니다 (LEFT JOIN 변환 검토)",
                                   evidence=_sql(proj), source="sql"))
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
               for e in select.expressions):
            out.append(Finding("SELECT_STAR", "SELECT * - 필요한 컬럼만 선택하면 스풀이 줄어듭니다",
                               evidence=_sql(select)[:120], source="sql"))

    for node in tree.find_all(exp.Not):
        if isinstance(node.this, exp.In) and node.this.args.get("query"):
            out.append(Finding("NOT_IN_SUBQUERY", "NOT IN (서브쿼리) - NULL 이 있으면 0건, NOT EXISTS 검토",
                               evidence=_sql(node), source="sql"))
    for node in tree.find_all(exp.In):
        if len(node.expressions) >= 100:
            out.append(Finding("LARGE_IN_LIST", f"IN 리스트 {len(node.expressions)}개 - volatile table 조인 검토",
                               evidence=_sql(node.this), source="sql"))
    for node in tree.find_all(exp.Union):
        if node.args.get("distinct"):
            out.append(Finding("UNION_DISTINCT", "UNION 은 중복 제거 정렬을 합니다. 중복이 불가능하면 UNION ALL",
                               source="sql"))
            break
    for win in tree.find_all(exp.Window):
        if not win.args.get("partition_by"):
            out.append(Finding("WINDOW_NO_PARTITION", "PARTITION BY 없는 윈도우 함수 - 모든 행이 한 AMP 에서 처리됩니다",
                               evidence=_sql(win), source="sql"))

    try:
        for scope in traverse_scope(tree):
            if scope.is_correlated_subquery and any(c.table for c in scope.external_columns):
                parent = scope.expression.parent
                in_projection = isinstance(parent, exp.Subquery) and isinstance(parent.parent, (exp.Select, exp.Alias)) \
                    and not isinstance(parent.parent, exp.From)
                if not in_projection:
                    out.append(Finding("CORRELATED_SUBQUERY", "바깥 테이블을 참조하는 상관 서브쿼리",
                                       evidence=_sql(scope.expression), source="sql"))
    except Exception:
        pass

    counts: dict[str, int] = {}
    for t in ctx.base_tables:
        counts[t.lower()] = counts.get(t.lower(), 0) + 1
    repeated = [t for t, n in counts.items() if n >= 2]
    if repeated:
        out.append(Finding("REPEATED_SCAN", f"같은 테이블을 여러 번 참조: {', '.join(repeated)}",
                           tables=repeated, source="sql"))

    unused = unused_columns(tree, _schema_mapping(store, ctx.base_tables))
    if unused:
        detail = "; ".join(f"{name}: {', '.join(cols)}" for name, cols in unused.items())
        out.append(Finding("UNUSED_COLUMNS", f"바깥에서 쓰지 않는 컬럼 - {detail}", source="sql"))
    return out


def _join_checks(select: exp.Select, ctx: QueryContext) -> list[Finding]:
    out = []
    where = select.args.get("where")
    for j in select.args.get("joins") or []:
        on = j.args.get("on")
        alias = j.this.alias_or_name.lower() if j.this else ""
        if not on and not j.args.get("using"):
            if (j.args.get("kind") or "").upper() == "CROSS":
                continue
            links = [eq for eq in (where.find_all(exp.EQ) if where is not None else [])
                     if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)
                     and alias in (eq.left.table.lower(), eq.right.table.lower())
                     and eq.left.table.lower() != eq.right.table.lower()]
            linked = bool(links)
            if links and all(eq.find_ancestor(exp.Or) for eq in links):
                out.append(Finding("OR_JOIN", f"'{alias}' 와의 조인 조건이 WHERE 의 OR 안에 있습니다",
                                   evidence=_sql(links[0].find_ancestor(exp.Or)), source="sql"))
            out.append(Finding("COMMA_JOIN",
                               "콤마 조인 (WHERE 의 조인 조건 확인됨)" if linked
                               else f"'{alias}' 에 대한 조인 조건을 찾을 수 없습니다 (product join 위험)",
                               evidence=_sql(j), source="sql", severity="info" if linked else "warning"))
            continue
        if on.find(exp.Or):
            out.append(Finding("OR_JOIN", "조인 조건에 OR 가 있습니다", evidence=_sql(on), source="sql"))
        col_eq = [e for e in on.find_all(exp.EQ) if isinstance(e.left, exp.Column) and isinstance(e.right, exp.Column)]
        ranges = [c for c in on.find_all(*_RANGE, exp.Like, exp.NEQ) if c.find(exp.Column)]
        if ranges and not col_eq:
            out.append(Finding("NONEQUI_JOIN", "동등 조건 없이 범위/LIKE 조건만으로 조인합니다",
                               evidence=_sql(on), source="sql"))
        for e in on.find_all(exp.EQ):
            if (_wraps_column(e.left) and e.right.find(exp.Column)) or \
                    (_wraps_column(e.right) and e.left.find(exp.Column)):
                out.append(Finding("FUNCTION_JOIN", "조인 컬럼에 함수/표현식이 적용되어 있습니다",
                                   evidence=_sql(e), source="sql"))
                break
    return out


def _where_checks(select: exp.Select, ctx: QueryContext) -> list[Finding]:
    out = []
    where = select.args.get("where")
    if where is None:
        return out
    for cmp in where.find_all(*_CMP, exp.Between, exp.In):
        sides = [cmp.this] + ([cmp.args.get("expression")] if cmp.args.get("expression") is not None else [])
        for side in sides:
            others = [s for s in sides if s is not side] + [cmp.args.get("low"), cmp.args.get("high")]
            if _wraps_column(side) and not any(o is not None and o.find(exp.Column) for o in others):
                col = side.find(exp.Column)
                part = ctx.is_partition_column(col)
                out.append(Finding("NON_SARGABLE",
                                   f"{col.sql('teradata')} 를 함수로 감싸 비교합니다"
                                   + (" - 파티션 컬럼이라 파티션 제거가 안 됩니다" if part else ""),
                                   evidence=_sql(cmp), source="sql", severity="critical" if part else "warning",
                                   tables=[t.full_name] if (t := ctx.table_of(col)) else []))
                break
        if isinstance(cmp, (exp.EQ, exp.In)) and isinstance(cmp.this, exp.Column):
            values = [cmp.expression] if isinstance(cmp, exp.EQ) else cmp.expressions
            ctype = ctx.column_type(cmp.this)
            if ctype.startswith(("CHAR", "VARCHAR")) and any(
                    isinstance(v, exp.Literal) and not v.is_string for v in values):
                out.append(Finding("IMPLICIT_CONVERSION",
                                   f"문자 컬럼 {cmp.this.sql('teradata')}({ctype}) 를 숫자 상수와 비교 → 컬럼이 숫자로 변환됩니다",
                                   evidence=_sql(cmp), source="sql"))
            elif ctype.startswith(("DATE", "TIMESTAMP")) and any(
                    isinstance(v, exp.Literal) and v.is_string for v in values):
                out.append(Finding("IMPLICIT_CONVERSION",
                                   f"날짜 컬럼 {cmp.this.sql('teradata')} 를 문자열과 비교 → DATE 리터럴 사용 권장",
                                   evidence=_sql(cmp), source="sql", severity="info"))
    for like in where.find_all(exp.Like):
        pat = like.expression
        if isinstance(pat, exp.Literal) and pat.is_string and pat.this.startswith("%"):
            out.append(Finding("LIKE_LEADING_WILDCARD", "선행 % LIKE 는 인덱스·통계를 활용하지 못합니다",
                               evidence=_sql(like), source="sql"))
    for c in _conjuncts(where.this):
        if isinstance(c, exp.Or):
            names = {col.name.lower() for col in c.find_all(exp.Column)}
            if len(names) >= 2:
                out.append(Finding("OR_FILTER", f"서로 다른 컬럼({', '.join(sorted(names))})의 OR 조건",
                                   evidence=_sql(c), source="sql"))
    return out


def _outer_join_checks(select: exp.Select) -> list[Finding]:
    out = []
    where = select.args.get("where")
    for j in select.args.get("joins") or []:
        if (j.args.get("side") or "").upper() != "LEFT" or not j.this:
            continue
        alias = j.this.alias_or_name.lower()
        for c in _conjuncts(where.this if where is not None else None):
            refs = [col for col in c.find_all(exp.Column) if col.table.lower() == alias]
            if refs and not c.find(exp.Is):
                out.append(Finding("OUTER_WHERE_FILTER",
                                   f"LEFT JOIN 안쪽 테이블 '{alias}' 조건이 WHERE 에 있어 사실상 INNER JOIN 입니다",
                                   evidence=_sql(c), source="sql"))
                break
        on = j.args.get("on")
        used = [col for col in select.find_all(exp.Column)
                if col.table.lower() == alias and not (on is not None and _within(col, on))]
        unqualified = [col for col in select.find_all(exp.Column) if not col.table]
        if not used and not unqualified:
            out.append(Finding("UNUSED_OUTER_JOIN",
                               f"LEFT JOIN '{alias}' 의 컬럼을 사용하지 않습니다 (조인 키가 유일하면 제거 가능)",
                               evidence=_sql(j), source="sql"))
    return out


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    p = node
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent
    return False


def unused_columns(tree: exp.Expression, mapping: dict) -> dict[str, list[str]]:
    """Columns of CTEs / derived tables that nothing outside reads."""
    def projections(t: exp.Expression) -> dict[str, list[str]]:
        out = {}
        for cte in t.find_all(exp.CTE):
            out[cte.alias_or_name] = list(cte.this.named_selects)
        for sq in t.find_all(exp.Subquery):
            if sq.alias and isinstance(sq.this, exp.Query):
                out[sq.alias] = list(sq.this.named_selects)
        return out

    try:
        q = qualify(tree.copy(), schema=mapping or None, dialect="teradata",
                    validate_qualify_columns=False, quote_identifiers=False)
        before = projections(q)
        after = projections(pushdown_projections(q.copy()))
    except Exception:
        return {}
    removed = {}
    for name, cols in before.items():
        gone = [c for c in cols if c not in after.get(name, cols)]
        if gone:
            removed[name] = gone
    return removed


# --------------------------------------------------------------------------- #
# Helpers for recommendations and the rewrite guard
# --------------------------------------------------------------------------- #

def predicate_columns(sql: str, store: SchemaStore) -> dict[str, dict[str, set[str]]]:
    """{table: {"join": cols, "filter": cols, "range": cols}} from ON / WHERE."""
    result: dict[str, dict[str, set[str]]] = {}

    def add(t: Table | None, kind: str, col: str):
        if t is not None:
            result.setdefault(t.full_name, {"join": set(), "filter": set(), "range": set()})[kind].add(col.upper())

    for tree in parse(sql):
        ctx = QueryContext(tree, store)
        for cmp in tree.find_all(*_CMP, exp.Between, exp.In):
            cols = list(cmp.find_all(exp.Column))
            left, right = cmp.this, cmp.args.get("expression")
            if isinstance(cmp, exp.EQ) and isinstance(left, exp.Column) and isinstance(right, exp.Column):
                add(ctx.table_of(left), "join", left.name)
                add(ctx.table_of(right), "join", right.name)
            elif len(cols) == 1 and isinstance(left, exp.Column):
                add(ctx.table_of(left), "filter", left.name)
                if isinstance(cmp, _RANGE):
                    add(ctx.table_of(left), "range", left.name)
    return result


def output_signature(sql: str) -> tuple | None:
    """What a rewrite must keep: the final column names (or INSERT target)."""
    trees = parse(sql)
    if len(trees) != 1:
        return None
    t = trees[0]
    if isinstance(t, exp.Insert):
        target = t.this.this if isinstance(t.this, exp.Schema) else t.this
        return ("insert", _sql(target).lower())
    if isinstance(t, exp.Query):
        return ("select", tuple(n.lower() for n in t.named_selects))
    return (type(t).__name__.lower(),)


def referenced_partitioned_tables(sql: str, store: SchemaStore) -> set[str]:
    out = set()
    for tree in parse(sql):
        for name in QueryContext(tree, store).base_tables:
            t = store.find(name)
            if t and t.partitioned_by:
                out.add(t.full_name)
    return out


def _predicate_column_names(tree: exp.Expression) -> set[str]:
    names = set()
    for node in list(tree.find_all(exp.Where, exp.Having, exp.Qualify)) + \
            [j.args["on"] for j in tree.find_all(exp.Join) if j.args.get("on") is not None]:
        names |= {c.name.upper() for c in node.find_all(exp.Column)}
    return names


def semantic_warnings(original: str, rewritten: str) -> list[str]:
    """Cheap signs that a rewrite may have changed the result. Not proof either way."""
    a, b = parse(original), parse(rewritten)
    if len(a) != 1 or len(b) != 1:
        return []
    out = []
    lost = sorted(_predicate_column_names(a[0]) - _predicate_column_names(b[0]))
    if lost:
        out.append(f"조건(WHERE/ON/HAVING)에서 사라진 컬럼: {', '.join(lost)}")
    ta = sorted(t.lower() for t in QueryContext(a[0], SchemaStore()).base_tables)
    tb = sorted(t.lower() for t in QueryContext(b[0], SchemaStore()).base_tables)
    if ta != tb:
        removed = sorted(set(ta) - set(tb))
        added = sorted(set(tb) - set(ta))
        bits = ([f"제거 {', '.join(removed)}"] if removed else []) + ([f"추가 {', '.join(added)}"] if added else [])
        out.append("참조 테이블 변경: " + ("; ".join(bits) or "참조 횟수 변경"))
    for kind, label in ((exp.Join, None),):
        sides_a = sorted((j.args.get("side") or "").upper() for j in a[0].find_all(kind))
        sides_b = sorted((j.args.get("side") or "").upper() for j in b[0].find_all(kind))
        if [x for x in sides_a if x] != [x for x in sides_b if x]:
            out.append("OUTER JOIN 구성이 바뀌었습니다")
    return out


def result_check_sql(original: str, rewritten: str) -> tuple[str, str] | None:
    """Two COUNT queries over EXCEPT ALL, or None when the shape can't be nested safely
    (WITH, ORDER BY, TOP/SAMPLE, non-SELECT)."""
    a, b = parse(original), parse(rewritten)
    if len(a) != 1 or len(b) != 1:
        return None
    for t in (a[0], b[0]):
        if not isinstance(t, exp.Query) or t.args.get("with") or t.args.get("order") or t.args.get("limit") \
                or t.find(exp.TableSample):
            return None
    qa, qb = original.strip().rstrip(";"), rewritten.strip().rstrip(";")
    if "?" in strip_comments_and_strings(qa + qb):
        return None  # parameter markers: the real values are unknown, comparing with dummies proves nothing
    return (f"SELECT COUNT(*) FROM ({qa} EXCEPT ALL {qb}) AS d",
            f"SELECT COUNT(*) FROM ({qb} EXCEPT ALL {qa}) AS d")
