"""Redis layer tests (P5) — requires Redis up (docker compose)."""

import asyncio
import uuid
from unittest import mock

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


def test_ingest_during_run_does_not_poison_cache():
    """An answer computed before an ingest must not be cached after it.

    The agent takes seconds. If stock is reloaded mid-run, the answer in hand
    describes the OLD data. Writing it without a pinned version files it under
    the NEW version, where it looks fresh and is served for a full TTL — a bump
    cannot evict an entry written after the bump.
    """

    async def go():
        msg = f"q-{uuid.uuid4()}"
        version = await cache.get_data_version()  # what the "run" answers against
        await cache.bump_data_version()  # ingest lands mid-run
        await cache.set_cached_answer(msg, "S1", "stale-answer", ttl=60, version=version)
        after = await cache.get_cached_answer(msg, "S1")
        await cache.close_client()
        return after

    # The stale answer is dropped, so the next question re-runs the agent.
    assert run(go()) is None


def test_session_turn_counts_from_one_and_is_per_session():
    """Turn 1 may use the shared cache; turn 2+ is a follow-up and must not."""

    async def go():
        a, b = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
        first = await cache.bump_session_turn(a)
        second = await cache.bump_session_turn(a)
        other = await cache.bump_session_turn(b)  # a different conversation
        await cache.close_client()
        return first, second, other

    first, second, other = run(go())
    assert first == 1  # self-contained question -> cacheable
    assert second == 2  # follow-up -> must bypass the cache
    assert other == 1  # sessions do not share a counter


def test_unchanged_version_still_caches():
    """The freshness guard must not disable caching in the common case."""

    async def go():
        msg = f"q-{uuid.uuid4()}"
        version = await cache.get_data_version()
        await cache.set_cached_answer(msg, "S1", "good-answer", ttl=60, version=version)
        after = await cache.get_cached_answer(msg, "S1")
        await cache.close_client()
        return after

    assert run(go()) == "good-answer"


def test_rate_limit_blocks_after_quota():
    async def go():
        user = f"u-{uuid.uuid4()}"
        allowed = [await cache.check_rate_limit(user, limit=3, window=60) for _ in range(5)]
        await cache.close_client()
        return allowed

    allowed = run(go())
    assert allowed == [True, True, True, False, False]


def test_credentials_fail_closed_when_store_is_empty():
    """An EMPTY credential store must reject everything, not accept everything.

    This is the regression that matters. The old implementation returned True for
    any (embed_id, public_key) whenever the `pharmacy:credentials` hash was empty
    — "open dev mode" — so a production deploy that never seeded a credential
    accepted every embed on the internet. Empty now means nobody is authorised.
    """

    async def go():
        await cache.get_client().delete(cache._CRED_KEY)
        empty_store = await cache.is_valid_credential("any", "any")
        await cache.close_client()
        return empty_store

    assert run(go()) is False


def test_credentials_strict_match():
    async def go():
        await cache.get_client().delete(cache._CRED_KEY)
        await cache.register_credential("emb1", "pk1")
        good = await cache.is_valid_credential("emb1", "pk1")
        wrong_key = await cache.is_valid_credential("emb1", "wrong")
        unknown_id = await cache.is_valid_credential("nobody", "pk1")
        blank = await cache.is_valid_credential("", "")
        await cache.get_client().delete(cache._CRED_KEY)
        await cache.close_client()
        return good, wrong_key, unknown_id, blank

    good, wrong_key, unknown_id, blank = run(go())
    assert good is True
    assert wrong_key is False
    assert unknown_id is False   # a registered KEY does not authorise another ID
    assert blank is False


def test_dev_credential_seeds_only_into_an_empty_store():
    """The seed exists so fail-closed doesn't brick the documented web/web snippet.

    It must fire on an empty store, and must NOT add itself to an operator's set —
    once any real credential is registered, an operator owns the store and we do
    not quietly widen it.
    """

    async def go():
        await cache.get_client().delete(cache._CRED_KEY)
        seeded = await cache.ensure_dev_credential()
        settings = get_settings()
        works = await cache.is_valid_credential(
            settings.embed_dev_credential_id, settings.embed_dev_credential_key
        )
        # A second call is a no-op: the store is no longer empty.
        again = await cache.ensure_dev_credential()

        # An operator's own credential must not attract the seed either.
        await cache.get_client().delete(cache._CRED_KEY)
        await cache.register_credential("real-tenant", "real-key")
        not_seeded = await cache.ensure_dev_credential()
        creds = await cache.list_credentials()

        await cache.get_client().delete(cache._CRED_KEY)
        await cache.close_client()
        return seeded, works, again, not_seeded, creds

    seeded, works, again, not_seeded, creds = run(go())
    assert seeded == get_settings().embed_dev_credential_id
    assert works is True
    assert again is None
    assert not_seeded is None
    assert set(creds) == {"real-tenant"}   # the seed did not join an owned store


def test_dev_credential_can_be_disabled_for_prod():
    """EMBED_DEV_CREDENTIAL=false leaves the store empty, i.e. fully closed."""

    from app.config import Settings

    async def go():
        await cache.get_client().delete(cache._CRED_KEY)
        off = Settings(**{**get_settings().model_dump(), "embed_dev_credential": False})
        with mock.patch("app.cache.get_settings", return_value=off):
            seeded = await cache.ensure_dev_credential()
            still_closed = await cache.is_valid_credential("web", "web")
        n = await cache.get_client().hlen(cache._CRED_KEY)
        await cache.close_client()
        return seeded, still_closed, n

    seeded, still_closed, n = run(go())
    assert seeded is None
    assert still_closed is False
    assert n == 0


def test_reload_endpoint_bumps_version(api_client):
    before = api_client.get("/ready").json()["data_version"]
    r = api_client.post("/api/embed/reload")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "reloaded"
    assert body["catalog_rows"] >= 1
    assert body["data_version"] == before + 1


# ---- semantic answer cache -------------------------------------------------

# Vectors chosen so cosine(_VA, _V_HIT) ~= 0.995 (>= 0.94) and
# cosine(_VA, _V_BELOW) ~= 0.902 (< 0.94), exercising both sides of the default
# threshold without touching the real embedding API.
_VA = [1.0, 0.0, 0.0]
_V_HIT = [1.0, 0.1, 0.0]
_V_BELOW = [1.0, 0.48, 0.0]


def _patch_embedder(monkeypatch, mapping):
    """Force ``embed_query_cached`` to return a fixed vector per message."""

    async def fake_embed(text):
        return mapping[text]

    monkeypatch.setattr("app.embeddings.embed_query_cached", fake_embed)


def _enable_semantic(monkeypatch, enabled=True, threshold=0.94):
    s = get_settings()
    monkeypatch.setattr(s, "semantic_cache_enabled", enabled, raising=False)
    monkeypatch.setattr(s, "semantic_cache_threshold", threshold, raising=False)


def test_semantic_exact_hit_still_works(monkeypatch):
    _enable_semantic(monkeypatch)

    async def boom(text):  # exact hit must NOT reach the embedder
        raise AssertionError("embed_query_cached called on an exact hit")

    # store path DOES embed; only guard the lookup's exact-hit path.
    async def go2():
        store = f"S-{uuid.uuid4()}"
        msg = "do i have panadol"
        _patch_embedder(monkeypatch, {msg: _VA})
        await cache.set_cached_answer(msg, store, "exact-answer", ttl=60)
        monkeypatch.setattr("app.embeddings.embed_query_cached", boom)
        hit = await cache.get_cached_answer(msg, store)  # same text -> exact hash hit
        await cache.close_client()
        return hit

    assert run(go2()) == "exact-answer"


def test_semantic_hit_above_threshold(monkeypatch):
    _enable_semantic(monkeypatch)

    async def go():
        store = f"S-{uuid.uuid4()}"
        stored = "do i have panadol"
        similar = "do we have panadol"
        _patch_embedder(monkeypatch, {stored: _VA, similar: _V_HIT})
        await cache.set_cached_answer(stored, store, "PANADOL: 50 in stock", ttl=60)
        hit = await cache.get_cached_answer(similar, store)  # no exact hash match
        await cache.close_client()
        return hit

    assert run(go()) == "PANADOL: 50 in stock"


def test_semantic_near_miss_below_threshold(monkeypatch):
    _enable_semantic(monkeypatch)

    async def go():
        store = f"S-{uuid.uuid4()}"
        stored = "do i have panadol"
        variant = "do i have panadol 1g"
        _patch_embedder(monkeypatch, {stored: _VA, variant: _V_BELOW})
        await cache.set_cached_answer(stored, store, "PANADOL: 50 in stock", ttl=60)
        miss = await cache.get_cached_answer(variant, store)  # below 0.94 -> no hit
        await cache.close_client()
        return miss

    assert run(go()) is None


def test_semantic_cross_store_never_hits(monkeypatch):
    _enable_semantic(monkeypatch)

    async def go():
        store_a = f"A-{uuid.uuid4()}"
        store_b = f"B-{uuid.uuid4()}"
        stored = "do i have panadol"
        similar = "do we have panadol"
        _patch_embedder(monkeypatch, {stored: _VA, similar: _V_HIT})
        await cache.set_cached_answer(stored, store_a, "STORE-A: 50 in stock", ttl=60)
        cross = await cache.get_cached_answer(similar, store_b)  # near vector, other store
        await cache.close_client()
        return cross

    assert run(go()) is None  # scope key includes store_id -> can never cross


def test_semantic_disabled_flag_no_hit(monkeypatch):
    _enable_semantic(monkeypatch, enabled=False)

    async def go():
        store = f"S-{uuid.uuid4()}"
        stored = "do i have panadol"
        similar = "do we have panadol"
        _patch_embedder(monkeypatch, {stored: _VA, similar: _V_HIT})
        await cache.set_cached_answer(stored, store, "PANADOL: 50 in stock", ttl=60)
        miss = await cache.get_cached_answer(similar, store)
        await cache.close_client()
        return miss

    assert run(go()) is None  # flag off -> exact-only, near phrasing misses


def test_semantic_no_store_id_fails_closed(monkeypatch):
    _enable_semantic(monkeypatch)

    async def go():
        stored = "do i have panadol"
        similar = "do we have panadol"
        _patch_embedder(monkeypatch, {stored: _VA, similar: _V_HIT})
        await cache.set_cached_answer(stored, None, "GLOBAL: 50 in stock", ttl=60)
        miss = await cache.get_cached_answer(similar, None)  # no store scope -> fail closed
        await cache.close_client()
        return miss

    assert run(go()) is None
