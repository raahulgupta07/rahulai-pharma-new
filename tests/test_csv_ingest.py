"""CSV ingestion tests — pure parsing, no DB or network.

Covers detect_kind on csv filenames plus parse_catalog / parse_inventory over
small temp CSV files (header at row 0, and a banner variant for catalog).
"""

import pandas as pd

from app.ingest import detect_kind, parse_catalog, parse_inventory


# ---- detect_kind -------------------------------------------------------------


def test_detect_kind_csv_names():
    assert detect_kind("articles-export-2026.csv") == "catalog"
    assert detect_kind("ARTICLES-EXPORT.CSV") == "catalog"
    assert detect_kind("balance_stock_20260701.csv") == "inventory"
    assert detect_kind("stock.csv") == "inventory"
    assert detect_kind("random-notes.csv") is None


# ---- parse_catalog -----------------------------------------------------------


def _catalog_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Article Code": ["A001", "A002", "A001"],  # last row is a dupe
            "Brand Name": ["Paracet", None, "Paracet"],
            "Generic Name": ["Paracetamol", "Ibuprofen", "Paracetamol"],
            "Composition": ["500mg", "200mg", "500mg"],
            "Category": ["OTC", "OTC", "OTC"],
            "Indication": ["ကိုယ်ပူချိန်ကျစေရန်", "Pain relief", "Fever"],
            "Dosage": ["1 tab", "1 tab", "1 tab"],
            "Side Effect": [None, "Nausea", None],
            "MM_Reg": ["R1", "R2", "R1"],
            "MM_Label": ["L1", "L2", "L1"],
            "Status": ["Active", "Active", "Active"],
        }
    )


def test_parse_catalog_csv_header_row0(tmp_path):
    """CSV with the header on row 0 parses without banner skipping."""

    path = tmp_path / "articles-export.csv"
    _catalog_df().to_csv(path, index=False, encoding="utf-8-sig")

    rows = parse_catalog(str(path))
    assert len(rows) == 2  # duped article_code collapsed
    by_code = {r["article_code"]: r for r in rows}
    assert by_code["A001"]["brand_name"] == "Paracet"
    assert by_code["A001"]["indication"] == "ကိုယ်ပူချိန်ကျစေရန်"  # Burmese survives round-trip
    assert by_code["A002"]["brand_name"] == "A002"  # NULL brand falls back to code
    assert by_code["A002"]["side_effect"] == "Nausea"


def test_parse_catalog_csv_with_banner(tmp_path):
    """CSV carrying the xlsx-style 4-row banner is detected and skipped."""

    path = tmp_path / "articles-export-banner.csv"
    header = ",".join(f'"{c}"' for c in _catalog_df().columns)
    banner = "\n".join(["CityCare Export", "", "Generated 2026-07-01", ""])
    body = _catalog_df().to_csv(index=False, encoding=None)
    path.write_text(banner + "\n" + body, encoding="utf-8")
    assert header  # sanity: columns exist

    rows = parse_catalog(str(path))
    assert {r["article_code"] for r in rows} == {"A001", "A002"}


# ---- parse_inventory ---------------------------------------------------------


def test_parse_inventory_csv(tmp_path):
    path = tmp_path / "balance_stock.csv"
    pd.DataFrame(
        {
            "article_code": ["A001", "A002", "A001", "A003"],
            "site_code": ["S1", "S1", "S1", "S2"],  # row 3 dupes (A001, S1)
            "stock_qty": [10, "not-a-number", 99, 5],
            "weighted_cost_price": [1500.5, 200, 1, "unknown"],
        }
    ).to_csv(path, index=False, encoding="utf-8-sig")

    records = parse_inventory(str(path))
    assert len(records) == 3  # dupe (A001, S1) dropped
    rec = {(r[0], r[1]): r for r in records}
    assert rec[("A001", "S1")][3] == 10
    assert rec[("A001", "S1")][4] == 1500.5
    assert rec[("A002", "S1")][3] == 0  # bad qty coerces to 0
    assert rec[("A003", "S2")][4] == 0.0  # non-numeric price coerces to 0.0
    assert all(r[5] == "MMK" for r in records)
