from pathlib import Path

from tdagent import cli
from tdagent.agent import build_agent
from tdagent.llm import LLMReply

ROOT = Path(__file__).resolve().parents[1]
ORIG = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t, SALES_DB.CUSTOMER c
WHERE CAST(t.SALE_DT AS CHAR(7)) = '2024-01' AND t.CUST_ID = c.CUST_ID
GROUP BY 1"""
GOOD_SQL = """SELECT c.GRADE_CD, SUM(t.SALE_AMT) AS amt
FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID
WHERE t.SALE_DT >= DATE '2024-01-01' AND t.SALE_DT < DATE '2024-02-01'
GROUP BY 1"""


class Model:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools):
        return LLMReply(content=self.replies.pop(0))


def test_tune_offline_writes_outputs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    model = Model([f"<rewrite>\n{GOOD_SQL}\n</rewrite><changes>- [TP-PART-02]</changes>", "## 요약\n리포트"])
    monkeypatch.setattr(cli, "build_agent", lambda cfg: build_agent(cfg, model=model))
    sql = tmp_path / "q.sql"
    sql.write_text(ORIG, encoding="utf-8")
    out = tmp_path / "out"
    code = cli.main(["-c", str(tmp_path / "none.toml"), "tune", str(sql),
                     "--explain", str(ROOT / "tests/fixtures/explain_bad.txt"), "--out", str(out)])
    text = capsys.readouterr().out
    assert code == 1  # not verified -> goal not met
    assert "PRODUCT_JOIN" in text and "검증하지 못한 재작성 제안" in text
    assert (out / "suggested_unverified.sql").read_text().strip() == GOOD_SQL
    assert "COLLECT STATISTICS COLUMN (CUST_ID) ON SALES_DB.CUSTOMER;" in (out / "collect_stats.sql").read_text()
    assert (out / "report.md").read_text().startswith("## 요약")


def test_skills_command(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert cli.main(["-c", "none.toml", "skills", "NOT IN"]) == 0
    assert "TP-SUB-01" in capsys.readouterr().out
