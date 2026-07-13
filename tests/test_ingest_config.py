"""Redis-backed ingest config + its admin endpoints.

The api and the ingest worker are separate containers, so ``poll_seconds`` and
``catalog_mode`` live in a shared Redis hash (``pharmacy:ingest_config``), not in
either process's memory. These pin:

* the round-trip through Redis (write, read back the effective config);
* the clamp on ``poll_seconds`` (5..3600) and rejection of a bad ``catalog_mode``;
* that every /admin/ingest/config endpoint is super_admin only (403 for a plain
  admin — these change ingest behaviour for the whole tenant).

Redis + Postgres up, like the rest of the suite. The endpoint tests mint a real
users row + bearer token (the ``_Admin`` helper), so the auth path is production.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import redis as _redis_sync

from app import auth as authmod
from app import cache
from app.config import get_settings

# Force app.api's import (and agno's import-time asyncio.Lock) during collection,
# while a loop exists — the run()/asyncio.run tests here otherwise clear it
# before api_client's first import. Same guard as tests/test_admin_scope.py.
from app.api import app as _app  # noqa: F401


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_ingest_config():
    """Clear the shared config key around each test so nothing leaks into others.

    A leftover ``poll_seconds`` would otherwise make tests/test_sftp_page's
    ``poll_seconds == watch_interval_seconds`` assertion flap. Sync client, for
    the same reason conftest uses one: this runs outside any test event loop.
    """

    def _clear():
        c = _redis_sync.from_url(get_settings().redis_url, decode_responses=True)
        try:
            c.delete(cache._INGEST_CONFIG_KEY)
        finally:
            c.close()

    _clear()
    yield
    _clear()


# ---- cache layer: round-trip, clamp, validation ----------------------------


def test_defaults_when_unset():
    async def go():
        cfg = await cache.get_ingest_config()
        await cache.close_client()
        return cfg

    cfg = run(go())
    # Default is full_sync: an article upload is authoritative (clear-and-replace),
    # the same shape stock files use. The empty-file guard stops a bad upload from
    # emptying the catalog. Only an unreachable Redis degrades to merge.
    assert cfg["catalog_mode"] == "full_sync"
    assert cfg["poll_seconds"] == get_settings().watch_interval_seconds


def test_redis_error_degrades_to_merge_not_full_sync(monkeypatch):
    """The unset DEFAULT is full_sync, but an unreachable Redis must fall to the
    NON-destructive mode — deleting catalog rows on a config we could not even
    read is the one failure we refuse. So: default full_sync, error merge."""

    class _Boom:
        async def hget(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get_client", lambda: _Boom())

    async def go():
        return await cache.get_catalog_mode()

    assert run(go()) == "merge"


def test_config_round_trips_through_redis():
    async def go():
        await cache.set_ingest_config(poll_seconds=42, catalog_mode="full_sync")
        cache._client = None  # force a fresh client — prove it came from Redis, not memory
        cfg = await cache.get_ingest_config()
        await cache.close_client()
        return cfg

    cfg = run(go())
    assert cfg == {"poll_seconds": 42, "catalog_mode": "full_sync"}


def test_poll_seconds_clamps_low_and_high():
    async def go():
        low = (await cache.set_ingest_config(poll_seconds=1))["poll_seconds"]
        high = (await cache.set_ingest_config(poll_seconds=99999))["poll_seconds"]
        await cache.close_client()
        return low, high

    low, high = run(go())
    assert low == 5      # clamped up to the floor
    assert high == 3600  # clamped down to the ceiling


def test_bad_catalog_mode_rejected():
    async def go():
        with pytest.raises(ValueError):
            await cache.set_ingest_config(catalog_mode="nuke_everything")
        # and nothing was written — so it stays at the unset default
        mode = await cache.get_catalog_mode()
        await cache.close_client()
        return mode

    assert run(go()) == "full_sync"


def test_non_numeric_poll_rejected():
    async def go():
        with pytest.raises(ValueError):
            await cache.set_ingest_config(poll_seconds="soon")
        await cache.close_client()

    run(go())


def test_partial_update_leaves_the_other_field():
    async def go():
        await cache.set_ingest_config(poll_seconds=30, catalog_mode="full_sync")
        after = await cache.set_ingest_config(poll_seconds=60)  # mode untouched
        await cache.close_client()
        return after

    after = run(go())
    assert after == {"poll_seconds": 60, "catalog_mode": "full_sync"}


# ---- endpoints: super_admin only -------------------------------------------


class _Admin:
    """An approved account of a given role + its bearer header."""

    def __init__(self, role="admin"):
        self.email = f"ing-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'Ingest',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


def _pg(query: str, *args, fetch: bool = False):
    async def go():
        import asyncpg

        conn = await asyncpg.connect(get_settings().postgres_url)
        try:
            if fetch:
                return [dict(r) for r in await conn.fetch(query, *args)]
            await conn.execute(query, *args)
            return None
        finally:
            await conn.close()

    return asyncio.run(go())


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


def test_config_get_requires_super_admin(api_client, plain_admin):
    r = api_client.get("/admin/ingest/config", headers=plain_admin.headers)
    assert r.status_code == 403


def test_config_post_requires_super_admin(api_client, plain_admin):
    r = api_client.post(
        "/admin/ingest/config", headers=plain_admin.headers, json={"poll_seconds": 30}
    )
    assert r.status_code == 403


def test_config_get_and_set_as_super_admin(api_client, super_admin):
    # Clear any value a prior test left so we read the true unset default.
    _redis_sync.from_url(get_settings().redis_url).hdel(
        "pharmacy:ingest_config", "catalog_mode"
    )
    got = api_client.get("/admin/ingest/config", headers=super_admin.headers)
    assert got.status_code == 200
    assert got.json()["catalog_mode"] == "full_sync"  # unset default

    put = api_client.post(
        "/admin/ingest/config",
        headers=super_admin.headers,
        json={"poll_seconds": 20, "catalog_mode": "full_sync"},
    )
    assert put.status_code == 200
    assert put.json() == {"poll_seconds": 20, "catalog_mode": "full_sync"}


def test_config_post_rejects_bad_mode(api_client, super_admin):
    r = api_client.post(
        "/admin/ingest/config", headers=super_admin.headers, json={"catalog_mode": "wipe"}
    )
    assert r.status_code == 400
