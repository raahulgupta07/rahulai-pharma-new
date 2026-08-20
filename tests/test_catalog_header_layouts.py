"""The article export's header is not always on the same row — read both.

Every article export we had seen carried a 4-row banner ("Pharmacy Search
Engine- Article Export") above the real header, so the xlsx reader hard-coded
``skiprows=4``. On 2026-08-18 CMHL sent one with no banner at all. ``skiprows=4``
then made row 4 — a sweetener — the header, and the file was REJECTED with
"missing required column(s): Article Code, Brand Name. Found: 1000000000416,
1104-SUGAR, Aspartame …". The stock file beside it loaded fine, so the console
showed 120,628 stock rows against a catalog of nothing but stubs.

Two layouts, one reader, and the validator delegates to it rather than keeping
a second copy — the copy is how these two drifted apart in the first place.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import parse_catalog, read_catalog_frame
from app.validation import validate_file

# 12 rows, not 3: the validator refuses a catalog with fewer than 10 usable
# rows, and a fixture that trips a DIFFERENT guard proves nothing about this one.
ROWS = [
    ("1000000308523", "RS BLACK SEED OIL100ML", "Black Seed Oil"),
    ("1000000248472", "COCOHEALTH EXTRA VIRGIN COCONUT OIL 207ML", "Coconut Oil"),
    ("1000000000416", "EQUAL SWEETENER SACHET 100`S 100G", "Aspartame"),
] + [(f"10000003085{i:02d}", f"TEST PRODUCT {i}", "Generic") for i in range(24, 33)]
HEADER = ["ID", "Article Code", "Brand Name", "Generic Name"]


def _frame():
    return pd.DataFrame(
        [[i + 1, c, b, g] for i, (c, b, g) in enumerate(ROWS)], columns=HEADER
    )


def _write(path, banner: bool):
    """Write an article export with or without the 4-row banner above the header."""

    if not banner:
        _frame().to_excel(path, index=False)
        return str(path)
    # Banner: 4 rows of title/blank junk, then the real header, then the rows.
    blank = ["", "", "", ""]
    rows = [
        ["", "", "Pharmacy Search Engine- Article Export", ""],
        blank, blank, blank,
        HEADER,
    ] + [[i + 1, c, b, g] for i, (c, b, g) in enumerate(ROWS)]
    pd.DataFrame(rows).to_excel(path, index=False, header=False)
    return str(path)


@pytest.mark.parametrize("banner", [True, False], ids=["with-banner", "no-banner"])
def test_the_header_is_found_either_way(tmp_path, banner):
    path = _write(tmp_path / "articles-export.xlsx", banner)

    df = read_catalog_frame(path)
    assert "Article Code" in df.columns, list(df.columns)
    assert "Brand Name" in df.columns, list(df.columns)

    rows = parse_catalog(path)
    assert len(rows) == len(ROWS), rows
    assert rows[0]["brand_name"] == "RS BLACK SEED OIL100ML"
    # The banner file must not lose its first data row to the skip, and the
    # bannerless one must not lose four rows to a skip it does not need.
    assert {r["article_code"] for r in rows} == {c for c, _, _ in ROWS}


@pytest.mark.parametrize("banner", [True, False], ids=["with-banner", "no-banner"])
def test_the_validator_accepts_both_layouts(tmp_path, banner):
    """The validator must not reject a file the loader can read.

    It used to keep its own copy of this logic. The copy is why a bannerless
    export was refused at the door while parse_catalog would have handled it.
    """

    path = _write(tmp_path / "articles-export.xlsx", banner)
    report = validate_file(path)
    assert report.ok, report.summary
    assert report.stats["usable_rows"] == len(ROWS), report.stats


def test_a_file_with_neither_layout_is_still_refused(tmp_path):
    """The fallback must not become "accept anything".

    A file whose header is genuinely missing has to fail, or the adaptive read
    turns every malformed export into a silent partial load.
    """

    path = tmp_path / "articles-export.xlsx"
    # Enough rows that the row-count guard is not what fails — the point is
    # that the COLUMN check still fires when no layout matches.
    pd.DataFrame(
        [[f"x{i}", f"y{i}"] for i in range(30)], columns=["Nonsense", "Junk"]
    ).to_excel(path, index=False)
    report = validate_file(str(path))
    assert not report.ok
    assert "Article Code" in report.summary
