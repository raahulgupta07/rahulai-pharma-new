"""Edge / failure-case tests (P-edge) — tools + API against the REAL data.

These assert on STRUCTURE (empty list, found=False, status code), never on
exact numbers, so they stay stable as the underlying data changes. Bogus codes
and gibberish must degrade gracefully (no crash, empty result), and the API
must reject malformed/unauthenticated requests.
"""

import asyncio

import pytest

from app import tools
from app.db import close_pool

BOGUS = "9999999999999"  # a code that matches no article/inventory row


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.run_until_complete(close_pool())
    lp.close()


@pytest.fixture(autouse=True)
def _drain_pool(loop):
    """Close the pool created on the module loop after each tool test.

    conftest's autouse ``_reset_db_pool`` only nulls the pool reference between
    tests; it does not close it, so orphaned asyncpg connections accumulate and
    can exhaust Postgres when this file runs alongside others. Closing on the
    module loop here drains them deterministically. (Skipped harmlessly for the
    API tests, whose pool lives on the TestClient's own loop.)
    """

    yield
    try:
        loop.run_until_complete(close_pool())
    except Exception:  # noqa: BLE001 - pool may belong to another loop (API tests)
        pass


def run(loop, coro):
    return loop.run_until_complete(coro)


# ---- tool-level edge cases -------------------------------------------------


def test_get_stock_bogus_code_empty(loop):
    rows = run(loop, tools.get_stock(BOGUS))
    assert rows == []


def test_get_article_info_bogus_code_empty(loop):
    rows = run(loop, tools.get_article_info(BOGUS))
    assert rows == []


def test_summarize_article_bogus_not_found(loop):
    s = run(loop, tools.summarize_article(BOGUS))
    assert s["found"] is False


def test_top_by_stock_unknown_site_empty(loop):
    rows = run(loop, tools.top_by_stock("NOSUCHSITE", 5))
    assert rows == []


def test_filter_by_price_absurd_min_empty(loop):
    rows = run(loop, tools.filter_by_price(99_999_999))
    assert rows == []


def test_search_by_name_gibberish_empty(loop):
    rows = run(loop, tools.search_by_name("zzzqqq"))
    assert rows == []


def test_get_substitutes_bogus_code_empty(loop):
    subs = run(loop, tools.get_substitutes(BOGUS))
    assert subs == []


# ---- API-level edge cases --------------------------------------------------


def test_chat_invalid_session_token_401(api_client):
    r = api_client.post(
        "/api/embed/chat",
        json={"session_token": "not-a-real-token", "message": "hi"},
    )
    assert r.status_code == 401


def test_session_create_missing_fields_422(api_client):
    r = api_client.post("/api/embed/session/create", json={})
    assert r.status_code == 422
