"""The two endpoints that expose the version, and who may read them."""

from __future__ import annotations

from app.version import VERSION


def test_version_endpoint_is_public(api_client):
    """No token: whoever is debugging a deployment must be able to read this."""

    r = api_client.get("/version")

    assert r.status_code == 200
    body = r.json()
    assert body["version"] == VERSION
    assert "git_sha_short" in body and "is_release_build" in body


def test_version_endpoint_carries_the_latest_release_note(api_client):
    body = api_client.get("/version").json()

    assert body["latest_release"] is not None
    assert body["latest_release"]["version"] == VERSION


def test_full_release_history_requires_a_token(api_client):
    """The changelog names which fixes an older deployment is missing."""

    assert api_client.get("/admin/releases").status_code in (401, 403)
