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


@pytest.fixture(autouse=True)
def _reset_db_pool():
    """Drop stale pool/redis refs so each test binds clients to its own loop."""

    db._pool = None
    cache._client = None
    yield
    db._pool = None
    cache._client = None


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
