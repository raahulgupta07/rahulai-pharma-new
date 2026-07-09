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


async def login_ldap(username: str, password: str) -> Dict:
    s = get_settings()
    if not s.ldap_enabled:
        raise AuthError("ldap disabled")
    import ldap3

    server = ldap3.Server(s.ldap_host, port=s.ldap_port, use_ssl=s.ldap_use_ssl)
    # 1) bind with service account, find the user entry
    conn = ldap3.Connection(server, s.ldap_bind_dn, s.ldap_bind_password, auto_bind=True)
    flt = s.ldap_user_filter.format(username=ldap3.utils.conv.escape_filter_chars(username))
    conn.search(s.ldap_base_dn, flt, attributes=[s.ldap_email_attr, s.ldap_name_attr])
    if not conn.entries:
        raise AuthError("ldap user not found")
    entry = conn.entries[0]
    user_dn = entry.entry_dn
    email = str(entry[s.ldap_email_attr]).strip().lower()
    name = str(entry[s.ldap_name_attr]) if s.ldap_name_attr in entry else username
    # 2) rebind as the user to verify the password
    if not ldap3.Connection(server, user_dn, password, auto_bind=True):
        raise AuthError("invalid credentials")
    user = await _merge_external(email, name, "ldap")
    return make_token(user) | {"user": _public(user)}


# ---- OIDC / Keycloak (merge by email) --------------------------------------


async def oidc_authorize_url(state: str) -> str:
    s = get_settings()
    if not s.oidc_enabled:
        raise AuthError("oidc disabled")
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        meta = (await c.get(s.oidc_discovery_url)).json()
    from urllib.parse import urlencode

    qs = urlencode({
        "client_id": s.oidc_client_id, "response_type": "code",
        "scope": s.oidc_scopes, "redirect_uri": s.oidc_redirect_uri, "state": state,
    })
    return f"{meta['authorization_endpoint']}?{qs}"


async def oidc_callback(code: str) -> Dict:
    s = get_settings()
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        meta = (await c.get(s.oidc_discovery_url)).json()
        tok = (await c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": s.oidc_redirect_uri,
            "client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret,
        })).json()
        userinfo = (await c.get(
            meta["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )).json()
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
    "make_token", "decode_token", "list_users", "create_user", "update_user",
    "delete_user", "get_by_email", "AuthError",
]
