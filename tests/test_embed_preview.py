"""Shareable, short-lived embed preview links.

`_demo_page` has always produced a working page, but only as a `demo_html`
string in the `/admin/embed/snippet` response — there was no URL to open or to
send anyone. This adds one, and the whole risk lives in how it is authorised:
the page CANNOT require a Bearer header (a browser navigation sends none, which
is why it is registered on `app` and not on the Bearer-guarded admin router), so
a token in the query string is the only thing standing in front of it.

Pinned here:
  * a minted token renders the real demo page for the store it names;
  * garbage / tampered / expired tokens are indistinguishable 404s — the route
    is not an oracle for which stores have live previews;
  * **a chat session token is refused.** `app.security.create_session_token`
    signs HS256 with the SAME `secret_key` and is handed to every browser that
    loads an embed. Without the `purpose` claim check, any customer's widget
    session would decode here and render a preview page. This is the test that
    would fail if someone "simplified" the decoder;
  * minting is super_admin only, and refuses an unregistered credential exactly
    as `/embed/snippet` does.

Needs live Postgres + Redis, like the rest of the suite (the `api_client`
fixture runs the app lifespan, and `_validate_outlet_request` reads `inventory`).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import jwt
import pytest

from app import auth as authmod
from app.admin import (
    OutletSnippetRequest,
    PREVIEW_PURPOSE,
    PREVIEW_TTL_SECONDS,
    _decode_preview_token,
    _mint_preview_token,
)
from app.config import get_settings

from tests.pgconn import pg
from app.security import create_session_token
from tests.conftest import TEST_EMBED_ID, TEST_PUBLIC_KEY

# A real branch — `_validate_outlet_request` rejects a store_id that is not in
# `inventory`, so an invented code would fail for the wrong reason.
STORE = "20005-CCYK"
BASE_URL = "https://pharma.example.com"


def _pg(query: str, *args, fetch: bool = False):
    """One statement on a private connection — never app.db's loop-bound pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for
    why the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


class _Account:
    """An approved account of a given role, plus its bearer header."""

    def __init__(self, role: str = "super_admin"):
        self.email = f"preview-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'Preview',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.headers = {"Authorization": f"Bearer {authmod.make_token(rows[0])['token']}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def super_admin():
    a = _Account("super_admin")
    yield a
    a.drop()


@pytest.fixture
def plain_admin():
    a = _Account("admin")
    yield a
    a.drop()


def _req(store: str = STORE, **kw) -> OutletSnippetRequest:
    return OutletSnippetRequest(
        store_id=store,
        embed_id=TEST_EMBED_ID,
        public_key=TEST_PUBLIC_KEY,
        base_url=BASE_URL,
        **kw,
    )


# ---- the token itself --------------------------------------------------------


def test_minted_token_carries_the_purpose_claim_and_a_30_minute_ttl():
    claims = _decode_preview_token(_mint_preview_token(_req()))
    assert claims is not None
    assert claims["purpose"] == PREVIEW_PURPOSE
    assert claims["store_id"] == STORE
    assert claims["exp"] - claims["iat"] == PREVIEW_TTL_SECONDS == 1800


@pytest.mark.parametrize("bad", ["", "not-a-token", "a.b.c", "x" * 200])
def test_decoder_refuses_garbage(bad):
    assert _decode_preview_token(bad) is None


def test_decoder_refuses_a_token_signed_with_another_secret():
    now = int(time.time())
    forged = jwt.encode(
        {"purpose": PREVIEW_PURPOSE, "store_id": STORE, "iat": now, "exp": now + 1800},
        "not-the-servers-secret",
        algorithm="HS256",
    )
    assert _decode_preview_token(forged) is None


def test_decoder_refuses_an_unsigned_alg_none_token():
    """`alg: none` is the classic JWT bypass; algorithms=["HS256"] must reject it."""

    now = int(time.time())
    unsigned = jwt.encode(
        {"purpose": PREVIEW_PURPOSE, "store_id": STORE, "iat": now, "exp": now + 1800},
        key="",
        algorithm="none",
    )
    assert _decode_preview_token(unsigned) is None


def test_decoder_refuses_a_chat_session_token():
    """The guard this whole claim exists for.

    A widget session token is HS256 over the very same `secret_key` and is
    already in every embed visitor's browser. It has no `purpose`, so it must
    not decode here — otherwise anyone holding one could render a preview page
    for whatever store it names.
    """

    session = create_session_token(user_id="outlet:x", store_id=STORE, embed_id=TEST_EMBED_ID)
    # sanity: it IS a valid token, just not one for this door
    assert jwt.decode(
        session["session_token"], get_settings().secret_key, algorithms=["HS256"]
    )["store_id"] == STORE
    assert _decode_preview_token(session["session_token"]) is None


# ---- GET /embed/preview ------------------------------------------------------


def test_preview_page_renders_for_a_valid_token(api_client):
    token = _mint_preview_token(_req())
    r = api_client.get("/embed/preview", params={"t": token})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-robots-tag"] == "noindex"

    body = r.text
    assert STORE in body                              # the store it was minted for
    assert "/api/embed/widget.js" in body             # the real widget script
    assert f'data-embed-id="{TEST_EMBED_ID}"' in body
    assert "data-user-sig=" in body                   # HMAC-locked to this store


def test_preview_page_needs_no_authorization_header(api_client):
    """The point of the route: a browser navigation carries no Bearer token."""

    r = api_client.get("/embed/preview", params={"t": _mint_preview_token(_req())})
    assert r.status_code == 200
    assert "Authorization" not in r.request.headers


@pytest.mark.parametrize(
    "params",
    [{}, {"t": ""}, {"t": "garbage"}, {"t": "eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.zzzz"}],
)
def test_absent_or_tampered_token_is_a_flat_404(api_client, params):
    r = api_client.get("/embed/preview", params=params)
    assert r.status_code == 404
    assert STORE not in r.text


def test_a_tampered_payload_does_not_verify(api_client):
    """Flip a character in the payload segment: the signature no longer matches."""

    head, payload, sig = _mint_preview_token(_req()).split(".")
    tampered = f"{head}.{payload[:-2]}{'AA' if payload[-2:] != 'AA' else 'BB'}.{sig}"
    assert api_client.get("/embed/preview", params={"t": tampered}).status_code == 404


def test_expired_token_is_a_404_and_says_nothing_about_expiry(api_client):
    expired = _mint_preview_token(_req(), ttl_seconds=-60)
    r = api_client.get("/embed/preview", params={"t": expired})

    assert r.status_code == 404
    # Same body as a forged token — the route must not confirm that a link for
    # this store ever existed.
    forged = api_client.get("/embed/preview", params={"t": "garbage"})
    assert r.text == forged.text


def test_chat_session_token_is_a_404_at_the_route(api_client):
    session = create_session_token(user_id="outlet:x", store_id=STORE, embed_id=TEST_EMBED_ID)
    r = api_client.get("/embed/preview", params={"t": session["session_token"]})
    assert r.status_code == 404
    assert STORE not in r.text


# ---- POST /admin/embed/preview-link -----------------------------------------


def test_preview_link_mints_a_working_url(api_client, super_admin):
    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": STORE,
            "embed_id": TEST_EMBED_ID,
            "public_key": TEST_PUBLIC_KEY,
            "base_url": BASE_URL,
        },
        headers=super_admin.headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store_id"] == STORE
    assert body["expires_in"] == 1800
    assert body["url"].startswith(f"{BASE_URL}/embed/preview?t=")

    # the URL's token is the credential — follow it against this app
    token = body["url"].split("?t=", 1)[1]
    assert api_client.get("/embed/preview", params={"t": token}).status_code == 200


def test_preview_link_requires_a_bearer_token(api_client):
    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": STORE,
            "embed_id": TEST_EMBED_ID,
            "public_key": TEST_PUBLIC_KEY,
            "base_url": BASE_URL,
        },
    )
    assert r.status_code == 401


def test_preview_link_refuses_a_plain_admin(api_client, plain_admin):
    """Same bar as /embed/snippet: minting a public link is a super_admin act."""

    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": STORE,
            "embed_id": TEST_EMBED_ID,
            "public_key": TEST_PUBLIC_KEY,
            "base_url": BASE_URL,
        },
        headers=plain_admin.headers,
    )
    assert r.status_code == 403


def test_preview_link_refuses_an_unregistered_credential(api_client, super_admin):
    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": STORE,
            "embed_id": "not-a-tenant",
            "public_key": "not-a-key",
            "base_url": BASE_URL,
        },
        headers=super_admin.headers,
    )
    assert r.status_code == 400


def test_preview_link_refuses_an_unknown_store(api_client, super_admin):
    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": "99999-NOPE",
            "embed_id": TEST_EMBED_ID,
            "public_key": TEST_PUBLIC_KEY,
            "base_url": BASE_URL,
        },
        headers=super_admin.headers,
    )
    assert r.status_code == 404


def test_preview_link_refuses_a_non_http_base_url(api_client, super_admin):
    r = api_client.post(
        "/admin/embed/preview-link",
        json={
            "store_id": STORE,
            "embed_id": TEST_EMBED_ID,
            "public_key": TEST_PUBLIC_KEY,
            "base_url": "pharma.example.com",
        },
        headers=super_admin.headers,
    )
    assert r.status_code == 400
