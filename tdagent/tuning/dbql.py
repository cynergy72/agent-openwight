"""DBQL access: find heavy queries and rebuild their full SQL text."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import DBQLConfig
from ..teradata import TeradataClient
from .findings import dbql_ratios

ORDER_BY = {
    "cpu": "l.AMPCPUTime DESC",
    "io": "l.TotalIOCount DESC",
    "impact": "l.MaxAMPCPUTime * l.NumOfActiveAMPs DESC",
    "skew": "l.MaxAMPCPUTime * l.NumOfActiveAMPs / NULLIFZERO(l.AMPCPUTime) DESC",
    "spool": "l.SpoolUsage DESC",
}
ORDER_LABELS = {"cpu": "CPU", "io": "IO", "impact": "Impact CPU", "skew": "CPU 스큐", "spool": "스풀"}
_IDENT = re.compile(r"^[A-Za-z_$#][\w$#]*\.[A-Za-z_$#][\w$#]*$")

METRIC_COLS = ["QueryID", "ProcID", "UserName", "StartTime", "AMPCPUTime", "TotalIOCount", "MaxAMPCPUTime",
               "MaxAMPIO", "NumOfActiveAMPs", "SpoolUsage", "SqlPreview"]


@dataclass
class DBQLFilter:
    start: str                 # 'YYYY-MM-DD HH:MM:SS'
    end: str
    user: str = ""
    min_cpu: float = 0.0
    order: str = "cpu"
    top: int = 20


def _table(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"DBQL 테이블 이름이 올바르지 않습니다: {name}")
    return name


def top_queries_sql(cfg: DBQLConfig, f: DBQLFilter) -> tuple[str, list]:
    where = ["l.StartTime >= CAST(? AS TIMESTAMP(0))", "l.StartTime < CAST(? AS TIMESTAMP(0))",
             "l.ErrorCode = 0", "l.AMPCPUTime >= ?"]
    params: list = [f.start, f.end, f.min_cpu]
    if f.user.strip():
        where.append("TRIM(l.UserName) = ?")
        params.append(f.user.strip())
    sql = (
        f"SELECT TOP {int(f.top)} l.QueryID, l.ProcID, TRIM(l.UserName), l.StartTime, l.AMPCPUTime, l.TotalIOCount, "
        "l.MaxAMPCPUTime, l.MaxAMPIO, l.NumOfActiveAMPs, l.SpoolUsage, SUBSTR(s.SqlTextInfo, 1, 300) "
        f"FROM {_table(cfg.log_table)} l "
        f"LEFT JOIN {_table(cfg.sql_table)} s ON s.ProcID = l.ProcID AND s.QueryID = l.QueryID AND s.SqlRowNo = 1 "
        f"WHERE {' AND '.join(where)} ORDER BY {ORDER_BY.get(f.order, ORDER_BY['cpu'])}"
    )
    return sql, params


def top_queries(td: TeradataClient, cfg: DBQLConfig, f: DBQLFilter) -> list[dict]:
    sql, params = top_queries_sql(cfg, f)
    _, rows = td.query(sql, params)
    out = []
    for r in rows:
        d = dict(zip(METRIC_COLS, r))
        d.update({k: round(v, 2) for k, v in dbql_ratios(d).items()})
        out.append(d)
    return out


def full_sql(td: TeradataClient, cfg: DBQLConfig, proc_id, query_id) -> str:
    _, rows = td.query(f"SELECT SqlTextInfo FROM {_table(cfg.sql_table)} "
                       "WHERE ProcID = ? AND QueryID = ? ORDER BY SqlRowNo", [proc_id, query_id])
    return "".join(str(r[0]) for r in rows).strip()
