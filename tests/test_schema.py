from pathlib import Path

from tdagent.schema import Column, SchemaStore, Table
from tdagent.sql_lint import catalog_issues

ROOT = Path(__file__).resolve().parents[1]


def sample_store():
    return SchemaStore.load_dir(ROOT / "schema")


def test_ddl_parsing():
    store = sample_store()
    t = store.find("SALES_DB.SALES_TXN")
    assert t is not None
    assert t.comment == "판매 거래 내역 (매출)"
    assert t.primary_index == ["CUST_ID"]
    assert "RANGE_N" in t.partitioned_by
    sale_dt = next(c for c in t.columns if c.name == "SALE_DT")
    assert sale_dt.type == "DATE" and not sale_dt.nullable and "파티션" in sale_dt.comment
    assert store.find("customer").primary_index == ["CUST_ID"]  # unique short name lookup


def test_search_korean_with_particles():
    store = sample_store()
    names = [t.name for t in store.search("고객별 매출을 알려줘")]
    assert names[:2] == ["SALES_TXN", "CUSTOMER"] or set(names[:2]) == {"SALES_TXN", "CUSTOMER"}
    assert store.search("상품명")[0].name == "PRODUCT"


def test_json_roundtrip(tmp_path):
    store = SchemaStore([Table("DB", "T1", comment="테스트", columns=[Column("A", "INTEGER", False, "에이")],
                               primary_index=["A"])])
    store.save_json(tmp_path / "c.json")
    loaded = SchemaStore.load_dir(tmp_path)
    assert loaded.find("DB.T1").render() == store.find("DB.T1").render()


def test_catalog_issues_detects_hallucinations():
    store = sample_store()
    ok = "SELECT c.CUST_NM, SUM(t.SALE_AMT) FROM SALES_DB.SALES_TXN t JOIN SALES_DB.CUSTOMER c ON t.CUST_ID = c.CUST_ID GROUP BY 1"
    assert catalog_issues(ok, store) == []
    assert catalog_issues("SELECT a FROM SALES_DB.ORDERS", store)[0].code == "unknown_table"
    bad_col = "SELECT c.CUSTOMER_NAME FROM SALES_DB.CUSTOMER c"
    assert catalog_issues(bad_col, store)[0].code == "unknown_column"
    assert catalog_issues("SELECT 1", SchemaStore()) == []
