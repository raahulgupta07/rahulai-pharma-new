"""Guard JIT provisioning, the sign-in mode, and the provider-branding fields.

Three features shipped together on 2026-08-17, and two of them can lock people
out or let the wrong people in, so every test here is written to FAIL when its
fix is reverted rather than to merely pass.

* **JIT provisioning** (`oidc_auto_create` / `ldap_auto_create`, per source, both
  OFF by default) turns "no account for this email" into a *provisioning* step.
  The thing being defended is that provisioning is not promotion: whatever the
  realm asserts, the created row is `role='user'` and `approved=FALSE`, and a
  pending account is refused by `require_admin` on every request. A JIT feature
  that quietly created approved accounts would be a self-service admin console.
* **`signin_mode`** (`local` | `hybrid` | `sso_only`). `sso_only` ALWAYS exempts
  a `super_admin`: without the carve-out, one wrong discovery URL locks out
  every human including the person who fixes realms. That carve-out has its own
  test, and it is the one to re-check after any refactor of `/auth/login`.
* **`signin_mode` + `oidc_provider_type` in the public `/auth/config`**, so the
  login screen renders the right controls and the right logo.

Needs live Postgres + Redis, like the rest of the suite. No IdP: the OIDC tests
stub `_oidc_metadata` and `httpx.AsyncClient`, so a real `oidc_callback` runs
end to end against a fake realm.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth as authmod
from app.auth import AuthError
from app.config import get_settings

# Imported at collection time: app.api pulls in agno, which builds an
# asyncio.Lock() at import — on py3.9 that fails once a prior asyncio.run() has
# closed its loop. Same reason test_approval.py imports here.
from app.api import app as fastapi_app
from app.api import require_admin


# ---- harness (mirrors tests/test_auth_hardening.py) -------------------------


def run(coro):
    """Run one coroutine on a fresh loop, closing the loop-bound clients after."""

    from app import cache, db

    async def _wrapped():
        try:
            return await coro
        finally:
            await db.close_pool()
            await cache.close_client()

    return asyncio.run(_wrapped())


@contextlib.contextmanager
def client():
    """A TestClient with its own lifespan, usable between `run()` phases."""

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with TestClient(fastapi_app) as c:
        yield c


def _sync_redis():
    import redis as _redis_sync

    return _redis_sync.from_url(get_settings().redis_url, decode_responses=True)


@contextlib.contextmanager
def auth_override(**kv):
    """Set `auth.*` Redis overrides with a SYNC client, and always clean up.

    Sync on purpose — see the identical helper in test_auth_hardening: the async
    client binds to whichever loop first touches it, and binding it here would
    make the app's own read fail and fall back to env, which is the thing under
    test.
    """

    from app import cache

    c = _sync_redis()
    keys = ["auth." + k for k in kv]
    try:
        for k, v in kv.items():
            c.hset(cache._CONFIG_KEY, "auth." + k, v)
        yield
    finally:
        try:
            c.hdel(cache._CONFIG_KEY, *keys)
        finally:
            c.close()


def _fresh_email(tag: str = "jit") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@corp.mm"


async def _rows(email: str):
    from app.db import q

    return await q("SELECT * FROM users WHERE email=$1", email.strip().lower())


async def _events(email: str):
    from app.db import q

    return await q(
        "SELECT event, detail FROM auth_events WHERE email=$1 ORDER BY id",
        email.strip().lower(),
    )


async def _purge(*emails: str):
    from app.db import execute

    for email in emails:
        email = (email or "").strip().lower()
        await execute("DELETE FROM users WHERE email=$1", email)
        await execute("DELETE FROM auth_events WHERE email=$1", email)


# ---- a fake realm for the end-to-end OIDC tests -----------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _fake_realm(monkeypatch, userinfo: dict):
    """Make `oidc_callback` complete against an in-process realm.

    No `id_token` is returned, so `verify_id_token` is skipped and the profile
    comes from `userinfo` — which is exactly the shape a hostile realm would use
    to try to assert a role.
    """

    async def _meta(cfg):
        return {
            "authorization_endpoint": "https://idp.invalid/auth",
            "token_endpoint": "https://idp.invalid/token",
            "userinfo_endpoint": "https://idp.invalid/userinfo",
            "issuer": "https://idp.invalid",
            "jwks_uri": "https://idp.invalid/jwks",
        }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            return _Resp({"access_token": "at", "token_type": "Bearer"})

        async def get(self, url, headers=None, **kw):
            return _Resp(userinfo)

    import httpx

    monkeypatch.setattr(authmod, "_oidc_metadata", _meta)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


# ---- Feature 1: JIT provisioning --------------------------------------------


def test_unknown_email_is_refused_when_auto_create_is_off():
    """The default. Regression guard: the refusal message must not drift either,
    because the SPA shows it verbatim on the login screen."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        try:
            with pytest.raises(AuthError) as exc:
                await authmod._merge_external(email, "Someone", "oidc")
            return str(exc.value), len(await _rows(email))
        finally:
            await _purge(email)

    message, rows = run(go())
    assert message == "no account for this email — ask an administrator to create one"
    assert rows == 0, "auto-create fired with the flag OFF"


def test_auto_create_lands_a_pending_plain_user():
    """Flag ON: the row is created, and every field of it is the safe one."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        try:
            await authmod._merge_external(email, "New Person", "oidc")
            return (await _rows(email))[0]
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        row = run(go())

    assert row["approved"] is False, "an auto-created account must be PENDING"
    assert row["role"] == "user", "an auto-created account must never be privileged"
    assert row["active"] is True
    assert row["password_hash"] is None, "an SSO-provisioned row must have no password"
    assert list(row["auth_sources"]) == ["oidc"]
    assert row["name"] == "New Person"


def test_ldap_auto_create_is_a_separate_flag():
    """Per-source, not global: the OIDC flag must not provision LDAP logins."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        try:
            with pytest.raises(AuthError):
                await authmod._merge_external(email, "N", "ldap")
            return len(await _rows(email))
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        assert run(go()) == 0

    async def go2():
        try:
            await authmod._merge_external(email, "N", "ldap")
            row = (await _rows(email))[0]
            return row["role"], row["approved"], list(row["auth_sources"])
        finally:
            await _purge(email)

    with auth_override(ldap_auto_create="true"):
        role, approved, sources = run(go2())
    assert (role, approved, sources) == ("user", False, ["ldap"])


def test_auto_created_user_is_genuinely_blocked_until_approved():
    """The whole point of `approved=FALSE`, through the real dependency.

    The role is bumped to `admin` mid-test on purpose: without it, the second
    403 could come from the role check instead of the approval gate and the
    test would pass for the wrong reason. With it, the ONLY thing standing
    between the token and the console is `approved`.
    """

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        results = {}
        try:
            user = await authmod._merge_external(email, "New Person", "oidc")
            header = f"Bearer {authmod.make_token(user)['token']}"

            async def call():
                try:
                    await require_admin(header)
                    return "allowed"
                except Exception as exc:            # HTTPException
                    return (getattr(exc, "status_code", "err"),
                            str(getattr(exc, "detail", "")))

            results["as_created"] = await call()
            # Promote the role but leave it pending: isolates the approval gate.
            await authmod.update_user(user["id"], role="admin")
            results["pending_admin"] = await call()
            await authmod.update_user(user["id"], approved=True)
            results["approved"] = await call()      # same, unchanged token
            return results
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        r = run(go())

    assert r["as_created"][0] == 403
    assert r["pending_admin"] == (403, "account pending administrator approval")
    assert r["approved"] == "allowed", "approval must work on the existing token"


def test_auto_create_records_an_auth_event():
    """Provisioning is security-relevant, so it must be in the audit trail."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        await authmod.ensure_auth_events()
        try:
            await authmod._merge_external(email, "New Person", "oidc")
            return await _events(email)
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        rows = run(go())

    events = [r["event"] for r in rows]
    assert authmod.EV_USER_AUTOCREATE in events, f"no autocreate event: {events}"
    detail = next(r["detail"] for r in rows if r["event"] == authmod.EV_USER_AUTOCREATE)
    assert "oidc" in detail


def test_idp_claims_cannot_influence_the_created_role(monkeypatch):
    """A hostile (or merely enthusiastic) realm asserting a role must be ignored.

    Runs the REAL `oidc_callback` against a stubbed realm, so the claims travel
    the whole path a live login would take.
    """

    email = _fresh_email()
    _fake_realm(monkeypatch, {
        "email": email,
        "name": "Claims Person",
        "role": "super_admin",
        "roles": ["super_admin", "admin"],
        "groups": ["/pharmacy-admins"],
        "is_admin": True,
        "approved": True,
        "realm_access": {"roles": ["super_admin"]},
    })

    async def go():
        await authmod.ensure_users_table()
        try:
            result = await authmod.oidc_callback("an-auth-code")
            row = (await _rows(email))[0]
            return result["user"], row
        finally:
            await _purge(email)

    with auth_override(oidc_enabled="true", oidc_auto_create="true"):
        public, row = run(go())

    assert row["role"] == "user", "an IdP claim reached the role column"
    assert row["approved"] is False, "an IdP claim reached the approval column"
    assert public["role"] == "user" and public["approved"] is False


def test_a_disabled_account_is_still_refused_not_resurrected():
    """`active=false` is a revocation. JIT must not step around it, and must not
    create a second row for the same address."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        try:
            u = await authmod.create_user(email, "Gone", "pw12345", "admin", approved=True)
            await authmod.update_user(u["id"], active=False)
            with pytest.raises(AuthError) as exc:
                await authmod._merge_external(email, "Gone", "oidc")
            rows = await _rows(email)
            return str(exc.value), rows
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        message, rows = run(go())

    assert message == "account disabled"
    assert len(rows) == 1 and rows[0]["active"] is False
    assert rows[0]["approved"] is True and rows[0]["role"] == "admin", \
        "the existing row was rewritten"


def test_case_and_whitespace_cannot_shadow_an_existing_user():
    """The lookup lower-cases and strips; the create path must use the SAME key,
    or JIT mints a pending duplicate that shadows a real admin."""

    email = _fresh_email()

    async def go():
        await authmod.ensure_users_table()
        try:
            u = await authmod.create_user(email, "Real", "pw12345", "admin", approved=True)
            merged = await authmod._merge_external(
                f"  {email.upper()}  ", "Real", "oidc")
            rows = await _rows(email)
            return u["id"], merged, rows
        finally:
            await _purge(email)

    with auth_override(oidc_auto_create="true"):
        uid, merged, rows = run(go())

    assert len(rows) == 1, "a case/whitespace variant created a second row"
    assert merged["id"] == uid
    assert rows[0]["role"] == "admin" and rows[0]["approved"] is True
    assert "oidc" in list(rows[0]["auth_sources"])   # merged, not replaced


# ---- Feature 2: sign-in mode ------------------------------------------------


def _make_local_user(email: str, role: str):
    async def go():
        await authmod.ensure_users_table()
        return await authmod.create_user(email, "Signin", "pw12345", role, approved=True)

    return run(go())


def test_sso_only_refuses_a_plain_user_but_allows_a_super_admin():
    """The carve-out. Reverting it locks the console's own operator out."""

    user_email = _fresh_email("mode-user")
    sa_email = _fresh_email("mode-sa")
    _make_local_user(user_email, "user")
    _make_local_user(sa_email, "super_admin")

    try:
        with auth_override(signin_mode="sso_only"):
            with client() as c:
                blocked = c.post("/auth/login",
                                 json={"email": user_email, "password": "pw12345"})
                allowed = c.post("/auth/login",
                                 json={"email": sa_email, "password": "pw12345"})

        assert blocked.status_code == 403, blocked.text
        assert "single sign-on" in blocked.json()["detail"].lower()
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["user"]["role"] == "super_admin"
        assert allowed.json()["token"]

        # The password was RIGHT: a policy refusal must not feed the lockout
        # counter, or the mode quietly locks the account after N attempts.
        events = [r["event"] for r in run(_events(user_email))]
        assert authmod.EV_LOGIN_BLOCKED in events
        assert authmod.EV_LOGIN_FAIL not in events
    finally:
        run(_purge(user_email, sa_email))


def test_hybrid_is_the_default_and_lets_a_plain_user_in():
    """Non-vacuity control for the test above: the same call, no override."""

    email = _fresh_email("mode-hybrid")
    _make_local_user(email, "user")
    try:
        with client() as c:
            r = c.post("/auth/login", json={"email": email, "password": "pw12345"})
        assert r.status_code == 200, r.text
    finally:
        run(_purge(email))


def test_local_mode_makes_sso_login_403():
    with auth_override(signin_mode="local", oidc_enabled="true"):
        with client() as c:
            r = c.get("/auth/sso/login", follow_redirects=False)
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


def test_local_mode_also_refuses_the_callback():
    """The button is hidden AND the route is shut: a login in flight when the
    mode changed must not complete."""

    async def go():
        with pytest.raises(AuthError) as exc:
            await authmod.oidc_callback("a-code")
        return str(exc.value)

    with auth_override(signin_mode="local", oidc_enabled="true"):
        assert "disabled" in run(go()).lower()


# ---- Feature 3: config surface ----------------------------------------------


def test_auth_config_exposes_signin_mode_and_provider_type():
    with client() as c:
        default = c.get("/auth/config").json()
    assert default["signin_mode"] == "hybrid"
    assert default["oidc_provider_type"] == "keycloak"
    # additive only — the existing keys must still be there
    assert {"ldap_enabled", "oidc_enabled", "oidc_provider_name"} <= set(default)

    with auth_override(signin_mode="sso_only", oidc_provider_type="entra"):
        with client() as c:
            changed = c.get("/auth/config").json()
    assert changed["signin_mode"] == "sso_only"
    assert changed["oidc_provider_type"] == "entra"


def test_an_unknown_enum_value_falls_back_to_the_default():
    """A hand-edited Redis hash must not put an unknown string into a policy
    decision — `signin_mode='sso-only'` (a typo) must read as `hybrid`, not as
    something that neither branch matches."""

    with auth_override(signin_mode="sso-only", oidc_provider_type="okta"):
        cfg = run(authmod.effective_auth())
    assert cfg.signin_mode == "hybrid"
    assert cfg.oidc_provider_type == "keycloak"


def test_set_auth_config_refuses_a_bad_enum():
    """Refused at the write, so the admin page cannot store a mode that silently
    reads back as a different one."""

    async def go():
        with pytest.raises(AuthError):
            await authmod.set_auth_config({"signin_mode": "nope"})
        with pytest.raises(AuthError):
            await authmod.set_auth_config({"oidc_provider_type": "okta"})
        return (await authmod.effective_auth()).signin_mode

    assert run(go()) == "hybrid"


def test_auth_overview_reports_the_new_settings():
    """The settings page must render current state without a second call.

    The router function is called directly: `require_super_admin` is a route
    dependency, and minting a super_admin session here would test FastAPI's DI,
    not the payload.
    """

    from app.admin import auth_overview

    async def go():
        await authmod.ensure_users_table()
        return await auth_overview()

    with auth_override(signin_mode="sso_only", oidc_provider_type="entra",
                       oidc_auto_create="true", ldap_auto_create="true"):
        body = run(go())

    assert body["signin_mode"] == "sso_only"
    assert body["oidc"]["provider_type"] == "entra"
    assert body["oidc"]["auto_create"] is True
    assert body["ldap"]["auto_create"] is True
    assert body["pending"] == body["local"]["pending"]
    # additive only
    assert body["local"]["users"] >= 0 and body["oidc"]["provider_name"] is not None
    assert body["self_signup"] is False


def test_auto_create_flags_default_off_in_the_effective_config():
    cfg = run(authmod.effective_auth())
    assert cfg.oidc_auto_create is False
    assert cfg.ldap_auto_create is False
