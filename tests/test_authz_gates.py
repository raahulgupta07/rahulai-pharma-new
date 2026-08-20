"""Authorization gates on the admin surface: privilege escalation + conversation scope.

Three holes, one theme — the router-level ``require_admin`` was treated as if it
were the whole authorization story, and it is not:

* ``POST /admin/users`` passed a caller-supplied ``role`` straight through to
  ``auth.create_user``. Any ``admin`` — including one pinned to a single branch —
  could mint a ``super_admin``, approve it, and log in as it. ``PATCH`` could
  promote an existing account the same way. A full privilege escalation, reachable
  from the console's own Users page.
* ``GET/PUT /admin/auth-config`` let any admin read the customer's auth
  infrastructure and repoint OIDC at a realm they control — an account takeover,
  since ``_merge_external`` matches on whatever email the IdP asserts.
* ``GET /admin/conversations`` had **no store scope at all**, so a branch-pinned
  admin read every other branch's customer conversations verbatim. The analytics
  block in ``app/admin.py`` named it in a comment as "exactly the mistake not to
  copy" and it stayed unfixed anyway.

Plus the two operator endpoints that ship with them: the auth-config test probes
and ``GET /admin/security-log``, which must degrade rather than 500 when the
``auth_events`` table has not been deployed yet.

Fixtures follow ``tests/test_admin_scope.py``: real rows through a private
asyncpg connection, and **substring decoys** for every scope assertion.

⚠️ Every DB setup/teardown here goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop created
it, and under ``api_client`` that is the TestClient's portal loop. An
``asyncio.run`` in the test body would hand that pool to a second loop and raise
"attached to a different loop". A private connection has no such affinity.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app import auth as authmod
from app.config import get_settings

from tests.pgconn import pg


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _ensure_schema():
    """The columns/tables these tests need, without importing the app's pool."""

    _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    _pg(
        """CREATE TABLE IF NOT EXISTS chat_logs (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               lang TEXT, store_id TEXT,
               question TEXT, answer TEXT,
               cached BOOLEAN, latency_ms INT
           )"""
    )


# ---- accounts --------------------------------------------------------------


class _Admin:
    """An approved account + a bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"authz-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved, store_id)
               VALUES ($1,'AuthzProbe',$2,ARRAY['local'],TRUE,TRUE,$3)
               RETURNING id, email, role""",
            self.email, role, store_id, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def admin():
    """A plain, approved `admin`. Passes require_admin; must fail the new gates."""

    a = _Admin(role="admin")
    yield a
    a.drop()


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


@pytest.fixture
def victim():
    """A throwaway account for the mutation endpoints to aim at.

    The escalation tests must not be able to pass by accident because the target
    did not exist — a 404/400 would be indistinguishable from a 403 if we asserted
    "not 200". So the target is real and the assertion is exactly 403.
    """

    a = _Admin(role="user")
    yield a
    a.drop()


# ---- 1. privilege escalation: only a super_admin may mutate accounts --------


def test_plain_admin_cannot_create_a_user(api_client, admin):
    """`POST /admin/users` is super_admin-only — it is the escalation path."""

    r = api_client.post(
        "/admin/users",
        headers=admin.headers,
        json={"email": f"nope-{uuid.uuid4().hex[:8]}@corp.mm", "name": "N",
              "password": "Passw0rd!", "role": "user"},
    )
    assert r.status_code == 403, r.text


def test_plain_admin_cannot_mint_a_super_admin(api_client, admin):
    """**The escalation itself.** An admin asking for `role: super_admin` must be
    refused, AND no such row may exist afterwards.

    Asserting the 403 alone would not prove the hole is closed: a handler that
    rejected the response but had already committed the INSERT would still pass.
    So the database is checked directly.
    """

    email = f"escalate-{uuid.uuid4().hex[:8]}@corp.mm"
    r = api_client.post(
        "/admin/users",
        headers=admin.headers,
        json={"email": email, "name": "Escalated",
              "password": "Passw0rd!", "role": "super_admin"},
    )
    assert r.status_code == 403, r.text

    rows = _pg("SELECT id, role FROM users WHERE email=$1", email, fetch=True)
    assert rows == [], f"the account was created anyway: {rows}"


def test_plain_admin_cannot_promote_an_existing_account(api_client, admin, victim):
    """`PATCH` is the same escalation by a different door — promotion."""

    r = api_client.patch(
        f"/admin/users/{victim.id}",
        headers=admin.headers,
        json={"role": "super_admin"},
    )
    assert r.status_code == 403, r.text

    rows = _pg("SELECT role FROM users WHERE id=$1", victim.id, fetch=True)
    assert rows[0]["role"] == "user", "the role changed despite the 403"


def test_plain_admin_cannot_delete_an_account(api_client, admin, victim):
    """Deleting the last super_admin is not a branch manager's call."""

    r = api_client.delete(f"/admin/users/{victim.id}", headers=admin.headers)
    assert r.status_code == 403, r.text
    assert _pg("SELECT id FROM users WHERE id=$1", victim.id, fetch=True) != []


def test_super_admin_can_create_and_delete_a_user(api_client, super_admin):
    """The gate must not have broken the legitimate path — a super_admin still works."""

    email = f"created-{uuid.uuid4().hex[:8]}@corp.mm"
    r = api_client.post(
        "/admin/users",
        headers=super_admin.headers,
        json={"email": email, "name": "Legit", "password": "Passw0rd!", "role": "user"},
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    try:
        r = api_client.patch(
            f"/admin/users/{new_id}", headers=super_admin.headers,
            json={"role": "admin"},
        )
        assert r.status_code == 200, r.text
        assert _pg("SELECT role FROM users WHERE id=$1", new_id, fetch=True)[0]["role"] == "admin"

        r = api_client.delete(f"/admin/users/{new_id}", headers=super_admin.headers)
        assert r.status_code == 200, r.text
        assert _pg("SELECT id FROM users WHERE id=$1", new_id, fetch=True) == []
    finally:
        _pg("DELETE FROM users WHERE email=$1", email)


def test_admin_can_still_read_the_user_roster(api_client, admin):
    """Deliberate judgement call: the READ stays at require_admin.

    Reading who exists is not the dangerous half — an admin already sees their
    colleagues in the console, and the exposure is a name and a role, not a new
    super_admin. Narrowing the read too would blank the Users page for every
    plain admin to protect information they already have. If this test is ever
    changed to expect 403, that is a product decision, not a security fix.
    """

    r = api_client.get("/admin/users", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert any(u["email"] == admin.email for u in r.json())


# ---- 2. auth-config is super_admin-only, read and write --------------------


def test_plain_admin_cannot_read_auth_config(api_client, admin):
    """The non-secret half is still a map of the customer's auth infrastructure."""

    r = api_client.get("/admin/auth-config", headers=admin.headers)
    assert r.status_code == 403, r.text


def test_plain_admin_cannot_repoint_oidc(api_client, admin, super_admin):
    """Repointing the discovery URL at a hostile realm is an account takeover:
    `_merge_external` trusts the email the IdP asserts."""

    r = api_client.put(
        "/admin/auth-config",
        headers=admin.headers,
        json={"oidc_discovery_url": "http://evil.example/.well-known/openid-configuration"},
    )
    assert r.status_code == 403, r.text

    # And nothing was written on the way to the refusal. Read back through the
    # super_admin, since the plain admin can no longer read the config either.
    cfg = api_client.get("/admin/auth-config", headers=super_admin.headers)
    assert cfg.status_code == 200, cfg.text
    assert "evil.example" not in str(cfg.json())


def test_super_admin_can_read_auth_config(api_client, super_admin):
    """The legitimate path still works — and secrets are still masked."""

    r = api_client.get("/admin/auth-config", headers=super_admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ldap_bind_password"] == ""       # never sent back
    assert body["oidc_client_secret"] == ""
    assert "ldap_bind_password_set" in body


def test_auth_config_test_endpoints_are_super_admin_only(api_client, admin):
    """The probes reach out to infrastructure; a plain admin may not fire them."""

    for path in ("/admin/auth-config/test-ldap", "/admin/auth-config/test-oidc"):
        r = api_client.post(path, headers=admin.headers)
        assert r.status_code == 403, f"{path}: {r.text}"


def test_oidc_probe_reports_an_unconfigured_realm_without_500(api_client, super_admin):
    """With no discovery URL set the probe must answer the contract, not error.

    The whole point of a test button is that it never itself fails; an operator
    debugging auth cannot also be debugging the debugger.
    """

    r = api_client.post("/admin/auth-config/test-oidc", headers=super_admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "detail", "ms", "issuer", "endpoints"}
    assert isinstance(body["ok"], bool)
    assert isinstance(body["ms"], int)
    assert body["detail"]


def test_ldap_probe_never_echoes_the_bind_password(api_client, super_admin):
    """`detail` must be actionable and secret-free, whatever the failure was."""

    r = api_client.post("/admin/auth-config/test-ldap", headers=super_admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "detail", "ms"}
    assert isinstance(body["ms"], int)
    assert body["detail"]

    pw = get_settings().ldap_bind_password
    if pw:
        assert pw not in body["detail"]


# ---- 3. /admin/conversations is store-scoped -------------------------------

# Substring decoys, the technique from test_tools_scope.py / test_admin_scope.py:
# a branch whose code CONTAINS the scope token but is a DIFFERENT branch.
# `200059-CCZZ` contains `20005`; `20099-CCYKX` contains `CCYK`. Anchored matching
# (tools._site_clause: full code / numeric prefix / alpha suffix) excludes both;
# the documented-bad `store_id ILIKE '%'||$n||'%'` returns them.
MINE = "20005-CCYK"
SIBLING = "20024-CC73"
DECOYS = ("200059-CCZZ", "20099-CCYKX")


@pytest.fixture
def seeded_turns():
    """One chat turn at our branch, one at a sibling, one at each decoy branch.

    Every question is tagged with a run-unique marker so assertions can ignore
    the 122+ real rows already in the dev database — a test that asserted on the
    whole result set would pass or fail depending on production traffic.
    """

    _ensure_schema()
    tag = f"AUTHZ-{uuid.uuid4().hex[:10]}"
    rows = [(MINE, "mine"), (SIBLING, "sibling")] + [(d, "decoy") for d in DECOYS]
    for site, kind in rows:
        _pg(
            """INSERT INTO chat_logs (lang, store_id, question, answer, cached, latency_ms)
               VALUES ('EN',$1,$2,$3,FALSE,10)""",
            site, f"{tag} {kind} question", f"{tag} {kind} answer",
        )
    yield {"tag": tag, "mine": MINE, "sibling": SIBLING, "decoys": DECOYS}
    _pg("DELETE FROM chat_logs WHERE question LIKE $1", f"{tag}%")


def _sites_for(api_client, headers, tag, **params):
    """The branches visible to this caller, restricted to this run's seeded rows."""

    r = api_client.get("/admin/conversations", headers=headers,
                       params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return {row["store_id"] for row in r.json() if str(row["question"]).startswith(tag)}


def test_conversations_scopes_a_pinned_admin_to_their_own_branch(api_client, seeded_turns):
    """A branch-pinned admin must not read another branch's customer turns.

    These are the most sensitive rows in the system — questions and answers
    verbatim, more so than the stock numbers the catalog endpoints were already
    fixed for.
    """

    a = _Admin(role="admin", store_id=seeded_turns["mine"])
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"])
        assert sites == {seeded_turns["mine"]}
        assert seeded_turns["sibling"] not in sites
    finally:
        a.drop()


def test_conversations_are_unscoped_for_a_super_admin(api_client, seeded_turns):
    """A super_admin is always global — `caller_store_scope` returns None."""

    a = _Admin(role="super_admin")
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"])
        assert sites == {seeded_turns["mine"], seeded_turns["sibling"], *seeded_turns["decoys"]}
    finally:
        a.drop()


def test_conversations_are_unscoped_for_an_unpinned_admin(api_client, seeded_turns):
    """Every existing account has store_id NULL, so the console is unchanged.

    Without this, "scoping works" could be satisfied by an endpoint that returns
    nothing to anybody.
    """

    a = _Admin(role="admin", store_id=None)
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"])
        assert seeded_turns["sibling"] in sites
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_conversations_scope_accepts_prefix_and_suffix_tokens(
    api_client, seeded_turns, token_form
):
    """Scope goes through tools._site_clause, so the full code, its numeric prefix
    and its alpha suffix all resolve to the SAME single branch — never a bare `=`,
    which would make two of these three forms silently return nothing."""

    a = _Admin(role="admin", store_id=token_form)
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"])
        assert sites == {seeded_turns["mine"]}
    finally:
        a.drop()


@pytest.mark.parametrize(
    "token_form, decoy",
    [("20005", "200059-CCZZ"), ("CCYK", "20099-CCYKX")],
)
def test_conversations_scope_does_not_substring_match_a_sibling(
    api_client, seeded_turns, token_form, decoy
):
    """**The vacuity guard.** The scope must match ANCHORED, not as a substring.

    `20005-CCYK` and `20024-CC73` are substring-disjoint, so they cannot tell a
    correct matcher from a wrong one: swap `_site_clause` for the documented-bad
    `store_id ILIKE '%'||$n||'%'` and every other test in this section stays
    green. Only a decoy whose code CONTAINS the token can fail — which is exactly
    the leak CLAUDE.md records shipping, a prefix-shaped `store_id`
    substring-matching sibling branches.

    Verified by doing it: weakening the predicate to a substring ILIKE makes this
    test — and only this test — fail.
    """

    a = _Admin(role="admin", store_id=token_form)
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"])
        assert decoy not in sites, f"substring leak: {token_form} matched {decoy}"
        assert sites == {seeded_turns["mine"]}
    finally:
        a.drop()


def test_conversations_store_filter_cannot_widen_the_scope(api_client, seeded_turns):
    """`store` is the operator's filter box; `scope` is the boundary. ANDed, never
    substituted — a pinned caller who types a sibling's code narrows to nothing
    rather than crossing the boundary."""

    a = _Admin(role="admin", store_id=seeded_turns["mine"])
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"],
                           store=seeded_turns["sibling"])
        assert sites == set(), f"the filter widened the scope: {sites}"

        # Intersecting with their OWN branch still works — the filter is a filter.
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"],
                           store=seeded_turns["mine"])
        assert sites == {seeded_turns["mine"]}
    finally:
        a.drop()


def test_conversations_store_filter_cannot_widen_via_a_decoy(api_client, seeded_turns):
    """The same widening attempt aimed at a substring decoy rather than a sibling."""

    a = _Admin(role="admin", store_id="20005")
    try:
        sites = _sites_for(api_client, a.headers, seeded_turns["tag"],
                           store="200059-CCZZ")
        assert sites == set(), f"the filter reached a decoy branch: {sites}"
    finally:
        a.drop()


# ---- 4. security log -------------------------------------------------------


def test_security_log_is_super_admin_only(api_client, admin):
    """A list of emails and source IPs correlated with failed passwords is the
    raw material for targeting the people in it."""

    r = api_client.get("/admin/security-log", headers=admin.headers)
    assert r.status_code == 403, r.text


def test_security_log_degrades_when_the_table_is_absent(api_client, super_admin):
    """`auth_events` is created by the login-audit change, which ships separately
    and may land after this endpoint. A missing table is a deployment state, not
    an error: the endpoint must answer an empty result with an explanation rather
    than 500, so the two changes stay independently deployable in either order.

    The table is renamed rather than dropped, so a database that already has real
    audit rows keeps them.
    """

    present = _pg("SELECT to_regclass('public.auth_events') AS t", fetch=True)[0]["t"]
    parked = f"auth_events_parked_{uuid.uuid4().hex[:8]}"
    if present:
        _pg(f'ALTER TABLE auth_events RENAME TO "{parked}"')
    try:
        r = api_client.get("/admin/security-log", headers=super_admin.headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 0
        assert body["rows"] == []
        assert "auth_events" in body["detail"]      # says WHY, not just "empty"
    finally:
        if present:
            _pg(f'ALTER TABLE "{parked}" RENAME TO auth_events')


def test_security_log_reads_and_filters_events_when_the_table_exists(
    api_client, super_admin
):
    """Newest-first, filterable, bound parameters only.

    The table is created here rather than skipped-if-absent so this endpoint is
    covered before the login-audit change lands. The columns match the contract
    that change writes to (`id, ts, event, email, actor_email, ip, detail`).
    """

    created_here = _pg("SELECT to_regclass('public.auth_events') AS t", fetch=True)[0]["t"] is None
    _pg(
        """CREATE TABLE IF NOT EXISTS auth_events (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               event TEXT, email TEXT, actor_email TEXT, ip TEXT, detail TEXT
           )"""
    )
    tag = f"authz-{uuid.uuid4().hex[:10]}@corp.mm"
    try:
        _pg(
            """INSERT INTO auth_events (ts, event, email, ip, detail) VALUES
               (now() - interval '2 hours','login_fail',$1,'10.0.0.1','bad password'),
               (now() - interval '1 hour','login_locked',$1,'10.0.0.1','locked'),
               (now(),'login_ok',$1,'10.0.0.2','')""",
            tag,
        )
        r = api_client.get("/admin/security-log", headers=super_admin.headers,
                           params={"email": tag, "limit": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        assert [row["event"] for row in body["rows"]] == \
            ["login_ok", "login_locked", "login_fail"]        # newest first

        r = api_client.get("/admin/security-log", headers=super_admin.headers,
                           params={"email": tag, "event": "login_fail"})
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1

        # A bad ISO date is a 400 naming the parameter, not a 500 out of asyncpg.
        r = api_client.get("/admin/security-log", headers=super_admin.headers,
                           params={"from": "not-a-date"})
        assert r.status_code == 400, r.text
    finally:
        _pg("DELETE FROM auth_events WHERE email=$1", tag)
        if created_here:
            _pg("DROP TABLE auth_events")
