"""Shared pytest fixtures for the pharmacy-agent test suite.

The async DB layer uses a module-global asyncpg pool. Different tests run on
different event loops (the FastAPI TestClient uses its own loop; the tool tests
use a per-module loop), and an asyncpg pool is bound to the loop that created
it. Reusing a pool across loops raises "attached to a different loop" errors.

The autouse fixture below resets the pool reference before each test so every
test recreates a fresh pool on its own running loop. This is a test-only
concern — in production the pool is created once inside the serving loop.
"""

import pytest
from fastapi.testclient import TestClient

import app.cache as cache
import app.db as db
from app.config import get_settings


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
    """

    from app.api import app

    with TestClient(app) as client:
        yield client
