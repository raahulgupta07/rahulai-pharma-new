"""The two endpoints behind the redesigned Authentication page.

* ``GET /admin/auth-overview`` — one card per sign-in method. Its whole reason
  to exist is that **enabled and configured are different states**: a method
  switched on but missing the fields a login needs fails at the login screen,
  where nobody who can fix it is looking. So the tests assert the two
  independently, including the "on but not configured" combination.
* ``GET/PUT /admin/security-config`` — the login-throttle numbers plus the
  process-level posture around them.

Two invariants outrank every status code here:

1. **No secret material in either response.** Asserted against `r.text`, with
   the real values planted first, so a field added later that happens to carry a
   secret fails this rather than shipping.
2. **A PUT must reach the login path with no restart.** This is the
   anti-regression test for the bug the LDAP fallthrough shipped with — the UI
   wrote a Redis override while the enforcing code read `get_settings()`, so the
   toggle reported success and did nothing. `test_put_lockout_is_honoured_*`
   simulates a second uvicorn worker (one whose `Settings` singleton never saw
   the PUT) and drives real logins through it; reverting
   `auth.apply_security_overrides` out of the fail counters makes it fail.

Fixtures follow ``tests/test_authz_gates.py``: real rows through a private
asyncpg connection, super_admin vs plain admin, and cleanup that runs even when
a test dies mid-way.

⚠️ Every DB setup/teardown goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop
created it, and under ``api_client`` that is the TestClient's portal loop.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app import auth as authmod
from app.config import DEFAULT_SECRET_KEY, get_settings

from tests.pgconn import pg


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _sync_redis():
    import redis as _redis_sync

    return _redis_sync.from_url(get_settings().redis_url, decode_responses=True)


def _override_keys(prefix: str, *names):
    return [prefix + n for n in names]


@pytest.fixture(autouse=True)
def _no_stray_overrides():
    """Clear `security.*` before AND after every test in this module.

    A leaked lockout override is not a local failure: `apply_security_overrides`
    writes it onto the shared `Settings` singleton, so the next test file to
    exercise a login inherits somebody else's threshold. Cleaning up in a
    `finally` alone is not enough — a killed run never reaches it — so this
    clears on the way in too.
    """

    from app import cache

    keys = _override_keys("security.", *authmod.SECURITY_KEYS)
    s = get_settings()
    # Snapshot the live values, not the model defaults: `.env` may legitimately
    # set any of them, and restoring a default would quietly rewrite the
    # deployment's own configuration for every test that runs afterwards.
    before = {k: getattr(s, k) for k in authmod.SECURITY_KEYS}
    c = _sync_redis()
    try:
        c.hdel(cache._CONFIG_KEY, *keys)
        yield
    finally:
        try:
            c.hdel(cache._CONFIG_KEY, *keys)
        finally:
            c.close()
        # A test may have materialised an override onto the shared singleton.
        for key, val in before.items():
            setattr(s, key, val)


def _auth_override_set(**kv):
    from app import cache

    c = _sync_redis()
    try:
        for k, v in kv.items():
            c.hset(cache._CONFIG_KEY, "auth." + k, v)
    finally:
        c.close()


def _auth_override_clear(*names):
    from app import cache

    c = _sync_redis()
    try:
        c.hdel(cache._CONFIG_KEY, *_override_keys("auth.", *names))
    finally:
        c.close()


class _Admin:
    """An approved account + a bearer header. Mirrors test_authz_gates._Admin."""

    def __init__(self, role="admin"):
        _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
        self.email = f"sec-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'SecProbe',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def admin():
    a = _Admin(role="admin")
    yield a
    a.drop()


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


# ---- 1. both endpoints are super_admin only ---------------------------------


def test_auth_overview_is_super_admin_only(api_client, admin):
    """It names the directory host and the SSO issuer — an infrastructure map."""

    r = api_client.get("/admin/auth-overview", headers=admin.headers)
    assert r.status_code == 403, r.text


def test_security_config_is_super_admin_only(api_client, admin):
    """Read AND write: the read says where this deployment is weak."""

    assert api_client.get("/admin/security-config",
                          headers=admin.headers).status_code == 403
    r = api_client.put("/admin/security-config", headers=admin.headers,
                       json={"max_fail": 3})
    assert r.status_code == 403, r.text


def test_super_admin_can_read_both(api_client, super_admin):
    """Non-vacuity for the two 403s above: the legitimate caller still works."""

    o = api_client.get("/admin/auth-overview", headers=super_admin.headers)
    assert o.status_code == 200, o.text
    # Subset, not equality: the payload is additive (JIT provisioning added
    # `signin_mode` and a top-level `pending` on 2026-08-17). What must hold is
    # that the established keys are still there for the existing page.
    assert {"local", "oidc", "ldap", "self_signup"} <= set(o.json())

    s = api_client.get("/admin/security-config", headers=super_admin.headers)
    assert s.status_code == 200, s.text
    assert set(s.json()) == {"lockout", "session", "secret", "cookies",
                             "app_env", "events_24h"}


# ---- 2. enabled vs configured ------------------------------------------------


def test_overview_reports_local_and_counts(api_client, super_admin):
    """Local is always on (break-glass) and the counts come from `users`."""

    body = api_client.get("/admin/auth-overview", headers=super_admin.headers).json()
    assert body["local"]["enabled"] is True
    assert body["self_signup"] is False
    # The super_admin fixture is in the table, so these cannot be zero.
    assert body["local"]["users"] >= 1
    assert body["local"]["super_admins"] >= 1

    n = _pg("SELECT count(*) AS n FROM users", fetch=True)[0]["n"]
    assert body["local"]["users"] == n


def test_overview_oidc_enabled_but_not_configured(api_client, super_admin):
    """**The state that silently fails at login.** On, with no realm behind it.

    Asserting only `enabled` would be vacuous — a card that reported `enabled`
    twice would pass. The claim is that the two are computed independently, so
    the test pins the *combination*: enabled true, configured false, while the
    control below flips exactly that.
    """

    try:
        _auth_override_set(oidc_enabled="true", oidc_discovery_url="",
                           oidc_client_id="", oidc_client_secret="")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["oidc"]["enabled"] is True
        assert body["oidc"]["configured"] is False, \
            "an unconfigured realm reported as configured — the card cannot warn"
        assert body["oidc"]["issuer"] is None
    finally:
        _auth_override_clear("oidc_enabled", "oidc_discovery_url",
                             "oidc_client_id", "oidc_client_secret")


def test_overview_oidc_configured_but_not_enabled(api_client, super_admin):
    """The mirror image: fully filled in, switch off. `configured` must not
    imply `enabled`, or the page cannot tell a staged config from a live one."""

    url = "https://kc.example.test/realms/pharma/.well-known/openid-configuration"
    try:
        _auth_override_set(oidc_enabled="false", oidc_discovery_url=url,
                           oidc_client_id="pharmacy-console",
                           oidc_client_secret="not-a-real-secret-value")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["oidc"]["enabled"] is False
        assert body["oidc"]["configured"] is True
        # As configured, not as fetched: this endpoint must never touch the
        # network (a dead IdP would hang the page).
        assert body["oidc"]["issuer"] == url
    finally:
        _auth_override_clear("oidc_enabled", "oidc_discovery_url",
                             "oidc_client_id", "oidc_client_secret")


def test_overview_oidc_missing_only_the_secret_is_not_configured(api_client, super_admin):
    """`configured` is the MINIMUM set to attempt a login, so one missing field
    is enough to fail it — a client id with no secret cannot redeem a code."""

    url = "https://kc.example.test/realms/pharma/.well-known/openid-configuration"
    try:
        _auth_override_set(oidc_enabled="true", oidc_discovery_url=url,
                           oidc_client_id="pharmacy-console", oidc_client_secret="")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["oidc"]["enabled"] is True
        assert body["oidc"]["configured"] is False
    finally:
        _auth_override_clear("oidc_enabled", "oidc_discovery_url",
                             "oidc_client_id", "oidc_client_secret")


def test_overview_ldap_enabled_but_not_configured(api_client, super_admin):
    """Same two-state claim for LDAP, plus the TLS mode the card renders."""

    try:
        _auth_override_set(ldap_enabled="true", ldap_host="", ldap_base_dn="")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["ldap"]["enabled"] is True
        assert body["ldap"]["configured"] is False
        assert body["ldap"]["host"] is None
        assert body["ldap"]["encryption"] == "none"
    finally:
        _auth_override_clear("ldap_enabled", "ldap_host", "ldap_base_dn")


def test_overview_ldap_configured_and_encryption_modes(api_client, super_admin):
    """host + base DN is the minimum; LDAPS outranks StartTLS in the label."""

    try:
        _auth_override_set(ldap_enabled="false", ldap_host="ldap.corp.test",
                           ldap_base_dn="ou=users,dc=corp,dc=test",
                           ldap_start_tls="true", ldap_use_ssl="false")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["ldap"]["enabled"] is False
        assert body["ldap"]["configured"] is True
        assert body["ldap"]["host"] == "ldap.corp.test"
        assert body["ldap"]["encryption"] == "starttls"

        # With both set, `_ldap_connect` skips StartTLS — so must the label.
        _auth_override_set(ldap_use_ssl="true")
        body = api_client.get("/admin/auth-overview",
                              headers=super_admin.headers).json()
        assert body["ldap"]["encryption"] == "ldaps"
    finally:
        _auth_override_clear("ldap_enabled", "ldap_host", "ldap_base_dn",
                             "ldap_start_tls", "ldap_use_ssl")


# ---- 3. no secret material, anywhere ----------------------------------------


SECRET_SENTINEL = "S3CRET-signing-key-do-not-leak-0123456789abcdef"
OIDC_SENTINEL = "OIDCSECRET-do-not-leak-abcdef"
LDAP_SENTINEL = "LDAPBINDPW-do-not-leak-abcdef"


def test_no_secret_material_in_either_response(api_client, monkeypatch):
    """Plant the real values, then assert none of them appears in either body.

    Asserted on the raw response text rather than on named fields: the point is
    that a field added to these endpoints later cannot carry a secret past this
    test. `secret_key` is monkeypatched BEFORE the caller's token is minted,
    since the token is signed with it.
    """

    s = get_settings()
    monkeypatch.setattr(s, "secret_key", SECRET_SENTINEL, raising=False)

    caller = _Admin(role="super_admin")     # token signed with the sentinel
    try:
        _auth_override_set(oidc_client_secret=OIDC_SENTINEL,
                           ldap_bind_password=LDAP_SENTINEL)

        overview = api_client.get("/admin/auth-overview", headers=caller.headers)
        config = api_client.get("/admin/security-config", headers=caller.headers)
        assert overview.status_code == 200, overview.text
        assert config.status_code == 200, config.text

        for r in (overview, config):
            blob = repr(r.json()) + r.text
            for secret in (SECRET_SENTINEL, OIDC_SENTINEL, LDAP_SENTINEL):
                assert secret not in blob, f"{secret[:12]}… leaked into {r.url}"
                # Not even a prefix: four characters of a key is a head start.
                assert secret[:8] not in blob

        body = config.json()
        assert body["secret"] == {
            "is_set": True,
            "length": len(SECRET_SENTINEL),
            "is_default": False,
        }
    finally:
        _auth_override_clear("oidc_client_secret", "ldap_bind_password")
        caller.drop()


def test_secret_is_flagged_when_it_is_still_the_placeholder(api_client, monkeypatch):
    """`is_default` is the finding this deployment actually has."""

    s = get_settings()
    monkeypatch.setattr(s, "secret_key", DEFAULT_SECRET_KEY, raising=False)
    caller = _Admin(role="super_admin")
    try:
        body = api_client.get("/admin/security-config",
                              headers=caller.headers).json()
        assert body["secret"]["is_default"] is True
        assert body["secret"]["length"] == len(DEFAULT_SECRET_KEY)
        assert DEFAULT_SECRET_KEY not in repr(body)
    finally:
        caller.drop()


# ---- 4. session / cookie posture --------------------------------------------


def test_session_ttl_warning_mirrors_the_boot_check(api_client, super_admin, monkeypatch):
    """`exceeds_recommended` fires above 24h — the same line boot_security_checks
    warns on. Both directions, so it is not a constant."""

    s = get_settings()
    monkeypatch.setattr(s, "auth_token_ttl_hours", 168, raising=False)
    body = api_client.get("/admin/security-config", headers=super_admin.headers).json()
    assert body["session"]["token_ttl_hours"] == 168
    assert body["session"]["token_ttl_default"] == 12
    assert body["session"]["exceeds_recommended"] is True

    monkeypatch.setattr(s, "auth_token_ttl_hours", 12, raising=False)
    body = api_client.get("/admin/security-config", headers=super_admin.headers).json()
    assert body["session"]["exceeds_recommended"] is False


def test_cookie_warn_is_oidc_on_and_secure_off(api_client, super_admin, monkeypatch):
    """The SSO nonce cookie is the whole login-CSRF defence; without Secure it
    rides plaintext http. Warn only when both halves hold."""

    s = get_settings()
    monkeypatch.setattr(s, "cookie_secure", False, raising=False)
    try:
        _auth_override_set(oidc_enabled="true")
        body = api_client.get("/admin/security-config",
                              headers=super_admin.headers).json()
        assert body["cookies"] == {"cookie_secure": False, "oidc_enabled": True,
                                   "warn": True}

        monkeypatch.setattr(s, "cookie_secure", True, raising=False)
        body = api_client.get("/admin/security-config",
                              headers=super_admin.headers).json()
        assert body["cookies"]["warn"] is False

        monkeypatch.setattr(s, "cookie_secure", False, raising=False)
        _auth_override_set(oidc_enabled="false")
        body = api_client.get("/admin/security-config",
                              headers=super_admin.headers).json()
        assert body["cookies"]["warn"] is False
    finally:
        _auth_override_clear("oidc_enabled")


# ---- 5. events_24h: unknown is not zero -------------------------------------


def test_events_24h_is_null_when_the_table_is_absent(api_client, super_admin):
    """`None`, never 0. "No logins recorded" and "the audit table was never
    deployed" are different facts, and only one of them needs acting on.

    The table is renamed rather than dropped so real audit rows survive.
    """

    present = _pg("SELECT to_regclass('public.auth_events') AS t", fetch=True)[0]["t"]
    parked = f"auth_events_parked_{uuid.uuid4().hex[:8]}"
    if present:
        _pg(f'ALTER TABLE auth_events RENAME TO "{parked}"')
    try:
        r = api_client.get("/admin/security-config", headers=super_admin.headers)
        assert r.status_code == 200, r.text
        assert r.json()["events_24h"] is None, "absent table reported as zero events"
    finally:
        if present:
            _pg(f'ALTER TABLE "{parked}" RENAME TO auth_events')


def test_events_24h_counts_the_window_when_the_table_exists(api_client, super_admin):
    """And when it IS there, it is a number — including 0 for a quiet day."""

    _pg(
        """CREATE TABLE IF NOT EXISTS auth_events (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               event TEXT, email TEXT, actor_email TEXT, ip TEXT, detail TEXT
           )"""
    )
    tag = f"sec-{uuid.uuid4().hex[:10]}@corp.mm"
    before = api_client.get("/admin/security-config",
                            headers=super_admin.headers).json()["events_24h"]
    assert isinstance(before, int)
    try:
        _pg(
            """INSERT INTO auth_events (ts, event, email, ip) VALUES
               (now(),'login_fail',$1,'10.0.0.1'),
               (now() - interval '2 days','login_fail',$1,'10.0.0.1')""",
            tag,
        )
        after = api_client.get("/admin/security-config",
                               headers=super_admin.headers).json()["events_24h"]
        assert after == before + 1, "the 24h window counted the 2-day-old row"
    finally:
        _pg("DELETE FROM auth_events WHERE email=$1", tag)


# ---- 6. PUT: what it accepts -------------------------------------------------


def test_put_updates_the_lockout_numbers(api_client, super_admin):
    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"max_fail": 4, "lock_minutes": 20, "ip_max_fail": 60})
    assert r.status_code == 200, r.text
    assert r.json()["lockout"] == {"max_fail": 4, "lock_minutes": 20, "ip_max_fail": 60}

    # Persisted, not just echoed.
    again = api_client.get("/admin/security-config", headers=super_admin.headers)
    assert again.json()["lockout"]["max_fail"] == 4


def test_put_accepts_the_nested_shape_the_get_returns(api_client, super_admin):
    """The page must be able to round-trip its own body."""

    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"lockout": {"max_fail": 7}})
    assert r.status_code == 200, r.text
    assert r.json()["lockout"]["max_fail"] == 7


def test_put_rejects_token_ttl_hours_naming_the_env_var(api_client, super_admin):
    """Process-level and env-owned: lowering it here would not shorten a single
    already-issued token, so accepting it would report a protection it did not
    apply."""

    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"token_ttl_hours": 4})
    assert r.status_code == 400, r.text
    assert "AUTH_TOKEN_TTL_HOURS" in r.json()["detail"]


def test_put_rejects_cookie_secure_naming_the_env_var(api_client, super_admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "cookie_secure", False, raising=False)
    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"cookie_secure": True})
    assert r.status_code == 400, r.text
    assert "COOKIE_SECURE" in r.json()["detail"]


def test_put_rejects_a_secret_and_app_env(api_client, super_admin):
    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"secret_key": "hunter2hunter2hunter2hunter2hunt"})
    assert r.status_code == 400, r.text
    assert "SECRET_KEY" in r.json()["detail"]

    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"app_env": "production"})
    assert r.status_code == 400, r.text
    assert "APP_ENV" in r.json()["detail"]


def test_put_treats_an_unchanged_readonly_field_as_a_no_op(api_client, super_admin):
    """GET → edit one number → PUT the whole document back must work; the repo
    took the same line on the locked `catalog_mode` in `set_ingest_config`."""

    body = api_client.get("/admin/security-config", headers=super_admin.headers).json()
    body["lockout"]["max_fail"] = 6
    r = api_client.put("/admin/security-config", headers=super_admin.headers, json=body)
    assert r.status_code == 400, r.text
    # The nested read-only SECTIONS are still refused by name...
    assert "read-only" in r.json()["detail"]

    # ...while an unchanged scalar read-only field rides along fine.
    r = api_client.put(
        "/admin/security-config", headers=super_admin.headers,
        json={"max_fail": 6,
              "token_ttl_hours": body["session"]["token_ttl_hours"],
              "cookie_secure": body["cookies"]["cookie_secure"],
              "app_env": body["app_env"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lockout"]["max_fail"] == 6


@pytest.mark.parametrize("payload,expect", [
    ({"max_fail": 0}, "between 1 and 100"),
    ({"max_fail": 101}, "between 1 and 100"),
    ({"lock_minutes": 0}, "between 1 and 1440"),
    ({"lock_minutes": 100000}, "between 1 and 1440"),
    ({"ip_max_fail": 0}, "between 1 and 10000"),
    ({"max_fail": 5, "ip_max_fail": 4}, "at least"),
    ({"max_fail": "lots"}, "whole number"),
])
def test_put_range_validation(api_client, super_admin, payload, expect):
    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json=payload)
    assert r.status_code == 400, r.text
    assert expect in r.json()["detail"], r.text


def test_put_cross_field_rule_uses_the_resulting_pair(api_client, super_admin):
    """`ip_max_fail >= max_fail` is checked against what WOULD be stored, so
    raising only `max_fail` above an unchanged `ip_max_fail` is still refused.
    An IP throttle below the per-account one can never fire."""

    ok = api_client.put("/admin/security-config", headers=super_admin.headers,
                        json={"max_fail": 3, "ip_max_fail": 8})
    assert ok.status_code == 200, ok.text

    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"max_fail": 20})
    assert r.status_code == 400, r.text
    assert "at least" in r.json()["detail"]
    # And nothing moved.
    body = api_client.get("/admin/security-config", headers=super_admin.headers).json()
    assert body["lockout"]["max_fail"] == 3
    assert body["lockout"]["ip_max_fail"] == 8


def test_put_rejects_an_unknown_setting(api_client, super_admin):
    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"max_fales": 3})
    assert r.status_code == 400, r.text
    assert "unknown setting" in r.json()["detail"]


def test_put_rejects_an_empty_body(api_client, super_admin):
    r = api_client.put("/admin/security-config", headers=super_admin.headers, json={})
    assert r.status_code == 400, r.text


# ---- 7. THE anti-regression test: the login path honours a PUT ---------------


def _purge_events(email):
    _pg("CREATE TABLE IF NOT EXISTS auth_events ("
        "id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), event TEXT, "
        "email TEXT, actor_email TEXT, ip TEXT, detail TEXT)")
    _pg("DELETE FROM auth_events WHERE email=$1", email)


def test_put_lockout_is_honoured_by_the_login_path_without_a_restart(
    api_client, super_admin, monkeypatch
):
    """**The bug this guards.** The LDAP fallthrough read `get_settings()` while
    the admin page wrote a Redis override, so the toggle reported success and
    silently did nothing. `login_max_fail` had exactly that shape: the counter
    lives in `app/auth.py` but the threshold comparison lives in the login
    handler, reading the `Settings` singleton.

    Set `max_fail` to 2 through the API, then **reset the singleton to the env
    default** — that is the state of every OTHER uvicorn worker, none of which
    served the PUT. If the login path still reads env, the third bad login is a
    401 and this test fails. It must be a 429.

    A unique X-Forwarded-For keeps these fails out of the shared IP throttle
    counter that the rest of the suite feeds.
    """

    env_default = get_settings().login_max_fail
    email = f"lock-{uuid.uuid4().hex[:10]}@corp.mm"
    ip = f"198.51.100.{uuid.uuid4().int % 200 + 1}"
    hdr = {"X-Forwarded-For": ip}

    try:
        r = api_client.put("/admin/security-config", headers=super_admin.headers,
                           json={"max_fail": 2})
        assert r.status_code == 200, r.text
        assert r.json()["lockout"]["max_fail"] == 2

        # The other worker: its Settings object never saw this request.
        monkeypatch.setattr(get_settings(), "login_max_fail", env_default,
                            raising=False)

        codes = [
            api_client.post("/auth/login", headers=hdr,
                            json={"email": email, "password": "wrong"}).status_code
            for _ in range(2)
        ]
        third = api_client.post("/auth/login", headers=hdr,
                                json={"email": email, "password": "wrong"})

        assert codes == [401, 401], f"a fail below the new threshold changed early: {codes}"
        assert third.status_code == 429, (
            "the login path ignored the runtime lockout override — it is reading "
            f"env ({env_default}) instead of the effective layer: {third.text}"
        )
        assert "too many failed" in third.json()["detail"].lower()
    finally:
        _purge_events(email)


def test_lockout_stays_at_the_env_default_without_an_override(api_client):
    """Control for the test above: with no override, env still governs. Without
    this, a change that hard-locked at 2 would pass the anti-regression test."""

    env_default = get_settings().login_max_fail
    assert env_default >= 3, "this control needs an env default above the 2 used above"

    email = f"lock-{uuid.uuid4().hex[:10]}@corp.mm"
    ip = f"198.51.100.{uuid.uuid4().int % 200 + 1}"
    hdr = {"X-Forwarded-For": ip}
    try:
        codes = [
            api_client.post("/auth/login", headers=hdr,
                            json={"email": email, "password": "wrong"}).status_code
            for _ in range(3)
        ]
        assert codes == [401, 401, 401], f"locked out early with no override: {codes}"
    finally:
        _purge_events(email)


def test_lock_window_override_reaches_the_counters(api_client, super_admin):
    """`lock_minutes` is passed INTO the counter by the login handler, which
    read it before this module refreshed anything. The override has to win over
    that stale argument, or the window is one revision behind forever."""

    r = api_client.put("/admin/security-config", headers=super_admin.headers,
                       json={"lock_minutes": 1337})
    assert r.status_code == 200, r.text

    # The redis client is bound to whichever loop first touched it — the
    # TestClient's portal loop, above. Drop it so the fresh loop below builds
    # its own, or the read raises, falls back to env, and this test proves
    # nothing (it reads the singleton the PUT already mutated in-process).
    from app import cache

    cache._client = None

    async def go():
        try:
            cfg = await authmod.apply_security_overrides()
            assert cfg.login_lock_minutes == 1337
            assert "login_lock_minutes" in cfg.overridden
            # The caller's stale 15 must lose to the stored 1337.
            assert authmod._effective_window(15, cfg) == 1337
        finally:
            # Hand nothing loop-bound back to the TestClient's portal loop —
            # its lifespan shutdown would close a client attached to the loop
            # asyncio.run just destroyed.
            await cache.close_client()

    try:
        asyncio.run(go())
    finally:
        cache._client = None


# ---- 8. a Redis outage falls back to env, never to "no lockout" --------------


def test_redis_outage_falls_back_to_the_env_default(monkeypatch):
    """The dangerous failure is not "the setting did not apply", it is "the
    throttle silently switched off". A dead Redis must leave the env numbers in
    place."""

    from app import cache

    async def _boom(*a, **k):
        raise RuntimeError("redis is on fire")

    monkeypatch.setattr(cache, "get_config_overrides", _boom)
    cfg = asyncio.run(authmod.apply_security_overrides())

    s = get_settings()
    assert cfg.login_max_fail == s.login_max_fail >= 1
    assert cfg.login_lock_minutes == s.login_lock_minutes >= 1
    assert cfg.overridden == ()


def test_a_junk_override_is_ignored_rather_than_obeyed(monkeypatch):
    """A hand-edited `0` in the hash would mean "lock everyone out for ever"
    (0 fails >= 0) and `-1` would mean "never lock". Both are dropped."""

    from app import cache

    async def _junk():
        return {"security.login_max_fail": "0",
                "security.login_lock_minutes": "not-a-number",
                "security.login_ip_max_fail": "-1"}

    monkeypatch.setattr(cache, "get_config_overrides", lambda: _junk())
    cfg = asyncio.run(authmod.apply_security_overrides())
    s = get_settings()
    assert cfg.overridden == ()
    assert cfg.login_max_fail == s.login_max_fail
