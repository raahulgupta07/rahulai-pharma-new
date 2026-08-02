"""A nameless catalog load must be loud, and must be refused.

Background: the 2026-07-28 field reports (14 tickets, 4 branches) all reduce to
one state on the customer host — every ``catalog`` row was a stub, meaning
``brand_name = article_code``. ``search_by_name`` can never match a 13-digit
code, so the agent answered "not found" for products sitting on the shelf, and
in one screenshot reported the brand name of article 1000000008626 as
"1000000008626".

Nothing in the app reported this. The parse succeeded, the row count looked
healthy (5,292), and the admin Browse view actively filtered stub rows out of
sight. These tests pin the three signals that would have caught it.
"""

from pathlib import Path

import pandas as pd
import pytest

from app.ingest import CATALOG_FALLBACK_LIMIT, parse_catalog

REAL_EXPORT = Path("data/articles-export 1.xlsx")


def _write_xlsx(path: Path, frame: pd.DataFrame) -> None:
    """Write ``frame`` under the 4-row banner the real export carries."""

    with pd.ExcelWriter(path) as xl:
        banner = pd.DataFrame([[""], [""], [""], [""]])
        banner.to_excel(xl, index=False, header=False, startrow=0)
        frame.to_excel(xl, index=False, startrow=4)


def test_report_records_matched_columns_and_fallbacks(tmp_path):
    """A healthy file reports its columns and zero brand fallbacks."""

    src = tmp_path / "articles-export.xlsx"
    _write_xlsx(src, pd.DataFrame({
        "Article Code": ["1000000008626", "1000000024029"],
        "Brand Name": ["AEROCORT INHALER", "PARACAP PARACETAMOL 10`S"],
        "Generic Name": ["Beclometasone", "Paracetamol"],
        "Indication": ["asthma", "fever"],
    }))

    report: dict = {}
    rows = parse_catalog(str(src), report)

    assert len(rows) == 2
    assert report["brand_fallbacks"] == 0
    assert "Brand Name" in report["columns_matched"]
    assert "Article Code" in report["columns_matched"]
    # Columns the file genuinely lacks are reported, not silently ignored.
    assert "Dosage" in report["columns_missing"]


def test_missing_brand_column_makes_every_row_a_fallback(tmp_path, caplog):
    """A shifted/renamed header is the one failure that must not stay quiet.

    Without a ``Brand Name`` column every row takes ``brand = code``. The parse
    still "succeeds" and returns a healthy-looking row count — which is exactly
    how a broken load reaches production unnoticed.
    """

    src = tmp_path / "articles-export.xlsx"
    _write_xlsx(src, pd.DataFrame({
        "Article Code": ["1000000008626", "1000000024029"],
        "Brand_Name": ["AEROCORT INHALER", "PARACAP"],  # underscore: unmapped
    }))

    report: dict = {}
    with caplog.at_level("ERROR"):
        rows = parse_catalog(str(src), report)

    assert report["brand_fallbacks"] == len(rows) == 2
    assert "Brand Name" in report["columns_missing"]
    # Every row is nameless — the ratio the loader refuses on.
    assert report["brand_fallbacks"] / len(rows) > CATALOG_FALLBACK_LIMIT
    assert "no 'Brand Name' column" in caplog.text


def test_partial_fallbacks_stay_under_the_refusal_limit(tmp_path):
    """A few blank brands are normal and must NOT block a load.

    The guard has to distinguish "this file is broken" from "this file has some
    gaps". One blank row in four is 25%... so use one in ten, which is the
    shape real exports have.
    """

    src = tmp_path / "articles-export.xlsx"
    codes = [f"10000000{i:05d}" for i in range(10)]
    brands = ["REAL BRAND"] * 9 + [None]
    _write_xlsx(src, pd.DataFrame({"Article Code": codes, "Brand Name": brands}))

    report: dict = {}
    rows = parse_catalog(str(src), report)

    assert report["brand_fallbacks"] == 1
    assert report["brand_fallbacks"] / len(rows) <= CATALOG_FALLBACK_LIMIT


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not in data/")
def test_the_real_customer_export_parses_clean():
    """The shipped article export names every row.

    This is the control. It proves the pipeline was never the problem: the file
    the pharmacists' catalog should have been built from parses to 4,892 named
    rows with no fallbacks at all. Any stub rows in a live database therefore
    came from ``backfill_catalog_stubs`` covering inventory codes the export
    does not list — not from a bad parse.
    """

    report: dict = {}
    rows = parse_catalog(str(REAL_EXPORT), report)

    assert len(rows) > 4000
    assert report["brand_fallbacks"] == 0
    assert "Brand Name" in report["columns_matched"]
    assert "Indication" in report["columns_matched"]

    # The products the field reports said could not be found are all in here.
    by_code = {r["article_code"]: r["brand_name"] for r in rows}
    assert by_code["1000000008626"] == "AEROCORT INHALER"
    assert by_code["1000000024029"] == "PARACAP PARACETAMOL 10`S"

    brands = " | ".join(by_code.values()).upper()
    for missing_in_the_field in ("BIOGESIC", "OMEZ", "MOTILIUM", "AMNOTAC"):
        assert missing_in_the_field in brands
