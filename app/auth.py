"""User management + authentication (Open-WebUI style).

One user row per EMAIL — email is the merge key. A user may have several linked
auth sources (local password, LDAP, OIDC/Keycloak): logging in via any of them
resolves to the same row, so a person keeps one identity + role regardless of
method (mirrors OAUTH_MERGE_ACCOUNTS_BY_EMAIL).

No self-signup. A super_admin is seeded from env on startup; admins/super_admins
create users in the admin panel. Sessions are JWTs signed with settings.secret_key.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import bcrypt
import jwt

from app.config import get_settings
from app.db import execute, q

ROLES = ("super_admin", "admin", "user")

# Holds the CSRF nonce between /auth/sso/login and /auth/sso/callback.
SSO_NONCE_COOKIE = "sso_nonce"


# ---- schema ----------------------------------------------------------------


async def ensure_users_table() -> None:
    await execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            BIGSERIAL PRIMARY KEY,
            email         TEXT UNIQUE NOT NULL,
            name          TEXT,
            password_hash TEXT,                       -- null for SSO/LDAP-only
            role          TEXT NOT NULL DEFAULT 'user',
            auth_sources  TEXT[] NOT NULL DEFAULT '{}',
            active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ DEFAULT now(),
            last_login    TIMESTAMPTZ
        )
        """
    )


async def seed_super_admin() -> None:
    """Create the env-configured super admin if it does not exist yet."""

    s = get_settings()
    email = s.admin_email.strip().lower()
    if not email:
        return
    existing = await q("SELECT id FROM users WHERE email=$1", email)
    if existing:
        return
    await execute(
        """INSERT INTO users (email, name, password_hash, role, auth_sources, active)
           VALUES ($1,$2,$3,'super_admin', ARRAY['local'], TRUE)""",
        email, "Super Admin", hash_password(s.admin_password),
    )


# ---- password + token ------------------------------------------------------


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def make_token(user: Dict) -> Dict:
    s = get_settings()
    ttl = s.auth_token_ttl_hours * 3600
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user["role"],
        "iat": now,
        "exp": now + ttl,
    }
    return {"token": jwt.encode(payload, s.secret_key, algorithm="HS256"), "expires_in": ttl}


def decode_token(token: str) -> Dict:
    return jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])


# ---- user lookup / merge ---------------------------------------------------


def _public(u: Dict) -> Dict:
    return {
        "id": u["id"], "email": u["email"], "name": u.get("name"),
        "role": u["role"], "active": u["active"],
        "auth_sources": list(u.get("auth_sources") or []),
    }


async def get_by_email(email: str) -> Optional[Dict]:
    rows = await q("SELECT * FROM users WHERE email=$1", email.strip().lower())
    return rows[0] if rows else None


async def link_source(user_id: int, source: str) -> None:
    """Add an auth source to a user (idempotent) — the merge mechanism."""

    await execute(
        "UPDATE users SET auth_sources = (SELECT ARRAY(SELECT DISTINCT unnest(auth_sources || $2))) WHERE id=$1",
        user_id, [source],
    )


async def _touch_login(user_id: int) -> None:
    await execute("UPDATE users SET last_login = now() WHERE id=$1", user_id)


# ---- local login -----------------------------------------------------------


async def login_local(email: str, password: str) -> Dict:
    u = await get_by_email(email)
    if not u or not u["active"]:
        raise AuthError("invalid credentials")
    if not verify_password(password, u.get("password_hash")):
        raise AuthError("invalid credentials")
    await _touch_login(u["id"])
    return make_token(u) | {"user": _public(u)}


# ---- LDAP login (merge by email) -------------------------------------------


def _ldap_server():
    """Build the ldap3 Server, with real certificate validation when TLS is on."""

    import ssl

    import ldap3

    s = get_settings()
    tls = None
    if s.ldap_use_ssl or s.ldap_start_tls:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED if s.ldap_validate_cert else ssl.CERT_NONE,
            ca_certs_file=s.ldap_ca_cert_file or None,
        )
    return ldap3.Server(
        s.ldap_host,
        port=s.ldap_port,
        use_ssl=s.ldap_use_ssl,
        tls=tls,
        get_info=ldap3.NONE,
        connect_timeout=s.ldap_timeout_seconds,
    )


def _ldap_connect(server, user, password, *, authentication=None):
    """Open a connection, StartTLS if configured, and bind. Returns the bound conn.

    ``auto_bind=False`` on purpose: with ``auto_bind=True`` a failed bind raises,
    and ldap3's ``Connection`` defines neither ``__bool__`` nor ``__len__``, so the
    obvious ``if not Connection(...)`` test is ALWAYS false. Bind explicitly and
    check the documented ``.bound`` flag instead.
    """

    import ldap3

    s = get_settings()
    conn = ldap3.Connection(
        server,
        user or None,
        password or None,
        authentication=authentication or (ldap3.SIMPLE if user else ldap3.ANONYMOUS),
        auto_bind=False,
        raise_exceptions=False,
        receive_timeout=s.ldap_timeout_seconds,
    )
    conn.open()
    if s.ldap_start_tls and not s.ldap_use_ssl:
        conn.start_tls()          # must precede bind, or the password crosses in clear
    if not conn.bind() or not conn.bound:
        return None
    return conn


def _ldap_authenticate(username: str, password: str) -> tuple[str, str]:
    """Service-bind → search → rebind as the user. Returns (email, name).

    Blocking; ldap3's sync API is used, so callers must push this to a thread.
    """

    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    s = get_settings()
    server = _ldap_server()

    try:
        svc = _ldap_connect(server, s.ldap_bind_dn, s.ldap_bind_password)
        if svc is None:
            raise AuthError("ldap service account bind failed")
        try:
            flt = s.ldap_user_filter.format(username=escape_filter_chars(username))
            svc.search(s.ldap_base_dn, flt, attributes=[s.ldap_email_attr, s.ldap_name_attr])
            if not svc.entries:
                raise AuthError("invalid credentials")
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            # mail/cn are multi-valued in the schema; ldap3 hands back a list.
            mails = entry[s.ldap_email_attr].values if s.ldap_email_attr in entry else []
            if not mails:
                raise AuthError(f"ldap entry has no {s.ldap_email_attr} attribute")
            email = str(mails[0]).strip().lower()
            names = entry[s.ldap_name_attr].values if s.ldap_name_attr in entry else []
            name = str(names[0]) if names else username
        finally:
            svc.unbind()

        # Rebind as the located user. This — and only this — proves the password.
        user_conn = _ldap_connect(server, user_dn, password, authentication=ldap3.SIMPLE)
        if user_conn is None:
            raise AuthError("invalid credentials")
        user_conn.unbind()
    except LDAPException as exc:
        # A directory that is down must not read as "wrong password", and its
        # exception must not escape as a 500 with a stack trace.
        raise AuthError("ldap server unavailable") from exc

    return email, name


async def login_ldap(username: str, password: str) -> Dict:
    import asyncio

    s = get_settings()
    if not s.ldap_enabled:
        raise AuthError("ldap disabled")

    # ⚠️ A blank password makes the user rebind below an RFC 4513 §5.1.2
    # "unauthenticated simple bind" (valid DN, zero-length password). Servers
    # CONFIGURED to allow it — some AD deployments — answer *success*, so knowing
    # any provisioned email would be enough to log in as that user. ldap3 also
    # raises LDAPPasswordIsMandatoryError on a blank simple-bind password, which
    # the old code let escape as an HTTP 500. This guard closes both: reject
    # before we ever touch the directory. (Verified live: default OpenLDAP also
    # refuses the unauthenticated bind server-side, but do not rely on that.)
    if not password or not password.strip():
        raise AuthError("invalid credentials")
    if not username or not username.strip():
        raise AuthError("invalid credentials")

    email, name = await asyncio.to_thread(_ldap_authenticate, username.strip(), password)
    user = await _merge_external(email, name, "ldap")
    return make_token(user) | {"user": _public(user)}


# ---- OIDC / Keycloak (merge by email) --------------------------------------


_DISCOVERY: Dict[str, Any] = {}   # {url: (expires_at_monotonic, metadata)}


async def _oidc_metadata() -> Dict[str, Any]:
    """Fetch (and briefly cache) the realm's .well-known document.

    Keycloak's discovery doc changes only when the realm is reconfigured, so
    re-fetching it on every login just adds two round trips to the hot path.
    """

    import httpx

    s = get_settings()
    if not s.oidc_discovery_url:
        raise AuthError("oidc: OIDC_DISCOVERY_URL is not set")

    hit = _DISCOVERY.get(s.oidc_discovery_url)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(s.oidc_discovery_url)
            r.raise_for_status()
            meta = r.json()
    except httpx.HTTPError as exc:
        raise AuthError("oidc provider unreachable") from exc

    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not meta.get(key):
            raise AuthError(f"oidc: discovery document has no {key}")

    if s.oidc_discovery_ttl_seconds > 0:
        _DISCOVERY[s.oidc_discovery_url] = (
            time.monotonic() + s.oidc_discovery_ttl_seconds, meta,
        )
    return meta


def make_state() -> tuple[str, str]:
    """Mint a CSRF ``state``. Returns (state_token, nonce).

    The nonce goes into an httponly cookie; the state token — which carries the
    same nonce, signed and time-boxed — makes the round trip through Keycloak.
    The callback only proceeds when the two agree, which proves the login was
    started by this browser. Without it, an attacker can replay their own
    authorization ``code`` at a victim's callback and silently sign the victim
    into the attacker's account.
    """

    import secrets

    s = get_settings()
    nonce = secrets.token_urlsafe(24)
    now = int(time.time())
    state = jwt.encode(
        {"nonce": nonce, "iat": now, "exp": now + s.oidc_state_ttl_seconds},
        s.secret_key, algorithm="HS256",
    )
    return state, nonce


def verify_state(state: str, cookie_nonce: str) -> None:
    """Raise AuthError unless ``state`` is our own, unexpired, and matches the cookie."""

    import hmac as _hmac

    if not state or not cookie_nonce:
        raise AuthError("sso: missing state")
    try:
        claims = jwt.decode(state, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("sso: invalid or expired state") from exc
    if not _hmac.compare_digest(str(claims.get("nonce", "")), cookie_nonce):
        raise AuthError("sso: state does not match this browser")


async def oidc_authorize_url(state: str) -> str:
    s = get_settings()
    if not s.oidc_enabled:
        raise AuthError("oidc disabled")
    meta = await _oidc_metadata()
    from urllib.parse import urlencode

    qs = urlencode({
        "client_id": s.oidc_client_id, "response_type": "code",
        "scope": s.oidc_scopes, "redirect_uri": s.oidc_redirect_uri, "state": state,
    })
    return f"{meta['authorization_endpoint']}?{qs}"


async def oidc_callback(code: str) -> Dict:
    """Exchange the authorization code and resolve the user.

    The ``id_token`` signature is not checked. That is sound here and only here:
    ``code`` is redeemed over TLS directly against ``token_endpoint``, and the
    request is authenticated with ``client_secret`` — so the response cannot be
    forged by whoever handed us the code. The profile is then read from
    ``userinfo`` with the resulting access token, not from the id_token. If this
    client is ever changed to a *public* client (no secret), this reasoning fails
    and the id_token must be verified against the realm JWKS.
    """

    import httpx

    s = get_settings()
    if not s.oidc_enabled:
        raise AuthError("oidc disabled")
    if not code:
        raise AuthError("sso: no authorization code")
    meta = await _oidc_metadata()

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tr = await c.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": s.oidc_redirect_uri,
                "client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret,
            })
            tok = tr.json() if tr.content else {}
            access = tok.get("access_token")
            if not access:
                # Keycloak answers 400 + {"error": "invalid_grant"} on a replayed
                # or expired code. Without this the old code raised KeyError -> 500.
                raise AuthError(f"sso: {tok.get('error') or 'token exchange failed'}")
            ur = await c.get(
                meta["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access}"},
            )
            ur.raise_for_status()
            userinfo = ur.json()
    except httpx.HTTPError as exc:
        raise AuthError("oidc provider unreachable") from exc

    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        raise AuthError("oidc: no email in profile")
    name = userinfo.get("name") or userinfo.get("preferred_username") or email
    user = await _merge_external(email, name, "oidc")
    return make_token(user) | {"user": _public(user)}


async def _merge_external(email: str, name: str, source: str) -> Dict:
    """Resolve an external (LDAP/OIDC) identity to a user row by EMAIL.

    Merge rule: if a user with this email exists (created locally or via another
    source), link this source to it — no duplicate. If none exists, the user was
    never provisioned: reject (no self-signup). Admins must create the user first.
    """

    u = await get_by_email(email)
    if not u:
        raise AuthError("no account for this email — ask an administrator to create one")
    if not u["active"]:
        raise AuthError("account disabled")
    await link_source(u["id"], source)
    await _touch_login(u["id"])
    return await get_by_email(email)


# ---- admin user CRUD -------------------------------------------------------


async def list_users() -> List[Dict]:
    rows = await q("SELECT * FROM users ORDER BY created_at")
    return [_public(u) | {"last_login": str(u["last_login"]) if u["last_login"] else None} for u in rows]


async def create_user(email: str, name: str, password: Optional[str], role: str) -> Dict:
    email = email.strip().lower()
    if role not in ROLES:
        raise AuthError("invalid role")
    if await get_by_email(email):
        raise AuthError("email already exists")
    sources = ["local"] if password else []
    rows = await q(
        """INSERT INTO users (email, name, password_hash, role, auth_sources)
           VALUES ($1,$2,$3,$4,$5) RETURNING *""",
        email, name, hash_password(password) if password else None, role, sources,
    )
    return _public(rows[0])


async def update_user(user_id: int, *, role: str = None, active: bool = None,
                      password: str = None) -> Dict:
    sets, params = [], []
    if role is not None:
        if role not in ROLES:
            raise AuthError("invalid role")
        params.append(role); sets.append(f"role=${len(params)}")
    if active is not None:
        params.append(active); sets.append(f"active=${len(params)}")
    if password:
        params.append(hash_password(password)); sets.append(f"password_hash=${len(params)}")
        sets.append("auth_sources = (SELECT ARRAY(SELECT DISTINCT unnest(auth_sources || ARRAY['local'])))")
    if not sets:
        raise AuthError("nothing to update")
    params.append(user_id)
    rows = await q(f"UPDATE users SET {', '.join(sets)} WHERE id=${len(params)} RETURNING *", *params)
    if not rows:
        raise AuthError("user not found")
    return _public(rows[0])


async def delete_user(user_id: int) -> int:
    rows = await q("DELETE FROM users WHERE id=$1 AND role <> 'super_admin' RETURNING id", user_id)
    return len(rows)


class AuthError(Exception):
    pass


__all__ = [
    "ensure_users_table", "seed_super_admin", "ROLES",
    "login_local", "login_ldap", "oidc_authorize_url", "oidc_callback",
    "make_state", "verify_state", "SSO_NONCE_COOKIE",
    "make_token", "decode_token", "list_users", "create_user", "update_user",
    "delete_user", "get_by_email", "AuthError",
]
