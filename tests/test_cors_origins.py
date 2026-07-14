"""Runtime-managed CORS allowlist.

The embed API is called cross-origin from a customer's browser, so an origin
that is not allowed gets no ``Access-Control-Allow-Origin`` header and the widget
fails with "Failed to fetch". Origins used to be ONLY the ``ALLOWED_ORIGINS`` env
(read once at boot); this adds a Redis-backed runtime layer the admin UI writes to.

Pinned here:
  * normalize_origin accepts real origins, rejects junk and ``*``;
  * add/get/remove round-trip through Redis;
  * DynamicCORS.is_allowed_origin unions env origins with the runtime set;
  * an origin not in either list is refused.
"""

import asyncio

import pytest

from app import cache


def run(coro):
    # A fresh loop per call, and drop the cached redis client so it binds to this
    # loop — the shared client is otherwise left bound to whatever loop a prior
    # test closed, which surfaces as a RuntimeError only under the full suite.
    cache._client = None
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        cache._client = None


# ---- normalize_origin --------------------------------------------------------


@pytest.mark.parametrize(
    "good,expected",
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://Shop.Example.com", "https://shop.example.com"),
        ("http://192.168.2.46:8090", "http://192.168.2.46:8090"),
        ("http://localhost:8000/", "http://localhost:8000"),  # trailing slash trimmed
    ],
)
def test_normalize_accepts_real_origins(good, expected):
    assert cache.normalize_origin(good) == expected


@pytest.mark.parametrize("bad", ["*", "localhost:8000", "http://a/b", "ftp://x", "http://a b", ""])
def test_normalize_rejects_junk(bad):
    with pytest.raises(ValueError):
        cache.normalize_origin(bad)


def test_star_is_refused_from_the_ui():
    """`*` is an env-level decision, never a stray click."""

    with pytest.raises(ValueError):
        cache.normalize_origin("*")


# ---- redis round-trip --------------------------------------------------------


def test_add_get_remove_round_trip():
    async def go():
        o = await cache.add_cors_origin("http://shop.example.com")
        got = await cache.get_cors_origins()
        removed = await cache.remove_cors_origin("http://shop.example.com")
        after = await cache.get_cors_origins()
        await cache.close_client()
        return o, got, removed, after

    added, got, removed, after = run(go())
    assert added == "http://shop.example.com"
    assert "http://shop.example.com" in got
    assert removed == 1
    assert "http://shop.example.com" not in after


# ---- DynamicCORS union -------------------------------------------------------


def test_is_allowed_origin_unions_env_and_runtime(monkeypatch):
    import app.api as api

    # env origins are fixed at middleware init; simulate one
    class _Stub:
        allow_all_origins = False
        allow_origins = ["http://env-site.com"]
        is_allowed_origin = api.DynamicCORS.is_allowed_origin

    monkeypatch.setattr(api, "_EXTRA_CORS", {"http://runtime-site.com"})
    stub = _Stub()

    assert stub.is_allowed_origin("http://env-site.com") is True       # from env
    assert stub.is_allowed_origin("http://runtime-site.com") is True   # from runtime set
    assert stub.is_allowed_origin("http://ENV-SITE.com") is True       # case-insensitive
    assert stub.is_allowed_origin("http://evil.com") is False          # in neither


def test_wildcard_env_allows_everything(monkeypatch):
    import app.api as api

    class _Stub:
        allow_all_origins = True
        allow_origins = []
        is_allowed_origin = api.DynamicCORS.is_allowed_origin

    monkeypatch.setattr(api, "_EXTRA_CORS", set())
    assert _Stub().is_allowed_origin("http://anything.com") is True
