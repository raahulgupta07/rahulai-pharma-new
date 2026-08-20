"""`GET /admin/analytics/branch-stock` and the section it feeds.

The section exists because the Today page could say "79 rows hold a negative
quantity" and not which branch to ring.

Two things are guarded. The first is the **four bands**: out, low, unknown and
negative are four different answers about one row, and merging any two of them
reports a branch as something it is not — a blank cell folded into "out" turns
a stocked branch into an empty one. The second is **scope**: unscoped, this
hands a branch-pinned admin every other branch's stock position, which is the
leak class `_site_clause` exists for.

The SQL tests read the file rather than a database, so they cost nothing; the
scope test uses the shared api client.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "app" / "admin.py"
TODAY = ROOT / "admin" / "src" / "routes" / "+page.svelte"


@pytest.fixture(scope="module")
def handler() -> str:
    src = ADMIN.read_text()
    m = re.search(
        r'@router\.get\("/analytics/branch-stock"\)(.*?)\n@router\.', src, re.S
    )
    assert m, "the branch-stock endpoint is gone"
    return m.group(1)


@pytest.fixture(scope="module")
def page() -> str:
    return TODAY.read_text()


def test_the_four_bands_are_counted_separately(handler):
    """One FILTER each. A band that shares a counter cannot be told apart."""
    for band, expr in (
        ("out", r"FILTER \(WHERE stock_qty = 0\)\s+AS out"),
        ("low", r"FILTER \(WHERE stock_qty BETWEEN 1 AND 19\)\s+AS low"),
        ("unknown", r"FILTER \(WHERE stock_qty IS NULL\)\s+AS unknown"),
        ("negative", r"FILTER \(WHERE stock_qty < 0\)\s+AS negative"),
    ):
        assert re.search(expr, handler), (
            f"the {band!r} band is no longer counted on its own — merging it "
            f"into another reports a branch as something it is not"
        )


def test_a_blank_quantity_is_never_counted_as_out_of_stock(handler):
    """NULL is UNKNOWN. It is the one value the assistant cannot quote at all."""
    assert "stock_qty IS NULL" in handler
    # The out band must test equality with zero, never `IS NOT DISTINCT FROM`
    # or a COALESCE, both of which would sweep NULL in.
    assert "COALESCE(stock_qty" not in handler, (
        "a COALESCE on stock_qty would turn every unknown quantity into a "
        "measured zero, which is the invariant migration 0001 exists to protect"
    )


def test_coverage_is_null_for_a_branch_with_no_rows(handler):
    """0.0 would rank an unrecorded branch alongside an empty one."""
    assert '"coverage": round(good / n, 4) if n else None' in handler, (
        "coverage no longer returns null for a branch with no rows — a zero "
        "there reads as 'nothing on the shelf' rather than 'nothing recorded'"
    )


def test_the_ranking_is_by_the_band_that_has_a_shape(handler):
    """Measured on the live estate: out=2 rows in 111,654, unknown=0, and the
    1-19 band holds 86% of every branch because the median quantity is 6.
    Ranking by those is 53 near-ties dressed as a league table."""
    order = re.search(r"ORDER BY (.*?)\"\"\"", handler, re.S)
    assert order, "the ORDER BY is gone"
    first = order.group(1).split(",")[0]
    assert "stock_qty < 0" in first, (
        "branch-stock is no longer ranked by impossible quantities; the other "
        "bands do not separate the branches on this data"
    )


def test_the_totals_are_counted_before_the_limit(handler):
    """A caller showing six must be able to say what it is not showing, and
    `total` is the wrong number for that — it would imply the other 47 branches
    are also affected."""
    assert '"affected": sum(1 for r in rows if _i(r["negative"]) > 0)' in handler, (
        "`affected` is gone or is being computed from the limited page"
    )
    assert '"total": len(rows)' in handler, "`total` is no longer the full count"


def test_the_endpoint_is_store_scoped_with_the_only_correct_matcher(handler):
    """A prefix-shaped store id must not substring-match sibling branches."""
    assert "scope: Optional[str] = Depends(caller_store_scope)" in handler, (
        "branch-stock is no longer scoped — it would hand a branch-pinned "
        "admin every other branch's stock position"
    )
    assert '_site_clause("site_code", "$1")' in handler, (
        "the scope is matched by something other than _site_clause; a bare "
        "ILIKE '%x%' lets a numeric-prefix pin read its siblings"
    )


def test_the_section_says_how_many_branches_it_is_not_showing(page):
    """Six rows over thirty-two affected branches reads as "that is all" unless
    the rest are counted out loud — the same rule as the triage cards."""
    m = re.search(r'aria-labelledby="wrong-heading"(.*?)\n  </section>', page, re.S)
    assert m, "the section is gone"
    block = m.group(1)
    assert "branches?.affected > branchRows.length" in block, (
        "the overflow line is gone, so six rows over thirty-two affected "
        "branches reads as the complete list"
    )
    assert "with stock recorded" in block, (
        "the overflow line no longer distinguishes affected branches from all "
        "branches — 'of 53' would imply the other 47 are wrong too"
    )


def test_the_bar_is_scaled_to_the_ranking_not_to_a_share_of_rows(page):
    """A share-of-rows bar reads 0.6% for the worst branch and 0.1% for the
    best, which is the coverage bar this section replaced: 99.4%-100% across
    all 53."""
    m = re.search(r"let branchRows = \$derived\.by\(\(\) => \{(.*?)\n  \}\);", page, re.S)
    assert m, "branchRows is gone"
    body = m.group(1)
    assert "worst" in body and "/ worst" in body, (
        "the bar is no longer scaled to the worst branch shown, so it draws a "
        "share of rows — a quantity too small to see a difference in"
    )


def test_the_head_and_the_stale_card_are_judged_on_the_printed_number(page):
    """`ago()` rounds, so a 23.6-hour file prints "24 hours ago". Judging the
    unrounded value put "landed 24 hours ago, so answers are current" on screen
    against a threshold of 24."""
    assert "Math.round(h) >= STALE_HOURS" in page, (
        "staleness is being judged on the unrounded age again, so the sentence "
        "and the rule can disagree by a rounding step"
    )
    assert page.count("const STALE_HOURS = 24;") == 1
    assert "hours < STALE_HOURS" not in page, (
        "a second, unrounded staleness test has come back"
    )
