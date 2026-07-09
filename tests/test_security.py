"""Store-scoping + auth tests (deterministic, no LLM).

The whole point of store scoping is tenant isolation: a session locked to one
branch must NEVER read another branch's inventory, even if the caller (or a
compromised model) explicitly asks for a different site. These tests assert
that the scope is enforced at the tool layer and that the session-token auth
flow accepts good signatures and rejects bad/tampered ones.

Real anchors:
  article 1000000024029 = PARACAP PARACETAMOL 10`S
    - 53 sites, total 14963 units
    - at site 20059-CCGMPMTN: stock = 2589
  store CCBHSC = site_code 20060-CCBHSC
"""

import asyncio

import pytest

from app import tools
from app.config import get_settings
from app.db import close_pool
from app.security import decode_session_token, sign_user

ART = "1000000024029"          # PARACAP PARACETAMOL 10`S
SCOPE_SITE = "20059-CCGMPMTN"  # the branch the session is locked to
SCOPE_QTY = 2589               # PARACAP stock at SCOPE_SITE
OTHER_SITE = "20060-CCBHSC"    # store CCBHSC — a *different* branch

SECRET = get_settings().secret_key


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


# ---- tool-level store scoping ---------------------------------------------


def test_get_stock_scoped_to_site(loop):
    token = tools.set_store_scope(SCOPE_SITE)
    try:
        rows = run(loop, tools.get_stock(ART))
        assert rows, "PARACAP must have stock at the scoped site"
        assert all(SCOPE_SITE in r["site_code"] for r in rows)
    finally:
        tools.reset_store_scope(token)


def test_get_stock_cannot_cross_branch(loop):
    # Attacker is scoped to SCOPE_SITE but asks for OTHER_SITE explicitly.
    # The scope must override the requested site -> only SCOPE_SITE rows.
    token = tools.set_store_scope(SCOPE_SITE)
    try:
        rows = run(loop, tools.get_stock(ART, OTHER_SITE))
        assert rows, "scope should still return the locked site's rows"
        assert all(SCOPE_SITE in r["site_code"] for r in rows)
        assert all(OTHER_SITE not in r["site_code"] for r in rows)
    finally:
        tools.reset_store_scope(token)


def test_summarize_article_scoped_total(loop):
    token = tools.set_store_scope(SCOPE_SITE)
    try:
        s = run(loop, tools.summarize_article(ART))
        assert s["found"] is True
        assert s["total_stock"] == SCOPE_QTY  # just this site, NOT 14963
    finally:
        tools.reset_store_scope(token)


# ---- cross-branch availability (deliberate, narrow scope exemption) --------


def test_find_at_other_stores_excludes_scoped_site(loop):
    # Scoped session: the tool may look OUTSIDE the scope (that's its purpose),
    # but must never report the caller's own branch, and returns qty only.
    token = tools.set_store_scope(SCOPE_SITE)
    try:
        rows = run(loop, tools.find_at_other_stores(ART))
        assert rows, "PARACAP is stocked at 53 sites — other branches must show"
        assert all(SCOPE_SITE not in r["site_code"] for r in rows)
        assert any(SCOPE_SITE not in r["site_code"] for r in rows)
        assert all(r["stock_qty"] > 0 for r in rows)
        assert all("price" not in r for r in rows)
        assert len(rows) <= 15
    finally:
        tools.reset_store_scope(token)


def test_find_at_other_stores_does_not_weaken_get_stock(loop):
    # The exemption is tool-local: existing scoped tools stay locked. A scoped
    # session asking get_stock for OTHER_SITE still only gets SCOPE_SITE rows.
    token = tools.set_store_scope(SCOPE_SITE)
    try:
        rows = run(loop, tools.get_stock(ART, OTHER_SITE))
        assert rows
        assert all(SCOPE_SITE in r["site_code"] for r in rows)
        assert all(OTHER_SITE not in r["site_code"] for r in rows)
    finally:
        tools.reset_store_scope(token)


def test_find_at_other_stores_unscoped_no_exclusion(loop):
    rows = run(loop, tools.find_at_other_stores(ART))
    assert rows, "unscoped: article stocked widely, must return rows"
    assert all(r["stock_qty"] > 0 for r in rows)
    assert len(rows) <= 15


# ---- API auth flow ---------------------------------------------------------


def test_session_create_signed_user_carries_store_id(api_client):
    user = {"id": "7", "store_id": SCOPE_SITE}
    sig = sign_user(user, SECRET)
    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1", "user": user, "signature": sig},
    )
    assert r.status_code == 200
    token = r.json()["session_token"]
    claims = decode_session_token(token, SECRET)
    assert claims["store_id"] == SCOPE_SITE


def test_session_create_bad_signature_401(api_client):
    user = {"id": "7", "store_id": SCOPE_SITE}
    r = api_client.post(
        "/api/embed/session/create",
        json={
            "embed_id": "emb1",
            "public_key": "pk1",
            "user": user,
            "signature": "0" * 64,
        },
    )
    assert r.status_code == 401


def test_chat_tampered_token_401(api_client):
    # Mint a valid token, then corrupt its signature segment.
    user = {"id": "7", "store_id": SCOPE_SITE}
    sig = sign_user(user, SECRET)
    tok = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1", "user": user, "signature": sig},
    ).json()["session_token"]
    tampered = tok[:-4] + ("AAAA" if tok[-4:] != "AAAA" else "BBBB")
    r = api_client.post(
        "/api/embed/chat",
        json={"session_token": tampered, "message": "hi"},
    )
    assert r.status_code == 401
