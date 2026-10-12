"""Parser behaviour on Teradata 20.0 style plans.

The fixtures copy the phrasing of real TD 20.0 EXPLAIN output (view aliases,
unnamed spool sizes, few-AMP redistribution, outer joins, missing totals ...)
with the example schema's names. To check a real extract as well, run:

    TDAGENT_EXPLAIN_CORPUS=/path/teradata_explain_top50.txt pytest tests/test_explain_td20.py
"""

import os
from pathlib import Path

import pytest

from tdagent.config import TuningConfig
from tdagent.teradata import _clean_error, parameter_markers
from tdagent.tuning.corpus import check, load_extract
from tdagent.tuning.explain import parse_explain, plan_metrics
from tdagent.tuning.findings import from_explain

ROOT = Path(__file__).resolve().parents[1]
VIEW = (ROOT / "tests/fixtures/explain_td20_view.txt").read_text()
TACTICAL = (ROOT / "tests/fixtures/explain_td20_tactical.txt").read_text()


def test_view_aliases_and_quoted_conditions():
    p = parse_explain(VIEW)
    s = p.step("4.2")
    assert [(x.table, x.view, x.access) for x in s.scans] == [("SALES_DB.c", "SalesV", "all-rows")]
    # TRIM(BOTH FROM ...) inside the quoted condition and inside the redistribution keys is not a table access
    assert "TRIM(BOTH FROM SALES_DB.c in view SalesV.GRADE_CD" in s.conditions[0]
    assert s.redistribute_keys.startswith("TRIM(BOTH FROM SALES_DB.c.REGION_CD), (SUBSTR(")
    assert s.est_rows == 36_000 and s.confidence == "no"


def test_redistribution_variants():
    p = parse_explain(VIEW)
    assert p.step("3").geography == "redistributed" and p.step("3").redistribute_keys == ""
    assert p.step("6").redistribute_keys == "'ONLINE'" and p.step("6").redistribute_target == "few"
    assert p.step("7").geography == "redistributed"      # "redistributed by to all AMPs"
    assert p.step("8").geography == "redistributed"      # "assumed to be redistributed by value"
    assert p.step("8").top_n == 101 and p.step("8").est_rows == 20


def test_unnamed_spool_sizes_feed_join_inputs_and_lineage():
    p = parse_explain(VIEW)
    assert p.spool_rows[16] == 12 and p.spool_rows[5] == 12 and p.spool_rows[3] == 20
    s5 = p.step("5")
    assert s5.input_rows == [12, 36_000] and s5.product_comparisons == 432_000 and s5.small_product_join
    assert s5.lineage == ["SALES_DB.p", "SALES_DB.c"]
    s6 = p.step("6")
    assert s6.join_type == "left outer" and s6.join_method == "merge" and s6.input_rows == [432_000, None]
    assert "SALES_DB.t" in s6.lineage and "SALES_DB.p" in s6.lineage
    s7 = p.step("7")
    assert s7.join_type == "right outer" and s7.join_method == "single partition hash"
    assert s7.join_condition == "NOT (CUST_ID IS NULL)" or "CUST_ID" in s7.join_condition


def test_partition_counts_and_total_fallback():
    p = parse_explain(VIEW)
    scan = p.step("7").scans[0]
    assert scan.partitions == "4294967296" and not scan.eliminated and scan.full_scan
    m = plan_metrics(p, {"SALES_DB.SALES_TXN"})
    assert m["partition_misses"] == 1 and m["product_joins"] == 0 and m["small_product_joins"] == 1
    assert m["few_amp_redistributions"] == 1 and m["max_spool_bytes"] == 5_448_229_254
    assert p.total_time == pytest.approx(2.19) and p.total_time_source == "steps"
    assert p.notes and "IPE" in p.notes[0]


def test_tactical_plan_access_paths():
    p = parse_explain(TACTICAL)
    s1 = p.step("1")
    assert [(x.table, x.access, x.partitions) for x in s1.scans] == [
        ("SALES_DB.c", "unique primary index", ""), ("SALES_DB.t", "primary index", "all")]
    assert s1.join_method == "nested" and s1.geography == "local" and not s1.scans[1].full_scan
    assert p.step("2").geography == "local" and p.step("2").est_rows == 1
    assert p.step("3").scans[0].access == "index" and p.step("3").amps == "two-AMP"
    assert p.total_time == 0.0 and p.total_time_source == "steps"


def test_findings_on_td20_plan():
    f = {x.code: x for x in from_explain(parse_explain(VIEW), TuningConfig(), {"SALES_DB.SALES_TXN"})}
    assert f["PRODUCT_JOIN"].severity == "info" and "12 × 36,000" in f["PRODUCT_JOIN"].detail
    assert f["SKEWED_REDISTRIBUTION"].severity == "warning" and "'ONLINE'" in f["SKEWED_REDISTRIBUTION"].detail
    assert "행당 약 128,046 bytes" in f["LARGE_SPOOL"].detail
    assert f["PARTITION_NOT_ELIMINATED"].tables == ["SALES_DB.SALES_TXN"]
    stats = f["STATS_NO_CONFIDENCE"]
    assert "4.2(36,000행)" in stats.detail and "SALES_DB.c" in stats.tables
    assert "8(" not in stats.detail  # 20 rows: below stats_min_rows, not reported


def test_error_cleanup_and_parameter_markers():
    raw = ("[Version 20.0.0.14] [Session 8015393] [Teradata Database] [Error 3807] Object 'PDCR_SP_Status' "
           "does not exist.\n at gosqldriver/teradatasql.(*teradataConnection).makeChainedSystemError")
    msg = _clean_error(Exception(raw))
    assert msg.startswith("[Error 3807] Object 'PDCR_SP_Status' does not exist.") and "volatile" in msg
    assert parameter_markers("SELECT 1 FROM t WHERE a = TRIM(?) AND b = '?' -- ?\n AND c = :?") == 2


def test_extract_loader(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("#" * 20 + "\n# RUN\n\n" + "=" * 40 + "\n[1/2] QueryID=1 ProcID=2 User=U NumSteps=3\n" + "-" * 40 +
                 "\n-- SQL\nSELECT 1\n" + "-" * 40 + "\n-- EXPLAIN\n" + TACTICAL + "\n\n" + "=" * 40 +
                 "\n[2/2] QueryID=3 ProcID=2 User=U\n" + "-" * 40 + "\n-- SQL\nSELECT 2\n" + "-" * 40 +
                 "\n-- EXPLAIN\n!! EXPLAIN FAILED: [Error 3523] no access\n\n# DONE ok=1 failed=1\n",
                 encoding="utf-8")
    plans = load_extract(f)
    assert [p.n for p in plans] == [1, 2] and plans[0].meta["QueryID"] == "1" and plans[0].sql == "SELECT 1"
    assert plans[1].error.startswith("EXPLAIN FAILED") and not plans[1].explain
    cov = check(plans)
    assert cov.plans == 1 and cov.failed == 1 and cov.ok


@pytest.mark.skipif(not os.environ.get("TDAGENT_EXPLAIN_CORPUS"), reason="set TDAGENT_EXPLAIN_CORPUS to a real extract")
def test_real_corpus_has_no_unparsed_phrases():
    cov = check(load_extract(os.environ["TDAGENT_EXPLAIN_CORPUS"]))
    assert cov.plans > 0
    assert cov.ok, "\n".join(cov.problems[:30])
