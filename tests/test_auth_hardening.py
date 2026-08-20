"""Guard the five authentication-core defects fixed on 2026-08-17.

Written in the style of `tests/test_auth_sso.py`: every test is built so that
reverting its fix makes it FAIL, and the ones that could pass for the wrong
reason assert the mechanism (was the call made? was the row written?) rather
than only the status code.

* `test_ldap_fallthrough_*` — `/auth/login` read `get_settings().ldap_enabled`
  (env only) while `/auth/config` and `login_ldap` read the *effective* layer,
  so enabling LDAP from the admin panel put the banner on the login screen and
  left the actual login path off.
* `test_lockout_*` / `test_ip_throttle_*` — the endpoint had no throttle at all:
  unlimited online bcrypt guessing, and with LDAP on every guess is proxied into
  the directory (AD lockout amplification).
* `test_record_auth_event_never_raises` — the audit write must not be able to
  break a login, exactly like `ingest_events.record` and `history.record_turn`.
* `test_boot_*` — `secret_key` defaults to a published placeholder and signs
  admin JWTs, embed session tokens, the widget HMAC, SSO state and preview links.
* `test_id_token_*` — the id_token used to be accepted unverified.

Needs live Postgres, like the rest of the suite. The id_token tests are pure
unit tests: keys are generated in-process and the JWKS fetch is stubbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from app import auth as authmod
from app.auth import AuthError
from app.config import DEFAULT_SECRET_KEY, get_settings

# Import at collection time — app.api pulls in agno, which builds an
# asyncio.Lock() at import; on py3.9 that fails after a prior asyncio.run()
# closed its loop. Same reason test_approval.py imports here.
from app.api import app as fastapi_app


# ---- harness ----------------------------------------------------------------


def run(coro):
    """Run one coroutine on a fresh loop, closing the loop-bound clients after.

    asyncpg pools and the redis client bind to the loop that created them, so a
    leaked one raises on the next test's loop. Mirrors test_approval.run.
    """

    from app import cache, db

    async def _wrapped():
        try:
            return await coro
        finally:
            await db.close_pool()
            await cache.close_client()

    return asyncio.run(_wrapped())


def arun(coro):
    """Run a coroutine that touches neither Postgres nor Redis."""

    return asyncio.run(coro)


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

    Sync on purpose: the async client in app.cache binds to whichever loop first
    touches it, and this runs outside the TestClient's portal loop. Binding it
    here would make the app's own read fail (and silently fall back to env,
    which is precisely the thing under test).
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


async def _events(email):
    from app.db import q

    return await q(
        "SELECT event, ip, detail FROM auth_events WHERE email=$1 ORDER BY id", email
    )


async def _purge(email):
    from app.db import execute

    await execute("DELETE FROM auth_events WHERE email=$1", email)


def _fresh_email():
    return f"hard-{uuid.uuid4().hex[:10]}@corp.mm"


# ---- Fix 1: the LDAP fallthrough must read the EFFECTIVE layer ---------------


def test_ldap_fallthrough_honours_a_runtime_override():
    """A Redis override enabling LDAP must actually reach `login_ldap`.

    Asserting a 401 would be vacuous — the request 401s either way. The claim is
    that the LDAP path was ENTERED, so we assert the call happened.
    """

    email = _fresh_email()
    calls = []

    async def _fake_login_ldap(username, password):
        calls.append((username, password))
        raise AuthError("ldap says no")

    assert get_settings().ldap_enabled is False, "env must be off, or this proves nothing"

    try:
        with auth_override(ldap_enabled="true"):
            with client() as c:
                orig = authmod.login_ldap
                authmod.login_ldap = _fake_login_ldap
                try:
                    r = c.post("/auth/login", json={"email": email, "password": "x"})
                finally:
                    authmod.login_ldap = orig
        assert calls, "runtime LDAP override never reached login_ldap"
        assert r.status_code == 401
        assert r.json()["detail"] == "ldap says no"   # the LDAP error, not the generic one
    finally:
        run(_purge(email))


def test_ldap_fallthrough_stays_off_without_an_override():
    """The control: with no override and env off, LDAP must not be consulted."""

    email = _fresh_email()
    calls = []

    async def _fake_login_ldap(username, password):
        calls.append((username, password))
        raise AuthError("ldap says no")

    try:
        with client() as c:
            orig = authmod.login_ldap
            authmod.login_ldap = _fake_login_ldap
            try:
                r = c.post("/auth/login", json={"email": email, "password": "x"})
            finally:
                authmod.login_ldap = orig
        assert calls == []
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid credentials"
    finally:
        run(_purge(email))


# ---- Fix 2: lockout ---------------------------------------------------------


def test_lockout_returns_429_at_the_threshold_and_records_it():
    """N fails lock the account; the (N+1)th is refused before any auth attempt."""

    s = get_settings()
    email = _fresh_email()
    limit = s.login_max_fail
    try:
        with client() as c:
            codes = [
                c.post("/auth/login", json={"email": email, "password": "wrong"}).status_code
                for _ in range(limit)
            ]
            locked = c.post("/auth/login", json={"email": email, "password": "wrong"})

        assert codes == [401] * limit, f"a failed login changed status early: {codes}"
        assert locked.status_code == 429
        assert "too many failed" in locked.json()["detail"].lower()

        rows = run(_events(email))
        events = [r["event"] for r in rows]
        assert events.count(authmod.EV_LOGIN_FAIL) == limit
        assert authmod.EV_LOGIN_LOCKED in events, "the lockout was not recorded"
    finally:
        run(_purge(email))


def test_lockout_is_not_triggered_below_the_threshold():
    """Non-vacuity guard: limit-1 fails must still reach the password check."""

    s = get_settings()
    email = _fresh_email()
    try:
        with client() as c:
            codes = [
                c.post("/auth/login", json={"email": email, "password": "wrong"}).status_code
                for _ in range(s.login_max_fail - 1)
            ]
            after = c.post("/auth/login", json={"email": email, "password": "wrong"})
        assert set(codes) == {401}
        assert after.status_code == 401
    finally:
        run(_purge(email))


def test_a_successful_login_resets_the_counter():
    """The `login_ok` row IS the reset — fails are counted only since it."""

    s = get_settings()
    limit = s.login_max_fail
    email = None
    user_id = None
    try:
        async def _mk():
            await authmod.ensure_users_table()
            await authmod.ensure_auth_events()
            e = _fresh_email()
            u = await authmod.create_user(e, "Lock Test", "correct-horse", "user",
                                          approved=True)
            return e, u["id"]

        email, user_id = run(_mk())

        with client() as c:
            first = [
                c.post("/auth/login", json={"email": email, "password": "wrong"}).status_code
                for _ in range(limit - 1)
            ]
            good = c.post("/auth/login", json={"email": email, "password": "correct-horse"})
            # Without the reset the counter would already stand at limit-1, so
            # the SECOND of these would be 429.
            second = [
                c.post("/auth/login", json={"email": email, "password": "wrong"}).status_code
                for _ in range(limit - 1)
            ]

        assert set(first) == {401}
        assert good.status_code == 200, good.text
        assert set(good.json()) >= {"token", "expires_in", "user"}   # shape unchanged
        assert set(second) == {401}, f"counter was not reset by the success: {second}"
    finally:
        if email:
            run(_purge(email))
        if user_id:
            run(authmod.delete_user(user_id))


def test_ip_throttle_catches_spraying_across_many_accounts(monkeypatch):
    """Per-email counters never see a spray: one guess each at many accounts."""

    s = get_settings()
    monkeypatch.setattr(s, "login_ip_max_fail", 3, raising=False)
    ip = f"203.0.113.{uuid.uuid4().int % 200 + 1}"
    emails = [_fresh_email() for _ in range(5)]
    hdr = {"X-Forwarded-For": f"{ip}, 10.0.0.7"}   # first hop is the real client
    try:
        with client() as c:
            codes = [
                c.post("/auth/login", json={"email": e, "password": "wrong"},
                       headers=hdr).status_code
                for e in emails[:3]
            ]
            blocked = c.post("/auth/login", json={"email": emails[3], "password": "wrong"},
                             headers=hdr)
            # A different network must be unaffected — the throttle is per IP,
            # not a global kill switch.
            elsewhere = c.post("/auth/login", json={"email": emails[4], "password": "wrong"},
                               headers={"X-Forwarded-For": "198.51.100.9"})

        assert codes == [401, 401, 401]
        assert blocked.status_code == 429
        assert "network" in blocked.json()["detail"].lower()
        assert elsewhere.status_code == 401

        rows = run(_events(emails[0]))
        assert rows and rows[0]["ip"] == ip, "X-Forwarded-For's first hop was not used"
    finally:
        for e in emails:
            run(_purge(e))


def test_ip_throttle_threshold_is_well_above_the_email_one():
    """A single office behind one NAT must not be able to lock itself out."""

    s = get_settings()
    assert s.login_ip_max_fail > s.login_max_fail * 5


# ---- Fix 2b: recording must never break a login -----------------------------


def test_record_auth_event_never_raises(monkeypatch):
    """A dead audit table is a gap in a log, not an outage."""

    async def _boom(*a, **k):
        raise RuntimeError("postgres is on fire")

    monkeypatch.setattr(authmod, "execute", _boom)
    arun(authmod.record_auth_event(authmod.EV_LOGIN_FAIL, email="x@y.z", ip="1.2.3.4"))


def test_lockout_counters_fail_open(monkeypatch):
    """A DB blip must not lock every account in the company out at once."""

    async def _boom(*a, **k):
        raise RuntimeError("postgres is on fire")

    monkeypatch.setattr(authmod, "q", _boom)
    assert arun(authmod.failed_logins_for_email("x@y.z", 15)) == 0
    assert arun(authmod.failed_logins_for_ip("1.2.3.4", 15)) == 0


def test_login_still_works_when_the_audit_table_is_unreachable(monkeypatch):
    """End to end: a broken auth_events must not turn a good login into a 500."""

    email = None
    user_id = None
    try:
        async def _mk():
            await authmod.ensure_users_table()
            e = _fresh_email()
            u = await authmod.create_user(e, "Audit Test", "correct-horse", "user",
                                          approved=True)
            return e, u["id"]

        email, user_id = run(_mk())

        real_execute = authmod.execute

        async def _selective(sql, *a):
            if "auth_events" in sql:
                raise RuntimeError("postgres is on fire")
            return await real_execute(sql, *a)

        with client() as c:
            monkeypatch.setattr(authmod, "execute", _selective)
            try:
                r = c.post("/auth/login", json={"email": email, "password": "correct-horse"})
            finally:
                monkeypatch.setattr(authmod, "execute", real_execute)
        assert r.status_code == 200, r.text
    finally:
        if email:
            run(_purge(email))
        if user_id:
            run(authmod.delete_user(user_id))


# ---- Fix 3 + 4: boot-time guards --------------------------------------------


def test_boot_raises_in_production_on_the_default_secret(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production", raising=False)
    monkeypatch.setattr(s, "secret_key", DEFAULT_SECRET_KEY, raising=False)
    with pytest.raises(RuntimeError) as exc:
        arun(authmod.boot_security_checks())
    assert "SECRET_KEY" in str(exc.value), "the message must name the env var"


def test_boot_raises_in_production_on_a_short_secret(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production", raising=False)
    monkeypatch.setattr(s, "secret_key", "a" * 31, raising=False)
    with pytest.raises(RuntimeError):
        arun(authmod.boot_security_checks())


def test_boot_accepts_a_strong_secret_in_production(monkeypatch):
    """Non-vacuity: the guard must fire on the SECRET, not on app_env alone."""

    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production", raising=False)
    monkeypatch.setattr(s, "secret_key", "z" * 48, raising=False)
    monkeypatch.setattr(s, "oidc_enabled", False, raising=False)
    arun(authmod.boot_security_checks())      # must not raise


def test_boot_only_warns_in_dev(monkeypatch, caplog):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev", raising=False)
    monkeypatch.setattr(s, "secret_key", DEFAULT_SECRET_KEY, raising=False)
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        arun(authmod.boot_security_checks())   # must NOT raise
    assert "SECRET_KEY" in caplog.text and "INSECURE CONFIG" in caplog.text


def test_boot_warns_about_a_long_token_ttl(monkeypatch, caplog):
    """`.env` ships AUTH_TOKEN_TTL_HOURS=168 against a documented default of 12."""

    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev", raising=False)
    monkeypatch.setattr(s, "auth_token_ttl_hours", 168, raising=False)
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        arun(authmod.boot_security_checks())
    assert "168" in caplog.text and "AUTH_TOKEN_TTL_HOURS" in caplog.text


def test_boot_does_not_warn_about_a_sane_ttl(monkeypatch, caplog):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev", raising=False)
    monkeypatch.setattr(s, "auth_token_ttl_hours", 12, raising=False)
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        arun(authmod.boot_security_checks())
    assert "AUTH_TOKEN_TTL_HOURS" not in caplog.text


def test_boot_warns_when_sso_is_on_without_a_secure_cookie(monkeypatch, caplog):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev", raising=False)
    monkeypatch.setattr(s, "cookie_secure", False, raising=False)

    async def _cfg():
        return SimpleNamespace(oidc_enabled=True)

    monkeypatch.setattr(authmod, "effective_auth", _cfg)
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        arun(authmod.boot_security_checks())
    assert "COOKIE_SECURE" in caplog.text


def test_token_ttl_default_is_still_12():
    """The fix leaves the default alone and warns instead; pin that."""

    from app.config import Settings

    assert Settings.model_fields["auth_token_ttl_hours"].default == 12


# ---- Fix 5: id_token verification against the realm JWKS --------------------

CLIENT_ID = "pharmacy-agent"
ISSUER = "https://kc.example.com/realms/citcare"
JWKS_URI = "https://kc.example.com/realms/citcare/protocol/openid-connect/certs"
META = {
    "issuer": ISSUER,
    "jwks_uri": JWKS_URI,
    "authorization_endpoint": ISSUER + "/auth",
    "token_endpoint": ISSUER + "/token",
    "userinfo_endpoint": ISSUER + "/userinfo",
}
CFG = SimpleNamespace(oidc_client_id=CLIENT_ID, oidc_enabled=True)


def _rsa_pair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def keys():
    """Two independent RSA keys — the realm's, and an attacker's."""

    return {"good": _rsa_pair(), "evil": _rsa_pair()}


def _jwk(private_key, kid):
    from jwt.algorithms import RSAAlgorithm

    d = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    d.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return d


def _jwks(*pairs):
    return {"keys": [_jwk(k, kid) for kid, k in pairs]}


def _id_token(key, kid, **overrides):
    now = int(time.time())
    payload = {
        "iss": ISSUER, "aud": "account", "azp": CLIENT_ID, "sub": "u-1",
        "iat": now, "exp": now + 300, "email": "sso@corp.mm",
        "email_verified": True, "nonce": "the-nonce",
    }
    payload.update(overrides)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def warm_jwks(keys, monkeypatch):
    """Seed the JWKS cache with the realm's key and count any (re)fetch."""

    doc = _jwks(("kid-good", keys["good"]))
    monkeypatch.setitem(authmod._JWKS, JWKS_URI, (time.monotonic() + 600, doc))
    fetches = []

    async def _counted(uri):
        fetches.append(uri)
        return doc

    monkeypatch.setattr(authmod, "_fetch_jwks", _counted)
    yield fetches
    authmod._JWKS.pop(JWKS_URI, None)


def test_id_token_accepts_a_correctly_signed_token(keys, warm_jwks):
    tok = _id_token(keys["good"], "kid-good")
    claims = arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))
    assert claims["email"] == "sso@corp.mm"
    assert warm_jwks == [], "a warm cache should need no fetch"


def test_id_token_rejects_the_wrong_signing_key(keys, warm_jwks):
    """Same kid, different key: the classic forged-token shape."""

    tok = _id_token(keys["evil"], "kid-good")
    with pytest.raises(AuthError):
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))


def test_id_token_rejects_the_wrong_issuer(keys, warm_jwks):
    tok = _id_token(keys["good"], "kid-good", iss="https://evil.example.com/realms/x")
    with pytest.raises(AuthError):
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))


def test_id_token_rejects_a_foreign_audience(keys, warm_jwks):
    """Neither aud nor azp names us — a token minted for another client."""

    tok = _id_token(keys["good"], "kid-good", aud=["account"], azp="some-other-client")
    with pytest.raises(AuthError) as exc:
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))
    assert "client" in str(exc.value)


def test_id_token_accepts_keycloaks_account_audience(keys, warm_jwks):
    """Keycloak sets aud="account" and names the client in azp.

    A naive `verify_aud=True` against client_id rejects every real Keycloak
    login, so this is the case that proves the check is the right one, not just
    a strict one.
    """

    tok = _id_token(keys["good"], "kid-good", aud=["account"], azp=CLIENT_ID)
    assert arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))["azp"] == CLIENT_ID


def test_id_token_accepts_client_id_in_aud(keys, warm_jwks):
    tok = _id_token(keys["good"], "kid-good", aud=[CLIENT_ID, "account"], azp=None)
    assert arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))


def test_id_token_rejects_a_nonce_mismatch(keys, warm_jwks):
    tok = _id_token(keys["good"], "kid-good", nonce="someone-elses-login")
    with pytest.raises(AuthError) as exc:
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))
    assert "nonce" in str(exc.value)


def test_id_token_rejects_a_missing_nonce(keys, warm_jwks):
    tok = _id_token(keys["good"], "kid-good", nonce=None)
    with pytest.raises(AuthError):
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))


def test_unknown_kid_refetches_exactly_once_and_then_fails(keys, warm_jwks):
    """No "first key in the set" fallback.

    The token below is signed by the key that IS in the JWKS, but presents a kid
    the set does not contain. A first-key fallback would happily verify it — so
    the AuthError is what proves the fallback is absent, and the fetch count
    proves we refresh for a rotation exactly once rather than hammering the IdP.
    """

    tok = _id_token(keys["good"], "kid-rotated")
    with pytest.raises(AuthError) as exc:
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))
    assert "unknown key" in str(exc.value)
    assert len(warm_jwks) == 1, f"expected exactly one refetch, got {len(warm_jwks)}"


def test_id_token_without_a_kid_is_refused(keys, warm_jwks):
    tok = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "exp": int(time.time()) + 300},
                     keys["good"], algorithm="RS256")
    with pytest.raises(AuthError):
        arun(authmod.verify_id_token(tok, CFG, META, "the-nonce"))
    assert warm_jwks == []


def test_jwks_fetch_failure_is_an_autherror_not_a_500(monkeypatch):
    """An unreachable IdP must produce a clear 401, never a stack trace."""

    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    authmod._JWKS.pop(JWKS_URI, None)
    with pytest.raises(AuthError) as exc:
        arun(authmod._signing_key(JWKS_URI, "kid-good"))
    assert "signing keys" in str(exc.value)


def test_authorize_url_carries_the_state_nonce(monkeypatch):
    """The nonce must reach the IdP, or there is nothing to enforce later."""

    from urllib.parse import parse_qs, urlparse

    async def _cfg():
        return SimpleNamespace(
            oidc_enabled=True, oidc_client_id=CLIENT_ID, oidc_scopes="openid email",
            oidc_redirect_uri="https://p.example.com/auth/sso/callback",
            oidc_discovery_url="https://kc.example.com/x",
        )

    async def _meta(cfg):
        return META

    monkeypatch.setattr(authmod, "effective_auth", _cfg)
    monkeypatch.setattr(authmod, "_oidc_metadata", _meta)

    state, nonce = authmod.make_state()
    url = arun(authmod.oidc_authorize_url(state))
    qs = parse_qs(urlparse(url).query)
    assert qs["nonce"] == [nonce]
    assert qs["state"] == [state]
    assert authmod.state_nonce(state) == nonce
