"""Deterministic Teradata SQL checks.

Small models make dialect mistakes (LIMIT, ILIKE, ::cast ...) and cannot be
trusted to say "this query is valid". These checks run in code so the agent
loop can reject a bad answer and ask the model to fix it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass
class Issue:
    level: str  # "error" | "warning"
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}:{self.code}] {self.message}"


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

def strip_comments_and_strings(sql: str) -> str:
    """Remove comments and blank out string literals so regex rules don't
    fire on text inside them. Quoted identifiers are kept."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        elif c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            out.append("''")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    stripped = strip_comments_and_strings(sql)
    return [s.strip() for s in stripped.split(";") if s.strip()]


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", strip_comments_and_strings(sql)).strip().rstrip(";").strip().lower()


_SQL_BLOCK = re.compile(r"```(?:sql|teradata)\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_ANY_BLOCK = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.DOTALL)
_SQL_START = re.compile(r"^\s*(select|sel|with|create|insert|update|delete|merge|locking|replace)\b", re.I)


def extract_sql_blocks(text: str) -> list[str]:
    """Return SQL code blocks from a markdown answer."""
    blocks = [b.strip() for b in _SQL_BLOCK.findall(text or "")]
    if not blocks:
        blocks = [b.strip() for b in _ANY_BLOCK.findall(text or "") if _SQL_START.match(b)]
    return [b for b in blocks if b]


# --------------------------------------------------------------------------- #
# Read-only guard (used before executing anything against Teradata)
# --------------------------------------------------------------------------- #

_READ_START = re.compile(r"^(select|sel|with|help|show)\b", re.I)
_LOCKING_PREFIX = re.compile(
    r"^locking\s+(row|table\s+\S+|database\s+\S+|view\s+\S+)?\s*for\s+access\s+", re.I
)
_WRITE_WORDS = re.compile(
    r"\b(insert|ins|update|upd|delete|del|merge|drop|create|alter|replace|rename|"
    r"grant|revoke|call|exec|execute|collect|modify|give|begin|commit|abort|rollback|"
    r"database|checkpoint|release|dump)\b",
    re.I,
)


def is_read_only(sql: str) -> tuple[bool, str]:
    statements = split_statements(sql)
    if len(statements) != 1:
        return False, "한 번에 하나의 문장만 실행할 수 있습니다."
    stmt = _LOCKING_PREFIX.sub("", statements[0])
    if not _READ_START.match(stmt):
        return False, "SELECT / WITH / HELP / SHOW 문만 실행할 수 있습니다."
    m = _WRITE_WORDS.search(stmt)
    if m:
        return False, f"쓰기/DDL 키워드가 포함되어 실행하지 않습니다: {m.group(0).upper()}"
    return True, ""


# --------------------------------------------------------------------------- #
# Dialect rules
# --------------------------------------------------------------------------- #

_REGEX_RULES: list[tuple[str, re.Pattern, str]] = [
    ("limit", re.compile(r"\blimit\s+\d+", re.I),
     "Teradata 는 LIMIT 를 지원하지 않습니다. TOP n, SAMPLE n, 또는 QUALIFY ROW_NUMBER() 를 사용하세요."),
    ("offset", re.compile(r"\boffset\s+\d+", re.I),
     "Teradata 는 OFFSET 을 지원하지 않습니다. QUALIFY ROW_NUMBER() OVER (...) BETWEEN a AND b 를 사용하세요."),
    ("ilike", re.compile(r"\bilike\b", re.I),
     "ILIKE 는 없습니다. UPPER(col) LIKE UPPER('..') 또는 NOT CASESPECIFIC 비교를 사용하세요."),
    ("backtick", re.compile(r"`"),
     "백틱(`)은 식별자 인용에 쓸 수 없습니다. 큰따옴표(\")를 사용하세요."),
    ("pg_cast", re.compile(r"::"),
     "':: ' 캐스트는 PostgreSQL 문법입니다. CAST(x AS type) 을 사용하세요."),
    ("ifnull", re.compile(r"\b(ifnull|isnull)\s*\(", re.I),
     "IFNULL/ISNULL 대신 COALESCE 또는 ZEROIFNULL 을 사용하세요."),
    ("now", re.compile(r"\b(now|getdate|sysdatetime)\s*\(\s*\)", re.I),
     "NOW()/GETDATE() 대신 CURRENT_TIMESTAMP 또는 CURRENT_DATE 를 사용하세요."),
    ("datediff", re.compile(r"\b(datediff|date_add|dateadd|date_sub|timestampdiff)\s*\(", re.I),
     "DATEDIFF/DATE_ADD 는 없습니다. 날짜끼리 빼기(일수), ADD_MONTHS, + INTERVAL 'n' DAY 를 사용하세요."),
]


def _lint_ast(tree: exp.Expression) -> list[Issue]:
    issues: list[Issue] = []

    # SELECT * at the outermost level
    if isinstance(tree, exp.Select) and any(isinstance(e, exp.Star) for e in tree.expressions):
        issues.append(Issue("warning", "select_star",
                            "SELECT * 대신 필요한 컬럼만 명시하세요 (스풀/네트워크 비용)."))

    for select in tree.find_all(exp.Select):
        joins = select.args.get("joins") or []
        for j in joins:
            has_cond = j.args.get("on") or j.args.get("using")
            kind = (j.args.get("kind") or "").upper()
            if not has_cond and kind != "CROSS":
                where = select.args.get("where")
                if where is None:
                    issues.append(Issue("warning", "product_join",
                                        "조인 조건이 없습니다 (product join 위험). ON 절을 확인하세요."))
                else:
                    issues.append(Issue("warning", "comma_join",
                                        "콤마 조인입니다. 명시적 JOIN ... ON 으로 쓰는 것을 권장합니다."))
                break

        where = select.args.get("where")
        if where is not None:
            for cmp in where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
                for side in (cmp.this, cmp.args.get("expression")):
                    if isinstance(side, (exp.Func, exp.Cast)) and not isinstance(side, exp.Column) \
                            and side.find(exp.Column):
                        issues.append(Issue(
                            "warning", "non_sargable",
                            f"WHERE 에서 컬럼을 함수로 감쌌습니다 ({side.sql('teradata')[:60]}). "
                            "인덱스/파티션 제거가 안 될 수 있으니 컬럼 쪽은 그대로 두고 상수 쪽을 변환하세요."))
                        break

        if select is not tree and select.args.get("order") and not select.args.get("limit"):
            if select.find_ancestor(exp.Subquery, exp.CTE):
                issues.append(Issue("warning", "subquery_order_by",
                                    "Teradata 는 서브쿼리/파생테이블 안의 ORDER BY 를 허용하지 않습니다 (TOP 과 함께일 때 제외)."))

    for node in tree.find_all(exp.Not):
        if isinstance(node.this, exp.In) and node.this.args.get("query"):
            issues.append(Issue("warning", "not_in_subquery",
                                "NOT IN (서브쿼리)는 NULL 이 있으면 결과가 비고 성능도 나쁩니다. NOT EXISTS 를 고려하세요."))

    for win in tree.find_all(exp.Window):
        if isinstance(win.this, exp.AggFunc) and win.args.get("order") and not win.args.get("spec"):
            issues.append(Issue("warning", "window_frame",
                                "ORDER BY 가 있는 집계 윈도우 함수는 ROWS BETWEEN ... 을 명시하세요 "
                                "(Teradata 버전에 따라 기본 프레임이 다릅니다)."))
            break

    return issues


def lint_sql(sql: str) -> list[Issue]:
    issues: list[Issue] = []
    text = strip_comments_and_strings(sql)
    for code, pattern, message in _REGEX_RULES:
        if pattern.search(text):
            issues.append(Issue("error", code, message))

    try:
        trees = [t for t in sqlglot.parse(sql, read="teradata") if t is not None]
    except Exception as e:  # sqlglot's Teradata coverage is not perfect
        first_line = str(e).splitlines()[0][:200]
        issues.append(Issue("warning", "parse",
                            f"정적 파서가 해석하지 못했습니다 (Teradata 에서는 유효할 수도 있음): {first_line}"))
        return issues

    for tree in trees:
        issues.extend(_lint_ast(tree))

    # de-duplicate by code
    seen, unique = set(), []
    for i in issues:
        if i.code not in seen:
            seen.add(i.code)
            unique.append(i)
    return unique


def referenced_tables(sql: str) -> list[str]:
    """db.table names referenced by the query (CTE names excluded)."""
    try:
        trees = [t for t in sqlglot.parse(sql, read="teradata") if t is not None]
    except Exception:
        return []
    names: list[str] = []
    for tree in trees:
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if not t.name or t.name.lower() in ctes:
                continue
            full = f"{t.db}.{t.name}" if t.db else t.name
            if full not in names:
                names.append(full)
    return names


# --------------------------------------------------------------------------- #
# EXPLAIN interpretation
# --------------------------------------------------------------------------- #

_EXPLAIN_RULES = [
    (re.compile(r"product join", re.I),
     "Product join 이 있습니다. 조인 조건 누락 또는 비동등 조인인지 확인하세요."),
    (re.compile(r"duplicated on all amps", re.I),
     "한쪽 테이블이 모든 AMP 로 복제(duplicate)됩니다. 작은 테이블이면 정상, 큰 테이블이면 통계/조인키 확인."),
    (re.compile(r"redistributed by (the )?(rowkey-based )?hash code", re.I),
     "행 재분배(redistribution)가 발생합니다. 조인 키가 PI 와 다르면 생깁니다."),
    (re.compile(r"all-rows scan", re.I),
     "전체 테이블 스캔(all-rows scan)이 있습니다. 파티션 컬럼/PI 조건 추가를 검토하세요."),
    (re.compile(r"with no confidence", re.I),
     "통계가 없는(no confidence) 단계가 있습니다. COLLECT STATISTICS 를 검토하세요."),
    (re.compile(r"with low confidence", re.I),
     "통계 신뢰도가 낮은(low confidence) 단계가 있습니다."),
    (re.compile(r"\b(a single partition|\d+ partitions?) of\b", re.I),
     "파티션 제거(partition elimination)가 적용되었습니다 (좋음)."),
]

_TOTAL_TIME = re.compile(r"total estimated time is ([\d:.]+)", re.I)
_ROWS = re.compile(r"estimated size of the spool (?:file )?is ([\d,]+) rows", re.I)


def explain_hints(explain_text: str) -> list[str]:
    hints = [msg for pattern, msg in _EXPLAIN_RULES if pattern.search(explain_text)]
    rows = [int(r.replace(",", "")) for r in _ROWS.findall(explain_text)]
    if rows:
        hints.append(f"최대 예상 스풀 행 수: {max(rows):,}")
    t = _TOTAL_TIME.search(explain_text)
    if t:
        hints.append(f"총 예상 시간: {t.group(1)}")
    return hints


# --------------------------------------------------------------------------- #
# Catalog checks (hallucinated tables/columns) - used when no live EXPLAIN
# --------------------------------------------------------------------------- #

def catalog_issues(sql: str, store) -> list[Issue]:
    """Flag tables/columns that are not in the local schema catalog."""
    from sqlglot.optimizer.qualify import qualify

    if not len(store):
        return []
    tables = referenced_tables(sql)
    missing = [t for t in tables if store.find(t) is None]
    if missing:
        return [Issue("error", "unknown_table",
                      f"스키마 카탈로그에 없는 테이블: {', '.join(missing)}. search_schema 로 실제 이름을 확인하세요.")]

    found = [store.find(t) for t in tables]
    if not found or any("." not in t or not store.find(t).columns for t in tables):
        return []  # not enough information to check columns
    mapping: dict = {}
    for t in found:
        mapping.setdefault(t.database.lower(), {})[t.name.lower()] = {
            c.name.lower(): (c.type or "VARCHAR") for c in t.columns
        }
    try:
        for tree in sqlglot.parse(sql, read="teradata"):
            if tree is not None:
                qualify(tree, schema=mapping, dialect="teradata", validate_qualify_columns=True)
    except Exception as e:
        msg = str(e)
        if "Unknown column" in msg or "could not be resolved" in msg:
            return [Issue("error", "unknown_column",
                          f"{msg.splitlines()[0]} — describe_table 로 실제 컬럼명을 확인하세요.")]
    return []
