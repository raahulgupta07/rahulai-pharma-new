"""API-layer tests (P4) — embed contract + HMAC + store scoping.

Network-free tests always run. The live /chat test runs only with a real
OPENROUTER_API_KEY.
"""

import pytest

from app.config import get_settings
from app.security import sign_user

SECRET = get_settings().secret_key


def test_health(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ready_reports_counts(api_client):
    r = api_client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["catalog_rows"] >= 1


def test_session_public_mode(api_client):
    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1"},
    )
    assert r.status_code == 200
    assert r.json()["session_token"]
    assert r.json()["expires_in"] > 0


def test_session_hmac_mode_ok(api_client):
    user = {"id": "42", "store_id": "20025-CCSDIG"}
    sig = sign_user(user, SECRET)
    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1", "user": user, "signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["session_token"]


def test_session_hmac_bad_signature_rejected(api_client):
    user = {"id": "42", "store_id": "20025-CCSDIG"}
    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1", "user": user, "signature": "deadbeef"},
    )
    assert r.status_code == 401


def test_chat_rejects_invalid_token(api_client):
    r = api_client.post("/api/embed/chat", json={"session_token": "garbage", "message": "hi"})
    assert r.status_code == 401


def _has_real_key() -> bool:
    import os

    key = get_settings().openrouter_api_key or ""
    return (
        os.getenv("RUN_LIVE") == "1"
        and key.startswith("sk-or-")
        and "REPLACE" not in key
    )


@pytest.mark.skipif(not _has_real_key(), reason="no real OPENROUTER_API_KEY set")
def test_chat_live_scoped_answer(api_client):
    user = {"id": "42", "store_id": "20052-CCTLKK"}
    sig = sign_user(user, SECRET)
    tok = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "emb1", "public_key": "pk1", "user": user, "signature": sig},
    ).json()["session_token"]
    r = api_client.post(
        "/api/embed/chat",
        json={"session_token": tok, "message": "stock for article 1000000015837?"},
    )
    assert r.status_code == 200
    assert "4154" in r.json()["content"]  # scoped store qty (real anchor)
