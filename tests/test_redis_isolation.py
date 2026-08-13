"""The suite must never write to the Redis DB the running stack serves from.

`tests/conftest.py` seeds an embed credential (emb1/pk1) into
`pharmacy:credentials` so the fail-closed `/api/embed/session/create` path
works. Built from `get_settings().redis_url`, that write landed on the LIVE
Redis of the running stack — so anyone running `pytest` silently replaced the
real embed credentials with the fixture's, and every deployed widget 401'd
until someone re-seeded them. It happened three times, and each time it looked
like an auth bug rather than a test-suite bug.

conftest now rewrites REDIS_URL to a scratch DB index on the same server before
any client is built. These tests pin that, at three levels: the settings, the
client actually constructed from them, and — the one that matters — the live DB
itself, checked for the fixture's fingerprints after the suite has run.

No LLM and no network beyond localhost Redis, so they cannot flake. The
live-DB test skips when Redis is unreachable (CI without a stack); the first
two run anywhere.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import app.cache as cache
from app.config import get_settings
from tests.conftest import (
    LIVE_REDIS_URL,
    TEST_EMBED_ID,
    TEST_PUBLIC_KEY,
    TEST_REDIS_DB,
    TEST_REDIS_URL,
)

# Credential pairs the suite is known to write. `acme/secret` comes from
# tests/test_internal_credential.py; both have been found polluting the live
# stack. Either one appearing in the live hash means isolation has broken.
FIXTURE_CREDENTIALS = {TEST_EMBED_ID: TEST_PUBLIC_KEY, "acme": "secret"}


def _db_index(url: str) -> str:
    """Return the trailing DB index of a redis URL, or '0' when implicit."""

    m = re.search(r"/(\d+)$", url.rstrip("/"))
    return m.group(1) if m else "0"


def test_settings_point_at_the_scratch_db_not_the_live_one():
    """Every test reads its Redis URL from settings — so settings must be moved."""

    assert _db_index(get_settings().redis_url) == TEST_REDIS_DB
    assert get_settings().redis_url == TEST_REDIS_URL
    assert _db_index(TEST_REDIS_URL) != _db_index(LIVE_REDIS_URL)


def test_scratch_db_is_the_same_server_as_the_live_one():
    """Only the DB index may differ.

    Isolating by pointing at a different host would hide real integration
    failures — a wrong port would pass the test above while testing nothing.
    """

    strip = lambda u: re.sub(r"/\d+$", "", u.rstrip("/"))  # noqa: E731
    assert strip(TEST_REDIS_URL) == strip(LIVE_REDIS_URL)


def test_the_async_cache_client_is_built_from_the_isolated_url():
    """`cache.get_client()` reads settings lazily; pin what it actually built.

    Settings being right is not enough — a client constructed before the
    rewrite, or from a hardcoded URL, would still write to the live DB.
    """

    # Built inside a fresh loop on purpose: the async client's pool creates an
    # asyncio.Lock in __init__, and on 3.9 that needs a current event loop. By
    # the time the full suite reaches this test the last one has been closed,
    # so constructing it bare passes alone and fails in a suite run.
    async def _db_of_a_fresh_client() -> str:
        cache._client = None
        client = cache.get_client()
        return str(client.connection_pool.connection_kwargs.get("db"))

    try:
        assert asyncio.run(_db_of_a_fresh_client()) == TEST_REDIS_DB
    finally:
        cache._client = None


def test_the_live_db_carries_no_fixture_credentials():
    """The guard that would have caught the original bug.

    Reads the LIVE Redis directly — not through settings, which the suite has
    moved — and fails if the suite's own credential pairs are sitting in it.
    """

    redis_sync = pytest.importorskip("redis")

    client = redis_sync.from_url(LIVE_REDIS_URL, decode_responses=True)
    try:
        live = client.hgetall(cache._CRED_KEY)
    except Exception:  # noqa: BLE001 — no stack running is not a failure
        pytest.skip(f"live redis unreachable at {LIVE_REDIS_URL}")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    leaked = {k: v for k, v in live.items() if FIXTURE_CREDENTIALS.get(k) == v}
    assert not leaked, (
        f"test credentials found in the LIVE credential store at {LIVE_REDIS_URL}: "
        f"{sorted(leaked)}. The suite is writing to the running stack's Redis; "
        f"live embeds using these ids are now broken. Check the REDIS_URL rewrite "
        f"in tests/conftest.py."
    )


def test_the_fixture_credential_reached_the_scratch_db():
    """Isolation must not be achieved by the write silently failing.

    `_register_test_credential` swallows every exception so collection works
    without Redis. A wrong scratch URL would therefore look identical to
    working isolation — right up until the fail-closed embed tests 403.
    """

    redis_sync = pytest.importorskip("redis")

    client = redis_sync.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        stored = client.hget(cache._CRED_KEY, TEST_EMBED_ID)
    except Exception:  # noqa: BLE001
        pytest.skip(f"scratch redis unreachable at {TEST_REDIS_URL}")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    assert stored == TEST_PUBLIC_KEY
