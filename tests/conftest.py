"""Shared pytest fixtures for the pharmacy-agent test suite.

The async DB layer uses a module-global asyncpg pool. Different tests run on
different event loops (the FastAPI TestClient uses its own loop; the tool tests
use a per-module loop), and an asyncpg pool is bound to the loop that created
it. Reusing a pool across loops raises "attached to a different loop" errors.

The autouse fixture below resets the pool reference before each test so every
test recreates a fresh pool on its own running loop. This is a test-only
concern — in production the pool is created once inside the serving loop.
"""

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

# --- Redis isolation: this must run before anything builds a client ---------
#
# The suite writes real keys — the emb1/pk1 embed credential below, cache
# entries, rate-limit counters. Pointed at the URL the running stack serves
# from, `_register_test_credential` rewrites `pharmacy:credentials` on the LIVE
# instance, and every deployed embed starts 401-ing until someone re-seeds it.
# That happened three times before this fix; it is silent, and it looks like an
# auth bug rather than a test-suite bug.
#
# So the whole suite is redirected to a scratch DB **on the same server**: same
# host, same port, different DB index. Nothing else about the environment
# changes, and no test needs to know. Override with TEST_REDIS_DB if 15 is in
# use for something else.
TEST_REDIS_DB = os.environ.get("TEST_REDIS_DB", "15")


def _isolate_redis_db() -> str:
    """Rewrite REDIS_URL to the scratch DB and drop the cached Settings."""

    live = os.environ.get("REDIS_URL") or get_settings().redis_url
    isolated = re.sub(r"/\d+$", "", live.rstrip("/")) + f"/{TEST_REDIS_DB}"
    os.environ["REDIS_URL"] = isolated
    get_settings.cache_clear()  # the live URL is already cached by the read above
    return isolated


LIVE_REDIS_URL = os.environ.get("REDIS_URL") or get_settings().redis_url
TEST_REDIS_URL = _isolate_redis_db()

import app.cache as cache  # noqa: E402 — must import after the URL is rewritten
import app.db as db  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_db_pool():
    """Drop stale pool/redis refs so each test binds clients to its own loop."""

    db._pool = None
    cache._client = None
    yield
    db._pool = None
    cache._client = None


# The embed credential check is fail-closed (cache.is_valid_credential), so the
# emb1/pk1 pair the API and security tests have always used must actually be
# registered or every /session/create in the suite would 403.
#
# Seeded with a SYNCHRONOUS redis client on purpose: the async client in
# app.cache is bound to whichever event loop first touched it, and this fixture
# runs outside any test loop. Reaching for asyncio.run() here would bind the
# module-global client to a loop that is closed before the test body runs.
TEST_EMBED_ID = "emb1"
TEST_PUBLIC_KEY = "pk1"


@pytest.fixture(autouse=True)
def _register_test_credential():
    """Register the suite's embed credential in Redis (fail-closed API needs it)."""

    import redis as _redis_sync

    client = _redis_sync.from_url(get_settings().redis_url, decode_responses=True)
    try:
        client.hset(cache._CRED_KEY, TEST_EMBED_ID, TEST_PUBLIC_KEY)
    except Exception:  # noqa: BLE001 — Redis-less collection must not error here
        pass
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    yield


@pytest.fixture
def api_client():
    """Context-managed TestClient: one persistent portal loop for all requests.

    Using TestClient as a context manager runs the app lifespan and keeps a
    single event loop for the duration, so the loop-bound asyncpg/redis clients
    are reused across requests (mirroring uvicorn's single-loop runtime).

    A current event loop must exist before that starts. On Python 3.9,
    ``asyncio.run`` CLOSES its loop and leaves the thread with none set, so any
    test using it (several here call it directly through ``_pg``) poisons every
    later ``api_client`` in the same process::

        RuntimeError: There is no current event loop in thread 'MainThread'.

    It surfaced as an ERROR at fixture setup, not a failure, so the test never
    ran and the suite still reported "17 passed, 1 error" — green enough to
    scroll past. Installing a fresh loop when none is current makes the fixture
    independent of whatever ran before it.
    """

    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    from app.api import app

    with TestClient(app) as client:
        yield client
