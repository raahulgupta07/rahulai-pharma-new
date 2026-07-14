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
import hmac
import json
import logging
import re as _re
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
    message: str, store_id: Optional[str], model: Optional[str] = None,
    version: Optional[int] = None,
) -> str:
    """Build a version-scoped cache key for one (message, store, model) triple.

    Normalises the message (strip + lowercase) so trivially-different phrasings
    of the same question still hit. Includes data_version so a reload misses, and
    the resolved chat model so answers from different models don't collide.

    Pass ``version`` to pin the key to a version read earlier. Writers MUST do
    this: see :func:`set_cached_answer`.
    """

    if version is None:
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
    version: Optional[int] = None,
) -> None:
    """Cache an agent answer for this (message, store, model).

    ``version`` is the data_version observed when the answer was STARTED. An
    agent run takes seconds; an ingest can land inside that window. Without the
    pin, the key was built from the version at *write* time, so an answer
    computed against old stock got filed under the new version and served —
    fresh-looking and wrong — for a full TTL. Bumping the version could not
    dislodge it, because the poisoned entry was written after the bump.

    So: if the data changed while we were thinking, throw the answer away rather
    than cache it. The caller still returns it to the user who waited for it;
    only the caching is skipped. Callers that pass ``None`` keep the old
    read-at-write-time behaviour and are, by construction, racy.
    """

    if version is not None and await get_data_version() != version:
        logger.info("data_version changed during run; not caching stale answer")
        return

    ttl = ttl if ttl is not None else get_settings().cache_ttl_seconds
    await set_cached(await make_query_key(message, store_id, model, version), answer, ttl)
    await _semantic_store(message, store_id, answer, model, ttl, version)


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


async def _semantic_index_key(
    store_id: str, model: Optional[str], version: Optional[int] = None
) -> str:
    """Scope key for the semantic index — store_id is part of it (never cross)."""

    if version is None:
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
    version: Optional[int] = None,
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
        key = await _semantic_index_key(store_id, model, version)
        norm = " ".join(message.strip().lower().split())
        entry = json.dumps({"q": norm, "vec": qvec, "a": answer, "ts": time.time()})
        pipe = get_client().pipeline()
        pipe.rpush(key, entry)
        pipe.ltrim(key, -_SEM_MAX_ENTRIES, -1)
        pipe.expire(key, ttl)
        await pipe.execute()
    except Exception:   # noqa: BLE001 — indexing is best-effort, never fatal
        logger.exception("Semantic cache store failed; entry not indexed")


# ---- conversation turns ----------------------------------------------------


async def bump_session_turn(session_id: str) -> int:
    """Return this session's 1-based turn number, incrementing it.

    Used to decide whether a question may use the shared answer cache. The cache
    key is ``(data_version, model, store_id, message)`` — it does NOT include the
    conversation. That is fine for a first turn, which is self-contained. But a
    follow-up like "which other shop has it?" means nothing without its history:
    cache it globally and the next conversation to ask those same three words is
    served an answer about a different drug.

    So turn 1 may use the cache; later turns bypass it entirely.
    """

    client = get_client()
    key = f"pharmacy:turn:{session_id}"
    n = int(await client.incr(key))
    if n == 1:
        await client.expire(key, get_settings().session_ttl_seconds)
    return n


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
    """Validate (embed_id, public_key). FAIL-CLOSED: unknown pairs are rejected.

    This used to allow *everything* whenever the credential hash was empty ("open
    dev mode"). That is fail-OPEN: a production deploy that never seeded a
    credential accepted every embed on the internet, and nothing in the app said
    so. An empty store now means "nobody is authorised", which is the only safe
    reading of "no credentials configured".

    :func:`ensure_dev_credential` is what keeps the documented web/web snippet
    working out of the box — an explicit, logged, flag-gated seed, rather than a
    silent hole in the check itself.
    """

    if not embed_id or not public_key:
        return False
    stored = await get_client().hget(_CRED_KEY, embed_id)
    if stored is None:
        return False
    return hmac.compare_digest(str(stored), str(public_key))


async def ensure_dev_credential() -> Optional[str]:
    """Seed the default embed credential, but only into an EMPTY store.

    Called once from the app lifespan. Returns the seeded embed_id, or ``None``
    when nothing was seeded (flag off, or a credential already exists — the
    presence of any real credential means an operator has taken over, and we
    must never add to their set behind their back).

    Guarded by ``embed_dev_credential`` so a prod deploy can turn it off and get
    a credential store that is empty and therefore closed.
    """

    settings = get_settings()
    if not settings.embed_dev_credential:
        return None
    embed_id = settings.embed_dev_credential_id
    public_key = settings.embed_dev_credential_key
    if not embed_id or not public_key:
        return None
    if await get_client().hlen(_CRED_KEY) != 0:
        return None

    await register_credential(embed_id, public_key)
    logger.warning(
        "SEEDED DEFAULT EMBED CREDENTIAL embed_id=%r — the embed API now accepts this "
        "public key from anyone who knows it. This exists so the documented widget "
        "snippet works on a fresh dev stack. BEFORE PRODUCTION: set "
        "EMBED_DEV_CREDENTIAL=false and register real credentials via "
        "POST /admin/credentials.",
        embed_id,
    )
    return embed_id


# The first-party admin chat page (same-origin, behind admin auth) drives the
# embed API with this fixed pair. It is the app's OWN internal client, not a
# customer embed.
INTERNAL_CHAT_EMBED_ID = "admin-chat"
INTERNAL_CHAT_PUBLIC_KEY = "admin"


async def ensure_internal_credential() -> None:
    """Guarantee the admin chat's internal credential exists.

    ``is_valid_credential`` is fail-closed, so the moment ANY credential is
    registered (an operator mints one, or a test seeds one) the admin chat's
    fixed ``(admin-chat, admin)`` pair is rejected with 403 unless it too is
    present. Unlike :func:`ensure_dev_credential` this is neither flag-gated nor
    limited to an empty store — it is first-party and re-seeded idempotently on
    every boot so the console chat never silently breaks.
    """

    await register_credential(INTERNAL_CHAT_EMBED_ID, INTERNAL_CHAT_PUBLIC_KEY)


async def list_credentials() -> dict:
    """Return all registered credentials as {embed_id: public_key}."""

    creds = await get_client().hgetall(_CRED_KEY)
    # The internal admin-chat credential is an implementation detail of the
    # console, not a customer tenant — hide it from the Tenants list.
    creds.pop(INTERNAL_CHAT_EMBED_ID, None)
    return creds


async def remove_credential(embed_id: str) -> int:
    """Delete a credential. Returns number removed (0 or 1)."""

    return await get_client().hdel(_CRED_KEY, embed_id)


# ---- ingest config (shared by the api AND the worker container) -----------
#
# The api and the ingest worker are SEPARATE processes, so this cannot live in
# either one's memory — it lives in Redis, mirroring the credential/data_version
# helpers above. The worker re-reads poll_seconds every loop iteration and the
# catalog_mode on every scan, so an operator change takes effect with no restart.

_INGEST_CONFIG_KEY = "pharmacy:ingest_config"
CATALOG_MODES = ("merge", "full_sync")
_POLL_MIN, _POLL_MAX = 5, 3600


def _clamp_poll(value) -> int:
    """Coerce ``value`` to an int and clamp to [5, 3600]. Raises on non-numeric."""

    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("poll_seconds must be an integer")
    return max(_POLL_MIN, min(_POLL_MAX, n))


async def get_poll_seconds() -> int:
    """Poll cadence from Redis, clamped, falling back to the settings default.

    The worker reads this each loop iteration, so anything unreadable (Redis
    down, unset, corrupt) must degrade to the configured default rather than
    stall the loop.
    """

    default = get_settings().watch_interval_seconds
    try:
        raw = await get_client().hget(_INGEST_CONFIG_KEY, "poll_seconds")
    except Exception:  # noqa: BLE001 — never let a Redis blip stop the watcher
        return default
    if raw is None:
        return default
    try:
        return _clamp_poll(raw)
    except ValueError:
        return default


async def get_catalog_mode() -> str:
    """Catalog ingest mode from Redis: 'full_sync' (default) or 'merge'.

    Default is 'full_sync' — an article upload is authoritative: it adds the new
    rows and drops the ones it omits, so the catalog always mirrors the latest
    file (the same clear-and-replace shape stock files already use). The
    empty/partial-file guard in ``ingest_catalog`` stops a bad upload from
    emptying the catalog.

    But a Redis *error* degrades to 'merge', the non-destructive mode: if we
    cannot even read the config we must not delete rows on a guess. So an unset
    key returns the full_sync default; only an unreachable Redis falls to merge.
    """

    try:
        raw = await get_client().hget(_INGEST_CONFIG_KEY, "catalog_mode")
    except Exception:  # noqa: BLE001
        return "merge"
    return raw if raw in CATALOG_MODES else "full_sync"


async def get_ingest_config() -> dict:
    """The effective ingest config as the admin page reads it."""

    return {
        "poll_seconds": await get_poll_seconds(),
        "catalog_mode": await get_catalog_mode(),
    }


async def set_ingest_config(
    poll_seconds: Optional[int] = None, catalog_mode: Optional[str] = None
) -> dict:
    """Validate + persist a partial ingest-config update. Returns the effective config.

    ``poll_seconds`` is clamped; a ``catalog_mode`` outside :data:`CATALOG_MODES`
    is rejected. Raises ``ValueError`` on bad input so the admin endpoint can turn
    it into a 400.
    """

    mapping: dict = {}
    if poll_seconds is not None:
        mapping["poll_seconds"] = _clamp_poll(poll_seconds)
    if catalog_mode is not None:
        if catalog_mode not in CATALOG_MODES:
            raise ValueError(f"catalog_mode must be one of {', '.join(CATALOG_MODES)}")
        mapping["catalog_mode"] = catalog_mode
    if mapping:
        await get_client().hset(_INGEST_CONFIG_KEY, mapping=mapping)
    return await get_ingest_config()


# ---- agent config overrides (editable from admin) -------------------------

_CONFIG_KEY = "pharmacy:config"


async def get_config_overrides() -> dict:
    """Admin-set overrides (e.g. system_prompt). Empty if none set."""

    return await get_client().hgetall(_CONFIG_KEY)


async def set_config_override(key: str, value: str) -> None:
    """Store an admin config override (applied on next agent build/restart)."""

    await get_client().hset(_CONFIG_KEY, key, value)


# ---- CORS allowed origins (runtime-managed, shared across workers) ---------
#
# CORS origins used to be ONLY the ALLOWED_ORIGINS env var, read once at startup
# and baked into the middleware — so adding a customer's site meant editing env
# and restarting. This is the runtime layer: a Redis set the admin UI writes to,
# UNIONed with the env origins by the CORS middleware. The middleware refreshes
# its in-process copy of this set on a short loop, so an added origin takes
# effect within seconds across every worker, no restart.

_CORS_KEY = "pharmacy:cors_origins"
_ORIGIN_RE = _re.compile(r"^https?://[^/\s]+$")  # scheme://host[:port], no path/slash


def normalize_origin(origin: str) -> str:
    """Validate + canonicalise a browser Origin. Raises ValueError on junk.

    An Origin is scheme://host[:port] with NO path and NO trailing slash — the
    browser sends exactly that, so anything else would never match and is a
    typo. ``*`` is refused here: opening CORS to everyone is an env-level
    decision (ALLOWED_ORIGINS=*), never a stray click in the UI.
    """

    o = (origin or "").strip().rstrip("/")
    if o == "*":
        raise ValueError("refusing '*' from the UI; set ALLOWED_ORIGINS=* in env if you truly mean it")
    if not _ORIGIN_RE.match(o):
        raise ValueError("expected an origin like http://localhost:8000 or https://shop.example.com (scheme + host, no path)")
    return o.lower()


# ---- answer style (crisp / standard / detailed) ----------------------------
#
# Operator-tunable answer length, applied to the agent's system prompt and the
# fast path's phrasing. Global (not per-store), Redis-backed like the ingest
# config. Changing it bumps data_version at the admin layer so cached answers in
# the old style are invalidated.

_ANSWER_STYLE_KEY = "pharmacy:answer_style"
ANSWER_STYLES = ("crisp", "standard", "detailed")


async def get_answer_style() -> str:
    """The configured answer length, defaulting to 'standard'.

    Falls back to 'standard' on anything unexpected (Redis down, unset, corrupt)
    — the safe middle that matches the baseline system prompt.
    """

    try:
        raw = await get_client().get(_ANSWER_STYLE_KEY)
    except Exception:  # noqa: BLE001
        return "standard"
    return raw if raw in ANSWER_STYLES else "standard"


async def set_answer_style(style: str) -> str:
    """Persist the answer style. Raises ValueError on an unknown value."""

    s = (style or "").strip().lower()
    if s not in ANSWER_STYLES:
        raise ValueError(f"answer style must be one of {', '.join(ANSWER_STYLES)}")
    await get_client().set(_ANSWER_STYLE_KEY, s)
    return s


async def get_cors_origins() -> set:
    """The runtime-added origins from Redis. Empty set on any Redis trouble."""

    try:
        return set(await get_client().smembers(_CORS_KEY))
    except Exception:  # noqa: BLE001 — a Redis blip must not widen or crash CORS
        return set()


async def add_cors_origin(origin: str) -> str:
    """Register an allowed origin. Returns the normalised value."""

    o = normalize_origin(origin)
    await get_client().sadd(_CORS_KEY, o)
    return o


async def remove_cors_origin(origin: str) -> int:
    """Remove an allowed origin. Returns count removed (0 or 1)."""

    return await get_client().srem(_CORS_KEY, (origin or "").strip().rstrip("/").lower())


__all__ = [
    "get_client",
    "close_client",
    "normalize_origin",
    "get_cors_origins",
    "add_cors_origin",
    "remove_cors_origin",
    "get_answer_style",
    "set_answer_style",
    "ANSWER_STYLES",
    "get_cached",
    "set_cached",
    "get_data_version",
    "bump_data_version",
    "make_query_key",
    "bump_session_turn",
    "get_cached_answer",
    "set_cached_answer",
    "check_rate_limit",
    "register_credential",
    "is_valid_credential",
    "ensure_dev_credential",
    "ensure_internal_credential",
    "get_poll_seconds",
    "get_catalog_mode",
    "get_ingest_config",
    "set_ingest_config",
    "CATALOG_MODES",
]
