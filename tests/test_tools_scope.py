"""Tool-layer store scoping: the matcher must be ANCHORED, in both directions.

Every scoped query in `app/tools.py` goes through `_site_clause`, and until now
nothing proved it. The gap is not theoretical: swapping `_site_clause` for the
documented-bad `site_code ILIKE '%'||$n||'%'` in `app/admin.py` left the entire
`test_admin_scope.py` file green, because every site code the fixtures used was
substring-DISJOINT from the scope tokens. A test that cannot tell anchored from
substring matching is not covering the thing it names.

So each test here seeds a DECOY branch whose code CONTAINS the scope token but is
a different branch — `200059-CCZZ` contains `20005` — and asserts on which side
of the boundary it lands. Both directions matter and they fail differently:

* a positive clause (`get_stock`) over-matches -> one store READS another's
  stock, the leak CLAUDE.md records shipping;
* a negative clause (`find_at_other_stores`, `AND NOT _site_clause`)
  over-matches -> a real sibling branch is EXCLUDED, so the agent tells a
  customer nobody has the drug when a branch nearby does.

Scope is set through `tools.set_store_scope` (the `_STORE_SCOPE` contextvar),
never by passing a site argument — that is the only path the app uses.

Needs live Postgres. Seeding goes through a private asyncpg connection for the
same loop-affinity reason documented in `tests/test_admin_scope.py`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app import tools
from app.config import get_settings
from app.db import close_pool

MINE = "20005-CCYK"
SIBLING = "20024-CC73"
# Contains "20005" and "CCYK" as substrings; is neither's anchored match.
DECOY = "200059-CCZZ"
# Far above the real top line at 20005-CCYK (446), so a leaked row cannot hide
# below the LIMIT in a top-N query.
DECOY_STOCK = 9_999_999


def _pg(query: str, *args):
    async def go():
        import asyncpg

        conn = await asyncpg.connect(get_settings().postgres_url)
        try:
            await conn.execute(query, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.fixture
def scoped_article():
    """One article at my branch, a sibling, and a substring-decoy branch.

    Teardown deletes by article_code AND sweeps the decoy site outright, so a
    killed run cannot strand a row that makes the NEXT run look like a live scope
    leak somewhere else.
    """

    code = f"96{uuid.uuid4().int % 10**10:010d}"[:12]
    _pg("DELETE FROM inventory WHERE site_code=$1", DECOY)
    _pg(
        "INSERT INTO catalog (article_code, brand_name, generic_name) VALUES ($1,$2,$3)",
        code, "SCOPEOL 250MG", "Scopolol",
    )
    _pg(
        """INSERT INTO inventory (article_code, site_code, stock_qty, price)
           VALUES ($1,$2,10,100),($1,$3,7,100),($1,$4,$5,100)""",
        code, MINE, SIBLING, DECOY, DECOY_STOCK,
    )
    yield code
    _pg("DELETE FROM inventory WHERE article_code=$1", code)
    _pg("DELETE FROM inventory WHERE site_code=$1", DECOY)
    _pg("DELETE FROM catalog WHERE article_code=$1", code)


def _scoped(scope: str, coro_factory):
    """Run one tool coroutine under a store scope, on its own loop."""

    async def go():
        token = tools.set_store_scope(scope)
        try:
            return await coro_factory()
        finally:
            tools.reset_store_scope(token)
            await close_pool()

    return asyncio.run(go())


# ---- positive clauses: over-matching LEAKS ---------------------------------


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_get_stock_scope_excludes_a_substring_decoy(scoped_article, token_form):
    rows = _scoped(token_form, lambda: tools.get_stock(scoped_article))
    assert [r["site_code"] for r in rows] == [MINE]
    assert DECOY not in {r["site_code"] for r in rows}


def test_get_article_info_scope_excludes_a_substring_decoy(scoped_article):
    rows = _scoped("20005", lambda: tools.get_article_info(scoped_article))
    sites = {r["site_code"] for r in rows if r["site_code"]}
    assert sites == {MINE}


def test_top_by_stock_scope_excludes_a_substring_decoy(scoped_article):
    """A leaked row shows up as a QUANTITY, because this tool returns no site.

    `top_by_stock` selects article_code/brand_name/stock_qty only — there is no
    `site_code` in the result. Asserting `DECOY not in {r["site_code"] ...}` here
    passes no matter what the matcher does; it was written that way first and
    survived the substring experiment, which is the same vacuity this file exists
    to warn about.

    What is observable: my branch holds 10 of this article and the decoy branch
    holds 9,999,999, well above my branch's real top line (446). So under a
    correct matcher the article is nowhere near the top 50; leaked, it is rank 1
    carrying a quantity my branch does not have.
    """

    rows = _scoped("20005", lambda: tools.top_by_stock("", 50))
    mine = [r for r in rows if r["article_code"] == scoped_article]
    assert mine == [], "another branch's stock ranked inside my branch's top 50"
    assert DECOY_STOCK not in {r["stock_qty"] for r in rows}


# ---- the negative clause: over-matching HIDES a real branch -----------------


def test_find_at_other_stores_keeps_a_branch_that_merely_contains_the_token(
    scoped_article,
):
    """`AND NOT _site_clause(...)` — the inverse failure.

    `200059-CCZZ` is a genuinely different branch that happens to contain my
    token `20005`. Anchored, it is correctly reported as somewhere else that has
    the drug. Substring-matched, `NOT ILIKE '%20005%'` swallows it and the agent
    tells the customer no other branch stocks it — wrong in the direction that
    sends someone home empty-handed.
    """

    rows = _scoped("20005", lambda: tools.find_at_other_stores(scoped_article))
    sites = {r["site_code"] for r in rows}

    assert MINE not in sites, "own branch must never be listed as an OTHER store"
    assert SIBLING in sites
    assert DECOY in sites


# ---- list_sites: the one legitimate ILIKE ----------------------------------


def test_list_sites_scoped_branch_is_anchored(scoped_article):
    """Scoped, `list_sites` must expose exactly one branch — never the decoy.

    Its unscoped branch keeps a substring ILIKE on purpose (the token is the
    user's search string, not a boundary); that is the single legitimate one in
    the codebase per CLAUDE.md, and it is unreachable while a scope is set.
    """

    rows = _scoped("20005", lambda: tools.list_sites(""))
    assert [r["site_code"] for r in rows] == [MINE]


def test_list_sites_unscoped_search_still_matches_substrings():
    """The legitimate ILIKE is intact: a partial token is a SEARCH here, so it is
    allowed to return many branches. Scoping must not have broken it."""

    async def go():
        try:
            return await tools.list_sites("2000")
        finally:
            await close_pool()

    rows = asyncio.run(go())
    assert len(rows) > 1
