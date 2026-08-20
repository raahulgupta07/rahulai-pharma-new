"""Both loaders REPLACE: yesterday's file is gone, today's file is what remains.

A stock export lands every day and is authoritative. That has to mean two
things at once, and only one of them is obvious:

* every row in the new file is loaded, and
* every row that was in the OLD file and is NOT in the new one is gone.

The second half is what an upsert quietly gets wrong, and it is invisible from
the console: the SFTP page lists five copies of ``balance_stock.xlsx`` under one
name, so nothing on screen tells you whether the row you are reading came from
today's file or from one three weeks ago. These tests answer that question in
the only place it can be answered.

The delete/truncate here is GLOBAL by construction, so every case runs inside an
outer transaction that is rolled back — ``ingest_*``'s own ``conn.transaction()``
becomes a savepoint on that connection via a fake pool, and the real tables are
never touched. Same harness as tests/test_catalog_full_sync.py.

``backfill_catalog_stubs`` is stubbed out: it runs on the REAL pool (``q()``),
not the faked connection, so it would write outside the rollback. It is not what
these tests are about.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pandas as pd
import pytest

import app.ingest as ingest_mod
from app.config import get_settings
from app.ingest import FileRejected, ingest_catalog, ingest_file, ingest_inventory


def _code() -> str:
    return f"77{uuid.uuid4().int % 10**10:010d}"[:12]


def _write_stock(path, rows):
    """rows = [(article_code, site_code, qty)] -> a balance_stock CSV."""

    pd.DataFrame(
        {
            "article_code": [r[0] for r in rows],
            "site_code": [r[1] for r in rows],
            "stock_qty": [r[2] for r in rows],
            "weighted_cost_price": [100 for _ in rows],
        }
    ).to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


def _write_articles(path, rows):
    lines = ["Article Code,Brand Name"] + [f"{c},{b}" for c, b in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def run_isolated(body):
    async def go():
        conn = await asyncpg.connect(get_settings().postgres_url)
        await conn.execute("ALTER TABLE catalog ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
        tr = conn.transaction()
        await tr.start()
        orig_pool, orig_stub = ingest_mod.get_pool, ingest_mod.backfill_catalog_stubs

        async def _fake_get_pool():
            return _FakePool(conn)

        async def _no_stubs():
            return 0

        ingest_mod.get_pool = _fake_get_pool
        ingest_mod.backfill_catalog_stubs = _no_stubs
        try:
            return await body(conn)
        finally:
            ingest_mod.get_pool = orig_pool
            ingest_mod.backfill_catalog_stubs = orig_stub
            await tr.rollback()
            await conn.close()

    return asyncio.run(go())


# ---- inventory -------------------------------------------------------------


def test_yesterdays_stock_row_is_gone_and_todays_is_loaded(tmp_path):
    """The whole question, for stock, in one test."""

    gone, kept, fresh = _code(), _code(), _code()
    old = _write_stock(tmp_path / "balance_stock_old.csv",
                       [(gone, "S1", 11), (kept, "S1", 22)])
    new = _write_stock(tmp_path / "balance_stock_new.csv",
                       [(kept, "S1", 33), (fresh, "S2", 44)])

    async def body(conn):
        await ingest_inventory(old)
        first = await conn.fetch(
            "SELECT article_code, stock_qty FROM inventory "
            "WHERE article_code = ANY($1::text[])", [gone, kept, fresh])
        await ingest_inventory(new)
        second = await conn.fetch(
            "SELECT article_code, stock_qty FROM inventory "
            "WHERE article_code = ANY($1::text[])", [gone, kept, fresh])
        return ({r["article_code"]: r["stock_qty"] for r in first},
                {r["article_code"]: r["stock_qty"] for r in second})

    before, after = run_isolated(body)
    assert before == {gone: 11, kept: 22}, before
    # dropped from the file -> dropped from the table
    assert gone not in after, after
    # present in both -> the NEW quantity, not the old one
    assert after[kept] == 33, after
    # new in this file -> loaded
    assert after[fresh] == 44, after


def test_an_empty_stock_file_truncates_nothing(tmp_path):
    """The guard. Without it, POST /api/embed/reload could empty every stock row.

    ingest_file validates and its shrink guard would catch this, but
    reload_from_data_dir and ingest_paths call ingest_inventory DIRECTLY with no
    validation at all — so the guard has to live at the truncate, not above it.
    """

    keep = _code()
    good = _write_stock(tmp_path / "balance_stock.csv", [(keep, "S1", 9)])
    empty = _write_stock(tmp_path / "balance_stock_empty.csv", [])

    async def body(conn):
        await ingest_inventory(good)
        n = await ingest_inventory(empty)
        rows = await conn.fetch(
            "SELECT stock_qty FROM inventory WHERE article_code = $1", keep)
        total = await conn.fetchval("SELECT count(*) FROM inventory")
        return n, [r["stock_qty"] for r in rows], total

    n, kept_rows, total = run_isolated(body)
    assert n == 0
    assert kept_rows == [9], "the empty file wiped the previous stock"
    assert total > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows, because",
    [
        ([], "the file contains no rows"),
        # Rows present, but not one of them carries the two fields that make a
        # stock row. This is the shape a wrong header row produces.
        ([("", "S1", 5), ("", "S2", 6)], "valid article code"),
    ],
)
async def test_the_validated_path_refuses_a_file_that_would_load_nothing(tmp_path, rows, because):
    """Belt: ingest_file never reaches the truncate for a file with no usable rows.

    This is asserted rather than assumed because it is the reason ingest_file
    carries NO 0-row branch of its own — validate_file uses the same predicate
    parse_inventory does, so such a branch would be unreachable. If that ever
    stops being true, this test fails and the branch has to come back.
    """

    path = _write_stock(tmp_path / "balance_stock.csv", rows)
    with pytest.raises(FileRejected) as err:
        await ingest_file(path)
    assert because in str(err.value), str(err.value)


# ---- catalog ---------------------------------------------------------------


def test_yesterdays_article_is_gone_and_todays_is_loaded(tmp_path):
    """The same question for the product file. full_sync, not a merge."""

    gone, kept, fresh = _code(), _code(), _code()
    old = _write_articles(tmp_path / "articles-export-old.csv",
                          [(gone, "OLD ONLY"), (kept, "BEFORE")])
    new = _write_articles(tmp_path / "articles-export-new.csv",
                          [(kept, "AFTER"), (fresh, "NEW ONLY")])

    async def body(conn):
        await ingest_catalog(old, mode="full_sync")
        await ingest_catalog(new, mode="full_sync")
        rows = await conn.fetch(
            "SELECT article_code, brand_name FROM catalog "
            "WHERE article_code = ANY($1::text[])", [gone, kept, fresh])
        return {r["article_code"]: r["brand_name"] for r in rows}

    after = run_isolated(body)
    assert gone not in after, after
    assert after[kept] == "AFTER", after      # updated, not left at BEFORE
    assert after[fresh] == "NEW ONLY", after
