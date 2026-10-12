"""Prompts for the tuning loop. Code gathers all context up front (plan
summary, findings, matching tuning-point knowledge, table definitions,
previous attempts); the model only reasons and writes. One call per attempt,
no tool use - the most reliable shape for small local models."""

from __future__ import annotations

import re

REWRITE_SYSTEM = """\
You are a Teradata performance tuning expert. Rewrite ONE SQL statement so that its execution plan
improves toward the goal while returning exactly the same result.

Rules:
- Keep the same output columns (names, order, meaning) and the same rows. Do not change filters, join types
  (INNER/OUTER) or aggregation semantics. If an improvement could change the result, do NOT apply it -
  describe it under <assumptions> instead.
- Teradata SQL only (dialect rules below). Never use LIMIT.
- Fix the detected tuning points, citing their TP ids. Only rewrite fixes (grade A) are possible here;
  statistics (grade B) and design changes (grade C: PI, partitions, indexes) are reported separately - do not
  try to solve them by rewriting.
- Exactly one statement. No volatile tables in <rewrite> (put multi-step ideas in <changes> as suggestions).
- If earlier attempts were rejected, use the reason and try something different.
- If no rewrite can improve the plan, answer <rewrite>NONE</rewrite>.

Answer in exactly this format, text in Korean:
<analysis>1-3 lines: the main problem, citing TP ids</analysis>
<rewrite>
the full rewritten SQL
</rewrite>
<changes>
- [TP-ID] what you changed and why
</changes>
<assumptions>
- anything that could affect the result (leave empty if none)
</assumptions>
"""

REPORT_SYSTEM = """\
You are a Teradata tuning consultant. Write the final tuning report in Korean markdown for a DBA/developer.
Use ONLY the evidence provided (plan metrics, findings, attempts, recommendation candidates). Do not invent
numbers, tables, columns or statistics. Cite tuning point ids (TP-xxx) for every diagnosis.

Use these sections:
## 요약
(goal met or not, key before/after numbers in 2-4 lines)
## 튜닝 포인트별 진단
| TP | 문제 | 근거 | 조치 | 등급 |
(one row per distinct problem; 등급 A=쿼리 재작성, B=통계, C=설계 변경)
## 쿼리 재작성
(what the accepted rewrite changed, and any assumption the user must confirm; say so if nothing was accepted)
## 통계 수집 권고
(a ```sql block with the COLLECT STATISTICS candidates you agree with, most important first, plus one line why)
## 설계 변경 검토
(partition / PI / index proposals with DDL sketch and migration notes - only if candidates or grade C findings
exist; otherwise write "해당 없음")
## 적용 순서와 검증
(numbered steps: A first, then B, then C; how to verify each with EXPLAIN/DBQL)
"""

_TAG = {name: re.compile(rf"<{name}>(.*?)(?:</{name}>|(?=<[a-z]+>)|\Z)", re.S | re.I)
        for name in ("analysis", "rewrite", "changes", "assumptions")}
_FENCE = re.compile(r"```(?:sql|teradata)?\s*\n?(.*?)```", re.S | re.I)


def parse_rewrite_reply(text: str) -> dict[str, str]:
    out = {}
    for name, pattern in _TAG.items():
        m = pattern.search(text or "")
        out[name] = m.group(1).strip() if m else ""
    sql = out["rewrite"]
    fenced = _FENCE.search(sql or text or "")
    if fenced and (not sql or "```" in sql):
        sql = fenced.group(1).strip()
    out["rewrite"] = sql.strip().rstrip(";").strip() if sql.strip().upper() != "NONE" else "NONE"
    return out
