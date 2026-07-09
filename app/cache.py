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
import json
import logging
import time
from typing import List, Optional

import redis.asyncio as redis

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None

_DATA_VERSION_KEY = "pharmacy:data_version"
_CRED_KEY = "pharmacy:credentials"  # hash: embed_id -> public_key

# Semantic answer cache: a bounded per-(version, model, store) list of
# {question vector, answer} entries. The exact-hash cache above is free and
# ~3ms; this near-match layer only runs on its miss, and only when enabled.
_SEM_INDEX_PREFIX = "pharmacy:sqa:"  # list key: {prefix}{version}|{model}|{store}
_SEM_MAX_ENTRIES = 64                # hard cap per scope, so the index can't grow unbounded


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


def _resolve_model(model: Optional[str]) -> str:
    """Resolve a requested model id to the one the agent will actually use.

    Mirrors ``agent.get_agent``: unknown/empty ids fall back to the configured
    default, so the key matches the model that actually produced the answer.
    """

    from app.agent import ALLOWED_MODEL_IDS

    return model if model in ALLOWED_MODEL_IDS else get_settings().openrouter_model


async def make_query_key(
    message: str, store_id: Optional[str], model: Optional[str] = None
) -> str:
    """Build a version-scoped cache key for one (message, store, model) triple.

    Normalises the message (strip + lowercase) so trivially-different phrasings
    of the same question still hit. Includes data_version so a reload misses, and
    the resolved chat model so answers from different models don't collide.
    """

    version = await get_data_version()
    norm = " ".join(message.strip().lower().split())
    resolved = _resolve_model(model)
    raw = f"{version}|{resolved}|{store_id or '*'}|{norm}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"pharmacy:qa:{digest}"


async def get_cached_answer(
    message: str, store_id: Optional[str], model: Optional[str] = None
) -> Optional[str]:
    """Return a cached agent answer for this (message, store, model), or ``None``.

    Tries the exact hash first (free, ~3ms). On a miss, and only when
    ``semantic_cache_enabled``, falls back to an embedding near-match within the
    SAME (version, model, store) scope.
    """

    exact = await get_cached(await make_query_key(message, store_id, model))
    if exact is not None:
        return exact
    return await _semantic_lookup(message, store_id, model)


async def set_cached_answer(
    message: str,
    store_id: Optional[str],
    answer: str,
    ttl: Optional[int] = None,
    model: Optional[str] = None,
) -> None:
    """Cache an agent answer for this (message, store, model)."""

    ttl = ttl if ttl is not None else get_settings().cache_ttl_seconds
    await set_cached(await make_query_key(message, store_id, model), answer, ttl)
    await _semantic_store(message, store_id, answer, model, ttl)


# ---- semantic (embedding) near-match layer --------------------------------


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two equal-length vectors (-1.0 on a zero vector)."""

    import numpy as np

    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return float(np.dot(va, vb) / (na * nb))


async def _semantic_index_key(store_id: str, model: Optional[str]) -> str:
    """Scope key for the semantic index — store_id is part of it (never cross)."""

    version = await get_data_version()
    resolved = _resolve_model(model)
    return f"{_SEM_INDEX_PREFIX}{version}|{resolved}|{store_id}"


async def _semantic_lookup(
    message: str, store_id: Optional[str], model: Optional[str]
) -> Optional[str]:
    """Return the answer of the closest cached question above threshold, or ``None``.

    Fails closed: disabled flag, absent store_id, a missing embedding, or any
    error returns ``None`` so the caller falls through to the LLM. store_id is
    part of the scope key, so a hit can never cross stores.
    """

    settings = get_settings()
    if not settings.semantic_cache_enabled or not store_id:
        return None
    try:
        from app.embeddings import embed_query_cached

        qvec = await embed_query_cached(message)
        if not qvec:
            return None
        key = await _semantic_index_key(store_id, model)
        entries = await get_client().lrange(key, 0, -1)
        if not entries:
            return None

        ttl = settings.cache_ttl_seconds
        now = time.time()
        best_sim = settings.semantic_cache_threshold
        best_answer: Optional[str] = None
        for raw in entries:
            try:
                entry = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if now - entry.get("ts", 0.0) > ttl:
                continue  # stale relative to the exact cache's freshness window
            vec = entry.get("vec")
            answer = entry.get("a")
            if not vec or answer is None:
                continue
            sim = _cosine(qvec, vec)
            if sim >= best_sim:
                best_sim = sim
                best_answer = answer
        return best_answer
    except Exception:   # noqa: BLE001 — the near-match layer must never break lookup
        logger.exception("Semantic cache lookup failed; falling through to LLM")
        return None


async def _semantic_store(
    message: str,
    store_id: Optional[str],
    answer: str,
    model: Optional[str],
    ttl: int,
) -> None:
    """Append this (question vector, answer) to the scope index, bounded + TTL'd.

    No-op when disabled or store_id is absent. Trims to the newest
    ``_SEM_MAX_ENTRIES`` and expires the whole index at ``ttl`` so stock answers
    stay fresh. Errors are swallowed — indexing is best-effort.
    """

    settings = get_settings()
    if not settings.semantic_cache_enabled or not store_id:
        return
    try:
        from app.embeddings import embed_query_cached

        qvec = await embed_query_cached(message)
        if not qvec:
            return
        key = await _semantic_index_key(store_id, model)
        norm = " ".join(message.strip().lower().split())
        entry = json.dumps({"q": norm, "vec": qvec, "a": answer, "ts": time.time()})
        pipe = get_client().pipeline()
        pipe.rpush(key, entry)
        pipe.ltrim(key, -_SEM_MAX_ENTRIES, -1)
        pipe.expire(key, ttl)
        await pipe.execute()
    except Exception:   # noqa: BLE001 — indexing is best-effort, never fatal
        logger.exception("Semantic cache store failed; entry not indexed")


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
