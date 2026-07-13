"""`GET /admin/sftp/connection` — the partner handoff endpoint.

Two things can go wrong here, and both are quiet:

* **It hands out a password.** The router-level ``require_admin`` gate would give
  it to every branch admin, so the endpoint narrows to ``super_admin``. A test
  that only checked the status code would still pass if the body were returned
  alongside a 403, so these assert the password is not *anywhere* in the
  rejected response.
* **The filename rules could drift.** They are the actual ingest contract, and
  the page presents them to a partner as fact. ``ingest.detect_kind`` is the
  source of truth; these tests re-derive nothing and instead drive the real
  function against every rule the endpoint publishes.

Live Postgres + Redis, like the rest of the suite. The ``_Admin`` helper mirrors
tests/test_admin_scope.py — a real ``users`` row + a real bearer token, so the
auth path under test is the production one, not a mocked dependency.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app import auth as authmod
from app.config import get_settings
from app.ingest import detect_kind


def _pg(query: str, *args, fetch: bool = False):
    """One statement on a private connection — never app.db's loop-bound pool."""

    async def go():
        import asyncpg

        conn = await asyncpg.connect(get_settings().postgres_url)
        try:
            if fetch:
                return [dict(r) for r in await conn.fetch(query, *args)]
            await conn.execute(query, *args)
            return None
        finally:
            await conn.close()

    return asyncio.run(go())


class _Admin:
    """An approved account of a given role, plus its bearer header."""

    def __init__(self, role="admin"):
        self.email = f"sftp-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'SFTP',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


@pytest.fixture
def plain_admin():
    a = _Admin(role="admin")
    yield a
    a.drop()


# ---- auth ------------------------------------------------------------------


def test_connection_requires_a_token(api_client):
    r = api_client.get("/admin/sftp/connection")
    assert r.status_code == 401


def test_connection_is_super_admin_only(api_client, plain_admin):
    """A plain admin passes require_admin and must still be refused.

    The whole point of the endpoint is that it returns a shared secret, so the
    assertion is not just "403" — it is that the password does not appear in the
    body at all. A 403 carrying the password would be a leak with a red status.
    """

    r = api_client.get("/admin/sftp/connection", headers=plain_admin.headers)
    assert r.status_code == 403
    assert get_settings().sftp_password not in r.text


def test_connection_serves_a_super_admin(api_client, super_admin):
    r = api_client.get("/admin/sftp/connection", headers=super_admin.headers)
    assert r.status_code == 200

    s = get_settings()
    body = r.json()
    assert body["password"] == s.sftp_password
    assert body["port"] == s.sftp_public_port
    assert body["username"] == s.sftp_username
    assert body["poll_seconds"] == s.watch_interval_seconds


def test_host_is_never_a_placeholder(api_client, super_admin):
    """The page must be able to tell a CONFIRMED host from a guess.

    The old page printed a literal `<server>`, which is worse than nothing: it
    looks like a value. There is still no placeholder — but `host` may now be a
    *detected* value (the name this request arrived on), which is a suggestion,
    not fact. So the invariant is no longer "host_configured == host is
    non-empty": it is that host_configured is true ONLY for host_source="env".
    A detected host must never present itself as configured, or the operator
    stops confirming it. See tests/test_sftp_keys.py for the three sources.
    """

    body = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()
    assert "<" not in body["host"]
    assert body["host_source"] in ("env", "detected", "none")
    assert body["host_configured"] is (body["host_source"] == "env")
    if body["host_source"] == "none":
        assert body["host"] == ""


# ---- the filename rules agree with detect_kind ------------------------------


def test_rules_cover_every_detect_kind_branch(api_client, super_admin):
    """Every kind detect_kind can return is published, with at least one keyword.

    Pins the AST derivation in admin._detect_kind_keywords: a rewrite of
    detect_kind that it cannot read yields an empty mapping, and this fails
    rather than the page quietly showing a partner no rules at all.
    """

    rules = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()["rules"]
    kinds = {k["kind"]: k["keywords"] for k in rules["kinds"]}

    assert set(kinds) == {"catalog", "inventory"}
    assert all(kws for kws in kinds.values())


def test_every_published_keyword_really_classifies(api_client, super_admin):
    """Drive the real detect_kind with each keyword the page advertises.

    This is the anti-drift test: edit detect_kind's strings and the endpoint's
    output follows, but if the two ever disagree — someone reintroduces a
    hardcoded copy — a keyword will classify as something other than the kind it
    was published under, and this fails.
    """

    rules = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()["rules"]

    for entry in rules["kinds"]:
        for kw in entry["keywords"]:
            for ext in rules["extensions"]:
                assert detect_kind(f"partner-{kw}-export-20260713{ext}") == entry["kind"]


def test_good_examples_ingest_and_bad_examples_do_not(api_client, super_admin):
    """The concrete names shown to a partner, checked against the real matcher."""

    rules = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()["rules"]

    assert rules["good"], "the page shows examples; they must exist"
    for good in rules["good"]:
        # The advertised kind is what the watcher would actually do with it.
        assert good["kind"] is not None
        assert detect_kind(good["name"]) == good["kind"]

    for bad in rules["bad"]:
        assert bad["kind"] is None            # → failed/, exactly as labelled
        assert detect_kind(bad["name"]) is None

    assert rules["unmatched_dir"] == "failed/"
    assert rules["archive_dir"] == "archive/"


def test_key_auth_is_reported_as_managed(api_client, super_admin):
    """The page now offers a button, and the button must not be a lie.

    This used to assert needs_service_restart=True — the API could not install a
    key, because atmoz/sftp rebuilt authorized_keys at container boot and the api
    container could not see that file. With the `sftp_ssh` volume shared between
    the two containers, POST /admin/sftp/keys writes the file sshd re-reads on
    every connection, so a registered key is live with no restart. If that ever
    stops being true, this payload must go back to promising a restart rather
    than the UI keeping a button that appears to work and silently does nothing.
    """

    body = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()
    assert body["key_auth"]["manageable"] is True
    assert body["key_auth"]["needs_service_restart"] is False
    assert body["key_auth"]["keys_dir"]
