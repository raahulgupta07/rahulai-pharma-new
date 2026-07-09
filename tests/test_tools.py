"""Tool-layer tests (P2/P8) — run against the loaded REAL CityCare data.

Anchors are derived from the real article/balance_stock exports:
  article 1000000015837 = ROYAL-D 25G (Electrolyte Powder)
    - stocked at 53 sites, total 37605 units
    - top site 20052-CCTLKK: 4154 units @ 800
"""

import asyncio

import pytest

from app import tools
from app.db import close_pool

ART = "1000000015837"      # ROYAL-D 25G
SITE = "20052-CCTLKK"      # its top-stock site


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.run_until_complete(close_pool())
    lp.close()


def run(loop, coro):
    return loop.run_until_complete(coro)


def test_get_article_info_real(loop):
    rows = run(loop, tools.get_article_info(ART))
    assert rows, "ROYAL-D must exist"
    assert "ROYAL-D" in rows[0]["brand_name"]
    assert "indication" in rows[0]  # clinical field surfaced
    sites = {r["site_code"] for r in rows if r["site_code"]}
    assert SITE in sites


def test_get_stock_top_site(loop):
    rows = run(loop, tools.get_stock(ART, SITE))
    assert len(rows) == 1
    assert rows[0]["stock_qty"] == 4154  # real anchor


def test_summarize_article_real(loop):
    s = run(loop, tools.summarize_article(ART))
    assert s["total_stock"] == 37605     # real: sum across 53 sites
    assert s["site_count"] == 53
    assert s["weighted_avg_price"] > 0


def test_top_by_stock_real_site(loop):
    rows = run(loop, tools.top_by_stock(SITE, 5))
    assert len(rows) == 5
    assert rows[0]["article_code"] == ART   # ROYAL-D leads this site
    assert rows[0]["stock_qty"] == 4154
    qtys = [r["stock_qty"] for r in rows]
    assert qtys == sorted(qtys, reverse=True)


def test_filter_by_price_threshold(loop):
    rows = run(loop, tools.filter_by_price(50000))
    assert all(r["price"] >= 50000 for r in rows)


def test_search_by_name_real(loop):
    rows = run(loop, tools.search_by_name("royal-d"))
    assert any("ROYAL-D" in r["brand_name"] for r in rows)


def test_get_substitutes_excludes_self(loop):
    subs = run(loop, tools.get_substitutes(ART))
    codes = {r["article_code"] for r in subs}
    assert ART not in codes  # never returns itself
