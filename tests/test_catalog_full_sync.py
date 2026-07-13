"""catalog full_sync + the manual stale purge — the two delete paths, proven safe.

``ingest_catalog`` gained a ``full_sync`` mode and a ``last_seen`` watermark:

* **merge** (default) upserts and deletes nothing;
* **full_sync** stamps ``last_seen`` on every upserted row, then deletes rows the
  file did NOT contain (``last_seen IS DISTINCT FROM <run_start>``) — the
  authoritative-file semantic WITHOUT a TRUNCATE (a truncate would cascade the
  ``drug_alias`` FK). The critical guard: a file that parses to ZERO rows deletes
  nothing, so a partial/empty upload can never wipe the catalog.

The full_sync delete is global by construction. Testing it against the shared
dev catalog would wipe 5k real rows, so every full_sync case runs inside an OUTER
transaction that is ROLLED BACK — ``ingest_catalog``'s own ``conn.transaction()``
becomes a savepoint on that connection (via a fake pool), and nothing reaches the
real DB. This also keeps the "revert the guard and watch it fail" check safe: a
reverted guard's global delete is rolled back too.

The stale-purge endpoints are exercised through the real API. Their predicate
(``last_seen IS NULL`` included) would also match every legacy row, so a fixture
neutralises real rows to ``now()`` for the duration and restores them after.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
import redis as _redis_sync

import app.ingest as ingest_mod
from app import auth as authmod
from app.config import get_settings
from app.ingest import ingest_catalog

# Import app.api at collection time, while pytest-asyncio still has a current
# event loop: importing it pulls in agno, which builds an asyncio.Lock() at
# import (needs a running loop). The isolation tests below call asyncio.run,
# which clears the loop — so a lazy first import inside api_client would then
# fail. Same guard as tests/test_admin_scope.py.
from app.api import app as _app  # noqa: F401


# ---- helpers ---------------------------------------------------------------


def _rand_code() -> str:
    return f"88{uuid.uuid4().int % 10**10:010d}"[:12]


def _write_article(path, rows):
    """Write a minimal article-export CSV (header + given (code, brand) rows)."""

    lines = ["Article Code,Brand Name"]
    lines += [f"{code},{brand}" for code, brand in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _FakePool:
    """A pool whose ``acquire()`` always hands back the one test connection."""

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
    """Run ``body(conn)`` with ingest_catalog bound to a rolled-back connection.

    Everything ingest_catalog writes (including the global full_sync delete) lands
    in an outer transaction that is discarded, so the real catalog is untouched.
    """

    async def go():
        conn = await asyncpg.connect(get_settings().postgres_url)
        # Autocommit (before the outer txn starts) so the column survives rollback.
        await conn.execute("ALTER TABLE catalog ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
        tr = conn.transaction()
        await tr.start()
        orig = ingest_mod.get_pool

        async def _fake_get_pool():
            return _FakePool(conn)

        ingest_mod.get_pool = _fake_get_pool
        try:
            return await body(conn)
        finally:
            ingest_mod.get_pool = orig
            await tr.rollback()
            await conn.close()

    return asyncio.run(go())


async def _codes_present(conn, codes):
    rows = await conn.fetch(
        "SELECT article_code FROM catalog WHERE article_code = ANY($1::text[])", codes
    )
    return {r["article_code"] for r in rows}


# ---- merge vs full_sync ----------------------------------------------------


def test_merge_deletes_nothing(tmp_path):
    c1, c2 = _rand_code(), _rand_code()
    both = _write_article(tmp_path / "articles-both.csv", [(c1, "AAA"), (c2, "BBB")])
    only1 = _write_article(tmp_path / "articles-one.csv", [(c1, "AAA")])

    async def body(conn):
        await ingest_catalog(str(both), mode="merge")
        res = await ingest_catalog(str(only1), mode="merge")  # c2 omitted
        return res, await _codes_present(conn, [c1, c2])

    res, present = run_isolated(body)
    assert res["deleted"] == 0
    assert c1 in present and c2 in present  # merge keeps the omitted row


def test_full_sync_deletes_only_rows_absent_from_the_file(tmp_path):
    c1, c2 = _rand_code(), _rand_code()
    both = _write_article(tmp_path / "articles-both.csv", [(c1, "AAA"), (c2, "BBB")])
    only1 = _write_article(tmp_path / "articles-one.csv", [(c1, "AAA")])

    async def body(conn):
        await ingest_catalog(str(both), mode="merge")
        res = await ingest_catalog(str(only1), mode="full_sync")  # c2 discontinued
        return res, await _codes_present(conn, [c1, c2])

    res, present = run_isolated(body)
    assert c1 in present       # present in the file -> kept
    assert c2 not in present   # absent from the file -> deleted
    assert res["deleted"] >= 1


def test_full_sync_stamps_last_seen_on_upserted_rows(tmp_path):
    c1 = _rand_code()
    f = _write_article(tmp_path / "articles.csv", [(c1, "AAA")])

    async def body(conn):
        await ingest_catalog(str(f), mode="full_sync")
        return await conn.fetchval("SELECT last_seen FROM catalog WHERE article_code=$1", c1)

    assert run_isolated(body) is not None  # every upserted row carries a watermark


def test_full_sync_empty_file_deletes_nothing(tmp_path):
    """THE guard: a 0-row parse must not wipe the catalog in full_sync.

    Reverting the ``if not rows: return`` guard makes this fail — the delete then
    fires with a run_start no row carries and removes everything.
    """

    c1, c2 = _rand_code(), _rand_code()
    seed = _write_article(tmp_path / "articles-seed.csv", [(c1, "AAA"), (c2, "BBB")])
    empty = _write_article(tmp_path / "articles-empty.csv", [])  # header only, no rows

    async def body(conn):
        await ingest_catalog(str(seed), mode="merge")
        res = await ingest_catalog(str(empty), mode="full_sync")
        return res, await _codes_present(conn, [c1, c2])

    res, present = run_isolated(body)
    assert res == {"rows": 0, "deleted": 0}
    assert c1 in present and c2 in present  # nothing removed


# ---- stale purge endpoints -------------------------------------------------


def _pg(query: str, *args, fetch: bool = False):
    async def go():
        conn = await asyncpg.connect(get_settings().postgres_url)
        try:
            if fetch:
                return [dict(r) for r in await conn.fetch(query, *args)]
            await conn.execute(query, *args)
            return None
        finally:
            await conn.close()

    return asyncio.run(go())


class _Admin:
    def __init__(self, role="admin"):
        self.email = f"stale-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'Stale',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


@pytest.fixture
def plain_admin():
    a = _Admin(role="admin")
    yield a
    a.drop()


@pytest.fixture
def stale_row():
    """One catalog row aged well past any purge window, with real rows neutralised.

    The purge predicate includes ``last_seen IS NULL``, and every real dev row is
    NULL, so those are set to ``now()`` for the test and restored to NULL after —
    otherwise a purge here would delete the whole catalog.
    """

    _pg("ALTER TABLE catalog ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
    _pg("UPDATE catalog SET last_seen = now() WHERE last_seen IS NULL")
    code = _rand_code()
    _pg(
        "INSERT INTO catalog (article_code, brand_name, last_seen) "
        "VALUES ($1,'STALEOL', now() - interval '999 days')",
        code,
    )
    yield code
    _pg("DELETE FROM catalog WHERE article_code=$1", code)
    _pg("UPDATE catalog SET last_seen = NULL")  # restore the dev DB's original state


def _data_version() -> int:
    c = _redis_sync.from_url(get_settings().redis_url, decode_responses=True)
    try:
        v = c.get("pharmacy:data_version")
        return int(v) if v else 0
    finally:
        c.close()


def test_stale_preview_counts_and_deletes_nothing(api_client, super_admin, stale_row):
    r = api_client.get("/admin/ingest/stale?days=30", headers=super_admin.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert "cutoff" in body
    # Preview must not delete: the row is still there.
    still = _pg("SELECT 1 FROM catalog WHERE article_code=$1", stale_row, fetch=True)
    assert still == [{"?column?": 1}]


def test_purge_stale_deletes_and_busts_data_version(api_client, super_admin, stale_row):
    before = _data_version()
    r = api_client.post(
        "/admin/ingest/purge-stale", headers=super_admin.headers, json={"days": 30}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] >= 1
    assert body["data_version"] > before  # deletions change answers -> cache busted
    # the stale row is gone
    assert _pg("SELECT 1 FROM catalog WHERE article_code=$1", stale_row, fetch=True) == []


def test_purge_refuses_days_below_one(api_client, super_admin):
    r = api_client.post(
        "/admin/ingest/purge-stale", headers=super_admin.headers, json={"days": 0}
    )
    assert r.status_code == 400


def test_stale_preview_refuses_days_below_one(api_client, super_admin):
    r = api_client.get("/admin/ingest/stale?days=0", headers=super_admin.headers)
    assert r.status_code == 400


def test_stale_endpoints_are_super_admin_only(api_client, plain_admin):
    assert api_client.get("/admin/ingest/stale?days=30", headers=plain_admin.headers).status_code == 403
    assert (
        api_client.post(
            "/admin/ingest/purge-stale", headers=plain_admin.headers, json={"days": 30}
        ).status_code
        == 403
    )
