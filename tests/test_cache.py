"""Redis layer tests (P5) — requires Redis up (docker compose)."""

import asyncio
import uuid

import pytest

from app import cache
from app.config import get_settings

SECRET = get_settings().secret_key


def run(coro):
    return asyncio.run(coro)


def test_set_get_roundtrip():
    async def go():
        key = f"pharmacy:test:{uuid.uuid4()}"
        await cache.set_cached(key, "hello", ttl=30)
        got = await cache.get_cached(key)
        await cache.close_client()
        return got

    assert run(go()) == "hello"


def test_query_key_changes_with_version_and_store():
    async def go():
        k_a = await cache.make_query_key("stock?", "S1")
        k_b = await cache.make_query_key("stock?", "S2")
        k_norm = await cache.make_query_key("  STOCK?  ", "S1")  # normalises to k_a
        await cache.close_client()
        return k_a, k_b, k_norm

    a, b, norm = run(go())
    assert a != b  # different store -> different key
    assert a == norm  # whitespace/case-insensitive


def test_answer_cache_then_bump_invalidates():
    async def go():
        msg = f"q-{uuid.uuid4()}"
        await cache.set_cached_answer(msg, "S1", "answer-v1", ttl=60)
        hit = await cache.get_cached_answer(msg, "S1")
        await cache.bump_data_version()  # reload -> new namespace
        miss = await cache.get_cached_answer(msg, "S1")
        await cache.close_client()
        return hit, miss

    hit, miss = run(go())
    assert hit == "answer-v1"
    assert miss is None  # version bump invalidated it


def test_rate_limit_blocks_after_quota():
    async def go():
        user = f"u-{uuid.uuid4()}"
        allowed = [await cache.check_rate_limit(user, limit=3, window=60) for _ in range(5)]
        await cache.close_client()
        return allowed

    allowed = run(go())
    assert allowed == [True, True, True, False, False]


def test_credentials_open_then_strict():
    async def go():
        # fresh: ensure no creds -> open mode allows anything
        await cache.get_client().delete("pharmacy:credentials")
        open_ok = await cache.is_valid_credential("any", "any")
        # register one -> strict
        await cache.register_credential("emb1", "pk1")
        good = await cache.is_valid_credential("emb1", "pk1")
        bad = await cache.is_valid_credential("emb1", "wrong")
        await cache.get_client().delete("pharmacy:credentials")  # cleanup
        await cache.close_client()
        return open_ok, good, bad

    open_ok, good, bad = run(go())
    assert open_ok is True
    assert good is True
    assert bad is False


def test_reload_endpoint_bumps_version(api_client):
    before = api_client.get("/ready").json()["data_version"]
    r = api_client.post("/api/embed/reload")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "reloaded"
    assert body["catalog_rows"] >= 1
    assert body["data_version"] == before + 1
