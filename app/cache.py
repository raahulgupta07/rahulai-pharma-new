"""Redis layer: query cache, rate limiting, and multi-tenant credentials.

Backed by an async redis client built from ``settings.redis_url``. Three jobs:

* **Query cache** — pharmacy questions repeat hard (everyone asks the popular
  drugs). Cache the agent's answer keyed by (message, store_id, data_version)
  so repeats skip the LLM entirely. A monotonically-increasing ``data_version``
  namespaces the cache; bumping it on reload invalidates everything at once.
* **Rate limit** — fixed-window per-user counter to stop abuse / runaway cost.
* **Credentials** — validate (embed_id, public_key) for multi-tenant embeds.
"""

from __future__ import annotations

import hashlib
from typing import Optional

import redis.asyncio as redis

from app.config import get_settings

_client: Optional[redis.Redis] = None

_DATA_VERSION_KEY = "pharmacy:data_version"
_CRED_KEY = "pharmacy:credentials"  # hash: embed_id -> public_key


def get_client() -> redis.Redis:
    """Return the shared async redis client (lazily created)."""

    global _client
    if _client is None:
        _client = redis.from_url(
            get_settings().redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def close_client() -> None:
    """Close the redis client and reset the global."""

    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---- generic cache ---------------------------------------------------------


async def get_cached(key: str) -> Optional[str]:
    """Return a cached value by key, or ``None`` on a miss."""

    return await get_client().get(key)


async def set_cached(key: str, value: str, ttl: int) -> None:
    """Store a value under a key with a TTL (seconds)."""

    await get_client().set(key, value, ex=ttl)


# ---- data-version namespacing ---------------------------------------------


async def get_data_version() -> int:
    """Current data version (0 if never set)."""

    v = await get_client().get(_DATA_VERSION_KEY)
    return int(v) if v else 0


async def bump_data_version() -> int:
    """Advance the data version, invalidating all version-scoped query caches."""

    return int(await get_client().incr(_DATA_VERSION_KEY))


# ---- query-answer cache ----------------------------------------------------


async def make_query_key(message: str, store_id: Optional[str]) -> str:
    """Build a version-scoped cache key for one (message, store) pair.

    Normalises the message (strip + lowercase) so trivially-different phrasings
    of the same question still hit. Includes data_version so a reload misses.
    """

    version = await get_data_version()
    norm = " ".join(message.strip().lower().split())
    raw = f"{version}|{store_id or '*'}|{norm}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"pharmacy:qa:{digest}"


async def get_cached_answer(message: str, store_id: Optional[str]) -> Optional[str]:
    """Return a cached agent answer for this (message, store), or ``None``."""

    return await get_cached(await make_query_key(message, store_id))


async def set_cached_answer(
    message: str, store_id: Optional[str], answer: str, ttl: Optional[int] = None
) -> None:
    """Cache an agent answer for this (message, store)."""

    ttl = ttl if ttl is not None else get_settings().cache_ttl_seconds
    await set_cached(await make_query_key(message, store_id), answer, ttl)


# ---- rate limiting ---------------------------------------------------------


async def check_rate_limit(user: str, limit: Optional[int] = None, window: int = 60) -> bool:
    """Fixed-window per-user limiter. Returns ``True`` if allowed.

    Increments a per-user counter that expires after ``window`` seconds; once
    the count exceeds ``limit`` within the window, returns ``False``.
    """

    limit = limit if limit is not None else get_settings().rate_limit_per_min
    client = get_client()
    key = f"pharmacy:rl:{user}"
    count = await client.incr(key)
    if count == 1:
        await client.expire(key, window)
    return count <= limit


# ---- multi-tenant credentials ---------------------------------------------


async def register_credential(embed_id: str, public_key: str) -> None:
    """Register/active an embed credential (admin/seed use)."""

    await get_client().hset(_CRED_KEY, embed_id, public_key)


async def is_valid_credential(embed_id: str, public_key: str) -> bool:
    """Validate (embed_id, public_key).

    Open dev mode: if NO credentials are registered, allow everything. Once any
    credential is registered, enforce strict matching.
    """

    client = get_client()
    if await client.hlen(_CRED_KEY) == 0:
        return True  # dev: no credentials configured yet
    stored = await client.hget(_CRED_KEY, embed_id)
    return stored is not None and stored == public_key


async def list_credentials() -> dict:
    """Return all registered credentials as {embed_id: public_key}."""

    return await get_client().hgetall(_CRED_KEY)


async def remove_credential(embed_id: str) -> int:
    """Delete a credential. Returns number removed (0 or 1)."""

    return await get_client().hdel(_CRED_KEY, embed_id)


# ---- agent config overrides (editable from admin) -------------------------

_CONFIG_KEY = "pharmacy:config"


async def get_config_overrides() -> dict:
    """Admin-set overrides (e.g. system_prompt). Empty if none set."""

    return await get_client().hgetall(_CONFIG_KEY)


async def set_config_override(key: str, value: str) -> None:
    """Store an admin config override (applied on next agent build/restart)."""

    await get_client().hset(_CONFIG_KEY, key, value)


__all__ = [
    "get_client",
    "close_client",
    "get_cached",
    "set_cached",
    "get_data_version",
    "bump_data_version",
    "make_query_key",
    "get_cached_answer",
    "set_cached_answer",
    "check_rate_limit",
    "register_credential",
    "is_valid_credential",
]
