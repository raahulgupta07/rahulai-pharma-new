"""Security: HMAC user signatures, canonical JSON, and session tokens.

This mirrors the CityAgent embed contract used by the existing PHP client
(``CityAgentClient``). The canonical JSON encoding here MUST byte-match the
PHP ``canonical()`` (sorted keys, no spaces, unescaped slashes/unicode) or
HMAC verification will fail across the two languages.

Flow:
  1. Host server signs ``{id, store_id, ...}`` with the shared secret (HMAC).
  2. Browser/widget sends ``user`` + ``signature`` to ``/session/create``.
  3. We verify the signature, then mint a short-lived signed session token
     (JWT) that carries the authenticated ``store_id``.
  4. ``/chat`` decodes the token and force-scopes tool calls to that store —
     the LLM never chooses the store, so one branch cannot read another's data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import jwt

from app.config import get_settings


def canonical(user: Dict[str, Any]) -> str:
    """Return canonical JSON for HMAC, byte-matching the PHP client.

    PHP: ``ksort($user); json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)``.
    Python equivalent: sorted keys, no whitespace, non-ASCII left as-is, and
    json never escapes ``/`` so slashes stay literal.
    """

    return json.dumps(
        user,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sign_user(user: Dict[str, Any], secret: Optional[str] = None) -> str:
    """HMAC-SHA256 of the canonical user payload (hex digest)."""

    secret = secret or get_settings().secret_key
    if not secret:
        raise ValueError("secret_key required to sign user payloads")
    return hmac.new(
        secret.encode("utf-8"),
        canonical(user).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_user_signature(
    user: Dict[str, Any], signature: str, secret: Optional[str] = None
) -> bool:
    """Constant-time check that ``signature`` matches ``user``."""

    if not signature:
        return False
    expected = sign_user(user, secret)
    return hmac.compare_digest(expected, signature)


def create_session_token(
    *,
    user_id: Optional[str],
    store_id: Optional[str],
    embed_id: str,
    ttl_seconds: int = 900,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint a signed (JWT/HS256) session token carrying the authenticated store.

    Returns ``{"session_token": str, "expires_in": int}``. ``store_id`` may be
    ``None`` for public (unscoped) sessions.
    """

    secret = secret or get_settings().secret_key
    if not secret:
        raise ValueError("secret_key required to mint session tokens")
    now = int(time.time())
    payload = {
        "uid": user_id,
        "store_id": store_id,
        "embed_id": embed_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return {"session_token": token, "expires_in": ttl_seconds}


def decode_session_token(token: str, secret: Optional[str] = None) -> Dict[str, Any]:
    """Decode + verify a session token. Raises ``jwt.PyJWTError`` if invalid/expired."""

    secret = secret or get_settings().secret_key
    if not secret:
        raise ValueError("secret_key required to decode session tokens")
    return jwt.decode(token, secret, algorithms=["HS256"])


__all__ = [
    "canonical",
    "sign_user",
    "verify_user_signature",
    "create_session_token",
    "decode_session_token",
]
