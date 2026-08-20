"""User management + authentication (Open-WebUI style).

One user row per EMAIL — email is the merge key. A user may have several linked
auth sources (local password, LDAP, OIDC/Keycloak): logging in via any of them
resolves to the same row, so a person keeps one identity + role regardless of
method (mirrors OAUTH_MERGE_ACCOUNTS_BY_EMAIL).

No self-signup. A super_admin is seeded from env on startup; admins/super_admins
create users in the admin panel. Sessions are JWTs signed with settings.secret_key.

The one exception is opt-in JIT provisioning (`oidc_auto_create` /
`ldap_auto_create`, both OFF by default): an unknown email arriving from the IdP
gets a row, but always `role='user'` and always `approved=FALSE`, so it still
cannot reach the console until an admin approves it. That is provisioning, not
self-signup — the approval step is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import bcrypt
import jwt

from app.config import DEFAULT_SECRET_KEY, get_settings
from app.db import execute, q

logger = logging.getLogger(__name__)

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
            approved      BOOLEAN NOT NULL DEFAULT FALSE,  -- admin must approve access
            created_at    TIMESTAMPTZ DEFAULT now(),
            last_login    TIMESTAMPTZ
        )
        """
    )
    # Migration for tables created before the approval gate existed. Add the
    # column, then approve everyone already in the table in the SAME one-time
    # block — those accounts were usable before the gate, so keep them usable.
    # Guarding on "column did not exist" is what keeps this from re-approving
    # pending users on every boot.
    exists = await q(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='approved'"
    )
    if not exists:
        await execute("ALTER TABLE users ADD COLUMN approved BOOLEAN NOT NULL DEFAULT FALSE")
        await execute("UPDATE users SET approved=TRUE")


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
        """INSERT INTO users (email, name, password_hash, role, auth_sources, active, approved)
           VALUES ($1,$2,$3,'super_admin', ARRAY['local'], TRUE, TRUE)""",
        email, "Super Admin", hash_password(s.admin_password),
    )


# ---- auth events: audit trail + the lockout counter -------------------------
#
# One table serves both. The lockout is "how many `login_fail` rows for this
# email since the last `login_ok`, inside the window" — no separate counter to
# drift out of sync with the log, and no Redis key that a restart forgets.
#
# ⚠️ Recording must NEVER break a login. This mirrors `log_chat` and
# `ingest_events.record`, both of which the repo made non-fatal on purpose: a
# login that succeeds but is not logged is a gap in an audit trail; a login that
# FAILS because the audit table is unreachable is an outage. `record_auth_event`
# swallows everything, and the two counters below return 0 (= "not locked") when
# they cannot read. Fail OPEN on the counter is the deliberate choice: a
# Postgres blip must not lock every account in the company out at once.

EV_LOGIN_OK = "login_ok"
EV_LOGIN_FAIL = "login_fail"
EV_LOGIN_LOCKED = "login_locked"
EV_SSO_OK = "sso_ok"
EV_SSO_FAIL = "sso_fail"
# JIT provisioning created a row. Security-relevant: it is the only way a user
# row appears without an admin typing it, so it must be visible in the log.
EV_USER_AUTOCREATE = "user_autocreate"
# Authentication SUCCEEDED and was then refused by `signin_mode` (sso_only).
# Deliberately NOT `login_fail`: the password was right, so counting it toward
# the lockout would lock out a user for a policy the admin set.
EV_LOGIN_BLOCKED = "login_blocked"


async def ensure_auth_events() -> None:
    """Create the auth_events table + indexes. Idempotent; called from lifespan.

    Kept in step with ``migrations/0004_auth_events.sql`` — a fresh boot and a
    hand-migrated database must converge on the same schema.
    """

    await execute(
        """
        CREATE TABLE IF NOT EXISTS auth_events (
            id          BIGSERIAL PRIMARY KEY,
            ts          TIMESTAMPTZ DEFAULT now(),
            event       TEXT NOT NULL,
            email       TEXT,
            actor_email TEXT,
            ip          TEXT,
            detail      TEXT
        )
        """
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_events_email_ts ON auth_events (email, ts DESC)"
    )
    await execute("CREATE INDEX IF NOT EXISTS idx_auth_events_ts ON auth_events (ts DESC)")
    await execute("CREATE INDEX IF NOT EXISTS idx_auth_events_ip_ts ON auth_events (ip, ts DESC)")


async def record_auth_event(
    event: str,
    *,
    email: Optional[str] = None,
    actor_email: Optional[str] = None,
    ip: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Append one auth event. Never raises — see the module note above."""

    try:
        await execute(
            "INSERT INTO auth_events (event, email, actor_email, ip, detail) "
            "VALUES ($1,$2,$3,$4,$5)",
            event,
            (email or "").strip().lower() or None,
            (actor_email or "").strip().lower() or None,
            ip or None,
            detail or None,
        )
    except Exception:  # noqa: BLE001 — the login outranks its own audit trail
        logger.warning("could not record auth event %s", event, exc_info=True)


async def failed_logins_for_email(email: str, minutes: Optional[int] = None) -> int:
    """`login_fail` rows for *email* inside the window AND since the last success.

    "Since the last `login_ok`" is what resets the counter on a successful
    login, with no second write and no way for a reset to be half-applied: the
    success row IS the reset. Returns 0 if the table cannot be read (fail open).

    ⚠️ This is also the point where the runtime lockout config is materialised —
    see :func:`apply_security_overrides` for why it lives here and not in the
    caller. ``minutes`` is the caller's default window; a runtime override wins
    over it, because a threshold that only half the readers honour is the exact
    bug the LDAP fallthrough shipped with.
    """

    cfg = await apply_security_overrides()
    minutes = _effective_window(minutes, cfg)

    email = (email or "").strip().lower()
    if not email:
        return 0
    try:
        rows = await q(
            """
            SELECT count(*) AS n
              FROM auth_events
             WHERE event = $1
               AND email = $2
               AND ts > now() - make_interval(mins => $3::int)
               AND ts > COALESCE(
                     (SELECT max(ts) FROM auth_events WHERE event = $4 AND email = $2),
                     'epoch'::timestamptz)
            """,
            EV_LOGIN_FAIL, email, int(minutes), EV_LOGIN_OK,
        )
    except Exception:  # noqa: BLE001 — a DB blip must not lock everyone out
        logger.warning("lockout counter unavailable for %s", email, exc_info=True)
        return 0
    return int(rows[0]["n"]) if rows else 0


async def failed_logins_for_ip(ip: str, minutes: Optional[int] = None) -> int:
    """`login_fail` rows from *ip* inside the window — the spray throttle.

    Deliberately NOT reset by a success. A password-spraying run lands a hit
    every so often; resetting on it would hand the sprayer a free reset each
    time it guessed right, which is exactly backwards.

    Same runtime-override handling as :func:`failed_logins_for_email`.
    """

    cfg = await apply_security_overrides()
    minutes = _effective_window(minutes, cfg)

    ip = (ip or "").strip()
    if not ip:
        return 0
    try:
        rows = await q(
            """
            SELECT count(*) AS n
              FROM auth_events
             WHERE event = $1
               AND ip = $2
               AND ts > now() - make_interval(mins => $3::int)
            """,
            EV_LOGIN_FAIL, ip, int(minutes),
        )
    except Exception:  # noqa: BLE001
        logger.warning("ip throttle counter unavailable for %s", ip, exc_info=True)
        return 0
    return int(rows[0]["n"]) if rows else 0


# ---- boot-time security checks ---------------------------------------------


async def boot_security_checks() -> None:
    """Refuse to start a production deploy with a giveaway signing secret.

    ``secret_key`` signs EVERYTHING — admin JWTs, embed session tokens, the
    widget HMAC, SSO state, preview links — so the shipped placeholder is not a
    lint finding, it is "anyone can mint a super_admin token". Under
    ``APP_ENV=production`` that raises here, at startup, where an operator sees
    it; anywhere else it is a loud warning so the dev stack keeps working.

    Called from the API lifespan, NOT at import: raising at import time would
    break `pytest` collection and every tool that merely imports the app.
    """

    s = get_settings()
    problems: List[str] = []
    if s.secret_key == DEFAULT_SECRET_KEY:
        problems.append(
            "SECRET_KEY is still the shipped placeholder "
            f"({DEFAULT_SECRET_KEY!r}) — it signs admin JWTs, embed session "
            "tokens, the widget HMAC, SSO state and preview links"
        )
    elif len(s.secret_key or "") < 32:
        problems.append(
            f"SECRET_KEY is only {len(s.secret_key or '')} characters; "
            "use 32+ random bytes"
        )

    production = (s.app_env or "").strip().lower() in ("production", "prod")
    if problems:
        if production:
            raise RuntimeError(
                "refusing to start with APP_ENV=production: " + "; ".join(problems)
            )
        for p in problems:
            logger.warning("INSECURE CONFIG (APP_ENV=%s, not enforced): %s", s.app_env, p)

    # Token TTL. `.env` on the running stacks sets AUTH_TOKEN_TTL_HOURS=168 — a
    # 7-day admin session — while the default and `.env.example` both say 12.
    # `.env` is operator-owned, so this names the value rather than overriding it.
    if s.auth_token_ttl_hours > 24:
        logger.warning(
            "AUTH_TOKEN_TTL_HOURS=%s (%.1f days): admin sessions stay valid that "
            "long and cannot be revoked before expiry except by disabling the "
            "account. The default is 12.",
            s.auth_token_ttl_hours, s.auth_token_ttl_hours / 24.0,
        )

    # The SSO nonce cookie is the whole login-CSRF defence. Without Secure it
    # rides plaintext http, where anything on the path can read or set it.
    try:
        oidc_on = (await effective_auth()).oidc_enabled
    except Exception:  # noqa: BLE001 — a config read must not block boot
        oidc_on = s.oidc_enabled
    if oidc_on and not s.cookie_secure:
        logger.warning(
            "OIDC is enabled but COOKIE_SECURE=false: the %s cookie is sent "
            "without the Secure flag. Set COOKIE_SECURE=true once behind TLS.",
            SSO_NONCE_COOKIE,
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
        "approved": bool(u.get("approved")),
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


def signin_mode(cfg) -> str:
    """The effective sign-in mode of a config object, defaulting safely.

    `getattr` with a default rather than an attribute read: `oidc_authorize_url`
    and friends accept any config-shaped object (tests build one by hand, and a
    stale namespace could survive a rolling deploy), and a missing attribute
    must degrade to today's `hybrid` behaviour, never to an AttributeError
    inside a login.
    """

    val = str(getattr(cfg, "signin_mode", "") or "").strip().lower()
    return val if val in SIGNIN_MODES else AUTH_DEFAULTS["signin_mode"]


class SigninModeError(Exception):
    """A password sign-in refused by policy AFTER the password was verified.

    Deliberately NOT an :class:`AuthError`. `/auth/login` catches `AuthError`
    from the local attempt and falls through to LDAP; if this were one, an
    `sso_only` refusal of a local login would silently retry the same password
    against the directory and be refused there for an unrelated reason.
    """


async def enforce_signin_mode(result: Dict, cfg=None) -> Dict:
    """Apply `signin_mode` to an ALREADY-SUCCESSFUL password sign-in.

    Order matters and is the whole design: authenticate first, then decide. The
    role that the carve-out turns on lives in the `users` row, so it is only
    knowable once we know *who* signed in — checking the mode before the
    password would mean refusing on an unauthenticated claim of identity, i.e.
    telling an anonymous caller which addresses are super_admins.

    A `super_admin` is always let through (see :data:`SIGNIN_MODES`).
    """

    cfg = cfg or await effective_auth()
    if signin_mode(cfg) != "sso_only":
        return result
    if (result.get("user") or {}).get("role") == "super_admin":
        return result
    raise SigninModeError(
        "password sign-in is disabled for this deployment — "
        "please sign in with single sign-on"
    )


# ---- runtime-editable auth config (env defaults, Redis overrides) ----------
#
# ldap_* / oidc_* start from .env (pydantic Settings) but an admin may override
# them at runtime from the panel. Overrides live in the same Redis hash as
# system_prompt, under an "auth." prefix, and are read fresh on every login — so
# a change takes effect without a restart. Secrets are write-only from the UI:
# they are stored but never sent back (see get_auth_config).

from types import SimpleNamespace

# key -> (python type, is_secret)
AUTH_KEYS = {
    "ldap_enabled": (bool, False),
    "ldap_host": (str, False),
    "ldap_port": (int, False),
    "ldap_use_ssl": (bool, False),
    "ldap_start_tls": (bool, False),
    "ldap_validate_cert": (bool, False),
    "ldap_ca_cert_file": (str, False),
    "ldap_bind_dn": (str, False),
    "ldap_bind_password": (str, True),
    "ldap_base_dn": (str, False),
    "ldap_user_filter": (str, False),
    "ldap_email_attr": (str, False),
    "ldap_name_attr": (str, False),
    "ldap_auto_create": (bool, False),
    "oidc_enabled": (bool, False),
    "oidc_provider_name": (str, False),
    "oidc_provider_type": (str, False),
    "oidc_discovery_url": (str, False),
    "oidc_client_id": (str, False),
    "oidc_client_secret": (str, True),
    "oidc_redirect_uri": (str, False),
    "oidc_scopes": (str, False),
    "oidc_auto_create": (bool, False),
    "signin_mode": (str, False),
}
_AUTH_PREFIX = "auth."

# Defaults for keys that have no `Settings` field (they are runtime-only, born
# after config.py). `effective_auth` falls back to these, so adding the env var
# later still wins without touching this map.
AUTH_DEFAULTS: Dict[str, Any] = {
    "ldap_auto_create": False,
    "oidc_auto_create": False,
    "oidc_provider_type": "keycloak",
    "signin_mode": "hybrid",
}

# `local`    — password only; the SSO button is not offered and /auth/sso/login 403s.
# `hybrid`   — both (the default, and what every existing deploy does today).
# `sso_only` — password sign-in refused, EXCEPT for a super_admin.
#
# ⚠️ The super_admin carve-out is not a convenience. `sso_only` with a
# misconfigured realm locks every human out of the console — including the one
# person who can fix the realm — and the only way back would be hand-editing
# Redis on the box. The break-glass account keeps a local password.
SIGNIN_MODES = ("local", "hybrid", "sso_only")

# Drives the logo + the default button label on the login screen only.
OIDC_PROVIDER_TYPES = ("keycloak", "entra", "google", "generic")

# key -> allowed values. Enforced on WRITE (a bad PUT is refused) and again on
# READ (a hand-edited Redis hash falls back to the default rather than putting an
# unknown string into a policy decision).
AUTH_ENUMS: Dict[str, tuple] = {
    "signin_mode": SIGNIN_MODES,
    "oidc_provider_type": OIDC_PROVIDER_TYPES,
}


def _coerce(raw: str, typ):
    if typ is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if typ is int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return raw


async def effective_auth() -> SimpleNamespace:
    """Env defaults overlaid with any Redis override, as attributes.

    Reads Redis every call on purpose — enabling SSO from the panel must not
    need a process restart, and get_settings() is lru_cached against env only.
    """

    from app import cache

    s = get_settings()
    try:
        ov = await cache.get_config_overrides()
    except Exception:   # noqa: BLE001 — Redis down must fall back to env, never 500 a login
        ov = {}
    out = {}
    for key, (typ, _secret) in AUTH_KEYS.items():
        raw = ov.get(_AUTH_PREFIX + key)
        if raw is not None:
            out[key] = _coerce(raw, typ)
        else:
            out[key] = getattr(s, key, AUTH_DEFAULTS.get(key))
    for key, allowed in AUTH_ENUMS.items():
        val = str(out.get(key) or "").strip().lower()
        if val not in allowed:
            if val:
                logger.warning("ignoring unknown %s %r (allowed %s)", key, val, allowed)
            val = AUTH_DEFAULTS[key]
        out[key] = val
    return SimpleNamespace(**out)


async def auth_config_public() -> Dict:
    """What the login screen needs — no secrets, no infra detail."""

    cfg = await effective_auth()
    return {
        "ldap_enabled": cfg.ldap_enabled,
        "oidc_enabled": cfg.oidc_enabled,
        "oidc_provider_name": cfg.oidc_provider_name,
        # Additive. `signin_mode` tells the login screen which controls to
        # render; `oidc_provider_type` picks the logo + default button label.
        # Neither is a secret and neither is load-bearing on the client — the
        # server enforces the same values on every request.
        "signin_mode": cfg.signin_mode,
        "oidc_provider_type": cfg.oidc_provider_type,
    }


async def get_auth_config() -> Dict:
    """Full config for the admin page. Secrets are masked to a boolean."""

    cfg = await effective_auth()
    out = {}
    for key, (_typ, secret) in AUTH_KEYS.items():
        val = getattr(cfg, key)
        if secret:
            out[key] = ""                      # never send the secret back
            out[key + "_set"] = bool(val)      # only whether one exists
        else:
            out[key] = val
    return out


async def set_auth_config(updates: Dict) -> None:
    """Persist a partial update from the admin page.

    A secret sent empty is left untouched (blank field == "keep current"), so the
    UI can render an unfilled password box without wiping the stored value.

    An enum key (`signin_mode`, `oidc_provider_type`) is validated BEFORE
    anything is written: storing `signin_mode="sso-only"` would read back as the
    default `hybrid` and the page would report a policy that is not in force.
    Raises :class:`AuthError` on a bad value.
    """

    from app import cache

    updates = updates or {}
    for key, allowed in AUTH_ENUMS.items():
        if key in updates:
            val = str(updates[key] or "").strip().lower()
            if val not in allowed:
                raise AuthError(f"{key} must be one of {', '.join(allowed)}")

    for key, value in updates.items():
        if key not in AUTH_KEYS:
            continue                            # ignore anything not whitelisted
        typ, secret = AUTH_KEYS[key]
        if secret and (value is None or value == ""):
            continue
        if key in AUTH_ENUMS:
            await cache.set_config_override(
                _AUTH_PREFIX + key, str(value).strip().lower())
            continue
        if typ is bool:
            stored = "true" if _coerce(str(value), bool) or value is True else "false"
        else:
            stored = str(value)
        await cache.set_config_override(_AUTH_PREFIX + key, stored)


# ---- runtime-editable SECURITY config (the lockout numbers) -----------------
#
# `login_max_fail` / `login_lock_minutes` / `login_ip_max_fail` were env-only:
# tuning a lockout meant editing `.env` and restarting the API, which in a
# pharmacy means taking the console offline to raise a threshold. They now live
# in the same Redis override hash as the auth keys, under a `security.` prefix.
#
# **Why a parallel whitelist instead of more AUTH_KEYS entries.** AUTH_KEYS is
# not just a list of names — it *is* the wire shape of `GET/PUT
# /admin/auth-config` (`get_auth_config` iterates it, `set_auth_config` accepts
# exactly it). Adding three keys there would silently change that endpoint's
# response body, and would let the Authentication tab write a lockout threshold
# through a path that has no range validation at all — `set_auth_config` stores
# whatever `str(value)` produces. These three also need bounds and a
# cross-field rule (ip >= email), which the auth keys have no concept of. Two
# concerns, two whitelists; `_coerce` and the Redis hash are still shared.
#
# ⚠️ **The bug this must not reintroduce.** The LDAP fallthrough in
# `/auth/login` read `get_settings().ldap_enabled` while the admin page wrote a
# Redis override, so the toggle reported success and did nothing. The lockout
# has the same shape: the *count* is read here, but the *threshold* comparison
# (`fails >= s.login_max_fail`) and the lock window live in `app/api.py`'s login
# handler, reading the `Settings` singleton. Rather than leave one reader on env
# and one on Redis, `apply_security_overrides()` MATERIALISES the effective
# values onto that singleton — so every reader, in this module or any other,
# sees one number. It is called from both fail counters, which is the first
# thing the login path does, before it reads any threshold.

SECURITY_KEYS = ("login_max_fail", "login_lock_minutes", "login_ip_max_fail")
_SECURITY_PREFIX = "security."

# key -> (min, max). Bounds are refusals at the API edge AND a sanity filter on
# whatever is already in Redis: a stored 0 would mean "lock everyone out
# permanently" (0 fails >= 0) and a stored -1 would mean "never lock" — both
# reachable by hand-editing the hash, neither survivable as a silent default.
SECURITY_BOUNDS = {
    "login_max_fail": (1, 100),
    "login_lock_minutes": (1, 1440),      # 1 minute .. 24 hours
    "login_ip_max_fail": (1, 10000),
}


def _security_overrides(ov: Dict) -> Dict[str, int]:
    """The valid `security.*` values in a raw override hash. Junk is dropped."""

    out: Dict[str, int] = {}
    for key in SECURITY_KEYS:
        raw = ov.get(_SECURITY_PREFIX + key)
        if raw is None:
            continue
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning("ignoring non-numeric %s override %r", key, raw)
            continue
        lo, hi = SECURITY_BOUNDS[key]
        if not lo <= val <= hi:
            logger.warning("ignoring out-of-range %s override %r (allowed %s..%s)",
                           key, val, lo, hi)
            continue
        out[key] = val
    return out


async def apply_security_overrides() -> SimpleNamespace:
    """Materialise the effective lockout numbers onto the `Settings` singleton.

    Returns the effective values, and carries the names it actually overrode in
    ``.overridden``.

    Mutating the cached `Settings` object is deliberate and is the whole point:
    `app/api.py`'s login handler holds that object and compares against
    ``s.login_max_fail`` / ``s.login_lock_minutes``. Writing the override there
    is what makes a PUT take effect *for the readers we do not own*, instead of
    creating a second source of truth that disagrees with them — which is
    exactly how the LDAP toggle came to report success and do nothing.

    Two rules keep it safe:

    * **Only keys with a valid override are written.** Absent an override the
      field keeps whatever it holds (the env default — or a test's
      monkeypatched value, which must not be clobbered by a helper it did not
      call).
    * **A Redis outage changes nothing.** The read is wrapped and returns the
      current values, so an unreachable Redis falls back to the env defaults and
      never to "no lockout" — failing open on the *threshold* would be a silent
      removal of the throttle, which is worse than an unapplied setting.
    """

    from app import cache

    s = get_settings()
    try:
        ov = await cache.get_config_overrides()
    except Exception:  # noqa: BLE001 — Redis down must fall back to env, never disable the lockout
        ov = {}

    applied = _security_overrides(ov or {})
    for key, val in applied.items():
        if getattr(s, key, None) != val:
            logger.info("security override: %s = %s (was %s)", key, val, getattr(s, key, None))
            setattr(s, key, val)

    return SimpleNamespace(
        overridden=tuple(applied),
        **{key: getattr(s, key) for key in SECURITY_KEYS},
    )


def _effective_window(minutes: Optional[int], cfg: SimpleNamespace) -> int:
    """The lock window to count over: the runtime override, else the caller's.

    `app/api.py` reads ``s.login_lock_minutes`` and passes it in *before* this
    module has refreshed the singleton, so on the first login after a change
    that argument is one revision stale. When an override exists it wins — the
    stored setting is the authority, not the value the caller happened to
    snapshot. With no override the argument stands, which keeps an explicit
    caller (and every existing test) in charge.
    """

    if "login_lock_minutes" in cfg.overridden or minutes is None:
        return int(cfg.login_lock_minutes)
    return int(minutes)


async def effective_security() -> SimpleNamespace:
    """Env defaults overlaid with the Redis `security.*` override, as attributes.

    The read-only twin of :func:`apply_security_overrides` — same values, but it
    also materialises them, because a caller that only *reports* the effective
    config while the login path still enforces the env one would be a lie on a
    security page.
    """

    return await apply_security_overrides()


class SecurityConfigError(Exception):
    """A rejected security-config write. Carries the operator-facing reason."""


async def set_security_config(updates: Dict) -> SimpleNamespace:
    """Persist the three lockout numbers. Raises SecurityConfigError on a bad one.

    Validation happens against the values that would RESULT, not against the
    request alone: ``ip_max_fail >= max_fail`` has to hold for the pair that
    ends up stored, so raising `max_fail` above an unchanged `ip_max_fail` is
    refused even though the request named only one field. An IP throttle below
    the per-email one is a throttle that can never fire.

    All-or-nothing: everything is validated before anything is written, so a
    rejected request cannot leave one number changed and the other not.
    """

    from app import cache

    current = await apply_security_overrides()
    wanted: Dict[str, int] = {}

    for key, value in (updates or {}).items():
        if key not in SECURITY_KEYS:
            raise SecurityConfigError(f"unknown security setting {key!r}")
        try:
            val = int(str(value).strip())
        except (TypeError, ValueError):
            raise SecurityConfigError(f"{key} must be a whole number, got {value!r}")
        lo, hi = SECURITY_BOUNDS[key]
        if not lo <= val <= hi:
            raise SecurityConfigError(f"{key} must be between {lo} and {hi}, got {val}")
        wanted[key] = val

    if not wanted:
        raise SecurityConfigError("nothing to update")

    result = {key: wanted.get(key, getattr(current, key)) for key in SECURITY_KEYS}
    if result["login_ip_max_fail"] < result["login_max_fail"]:
        raise SecurityConfigError(
            f"login_ip_max_fail ({result['login_ip_max_fail']}) must be at least "
            f"login_max_fail ({result['login_max_fail']}) — an IP throttle below "
            "the per-account one can never fire"
        )

    for key, val in wanted.items():
        await cache.set_config_override(_SECURITY_PREFIX + key, str(val))

    # Apply immediately rather than waiting for the next login to refresh: the
    # PUT's own response must describe the config that is now in force.
    return await apply_security_overrides()


# ---- LDAP login (merge by email) -------------------------------------------


def _ldap_server(cfg):
    """Build the ldap3 Server, with real certificate validation when TLS is on."""

    import ssl

    import ldap3

    tls = None
    if cfg.ldap_use_ssl or cfg.ldap_start_tls:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED if cfg.ldap_validate_cert else ssl.CERT_NONE,
            ca_certs_file=cfg.ldap_ca_cert_file or None,
        )
    return ldap3.Server(
        cfg.ldap_host,
        port=cfg.ldap_port,
        use_ssl=cfg.ldap_use_ssl,
        tls=tls,
        get_info=ldap3.NONE,
        connect_timeout=get_settings().ldap_timeout_seconds,
    )


def _ldap_connect(server, user, password, cfg, *, authentication=None):
    """Open a connection, StartTLS if configured, and bind. Returns the bound conn.

    ``auto_bind=False`` on purpose: with ``auto_bind=True`` a failed bind raises,
    and ldap3's ``Connection`` defines neither ``__bool__`` nor ``__len__``, so the
    obvious ``if not Connection(...)`` test is ALWAYS false. Bind explicitly and
    check the documented ``.bound`` flag instead.
    """

    import ldap3

    conn = ldap3.Connection(
        server,
        user or None,
        password or None,
        authentication=authentication or (ldap3.SIMPLE if user else ldap3.ANONYMOUS),
        auto_bind=False,
        raise_exceptions=False,
        receive_timeout=get_settings().ldap_timeout_seconds,
    )
    conn.open()
    if cfg.ldap_start_tls and not cfg.ldap_use_ssl:
        conn.start_tls()          # must precede bind, or the password crosses in clear
    if not conn.bind() or not conn.bound:
        return None
    return conn


def _ldap_authenticate(username: str, password: str, cfg) -> tuple[str, str]:
    """Service-bind → search → rebind as the user. Returns (email, name).

    Blocking; ldap3's sync API is used, so callers must push this to a thread.
    """

    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    server = _ldap_server(cfg)

    try:
        svc = _ldap_connect(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, cfg)
        if svc is None:
            raise AuthError("ldap service account bind failed")
        try:
            flt = cfg.ldap_user_filter.format(username=escape_filter_chars(username))
            svc.search(cfg.ldap_base_dn, flt, attributes=[cfg.ldap_email_attr, cfg.ldap_name_attr])
            if not svc.entries:
                raise AuthError("invalid credentials")
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            # mail/cn are multi-valued in the schema; ldap3 hands back a list.
            mails = entry[cfg.ldap_email_attr].values if cfg.ldap_email_attr in entry else []
            if not mails:
                raise AuthError(f"ldap entry has no {cfg.ldap_email_attr} attribute")
            email = str(mails[0]).strip().lower()
            names = entry[cfg.ldap_name_attr].values if cfg.ldap_name_attr in entry else []
            name = str(names[0]) if names else username
        finally:
            svc.unbind()

        # Rebind as the located user. This — and only this — proves the password.
        user_conn = _ldap_connect(server, user_dn, password, cfg, authentication=ldap3.SIMPLE)
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

    cfg = await effective_auth()
    if not cfg.ldap_enabled:
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

    email, name = await asyncio.to_thread(_ldap_authenticate, username.strip(), password, cfg)
    user = await _merge_external(email, name, "ldap", cfg=cfg)
    return make_token(user) | {"user": _public(user)}


# ---- OIDC / Keycloak (merge by email) --------------------------------------


_DISCOVERY: Dict[str, Any] = {}   # {url: (expires_at_monotonic, metadata)}


async def _oidc_metadata(cfg) -> Dict[str, Any]:
    """Fetch (and briefly cache) the realm's .well-known document.

    Keycloak's discovery doc changes only when the realm is reconfigured, so
    re-fetching it on every login just adds two round trips to the hot path.
    """

    import httpx

    if not cfg.oidc_discovery_url:
        raise AuthError("oidc: discovery URL is not set")

    hit = _DISCOVERY.get(cfg.oidc_discovery_url)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(cfg.oidc_discovery_url)
            r.raise_for_status()
            meta = r.json()
    except httpx.HTTPError as exc:
        raise AuthError("oidc provider unreachable") from exc

    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not meta.get(key):
            raise AuthError(f"oidc: discovery document has no {key}")

    if get_settings().oidc_discovery_ttl_seconds > 0:
        _DISCOVERY[cfg.oidc_discovery_url] = (
            time.monotonic() + get_settings().oidc_discovery_ttl_seconds, meta,
        )
    return meta


# ---- id_token verification (realm JWKS) -------------------------------------
#
# {jwks_uri: (expires_at_monotonic, jwks_document)} — same shape as _DISCOVERY.

_JWKS: Dict[str, Any] = {}


async def _fetch_jwks(uri: str) -> Dict[str, Any]:
    """GET the key set and cache it. Raises AuthError, never httpx's exception."""

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(uri)
            r.raise_for_status()
            doc = r.json()
    except httpx.HTTPError as exc:
        # A fetch failure must read as "we could not verify you" (401), never as
        # a 500 with a stack trace naming the realm.
        raise AuthError("oidc: could not fetch the realm signing keys") from exc
    except ValueError as exc:
        raise AuthError("oidc: signing key set is not valid JSON") from exc

    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not keys:
        raise AuthError("oidc: signing key set is empty")
    ttl = get_settings().oidc_jwks_ttl_seconds
    if ttl > 0:
        _JWKS[uri] = (time.monotonic() + ttl, doc)
    return doc


def _find_kid(doc: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    for jwk in doc.get("keys") or []:
        if isinstance(jwk, dict) and jwk.get("kid") == kid:
            return jwk
    return None


async def _signing_key(uri: str, kid: str):
    """Resolve `kid` to a public key, refetching the key set at most ONCE.

    ⚠️ There is no "just use the first key" fallback, and adding one would be a
    mistake: during a realm key rotation the first key is usually the NEW one
    while the token in hand is signed by the old, so the fallback converts a
    clear "unknown key id" into an inscrutable signature error — and, worse, it
    means a token carrying an unknown `kid` is still checked against *some* key
    instead of being refused outright.
    """

    if not kid:
        raise AuthError("oidc: id_token has no key id")

    hit = _JWKS.get(uri)
    if hit and hit[0] > time.monotonic():
        doc = hit[1]
        jwk = _find_kid(doc, kid)
        if jwk is None:
            # Cached set predates a rotation — refetch exactly once.
            jwk = _find_kid(await _fetch_jwks(uri), kid)
    else:
        jwk = _find_kid(await _fetch_jwks(uri), kid)

    if jwk is None:
        raise AuthError("oidc: id_token signed by an unknown key")

    alg = jwk.get("alg") or {"RSA": "RS256", "EC": "ES256"}.get(jwk.get("kty", ""))
    if alg not in ID_TOKEN_ALGORITHMS:
        raise AuthError(f"oidc: unsupported id_token signing algorithm {alg!r}")
    try:
        return jwt.PyJWK(jwk, algorithm=alg).key
    except Exception as exc:  # noqa: BLE001 — a malformed JWK is a 401, not a 500
        raise AuthError("oidc: signing key could not be parsed") from exc


ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]


async def verify_id_token(id_token: str, cfg, meta: Dict[str, Any],
                          expected_nonce: str = "") -> Dict[str, Any]:
    """Verify the id_token against the realm JWKS. Returns its claims.

    Four checks, each closing something the old "don't verify at all" path left
    open once the Keycloak client is made *public* (no secret):

    * **signature** against the `kid`'s key from `jwks_uri` — RS256/ES256 only,
      so a token can't downgrade itself to `alg: none` or to HS256 signed with
      the (public) modulus.
    * **issuer** must equal the discovery document's `issuer`.
    * **audience**: `client_id` in `aud` OR `azp == client_id`. A naive
      `verify_aud=True` FAILS against a real Keycloak, which puts `aud:
      "account"` in the id_token and identifies the client in `azp`.
    * **nonce** must match the one we minted for this login, which is what stops
      an id_token captured from another session being replayed here.
    """

    import hmac as _hmac

    jwks_uri = meta.get("jwks_uri")
    if not jwks_uri:
        raise AuthError("oidc: discovery document has no jwks_uri")
    issuer = meta.get("issuer")
    if not issuer:
        raise AuthError("oidc: discovery document has no issuer")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise AuthError("oidc: id_token is malformed") from exc

    key = await _signing_key(jwks_uri, header.get("kid", ""))

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=ID_TOKEN_ALGORITHMS,
            issuer=issuer,
            # Checked by hand below — Keycloak's aud is "account", not the client.
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"oidc: id_token rejected ({exc})") from exc

    aud = claims.get("aud")
    auds = [aud] if isinstance(aud, str) else list(aud or [])
    if cfg.oidc_client_id not in auds and claims.get("azp") != cfg.oidc_client_id:
        raise AuthError("oidc: id_token was not issued for this client")

    if expected_nonce:
        if not _hmac.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
            raise AuthError("sso: id_token nonce does not match this login")

    return claims


def state_nonce(state: str) -> str:
    """The nonce carried inside our own signed state token ("" if unreadable)."""

    try:
        claims = jwt.decode(state, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return ""
    return str(claims.get("nonce") or "")


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
    cfg = await effective_auth()
    if not cfg.oidc_enabled:
        raise AuthError("oidc disabled")
    if signin_mode(cfg) == "local":
        raise AuthError("single sign-on is disabled (sign-in mode is local)")
    meta = await _oidc_metadata(cfg)
    from urllib.parse import urlencode

    params = {
        "client_id": cfg.oidc_client_id, "response_type": "code",
        "scope": cfg.oidc_scopes, "redirect_uri": cfg.oidc_redirect_uri, "state": state,
    }
    # The OIDC `nonce` is the SAME value the signed state carries and the
    # httponly cookie holds — one per-login secret, not three. The IdP copies it
    # into the id_token, so `verify_id_token` can prove the token was minted for
    # THIS login rather than lifted from another session.
    nonce = state_nonce(state)
    if nonce:
        params["nonce"] = nonce
    qs = urlencode(params)
    return f"{meta['authorization_endpoint']}?{qs}"


async def oidc_callback(code: str, expected_nonce: str = "") -> Dict:
    """Exchange the authorization code, VERIFY the id_token, resolve the user.

    The ``id_token`` signature IS checked, against the realm's JWKS (see
    :func:`verify_id_token`). It used to be skipped, on the reasoning that
    ``code`` is redeemed over TLS against ``token_endpoint`` authenticated with
    ``client_secret``, so the response cannot be forged. That reasoning is true
    only while the Keycloak client stays *confidential* — and Keycloak 26
    defaults new clients to **public** (Client authentication Off), which
    silently removes the secret and with it the entire argument. Verifying is
    cheap and does not depend on how the client happens to be configured.

    ``userinfo`` remains the profile source (it is the endpoint that reflects a
    user edited in the realm since the token was minted), but a *verified*
    ``email`` claim from the id_token is preferred when one is present.
    """

    import httpx

    cfg = await effective_auth()
    if not cfg.oidc_enabled:
        raise AuthError("oidc disabled")
    # The callback is a second, independent reader of the mode: switching to
    # `local` must invalidate a login already in flight, not just hide the
    # button. Every consumer reads the same effective layer.
    if signin_mode(cfg) == "local":
        raise AuthError("single sign-on is disabled (sign-in mode is local)")
    if not code:
        raise AuthError("sso: no authorization code")
    meta = await _oidc_metadata(cfg)

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tr = await c.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": cfg.oidc_redirect_uri,
                "client_id": cfg.oidc_client_id, "client_secret": cfg.oidc_client_secret,
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

    # Verified id_token claims, when the provider returned one. A provider that
    # returns none (no `openid` scope) leaves us on the old userinfo-only path;
    # that is worth a warning, not a refusal, because it is a config choice the
    # operator can see in OIDC_SCOPES.
    claims: Dict[str, Any] = {}
    id_token = tok.get("id_token")
    if id_token:
        claims = await verify_id_token(id_token, cfg, meta, expected_nonce)
    else:
        logger.warning(
            "oidc: token response carried no id_token (is 'openid' in OIDC_SCOPES?) "
            "— falling back to userinfo alone"
        )

    email = ""
    # A verified claim beats an unauthenticated profile read, but an
    # email_verified of exactly False means the realm itself does not vouch for
    # it — and email is our merge key, so an unvouched one could claim another
    # person's account.
    if claims.get("email") and claims.get("email_verified") is not False:
        email = str(claims["email"]).strip().lower()
    if not email:
        email = (userinfo.get("email") or "").strip().lower()
    if not email:
        raise AuthError("oidc: no email in profile")
    name = (
        userinfo.get("name") or claims.get("name")
        or userinfo.get("preferred_username") or claims.get("preferred_username")
        or email
    )
    user = await _merge_external(email, name, "oidc", cfg=cfg)
    return make_token(user) | {"user": _public(user)}


NO_ACCOUNT_MESSAGE = "no account for this email — ask an administrator to create one"

# source -> the effective-config flag that allows JIT provisioning for it.
_AUTO_CREATE_FLAG = {"oidc": "oidc_auto_create", "ldap": "ldap_auto_create"}


def _auto_create_enabled(cfg, source: str) -> bool:
    key = _AUTO_CREATE_FLAG.get(source)
    return bool(key and getattr(cfg, key, False))


async def _autocreate_external(email: str, name: str, source: str) -> Optional[Dict]:
    """Insert a PENDING, plain-`user` row for an external identity.

    Returns the new row, or ``None`` if another request won the race (two
    parallel logins by the same new person is the ordinary case, not the exotic
    one — a browser reloading the callback does it).

    ⚠️ **`role` and `approved` are SQL literals, not parameters.** There is
    deliberately no argument for either, so no IdP claim, group mapping or
    caller can reach them: an auto-created account is always `role='user'` and
    always `approved=FALSE`. `require_admin` then holds it on the pending screen
    until a human approves it, which is the entire point of the feature — JIT
    provisioning removes the typing, not the approval.
    """

    rows = await q(
        """INSERT INTO users (email, name, password_hash, role, auth_sources, active, approved)
           VALUES ($1, $2, NULL, 'user', $3, TRUE, FALSE)
           ON CONFLICT (email) DO NOTHING
           RETURNING *""",
        email, (name or email), [source],
    )
    return rows[0] if rows else None


async def _merge_external(email: str, name: str, source: str, cfg=None) -> Dict:
    """Resolve an external (LDAP/OIDC) identity to a user row by EMAIL.

    Merge rule: if a user with this email exists (created locally or via another
    source), link this source to it — no duplicate.

    If none exists, behaviour depends on the source's `*_auto_create` flag in the
    effective auth config:

    * **off (the default)** — reject, exactly as before. Admins provision first.
    * **on** — provision just-in-time via :func:`_autocreate_external`, which
      lands the account PENDING and unprivileged.

    An existing row is never rewritten by this path: a disabled account is still
    refused rather than resurrected, and an existing role is never touched. The
    email is normalised the same way :func:`get_by_email` normalises it, so JIT
    cannot mint a second row that shadows a user whose address differs only in
    case or surrounding whitespace.
    """

    email = (email or "").strip().lower()
    if not email:
        raise AuthError("no email in profile")

    u = await get_by_email(email)
    if not u:
        cfg = cfg or await effective_auth()
        if not _auto_create_enabled(cfg, source):
            raise AuthError(NO_ACCOUNT_MESSAGE)
        created = await _autocreate_external(email, name, source)
        if created is not None:
            await record_auth_event(
                EV_USER_AUTOCREATE, email=email,
                detail=f"{source}: provisioned pending account (role=user, approved=false)",
            )
            await _touch_login(created["id"])
            return await get_by_email(email)
        # Lost the insert race — fall through and treat it as an existing row,
        # which also re-applies the `active` check to it.
        u = await get_by_email(email)
        if not u:
            raise AuthError(NO_ACCOUNT_MESSAGE)
    if not u["active"]:
        raise AuthError("account disabled")
    await link_source(u["id"], source)
    await _touch_login(u["id"])
    return await get_by_email(email)


# ---- admin user CRUD -------------------------------------------------------


async def list_users() -> List[Dict]:
    rows = await q("SELECT * FROM users ORDER BY created_at")
    return [_public(u) | {"last_login": str(u["last_login"]) if u["last_login"] else None} for u in rows]


async def create_user(email: str, name: str, password: Optional[str], role: str,
                      approved: bool = False) -> Dict:
    email = email.strip().lower()
    if role not in ROLES:
        raise AuthError("invalid role")
    if await get_by_email(email):
        raise AuthError("email already exists")
    sources = ["local"] if password else []
    rows = await q(
        """INSERT INTO users (email, name, password_hash, role, auth_sources, approved)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        email, name, hash_password(password) if password else None, role, sources, approved,
    )
    return _public(rows[0])


async def update_user(user_id: int, *, role: str = None, active: bool = None,
                      approved: bool = None, password: str = None) -> Dict:
    sets, params = [], []
    if role is not None:
        if role not in ROLES:
            raise AuthError("invalid role")
        params.append(role); sets.append(f"role=${len(params)}")
    if active is not None:
        params.append(active); sets.append(f"active=${len(params)}")
    if approved is not None:
        params.append(approved); sets.append(f"approved=${len(params)}")
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
    "ensure_auth_events", "record_auth_event",
    "failed_logins_for_email", "failed_logins_for_ip",
    "EV_LOGIN_OK", "EV_LOGIN_FAIL", "EV_LOGIN_LOCKED", "EV_SSO_OK", "EV_SSO_FAIL",
    "EV_USER_AUTOCREATE", "EV_LOGIN_BLOCKED",
    "boot_security_checks",
    "login_local", "login_ldap", "oidc_authorize_url", "oidc_callback",
    "verify_id_token", "state_nonce",
    "make_state", "verify_state", "SSO_NONCE_COOKIE",
    "make_token", "decode_token", "list_users", "create_user", "update_user",
    "delete_user", "get_by_email", "AuthError",
    "AUTH_KEYS", "AUTH_DEFAULTS", "AUTH_ENUMS", "effective_auth", "auth_config_public",
    "SIGNIN_MODES", "OIDC_PROVIDER_TYPES", "SigninModeError", "enforce_signin_mode",
    "signin_mode",
    "NO_ACCOUNT_MESSAGE",
    "get_auth_config", "set_auth_config",
    "SECURITY_KEYS", "SECURITY_BOUNDS", "SecurityConfigError",
    "effective_security", "apply_security_overrides", "set_security_config",
]
