"""A repeated key in a partner export: keep the LAST value, and say so.

Both parsers de-duplicated with ``if key in seen: continue`` — first occurrence
wins, later ones dropped in silence. Found on 2026-08-13 by sending a real file
over SFTP with a repeated (article, site) pair: the later quantity vanished and
the earlier one loaded.

Two separate defects, and the second is the worse one:

* **First-wins is backwards for this export.** The balance file carries an
  ascending ``id`` column, so a later row is the more recently written record —
  a correction, or a second movement for the same product at the same branch.
  Keeping the earlier one ships a number the partner's own system has already
  superseded.
* **It was silent.** The check reported "111,605 usable rows", the load
  reported 111,604, and nothing explained the difference. A stock figure can be
  quietly wrong with no trace, in a system whose whole job is quoting stock
  figures.

De-duplicating is not itself the bug and cannot be avoided — ``inventory`` is
keyed PRIMARY KEY (article_code, site_code), so a repeat has to collapse. What
was missing was choosing the right survivor and telling somebody.

Pure parsing: no database, no network, no LLM.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

from app.ingest import parse_catalog, parse_inventory


def _xlsx(rows, banner=False):
    """Write rows to a temp xlsx. `banner` mimics the article export's 4 rows."""

    path = tempfile.mktemp(suffix=".xlsx")
    df = pd.DataFrame(rows)
    if banner:
        # parse_catalog reads with skiprows=4; write four filler rows above it.
        with pd.ExcelWriter(path) as xl:
            df.to_excel(xl, index=False, startrow=4)
    else:
        df.to_excel(path, index=False)
    return path


def _inv(site, code, qty):
    return {
        "site_code": site,
        "article_code": code,
        "stock_qty": qty,
        "weighted_cost_price": 100,
    }


# ---- inventory -------------------------------------------------------------


def test_a_repeated_article_and_site_keeps_the_last_quantity():
    """The exact case measured live: 149 then 77777 for the same pair."""

    path = _xlsx([
        _inv("20005-CCYK", "1000000131948", 149),
        _inv("20024-CC73", "1000000131948", 314),
        _inv("20005-CCYK", "1000000131948", 77777),
    ])
    try:
        records = parse_inventory(path)
    finally:
        os.remove(path)

    by_site = {r[1]: r[3] for r in records}
    assert by_site["20005-CCYK"] == 77777, "the earlier row won — first-wins is back"
    assert by_site["20024-CC73"] == 314
    assert len(records) == 2


def test_the_duplicate_is_counted_and_the_counts_reconcile():
    """The silence was the real defect: rows_in_file - duplicates == rows_parsed."""

    path = _xlsx([
        _inv("20005-CCYK", "1000000131948", 149),
        _inv("20005-CCYK", "1000000131948", 77777),
        _inv("20024-CC73", "1000000131948", 314),
    ])
    report: dict = {}
    try:
        records = parse_inventory(path, report)
    finally:
        os.remove(path)

    assert report["duplicates"] == 1
    assert report["rows_in_file"] == 3
    assert report["rows_parsed"] == len(records) == 2
    assert report["rows_in_file"] - report["duplicates"] == report["rows_parsed"]


def test_no_duplicates_reports_zero_not_a_missing_key():
    """The watcher reads `duplicates` unconditionally; it must always be there."""

    path = _xlsx([_inv("20005-CCYK", "1000000131948", 149)])
    report: dict = {}
    try:
        parse_inventory(path, report)
    finally:
        os.remove(path)

    assert report["duplicates"] == 0


def test_the_same_article_at_different_branches_is_not_a_duplicate():
    """The key is (article, site). Collapsing on article alone would erase
    every branch but one — silently, and for the whole catalog."""

    path = _xlsx([
        _inv("20005-CCYK", "1000000131948", 149),
        _inv("20024-CC73", "1000000131948", 314),
        _inv("20026-CC19", "1000000131948", 22),
    ])
    report: dict = {}
    try:
        records = parse_inventory(path, report)
    finally:
        os.remove(path)

    assert len(records) == 3
    assert report["duplicates"] == 0


def test_a_later_blank_quantity_still_wins():
    """NULL means UNKNOWN, and a later UNKNOWN is still the newer fact.

    Tempting to treat a blank as "no information, keep the old number" — but
    that silently invents a quantity the file did not state, which is the
    invariant this repo already fought for (blank is not zero, and not stale).
    """

    path = _xlsx([
        _inv("20005-CCYK", "1000000131948", 149),
        _inv("20005-CCYK", "1000000131948", None),
    ])
    try:
        records = parse_inventory(path)
    finally:
        os.remove(path)

    assert len(records) == 1
    assert records[0][3] is None


# ---- catalog ---------------------------------------------------------------


def test_a_repeated_article_code_keeps_the_last_product_row():
    path = _xlsx(
        [
            {"Article Code": "1000000131948", "Brand Name": "OLD NAME"},
            {"Article Code": "1000000131948", "Brand Name": "NEW NAME"},
        ],
        banner=True,
    )
    report: dict = {}
    try:
        rows = parse_catalog(path, report)
    finally:
        os.remove(path)

    assert len(rows) == 1
    assert rows[0]["brand_name"] == "NEW NAME"
    assert report["duplicates"] == 1


def test_a_later_named_row_clears_the_stub_fallback():
    """A nameless row followed by a named one must not still count as a stub.

    `brand_fallbacks` feeds the stub ratio that /ready alarms on — the signal
    that caught the customer host's broken catalog. Counting a superseded
    fallback would raise a false alarm about data that is in fact fine.
    """

    path = _xlsx(
        [
            {"Article Code": "1000000131948", "Brand Name": None},
            {"Article Code": "1000000131948", "Brand Name": "REAL NAME"},
        ],
        banner=True,
    )
    report: dict = {}
    try:
        rows = parse_catalog(path, report)
    finally:
        os.remove(path)

    assert rows[0]["brand_name"] == "REAL NAME"
    assert report["brand_fallbacks"] == 0


def test_a_later_nameless_row_still_counts_as_a_stub():
    """The other direction: a named row superseded by a blank one is a stub."""

    path = _xlsx(
        [
            {"Article Code": "1000000131948", "Brand Name": "REAL NAME"},
            {"Article Code": "1000000131948", "Brand Name": None},
        ],
        banner=True,
    )
    report: dict = {}
    try:
        rows = parse_catalog(path, report)
    finally:
        os.remove(path)

    assert rows[0]["brand_name"] == "1000000131948"
    assert report["brand_fallbacks"] == 1


# ---- the operator-facing line ----------------------------------------------


@pytest.mark.parametrize(
    "kind, result, must_contain",
    [
        ("inventory", {"rows": 111604, "duplicates": 1}, "1 line(s) repeated"),
        ("catalog", {"rows": 5292, "deleted": 0, "duplicates": 3}, "3 line(s) repeated"),
    ],
)
def test_the_ingest_event_mentions_repeated_lines(kind, result, must_contain):
    """Whoever reads the file history must be told, not just the log file."""

    from app.watcher import _loaded_line

    assert must_contain in _loaded_line(kind, result)


@pytest.mark.parametrize("kind", ["inventory", "catalog"])
def test_a_clean_file_says_nothing_about_duplicates(kind):
    """No duplicates, no noise — an extra clause every time trains people to
    stop reading the line that matters."""

    from app.watcher import _loaded_line

    assert "repeated" not in _loaded_line(kind, {"rows": 10, "deleted": 0})
