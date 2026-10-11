"""Thin wrapper over the official `teradatasql` driver.

Everything the agent can do here is read-only: EXPLAIN (parses and plans a
query without running it), metadata lookups, and - only when
`allow_execute = true` - single SELECT statements with a row cap.
"""

from __future__ import annotations

from .config import TeradataConfig
from .schema import Column, SchemaStore, Table
from .sql_lint import is_read_only

TYPE_NAMES = {
    "I": "INTEGER", "I1": "BYTEINT", "I2": "SMALLINT", "I8": "BIGINT",
    "F": "FLOAT", "D": "DECIMAL", "N": "NUMBER",
    "CF": "CHAR", "CV": "VARCHAR", "CO": "CLOB",
    "DA": "DATE", "AT": "TIME", "TS": "TIMESTAMP", "TZ": "TIME WITH TIME ZONE",
    "SZ": "TIMESTAMP WITH TIME ZONE", "BF": "BYTE", "BV": "VARBYTE", "BO": "BLOB",
    "JN": "JSON", "XM": "XML", "PD": "PERIOD(DATE)", "PS": "PERIOD(TIMESTAMP)",
    "YR": "INTERVAL YEAR", "MO": "INTERVAL MONTH", "DY": "INTERVAL DAY",
}


class TeradataError(Exception):
    pass


def _clean_error(e: Exception) -> str:
    """teradatasql errors carry a long Go stack trace; keep the first line."""
    text = str(e).strip()
    first = text.splitlines()[0] if text else repr(e)
    return first[:500]


def _format_type(type_code, length, total, frac) -> str:
    code = (type_code or "").strip()
    name = TYPE_NAMES.get(code, code or "?")
    if code in ("CV", "CF") and length:
        return f"{name}({int(length)})"
    if code == "D" and total is not None:
        return f"{name}({int(total)},{int(frac or 0)})"
    return name


class TeradataClient:
    def __init__(self, cfg: TeradataConfig):
        self.cfg = cfg
        self._con = None

    def _connect(self):
        if self._con is None:
            try:
                import teradatasql
            except ImportError as e:
                raise TeradataError("teradatasql 이 설치되어 있지 않습니다: pip install 'tdagent[teradata]'") from e
            try:
                self._con = teradatasql.connect(
                    host=self.cfg.host, user=self.cfg.user,
                    password=self.cfg.password, logmech=self.cfg.logmech,
                )
            except Exception as e:
                raise TeradataError(f"접속 실패: {_clean_error(e)}") from e
        return self._con

    @property
    def connected(self) -> bool:
        return self._con is not None

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def _fetch(self, sql: str, params=None, max_rows: int | None = None):
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(sql, params or [])
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
                return cols, rows
        except Exception as e:
            raise TeradataError(_clean_error(e)) from e

    # ----------------------------------------------------------- agent ops
    def explain(self, sql: str) -> str:
        """Run EXPLAIN. Fails with the real Teradata error if the SQL is invalid
        (syntax, missing objects, missing privileges) - the best validator."""
        body = sql.strip().rstrip(";")
        _, rows = self._fetch("EXPLAIN " + body)
        return "\n".join(str(r[0]) for r in rows)

    def run_select(self, sql: str) -> tuple[list[str], list[tuple]]:
        if not self.cfg.allow_execute:
            raise TeradataError("쿼리 실행이 비활성화되어 있습니다 (teradata.allow_execute = false).")
        ok, reason = is_read_only(sql)
        if not ok:
            raise TeradataError(reason)
        return self._fetch(sql.strip().rstrip(";"), max_rows=self.cfg.max_rows)

    def describe(self, full_name: str) -> Table | None:
        db, _, name = full_name.rpartition(".")
        tables = self._load_tables([db] if db else None, name)
        return tables[0] if tables else None

    # ------------------------------------------------------- schema sync
    def sync_schema(self, databases: list[str]) -> SchemaStore:
        return SchemaStore(self._load_tables(databases))

    def _load_tables(self, databases: list[str] | None, table: str | None = None) -> list[Table]:
        where, params = [], []
        if databases:
            where.append(f"DatabaseName IN ({', '.join('?' * len(databases))})")
            params += databases
        if table:
            where.append("TableName = ?")
            params.append(table)
        cond = " AND ".join(where) or "1=1"

        _, trows = self._fetch(
            "SELECT TRIM(DatabaseName), TRIM(TableName), TableKind, CommentString "
            f"FROM DBC.TablesV WHERE {cond} AND TableKind IN ('T','V','O')", params)
        tables = {(r[0], r[1]): Table(database=r[0], name=r[1],
                                      kind="V" if r[2] == "V" else "T", comment=r[3] or "")
                  for r in trows}
        if not tables:
            return []

        _, crows = self._fetch(
            "SELECT TRIM(DatabaseName), TRIM(TableName), TRIM(ColumnName), ColumnType, ColumnLength, "
            "DecimalTotalDigits, DecimalFractionalDigits, Nullable, CommentString "
            f"FROM DBC.ColumnsV WHERE {cond} ORDER BY DatabaseName, TableName, ColumnId", params)
        for r in crows:
            t = tables.get((r[0], r[1]))
            if t:
                t.columns.append(Column(name=r[2], type=_format_type(r[3], r[4], r[5], r[6]),
                                        nullable=(r[7] != "N"), comment=r[8] or ""))

        _, irows = self._fetch(
            "SELECT TRIM(DatabaseName), TRIM(TableName), TRIM(ColumnName) "
            f"FROM DBC.IndicesV WHERE {cond} AND IndexType IN ('P','Q') "
            "ORDER BY DatabaseName, TableName, ColumnPosition", params)
        for r in irows:
            t = tables.get((r[0], r[1]))
            if t:
                t.primary_index.append(r[2])
        return list(tables.values())
