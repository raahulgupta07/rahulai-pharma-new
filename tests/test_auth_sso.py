"""Guard the two authentication bypasses that Keycloak/LDAP shipped with.

Both were live in the baseline commit and dormant only because `ldap_enabled`
and `oidc_enabled` default to False. Enabling SSO without these guards is an
unauthenticated path to an admin JWT, so each test below is written to FAIL if
its fix is reverted:

* `test_ldap_rejects_empty_password` — an empty password makes the user rebind an
  RFC 4513 unauthenticated simple bind, which real directories answer "success".
* `test_ldap_connection_object_is_always_truthy` — pins the ldap3 fact the old
  `if not Connection(...)` check got wrong, so an upgrade that "fixes" the
  truthiness does not silently make our guard redundant-looking.
* `test_sso_state_*` — a constant, unchecked `state` is login-CSRF.

No live directory or Keycloak needed: the bypasses are all reachable before any
network call.
"""

from __future__ import annotations

import asyncio

import pytest

from app import auth as authmod
from app.auth import AuthError
from app.config import get_settings


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ldap_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ldap_enabled", True, raising=False)
    return s


# ---- LDAP -------------------------------------------------------------------


@pytest.mark.parametrize("password", ["", "   ", "\t", "\n"])
def test_ldap_rejects_empty_password(ldap_on, monkeypatch, password):
    """Blank password must never reach the bind.

    If it does, OpenLDAP/AD perform an *unauthenticated simple bind* and return
    success — so any provisioned email logs in with no password at all.
    """

    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        return ("someone@corp.example", "Someone")

    monkeypatch.setattr(authmod, "_ldap_authenticate", _boom)

    with pytest.raises(AuthError):
        run(authmod.login_ldap("victim@corp.example", password))
    assert called is False, "empty password reached the LDAP bind"


def test_ldap_rejects_blank_username(ldap_on, monkeypatch):
    """Must short-circuit BEFORE the bind.

    Asserting only `raises(AuthError)` would be vacuous: _merge_external rejects
    an unprovisioned email anyway, so the test would pass with the guard removed.
    The claim under test is that we never touch the directory at all.
    """

    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        return ("someone@corp.example", "Someone")

    monkeypatch.setattr(authmod, "_ldap_authenticate", _boom)

    with pytest.raises(AuthError):
        run(authmod.login_ldap("  ", "a-real-password"))
    assert called is False, "blank username reached the LDAP bind"


def test_ldap_connection_object_is_always_truthy():
    """The fact the original `if not ldap3.Connection(...)` check depended on.

    ldap3's Connection defines neither __bool__ nor __len__, so it is truthy even
    when the bind failed and `.bound` is False. Any password check written as
    `if not Connection(...)` is dead code. We assert `.bound` instead.
    """

    import ldap3

    conn = ldap3.Connection(ldap3.Server("localhost"), "cn=nobody,dc=x", "wrong")
    assert bool(conn) is True          # <- the trap
    assert conn.bound is False         # <- what we actually check


def test_ldap_bind_failure_is_an_autherror_not_a_500(ldap_on, monkeypatch):
    """A wrong password must surface as 401, not an uncaught LDAPBindError."""

    from ldap3.core.exceptions import LDAPBindError

    def _raise(*a, **k):
        raise LDAPBindError("invalid credentials")

    monkeypatch.setattr(authmod, "_ldap_connect", _raise)
    monkeypatch.setattr(get_settings(), "ldap_bind_dn", "cn=svc", raising=False)

    with pytest.raises(AuthError):
        run(authmod.login_ldap("someone", "wrong-password"))


def test_ldap_tls_validates_certificates_by_default():
    """LDAPS without cert validation is MITM-able; the default must be strict."""

    import ssl

    s = get_settings()
    assert s.ldap_validate_cert is True

    from unittest.mock import patch

    with patch.object(s, "ldap_use_ssl", True):
        server = authmod._ldap_server()
    assert server.tls is not None, "TLS enabled but no Tls object -> no validation"
    assert server.tls.validate == ssl.CERT_REQUIRED


# ---- OIDC state / CSRF ------------------------------------------------------


def test_sso_state_is_unpredictable():
    """A constant state (the old literal 'citcare') is no CSRF defence at all."""

    states = {authmod.make_state()[0] for _ in range(5)}
    nonces = {authmod.make_state()[1] for _ in range(5)}
    assert len(states) == 5 and len(nonces) == 5


def test_sso_state_roundtrips():
    state, nonce = authmod.make_state()
    authmod.verify_state(state, nonce)   # must not raise


def test_sso_state_rejects_a_foreign_nonce():
    """The attacker's `code` replayed at a victim's callback: cookie won't match."""

    state, _ = authmod.make_state()
    _, other_nonce = authmod.make_state()
    with pytest.raises(AuthError):
        authmod.verify_state(state, other_nonce)


def test_sso_state_rejects_a_missing_cookie():
    state, _ = authmod.make_state()
    with pytest.raises(AuthError):
        authmod.verify_state(state, "")


def test_sso_state_rejects_an_unsigned_state():
    """State must be signed with secret_key, not merely well-formed."""

    import jwt

    forged = jwt.encode({"nonce": "n", "exp": 9999999999}, "not-our-key", algorithm="HS256")
    with pytest.raises(AuthError):
        authmod.verify_state(forged, "n")


def test_sso_state_expires():
    import time

    import jwt

    stale = jwt.encode(
        {"nonce": "n", "iat": int(time.time()) - 3600, "exp": int(time.time()) - 60},
        get_settings().secret_key, algorithm="HS256",
    )
    with pytest.raises(AuthError):
        authmod.verify_state(stale, "n")


def test_oidc_callback_rejects_a_missing_code(monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_enabled", True, raising=False)
    with pytest.raises(AuthError):
        run(authmod.oidc_callback(""))


# ---- merge policy: SSO must not be able to provision an account -------------


def test_external_login_cannot_create_a_user():
    """No self-signup: a Keycloak realm admin must not be able to mint a pharmacy
    admin just by creating a realm user with the right email."""

    import inspect

    src = inspect.getsource(authmod._merge_external)
    assert "INSERT" not in src.upper(), "_merge_external must never create users"
