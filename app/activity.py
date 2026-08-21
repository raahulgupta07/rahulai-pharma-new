"""App-wide activity log + per-turn and per-call capture.

Three capture layers that answer the questions this system could not answer
about itself:

1. **What did a turn cost?** :func:`extract_metrics` pulls
   ``agno.metrics.RunMetrics`` off a run (or off the streaming
   ``RunCompletedEvent``) and turns it into the six ``chat_logs`` columns added
   by ``migrations/0006_turn_metrics.sql``.

2. **Who changed the running system?** :func:`ensure_app_events` /
   :func:`record_event` are the ``app_events`` audit trail described in
   ``migrations/0007_app_events.sql``, written by the HTTP middleware in
   ``app.api`` for every mutating ``/admin/*`` request.

3. **What did each CALL do?** A turn is not one tool call and not one model
   call. :func:`begin_turn` / :func:`record_tool_call` / :func:`open_llm_call`
   buffer every one of them in memory during the turn, and :func:`flush_turn`
   writes them to the ``tool_calls`` / ``llm_calls`` tables from
   ``migrations/0008_turn_calls.sql`` once the turn row exists to point at.
   ``tool_calls.outcome`` has THREE states, and that is the whole point: a tool
   that deliberately declined and a tool that crashed must never share a bucket.

Three rules govern everything here, and all three are load-bearing.

**Recording never breaks the request.** Every write is wrapped and degrades to a
warning, exactly as ``auth.record_auth_event``, ``ingest_events.record`` and
``admin.log_chat`` already do. A change that happens but is not logged is a gap
in an audit trail; a change that FAILS because the audit table was unreachable
is an outage caused by the logging. That ordering is not negotiable — and it is
why this module is the one place allowed to swallow exceptions broadly.

**NULL is not zero.** A cache hit runs no model, so its metric columns stay
NULL; a provider that reports no price stores NULL, never ``0.0``. See
``migrations/0006_turn_metrics.sql`` for why a 0 here would be a lie the
analytics page then presents as a fact.

**The request body is never stored.** ``detail`` is built from a per-route
allowlist of non-sensitive summary fields (:data:`_ROUTES`); an unlisted route
records route + status and nothing else. The bodies passing through these routes
carry ``oidc_client_secret``, ``ldap_bind_password``, cleartext user passwords
and SSH/embed public keys — see the warning in the migration.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.db import execute, get_pool, q

logger = logging.getLogger("pharmacy.activity")


# ============================================================================
# Part 1 — per-turn token + cost metrics
# ============================================================================

# The six chat_logs columns from migration 0006, in the order log_chat takes
# them. Exported so app.api can decide whether admin.log_chat is new enough to
# accept them without guessing at a TypeError.
METRIC_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cost_usd",
    "ttft_ms",
)

NO_METRICS: Dict[str, Optional[float]] = {k: None for k in METRIC_FIELDS}

# Log the "agno moved the attribute" warning once per process, not once per
# turn. A renamed field is a deploy-time fact: the first line says it, and the
# next ten thousand would only bury the request logs.
_warned_shape = False


def _int_or_none(value: Any) -> Optional[int]:
    """An int, or None when the provider reported nothing.

    ``RunMetrics`` initialises every token counter to ``0``, so a run whose
    provider returned no usage block is indistinguishable from one that used
    zero tokens — except that no model has ever answered a question on zero
    tokens. ``0`` therefore means *unreported* and is stored as NULL, which is
    the honest reading and keeps a month of unreported turns from dragging an
    average down to zero.
    """

    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def extract_metrics(run: Any) -> Dict[str, Optional[float]]:
    """The six metric columns for one model run. Never raises.

    ``run`` is anything that may carry ``.metrics``: an agno ``RunOutput``, a
    streaming ``RunCompletedEvent``, or ``None``. Anything that carries no
    usable metrics yields all-NULL, which is the correct record for a turn whose
    cost is genuinely unknown.

    ⚠️ This runs on the answer path. A missing or renamed agno attribute must
    cost the user a metric, never an answer — so the whole extraction is wrapped
    and a failure returns all-NULL after one warning.
    """

    global _warned_shape

    out = dict(NO_METRICS)
    metrics = getattr(run, "metrics", None)
    if metrics is None:
        return out
    try:
        total = _int_or_none(getattr(metrics, "total_tokens", None))
        out["input_tokens"] = _int_or_none(getattr(metrics, "input_tokens", None))
        out["output_tokens"] = _int_or_none(getattr(metrics, "output_tokens", None))
        out["total_tokens"] = total

        # `reasoning_tokens` is the one counter whose 0 can be REAL: a
        # non-reasoning model genuinely spends none. So it is trusted as a zero
        # only when the run reported real usage at all; otherwise the whole
        # usage block was absent and 0 means nothing.
        reasoning = getattr(metrics, "reasoning_tokens", None)
        if total is not None and reasoning is not None:
            try:
                out["reasoning_tokens"] = max(0, int(reasoning))
            except (TypeError, ValueError):
                out["reasoning_tokens"] = None
        else:
            out["reasoning_tokens"] = _int_or_none(reasoning)

        # Cost. NEVER invented, and never stored as 0 — see migration 0006.
        # OpenRouter returns no price on the completion response, so agno leaves
        # this None on this stack; a 0.0 would be a claim that a turn was free.
        cost = getattr(metrics, "cost", None)
        if cost is not None:
            try:
                c = float(cost)
                out["cost_usd"] = round(c, 6) if c > 0 else None
            except (TypeError, ValueError):
                out["cost_usd"] = None

        # agno measures time_to_first_token in SECONDS (it comes off a Timer).
        ttft = getattr(metrics, "time_to_first_token", None)
        if ttft is not None:
            try:
                ms = int(round(float(ttft) * 1000))
                out["ttft_ms"] = ms if ms >= 0 else None
            except (TypeError, ValueError):
                out["ttft_ms"] = None
    except Exception:  # noqa: BLE001 — a metric is never worth an answer
        if not _warned_shape:
            _warned_shape = True
            logger.warning(
                "could not read RunMetrics off %s — token/cost capture is off "
                "until this is fixed; answers are unaffected",
                type(run).__name__,
                exc_info=True,
            )
        return dict(NO_METRICS)
    return out


async def ensure_turn_metrics() -> None:
    """Add the 0006 metric columns to chat_logs. Idempotent; called from lifespan.

    Kept in step with ``migrations/0006_turn_metrics.sql``. It lives here rather
    than in ``admin.ensure_chat_logs`` so that this half of the change deploys
    independently of the ``log_chat`` signature change: the columns can exist
    before anything writes them, and writing them is a no-op if they do not.

    Deliberately NOT wrapped — the lifespan block that calls it already logs and
    continues, exactly like every other ``ensure_*`` there.
    """

    for col, typ in (
        ("input_tokens", "INT"),
        ("output_tokens", "INT"),
        ("total_tokens", "INT"),
        ("reasoning_tokens", "INT"),
        ("cost_usd", "NUMERIC(12,6)"),
        ("ttft_ms", "INT"),
    ):
        await execute(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")


# ============================================================================
# Part 2 — the app-wide activity log
# ============================================================================


async def ensure_app_events() -> None:
    """Create app_events + its three indexes. Idempotent; called from lifespan.

    Kept in step with ``migrations/0007_app_events.sql`` — a fresh boot and a
    hand-migrated database must converge on the same schema.
    """

    await execute(
        """
        CREATE TABLE IF NOT EXISTS app_events (
            id          BIGSERIAL PRIMARY KEY,
            ts          TIMESTAMPTZ DEFAULT now(),
            actor_email TEXT,
            actor_role  TEXT,
            action      TEXT NOT NULL,
            target      TEXT,
            method      TEXT,
            path        TEXT,
            status      INT,
            detail      JSONB,
            ip          TEXT,
            duration_ms INT
        )
        """
    )
    await execute("CREATE INDEX IF NOT EXISTS idx_app_events_ts ON app_events (ts DESC)")
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_app_events_actor_ts ON app_events (actor_email, ts DESC)"
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_app_events_action_ts ON app_events (action, ts DESC)"
    )


async def record_event(
    action: str,
    *,
    actor_email: Optional[str] = None,
    actor_role: Optional[str] = None,
    target: Optional[str] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    status: Optional[int] = None,
    detail: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Append one activity event. **Never raises** — see the module docstring.

    ``detail`` is redacted by key name on the way in even though the caller is
    expected to have allowlisted it already. That is not belt-and-braces theatre:
    the allowlist names FIELDS, and a Pydantic model those field names point at
    can grow a secret in a later release without anyone revisiting this file.
    """

    try:
        payload = _redact(detail) if detail else None
        await execute(
            """INSERT INTO app_events
                   (actor_email, actor_role, action, target, method, path,
                    status, detail, ip, duration_ms)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)""",
            (actor_email or "").strip().lower() or None,
            actor_role or None,
            action,
            (str(target)[:200] if target is not None else None),
            method or None,
            (path or "")[:500] or None,
            int(status) if status is not None else None,
            json.dumps(payload) if payload else None,
            ip or None,
            int(duration_ms) if duration_ms is not None else None,
        )
    except Exception:  # noqa: BLE001 — the request outranks its own audit trail
        logger.warning("could not record activity event %s", action, exc_info=True)


# ---- redaction --------------------------------------------------------------

# Second line of defence, by KEY NAME. `key` is in here on purpose even though
# the two keys that reach these routes are PUBLIC ones: an allowlist that says
# "any field whose name contains key" cannot be wrong in the dangerous
# direction, and nothing downstream needs the bytes of a public key to know that
# one was registered.
_SENSITIVE = re.compile(r"secret|password|token|key|credential|passphrase", re.I)

REDACTED = "[redacted]"

# A stored string is a summary, never a document. `system_prompt` and a
# validation error list are both allowlist-able and both unbounded.
_MAX_STR = 200
_MAX_ITEMS = 40


def _redact(value: Any, _depth: int = 0) -> Any:
    """Recursively drop the value of any key whose NAME looks sensitive.

    Depth- and size-bounded: an audit row must not be able to grow without limit
    from a nested body, or the table becomes the thing that fills the disk.
    """

    if _depth > 4:
        return REDACTED
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in list(value.items())[:_MAX_ITEMS]:
            if _SENSITIVE.search(str(k)):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = _redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, _depth + 1) for v in list(value)[:_MAX_ITEMS]]
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STR]


# ---- the per-route allowlist ------------------------------------------------
#
# `(method, compiled path pattern, spec)`. The pattern may capture a `target`
# group — the identifying path parameter. Target is captured EXPLICITLY per
# route rather than inferred from the last path segment, because that inference
# records "rebuild" as the target of `POST /admin/graph/rebuild`.
#
# `spec` decides what, if anything, of the BODY is summarised:
#
#   ("keys", (...))   store the values of exactly these top-level fields
#   ("names", None)   store only WHICH fields were present, never any value.
#                     This is the shape for anything that carries a secret:
#                     "the OIDC client secret was changed" is the audit fact,
#                     and the new secret is not.
#   None              store nothing from the body
#
# **A route that is not in this table stores route + status only.** That is the
# default and it is the safe one: a route added next year is silent until
# somebody deliberately decides what of it is safe to keep.

_ROUTES: List[Tuple[str, Any, Optional[Tuple[str, Any]]]] = [
    # --- the security-critical writes: field NAMES only, never values --------
    ("PUT", re.compile(r"^/admin/auth-config$"), ("names", None)),
    ("PUT", re.compile(r"^/admin/security-config$"), ("names", None)),
    ("PUT", re.compile(r"^/admin/config$"), ("names", None)),
    ("PUT", re.compile(r"^/admin/branding$"), ("names", None)),
    ("POST", re.compile(r"^/admin/ingest/config$"), ("names", None)),
    # --- accounts ------------------------------------------------------------
    # `password` is absent from both key lists AND caught by _SENSITIVE.
    ("POST", re.compile(r"^/admin/users$"), ("keys", ("email", "role"))),
    (
        "PATCH",
        re.compile(r"^/admin/users/(?P<target>[^/]+)$"),
        ("keys", ("role", "active", "approved", "store_id")),
    ),
    ("DELETE", re.compile(r"^/admin/users/(?P<target>[^/]+)$"), None),
    # --- embed credentials: the id, never the key ----------------------------
    ("POST", re.compile(r"^/admin/credentials$"), ("keys", ("embed_id",))),
    ("DELETE", re.compile(r"^/admin/credentials/(?P<target>[^/]+)$"), None),
    # --- SFTP partner keys: the label, never the key -------------------------
    ("POST", re.compile(r"^/admin/sftp/keys$"), ("keys", ("label",))),
    ("DELETE", re.compile(r"^/admin/sftp/keys/(?P<target>[^/]+)$"), None),
    # --- plainly non-sensitive operational settings --------------------------
    ("POST", re.compile(r"^/admin/cors-origins$"), ("keys", ("origin",))),
    ("DELETE", re.compile(r"^/admin/cors-origins$"), None),
    ("POST", re.compile(r"^/admin/answer-style$"), ("keys", ("style",))),
    ("POST", re.compile(r"^/admin/aliases$"), ("keys", ("alias", "article_code"))),
    ("DELETE", re.compile(r"^/admin/aliases/(?P<target>[^/]+)$"), None),
    # --- branch visibility: who hid a branch, and what they said -------------
    # `note` is an operator's own words ("branch closed"), written for the next
    # admin to read. It is kept deliberately: without it the trail records that
    # somebody hid a branch and never why, which is the question anyone reading
    # this row six months later is actually asking. Nothing sensitive passes
    # through this body — the only other field is the status itself.
    (
        "POST",
        re.compile(r"^/admin/stores/(?P<target>[^/]+)/status$"),
        ("keys", ("status", "note")),
    ),
    # --- data movement: identified by the path, body is a file or a trigger --
    ("POST", re.compile(r"^/admin/sftp/file/(?P<target>[^/]+)/retry$"), None),
    ("DELETE", re.compile(r"^/admin/sftp/file/(?P<target>[^/]+)$"), None),
    ("POST", re.compile(r"^/admin/branding/asset/(?P<target>[^/]+)$"), None),
    ("DELETE", re.compile(r"^/admin/branding/asset/(?P<target>[^/]+)$"), None),
]


def match_route(method: str, path: str) -> Tuple[Optional[str], Optional[Any]]:
    """``(target, body-spec)`` for a route, or ``(None, None)`` when unlisted."""

    for m, pattern, spec in _ROUTES:
        if m != method:
            continue
        hit = pattern.match(path)
        if hit:
            try:
                target = hit.groupdict().get("target")
            except Exception:  # noqa: BLE001
                target = None
            # Edge whitespace is stripped because the ROUTES capture it from the
            # RAW url while the handlers normalise before they act. A request to
            # `/admin/stores/20043-CCSJ%20/status` really does change branch
            # `20043-CCSJ`, and filing the audit row under `'20043-CCSJ '`
            # records that act against a branch code that does not exist: the
            # Activity feed shows a branch nobody can look up, and the row no
            # longer collapses with the handler's own (correctly-named) row, so
            # one act reads as two. A URL segment with edge whitespace is the
            # same entity as one without — the audit trail is the last place
            # that should disagree about which thing was acted on.
            if isinstance(target, str):
                target = target.strip() or None
            return target, spec
    return None, None


def summarize_body(method: str, path: str, body: Any) -> Optional[Dict[str, Any]]:
    """The allowlisted summary of a request body, or None.

    **This is the function that keeps secrets out of the audit trail.** It never
    returns a field it was not explicitly told to return, and for the routes that
    carry a secret it returns only which fields were present. Anything it cannot
    parse, and anything from an unlisted route, summarises to None — the safe
    default, not an error.
    """

    if not isinstance(body, dict):
        return None
    _target, spec = match_route(method, path)
    if not spec:
        return None
    mode, keys = spec
    if mode == "names":
        # Field NAMES only. A name is not a secret — "somebody set
        # oidc_client_secret" is exactly the fact this table exists to keep —
        # and no value is read at all, so there is nothing to leak.
        names = sorted(str(k)[:60] for k in list(body.keys())[:_MAX_ITEMS])
        return {"fields": names} if names else None
    out: Dict[str, Any] = {}
    for k in keys or ():
        if k in body and body[k] is not None:
            out[k] = body[k]
    return _redact(out) if out else None


# ---- the action slug --------------------------------------------------------

_VERB = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}
_ID_LIKE = re.compile(r"^\d+$")


def action_for(method: str, path: str, target: Optional[str] = None) -> str:
    """A stable, low-cardinality slug for a route: ``admin.users.update``.

    The identifier is removed from the slug so that 400 user edits are one
    filterable action rather than 400 unique ones; the specific id lives in
    ``target``. Without this the ``(action, ts DESC)`` index is worthless — every
    row has a unique action and "show me every credential deletion" cannot be
    expressed.

    ⚠️ **Numeric-only collapsing is not enough.** Half the ids in this API are
    not numbers: an ``embed_id``, an SFTP key label, an SFTP filename. Caught by
    ``test_action_slugs_are_low_cardinality``, which produced
    ``admin.credentials.no-such-embed-xyz.delete``. So the captured ``target``
    (from the route's own pattern — see :data:`_ROUTES`) is dropped as well as
    any all-numeric segment.

    A route that is not in ``_ROUTES`` captures no target, so an id in ITS path
    still widens the slug. That is the visible symptom that the route needs an
    entry, and it is preferable to guessing which segment is an id.
    """

    # ⚠️ Compare STRIPPED, because `match_route` strips the target it captures
    # while this walks the RAW path. `/admin/stores/20043-CCSJ%20/status` decodes
    # to a segment `'20043-CCSJ '` and a target `'20043-CCSJ'`; matching them
    # literally fails, the branch code survives into the slug, and the very
    # cardinality explosion this function exists to prevent comes back — one
    # unique action per branch, plus a summary that falls back to the raw route
    # name and shows a console user our internals. Found on screen by an agent
    # reading the audit trail, after I introduced it by adding the strip to
    # `match_route` without changing the comparison here.
    drop = {target.strip()} if target else set()
    segs = [
        s for s in path.strip("/").split("/")
        if s and not _ID_LIKE.match(s) and s.strip() not in drop
    ]
    if not segs:
        segs = ["root"]
    return ".".join(segs[:6]) + "." + _VERB.get(method.upper(), method.lower())


# ---- what is worth recording at all -----------------------------------------

MUTATING = ("POST", "PUT", "PATCH", "DELETE")

# Belt and braces with the "/admin/ only" rule below. None of these is reachable
# by a mutating method today; naming them means a future POST /metrics does not
# quietly start filling the audit trail with health-check noise.
_SKIP_PATHS = frozenset({"/metrics", "/metrics/history", "/health", "/ready", "/version", "/brand"})


def should_record(method: str, path: str) -> bool:
    """True for a mutating admin request. This is an audit trail of CHANGES.

    Deliberately excluded:

    * **GET and HEAD** — reading is not a change, and a request log is a
      different (and much larger) artefact than an audit trail.
    * **Everything outside ``/admin/``** — the embed chat surface is already
      recorded per turn in ``chat_logs``, and recording it twice would bury the
      handful of rows a reviewer actually needs under the traffic.
    * **``/auth/*``** — logins live in ``auth_events``, which is also the lockout
      counter. Duplicating them here would double every sign-in in the merged
      feed; :func:`unified_feed` reads both tables instead.
    * **Static assets and the SPA shell** — anything whose last path segment has
      a file extension.
    """

    if method.upper() not in MUTATING:
        return False
    if path in _SKIP_PATHS:
        return False
    if path != "/admin" and not path.startswith("/admin/"):
        return False
    if "." in path.rsplit("/", 1)[-1]:
        return False
    return True


# ---- reading it back --------------------------------------------------------


async def unified_feed(
    limit: int = 100,
    *,
    actor: Optional[str] = None,
    action: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """The merged activity feed: system changes AND sign-ins, newest first.

    Two tables, one feed, and NO copying between them. `auth_events` stays the
    sole home of `login_ok` / `login_fail` because the lockout counter counts
    those rows (``auth.failed_logins_for_email``); moving or mirroring them
    would either double them here or silently change who gets locked out. So the
    merge happens on READ, projecting both into one shape:

        {source, ts, actor_email, actor_role, action, target,
         method, path, status, detail, ip, duration_ms}

    An auth row projects `event` -> `action` and `email` -> `target` (the
    account the attempt was AGAINST, which for a failed login is the one thing
    worth seeing). Everything an auth row does not have is None, which is why
    the columns are nullable rather than defaulted.

    Returns ``[]`` rather than raising if either table is unreadable — a feed
    page that renders empty is a better failure than an admin console that 500s.
    """

    limit = max(1, min(int(limit or 100), 1000))
    where_app, where_auth, args = ["TRUE"], ["TRUE"], []
    if actor:
        args.append(actor.strip().lower())
        where_app.append(f"actor_email = ${len(args)}")
        where_auth.append(f"COALESCE(actor_email, email) = ${len(args)}")
    if action:
        args.append(action)
        where_app.append(f"action = ${len(args)}")
        where_auth.append(f"event = ${len(args)}")
    args.append(limit)
    lim = f"${len(args)}"
    sql = f"""
        SELECT * FROM (
            SELECT 'app'::text AS source, ts, actor_email, actor_role, action,
                   target, method, path, status, detail::text AS detail, ip, duration_ms
              FROM app_events WHERE {' AND '.join(where_app)}
            UNION ALL
            SELECT 'auth'::text AS source, ts, COALESCE(actor_email, email) AS actor_email,
                   NULL::text AS actor_role, event AS action, email AS target,
                   NULL::text AS method, NULL::text AS path, NULL::int AS status,
                   detail AS detail, ip, NULL::int AS duration_ms
              FROM auth_events WHERE {' AND '.join(where_auth)}
        ) feed
        ORDER BY ts DESC
        LIMIT {lim}
    """
    try:
        rows = await q(sql, *args)
    except Exception:  # noqa: BLE001 — an unreadable feed is not a 500
        logger.warning("activity feed unavailable", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        raw = row.get("detail")
        if row["source"] == "app" and isinstance(raw, str):
            try:
                row["detail"] = json.loads(raw)
            except Exception:  # noqa: BLE001
                pass
        out.append(row)
    return out


# ============================================================================
# Part 3 — per-call capture: tool_calls + llm_calls (migration 0008)
# ============================================================================
#
# `chat_logs` says a turn happened and lists the NAMES of the tools it called.
# This half says what each of those calls actually did, and what each of the
# turn's several model calls cost. Two rules carry the whole design:
#
# **Three outcomes, decided at the return site.** See the CHECK constraint in
# `migrations/0008_turn_calls.sql`. A refusal and a crash must never share a
# bucket, and the classification must never be recovered later by matching on an
# error string — by then the reason is gone and only the wording is left.
#
# **Nothing here may cost an answer.** Capture is in-memory during the turn (no
# database round trip on the hot path at all) and the flush is one wrapped write
# afterwards. Every entry point swallows, exactly as `record_event` does.


# ---- outcomes ---------------------------------------------------------------

SUCCEEDED = "succeeded"
REFUSED = "refused"
FAILED = "failed"
OUTCOMES = frozenset({SUCCEEDED, REFUSED, FAILED})


@dataclass
class ToolCall:
    """One tool invocation. ``outcome`` is decided by the caller, at the site."""

    seq: int
    name: str
    arguments: Optional[Dict[str, Any]] = None
    outcome: str = SUCCEEDED
    error_message: Optional[str] = None
    attempt: int = 1
    duration_ms: Optional[int] = None


@dataclass
class LlmCall:
    """One provider request.

    Opened when the call starts and completed in place when it ends, because the
    numbers arrive from two directions: the raw provider usage block (tokens,
    the cache split, cost) and agno's own timer on the assistant message
    (duration, time to first token).
    """

    seq: int
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    ttft_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    cost_is_estimated: bool = False
    finish_reason: Optional[str] = None

    # Not columns. `_message` is the agno assistant Message this call wrote to;
    # its `.metrics` is still being filled when the call is opened, so it is read
    # at flush time rather than copied now. `_usage` is the RAW provider usage
    # object, kept because agno's MessageMetrics coerces every absent counter to
    # 0 and the raw object is the only place the difference between "the provider
    # said zero" and "the provider said nothing" survives.
    _message: Any = None
    _usage: Any = None
    _started: Optional[float] = None


class TurnCapture:
    """Everything one turn did, buffered until the turn has a `chat_logs.id`.

    The turn row is written last (`admin.log_chat`), so nothing captured during
    the turn has a foreign key to point at yet. Holding it in memory also keeps
    every insert off the answer path, which is the actual requirement — a
    pharmacist waiting on a stock level must never be waiting on analytics.
    """

    __slots__ = ("tools", "llms", "_seq", "gave_up", "actor_email", "actor_role")

    def __init__(self) -> None:
        self.tools: List[ToolCall] = []
        self.llms: List[LlmCall] = []
        # ONE counter across both tables. `seq` is call order within the turn, so
        # the trace endpoint can merge tool_calls and llm_calls and ORDER BY seq
        # to replay what actually happened. Two independent 0-based sequences
        # could not be interleaved by any query.
        self._seq = 0
        self.gave_up: Optional[bool] = None
        self.actor_email: Optional[str] = None
        self.actor_role: Optional[str] = None

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def __bool__(self) -> bool:
        return bool(self.tools or self.llms)


# The turn currently being answered. Set once at the top of a turn; tools and
# model calls run inside that context and find it here. `None` means "not inside
# an instrumented turn", which is the correct state for a cache hit (no tool ran
# and no model ran) and for anything running outside a request.
_TURN: "contextvars.ContextVar[Optional[TurnCapture]]" = contextvars.ContextVar(
    "turn_capture", default=None
)


def begin_turn() -> None:
    """Start capturing a turn in this context. Call once, at the top of a turn.

    **Resets; never appends.** A second call REPLACES the buffer rather than
    adding to it, so a turn can only ever be measured with its own calls. Both
    the streaming and the non-streaming path run through the instrumented code,
    and the failure this rules out is a leftover buffer from a turn whose flush
    never ran being credited to the next pharmacist's question.
    """

    _TURN.set(TurnCapture())


def current_turn() -> Optional[TurnCapture]:
    """The capture for the turn being answered in this context, or None."""

    return _TURN.get()


def end_turn() -> Optional[TurnCapture]:
    """Detach and return the current capture, leaving this context clean."""

    capture = _TURN.get()
    _TURN.set(None)
    return capture


# ---- recording --------------------------------------------------------------


def record_tool_call(
    name: str,
    *,
    outcome: str,
    arguments: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    attempt: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Buffer one tool call. **Never raises**, and never touches the database.

    ``outcome`` must be one of :data:`OUTCOMES` and is the caller's decision —
    this function deliberately does no inference. An unknown value is stored as
    ``failed`` and logged, because a capture layer inventing a fourth state
    silently is worse than a loud wrong one.

    ``attempt`` defaults to being counted here: the same tool called again with
    the same arguments inside one turn is attempt 2. Nothing in this codebase
    retries a tool, so a second identical call is the MODEL retrying — which is
    the interesting signal, and it is invisible if every row says ``1``.
    """

    try:
        capture = _TURN.get()
        if capture is None:
            return
        if outcome not in OUTCOMES:
            logger.warning("unknown tool outcome %r for %s; recording failed", outcome, name)
            outcome = FAILED
        safe_args = _redact(arguments) if arguments else None
        if attempt is None:
            attempt = 1 + sum(
                1 for t in capture.tools if t.name == name and t.arguments == safe_args
            )
        capture.tools.append(
            ToolCall(
                seq=capture.next_seq(),
                name=str(name)[:100],
                arguments=safe_args,
                outcome=outcome,
                error_message=(str(error_message)[:500] if error_message else None),
                attempt=max(1, int(attempt)),
                duration_ms=duration_ms,
            )
        )
    except Exception:  # noqa: BLE001 — an answer outranks its own instrumentation
        logger.warning("could not record tool call %s", name, exc_info=True)


def open_llm_call(model: Optional[str], message: Any = None) -> Optional[LlmCall]:
    """Buffer a model call that is STARTING; returns it so it can be completed.

    Returns ``None`` outside a turn, so callers must tolerate that — which they
    do, because they only ever mutate the returned record.
    """

    try:
        capture = _TURN.get()
        if capture is None:
            return None
        import time as _time

        call = LlmCall(seq=capture.next_seq(), model=model, _message=message,
                       _started=_time.monotonic())
        capture.llms.append(call)
        return call
    except Exception:  # noqa: BLE001
        logger.warning("could not open llm call record", exc_info=True)
        return None


def record_llm_call(model: Optional[str] = None, **fields: Any) -> None:
    """Buffer one COMPLETED model call. **Never raises**, no database round trip.

    The one-shot form, for a caller that already has every number.
    :func:`open_llm_call` is the two-phase form the model instrumentation uses,
    because the numbers arrive from two directions — the raw provider usage and
    agno's own timer, which is still running when the call is opened.

    Unknown field names are dropped rather than raising: this is a capture layer,
    and a typo in an analytics call must not become an exception on a turn.
    """

    try:
        capture = _TURN.get()
        if capture is None:
            return
        call = LlmCall(seq=capture.next_seq(), model=model)
        for key, value in fields.items():
            if hasattr(call, key) and not key.startswith("_"):
                setattr(call, key, value)
            else:
                logger.warning("record_llm_call: ignoring unknown field %r", key)
        capture.llms.append(call)
    except Exception:  # noqa: BLE001 — a metric is never worth an answer
        logger.warning("could not record llm call", exc_info=True)


def set_turn_actor(email: Optional[str], role: Optional[str] = None) -> None:
    """Attribute this turn to a signed-in operator. **Never raises.**

    ⚠️ **Console turns only, and anonymity is the correct answer everywhere else.**
    The admin chat tester is a first-party embed client (`embed_id='admin-chat'`)
    driven by a named operator, so "who was driving when this answer came out
    wrong" is a fact worth keeping. The public widget has no operator: the people
    typing into it are members of the public asking a pharmacy a question, and a
    NULL actor is their complete and correct record. Nothing here invents an
    identity for them, and no visitor cookie is set anywhere — that is a
    disclosure decision for the product owner, not an engineering convenience.
    """

    try:
        capture = _TURN.get()
        if capture is None or not email:
            return
        capture.actor_email, capture.actor_role = email, role
    except Exception:  # noqa: BLE001
        logger.warning("could not attribute a turn to an actor", exc_info=True)


# ---- reading the provider's numbers -----------------------------------------


def _reported_int(value: Any) -> Optional[int]:
    """An int, keeping a REPORTED zero.

    The opposite reading from :func:`_int_or_none`, and both are correct in their
    place. A turn-level ``total_tokens`` of 0 means the usage block was missing,
    because no model answers on zero tokens. A per-call ``cache_read_tokens`` of
    0 straight off the provider's own usage object means something else entirely:
    the cache was cold. Only fields read from the RAW usage go through here, and
    only when the enclosing detail object was actually present.
    """

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds: Any) -> Optional[int]:
    """Seconds (agno's Timer unit) to whole milliseconds, or None."""

    if seconds is None:
        return None
    try:
        ms = int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return None
    return ms if ms >= 0 else None


def _estimated_cost(model: Optional[str], prompt: Optional[int], completion: Optional[int]):
    """``(cost, True)`` from the published per-model price, or ``(None, False)``.

    OpenRouter does not return a price on the completion response for this stack,
    so the provider-reported cost is almost always absent. The three selectable
    models DO have published prices in ``app.agent.SELECTABLE_MODELS``, and a
    priced estimate flagged as an estimate is worth more than a NULL — but only
    because it is flagged. An estimate stored as if it were measured is how a
    cost dashboard becomes confidently wrong, so the boolean is not optional.
    """

    if not model or prompt is None or completion is None:
        return None, False
    try:
        from app.agent import SELECTABLE_MODELS

        prices = {m["id"]: m for m in SELECTABLE_MODELS}
        row = prices.get(model)
        if not row:
            return None, False
        cost = (prompt / 1_000_000.0) * float(row["price_in"]) + (
            completion / 1_000_000.0
        ) * float(row["price_out"])
        return round(cost, 6), True
    except Exception:  # noqa: BLE001 — a price table lookup is never worth an answer
        return None, False


def _finalize_llm(call: LlmCall) -> None:
    """Fill a model call's columns from the raw usage and agno's own timer.

    ⚠️ Reads the RAW provider usage for every token counter, and agno's
    ``MessageMetrics`` only for timing. ``MessageMetrics`` initialises each
    counter to ``0`` — so a provider that reports no cache detail is
    indistinguishable there from one reporting a cold cache, and taking the
    numbers from it would fabricate exactly the measurement this table exists to
    preserve.
    """

    usage = call._usage
    metrics = getattr(call._message, "metrics", None)

    if usage is not None:
        call.prompt_tokens = _reported_int(getattr(usage, "prompt_tokens", None))
        call.completion_tokens = _reported_int(getattr(usage, "completion_tokens", None))

        # Reasoning and the cache split live in nested "details" objects that the
        # provider omits entirely when it has nothing to say. Present => the
        # number is real (0 included). Absent => NULL, never 0.
        completion_details = getattr(usage, "completion_tokens_details", None)
        if completion_details is not None:
            call.reasoning_tokens = _reported_int(
                getattr(completion_details, "reasoning_tokens", None)
            )
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            call.cache_read_tokens = _reported_int(
                getattr(prompt_details, "cached_tokens", None)
            )
            # Anthropic-shaped cache-write counters, checked because this proxy
            # fronts several providers and the field is unbackfillable. On the
            # Gemini models in use today none of these appears, so the column
            # stays NULL — which is the honest record, not 0.
            for attr in ("cache_creation_tokens", "cache_creation_input_tokens"):
                if hasattr(prompt_details, attr):
                    call.cache_creation_tokens = _reported_int(getattr(prompt_details, attr))
                    break
        for attr in ("cache_creation_input_tokens", "cache_write_tokens"):
            if call.cache_creation_tokens is None and hasattr(usage, attr):
                call.cache_creation_tokens = _reported_int(getattr(usage, attr))

        cost = getattr(usage, "cost", None)
        if cost is not None:
            try:
                c = float(cost)
                if c > 0:
                    call.cost_usd, call.cost_is_estimated = round(c, 6), False
            except (TypeError, ValueError):
                pass

    if metrics is not None:
        # Timing only. agno measures both in SECONDS, off a Timer.
        call.duration_ms = _ms(getattr(metrics, "duration", None))
        call.ttft_ms = _ms(getattr(metrics, "time_to_first_token", None))
        if call.prompt_tokens is None:
            # No raw usage was seen (a streamed call whose provider sent no usage
            # chunk). agno's aggregate is the only figure left, and there a 0
            # means unreported — hence _int_or_none, not _reported_int.
            call.prompt_tokens = _int_or_none(getattr(metrics, "input_tokens", None))
            call.completion_tokens = _int_or_none(getattr(metrics, "output_tokens", None))

    if call.duration_ms is None and call._started is not None:
        import time as _time

        call.duration_ms = _ms(_time.monotonic() - call._started)

    if call.cost_usd is None:
        call.cost_usd, call.cost_is_estimated = _estimated_cost(
            call.model, call.prompt_tokens, call.completion_tokens
        )

    # `finish_reason` is NOT available. agno's OpenAI-compatible path reads
    # `choices[0].finish_reason` only to decide whether a stream is complete and
    # never carries it onto ModelResponse or Message, so there is nothing to
    # store. NULL, rather than a guessed "stop".


# ---- did the user actually get an answer ------------------------------------


def evaluate_gave_up(answer: Optional[str]) -> Optional[bool]:
    """Did the text the USER SAW apologise for failing?

    A turn can record every success it has — tools called, no exception, four
    seconds end to end — and still put "I could not produce a reliable answer"
    on the screen. `path` and `latency_ms` both look healthy on that row; only
    the answer text says what happened.

    This product's give-up text is fixed and lives in one place, so this matches
    the actual strings rather than sniffing for apologetic wording: the leak
    filter's two fallbacks (:data:`app.answer_filter.FALLBACK_EN` /
    ``FALLBACK_MM``), which are what gets sent when filtering removed everything.
    An EMPTY answer counts too — an empty bubble is the same failure with worse
    manners.

    Returns ``None`` when there is nothing to evaluate, never ``False``. NULL is
    "not evaluated"; ``False`` is a claim that somebody checked.
    """

    if answer is None:
        return None
    try:
        from app.answer_filter import FALLBACK_EN, FALLBACK_MM

        text = answer.strip()
        if not text:
            return True
        # Compare on the first sentence of each fallback: `apply_disclaimer` and
        # the streaming tail can append to it, and a prefix match on a fixed
        # constant cannot drift into matching a real answer.
        for fallback in (FALLBACK_EN, FALLBACK_MM):
            head = fallback.split("\n\n", 1)[0].strip()
            if head and head in text:
                return True
        return False
    except Exception:  # noqa: BLE001
        logger.warning("could not evaluate gave_up", exc_info=True)
        return None


# ---- schema -----------------------------------------------------------------


async def ensure_turn_calls() -> None:
    """Create tool_calls + llm_calls and their indexes. Idempotent; from lifespan.

    Kept in step with ``migrations/0008_turn_calls.sql`` — this codebase applies
    schema at boot, so the .sql file is documentation and this is the code that
    actually reaches a running database.

    Deliberately NOT wrapped: the lifespan block that calls it already logs and
    continues, exactly like every other ``ensure_*`` there.
    """

    await execute(
        """
        CREATE TABLE IF NOT EXISTS tool_calls (
            id            BIGSERIAL PRIMARY KEY,
            turn_id       BIGINT      NOT NULL REFERENCES chat_logs(id) ON DELETE CASCADE,
            seq           INT         NOT NULL,
            name          TEXT        NOT NULL,
            arguments     JSONB,
            outcome       TEXT        NOT NULL
                          CHECK (outcome IN ('succeeded','refused','failed')),
            error_message TEXT,
            attempt       INT         NOT NULL DEFAULT 1,
            duration_ms   INT,
            ts            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await execute("CREATE INDEX IF NOT EXISTS tool_calls_turn_idx ON tool_calls (turn_id, seq)")
    await execute(
        "CREATE INDEX IF NOT EXISTS tool_calls_outcome_idx ON tool_calls (outcome, ts DESC)"
    )
    await execute("CREATE INDEX IF NOT EXISTS tool_calls_name_idx ON tool_calls (name, ts DESC)")
    await execute(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id                    BIGSERIAL PRIMARY KEY,
            turn_id               BIGINT      NOT NULL REFERENCES chat_logs(id) ON DELETE CASCADE,
            seq                   INT         NOT NULL,
            model                 TEXT,
            prompt_tokens         INT,
            completion_tokens     INT,
            reasoning_tokens      INT,
            cache_read_tokens     INT,
            cache_creation_tokens INT,
            ttft_ms               INT,
            duration_ms           INT,
            cost_usd              NUMERIC(12,6),
            cost_is_estimated     BOOLEAN     NOT NULL DEFAULT FALSE,
            finish_reason         TEXT,
            ts                    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await execute("CREATE INDEX IF NOT EXISTS llm_calls_turn_idx ON llm_calls (turn_id, seq)")
    await execute("CREATE INDEX IF NOT EXISTS llm_calls_model_idx ON llm_calls (model, ts DESC)")


async def ensure_chat_logs_actor() -> None:
    """Add chat_logs.actor_email / actor_role / gave_up. Idempotent; from lifespan.

    Kept in step with ``migrations/0009_chat_logs_actor.sql``. ``gave_up`` is
    added with NO default on purpose: existing rows must read NULL (not
    evaluated), not ``false`` (evaluated and fine).
    """

    for col, typ in (
        ("actor_email", "TEXT"),
        ("actor_role", "TEXT"),
        ("gave_up", "BOOLEAN"),
    ):
        await execute(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")
    await execute(
        "CREATE INDEX IF NOT EXISTS chat_logs_actor_idx ON chat_logs (actor_email, ts DESC)"
    )


async def ensure_chat_feedback_turn() -> None:
    """chat_feedback.turn_id, with the delete rule that does not destroy ratings.

    Kept in step with ``migrations/0010_chat_feedback_turn.sql``.

    ⚠️ **This function is partly CORRECTIVE, and the correction is about data
    loss, not tidiness.** ``admin.ensure_feedback`` also creates this column, and
    creates it ``ON DELETE CASCADE``. ``admin.prune_chat_logs()`` runs on every
    boot and deletes ``chat_logs`` older than ``chat_log_retention_days``
    (default 30), so under CASCADE every rating attached to an older turn is
    deleted along with it — written corrections included. Verified live: one
    ``verdict='down'`` row with a correction, then the DELETE that
    ``prune_chat_logs`` issues, left zero feedback rows.

    A rating is a human taking the trouble to tell us we got something wrong. It
    is not a log line and must not inherit a log's retention. Under SET NULL the
    prune leaves the rating and merely unattributes it, which is the truth about
    it once the turn is gone.

    ⚠️ **Ordering is load-bearing.** This must run AFTER ``ensure_feedback`` in
    the lifespan, or the CASCADE version is applied last and wins. It is second
    today (``ensure_feedback`` at the chat_logs block, this in the activity
    block). The durable fix is one word in ``app/admin.py``; when that lands, the
    repair half of this function should be deleted and only the column and index
    kept.
    """

    await execute(
        "ALTER TABLE chat_feedback ADD COLUMN IF NOT EXISTS turn_id BIGINT"
    )
    # Repair the delete rule if anything created it as CASCADE (or NO ACTION,
    # which would make the prune fail outright instead). `confdeltype` is 'c' for
    # CASCADE, 'n' for SET NULL, 'a' for NO ACTION. Self-contained and guarded so
    # a table with pre-existing orphan turn_ids logs a NOTICE rather than taking
    # the boot down.
    await execute(
        """
        DO $$
        DECLARE
            fk record;
        BEGIN
            FOR fk IN
                SELECT c.conname, c.confdeltype
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = 'chat_feedback'
                   AND c.contype = 'f'
                   AND c.conkey = ARRAY[(
                       SELECT attnum FROM pg_attribute
                        WHERE attrelid = t.oid AND attname = 'turn_id'
                   )]::smallint[]
            LOOP
                IF fk.confdeltype <> 'n' THEN
                    EXECUTE format(
                        'ALTER TABLE chat_feedback DROP CONSTRAINT %I', fk.conname
                    );
                    RAISE NOTICE
                        'chat_feedback.turn_id: replaced delete rule % with SET NULL',
                        fk.confdeltype;
                END IF;
            END LOOP;

            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = 'chat_feedback' AND c.contype = 'f'
                   AND c.confdeltype = 'n'
            ) THEN
                BEGIN
                    ALTER TABLE chat_feedback
                        ADD CONSTRAINT chat_feedback_turn_fk
                        FOREIGN KEY (turn_id) REFERENCES chat_logs(id)
                        ON DELETE SET NULL;
                EXCEPTION WHEN others THEN
                    RAISE NOTICE 'chat_feedback.turn_id FK not added: %', SQLERRM;
                END;
            END IF;
        END $$;
        """
    )
    # Only if nothing already indexes the column. `ensure_feedback` creates the
    # same index under a different name (`idx_chat_feedback_turn`), and two
    # btrees on one column is real write cost for no read benefit.
    rows = await q(
        """SELECT 1 FROM pg_index i
             JOIN pg_class t ON t.oid = i.indrelid
             JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
            WHERE t.relname = 'chat_feedback' AND a.attname = 'turn_id'
            LIMIT 1"""
    )
    if not rows:
        await execute(
            "CREATE INDEX IF NOT EXISTS chat_feedback_turn_idx ON chat_feedback (turn_id)"
        )


# ---- the flush --------------------------------------------------------------
#
# ⚠️ THREE PROPERTIES HERE ARE LOAD-BEARING, and two of them are about the buffer
# rather than the write:
#
# 1. **`turn_id=None` is normal, not an error.** `admin.log_chat` swallows its
#    own failures and has three fallback INSERT tiers, so a turn CAN be answered
#    with no `chat_logs` row behind it. There is then nothing to attach to. The
#    capture is dropped at debug level — never written against a guessed id,
#    because a per-call row filed under the wrong turn looks correct and is not.
#
# 2. **The buffer is cleared FIRST, on every path.** Before the None check,
#    before any decision, before anything that could raise. A turn whose flush
#    bailed out early must not leave its tool calls in the contextvar for the
#    next turn on the same worker to inherit — that mis-attribution is worse than
#    losing the rows, because it silently moves one pharmacist's calls onto
#    another's turn.
#
# 3. **It does not block the response.** `flush_turn` is SYNC and schedules the
#    inserts as a background task, so the caller pays a few microseconds and
#    returns. Nothing is retried; a lost analytics row is a lost analytics row.


# Strong references to in-flight flushes. asyncio keeps only a weak reference to
# a task, so a flush that nobody holds can be garbage-collected mid-await and the
# rows silently never land.
_PENDING: "set" = set()


def flush_turn(turn_id: Optional[int], *, answer: Optional[str] = None) -> None:
    """Detach this turn's buffer and schedule its inserts. **Never raises.**

    Call once per turn, after the ``chat_logs`` row has been written.
    ``turn_id`` is that row's id, or ``None`` when it could not be written —
    both are valid, see the note above.

    ``answer`` is the text the USER SAW. Pass it and ``gave_up`` gets evaluated;
    omit it and ``gave_up`` stays NULL, which is the honest record for a turn
    nobody assessed. It is a pure string comparison against two fixed constants,
    so it costs no IO on the calling path.
    """

    # FIRST, unconditionally: whatever happens below, this turn's buffer is no
    # longer this context's. See property 2 above.
    capture = end_turn()
    if capture is None:
        return
    try:
        if answer is not None and capture.gave_up is None:
            capture.gave_up = evaluate_gave_up(answer)

        if turn_id is None:
            # Expected, not exceptional — debug, not warning. A warning per turn
            # on a database that has lost chat_logs would bury the real errors.
            logger.debug(
                "no turn id: dropping %d tool call(s) and %d llm call(s)",
                len(capture.tools), len(capture.llms),
            )
            return

        if not capture and capture.gave_up is None and not capture.actor_email:
            # Genuinely nothing to write: no tool ran, no model ran, nobody was
            # attributed and no answer was assessed. This is the cache-hit shape
            # when the caller passes no `answer`, and it costs zero queries.
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a sync caller, or shutdown). Nothing to schedule onto, and
            # spinning one up here would block the very path this must not block.
            logger.debug("no running loop; dropping a turn's per-call capture")
            return

        task = loop.create_task(flush_turn_now(int(turn_id), capture))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception:  # noqa: BLE001 — the answer outranks its own analytics
        logger.warning("could not schedule the flush for turn %s", turn_id, exc_info=True)


async def drain_flushes() -> None:
    """Await every in-flight flush. For tests and shutdown, not for the hot path.

    :func:`flush_turn` is deliberately fire-and-forget, which means a test that
    asserts on ``tool_calls`` straight after it would race the write. Await this
    first.
    """

    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


async def flush_turn_now(turn_id: int, capture: TurnCapture) -> None:
    """The actual writes. **Never raises** — this runs detached, with no caller.

    A failure here loses a row of analytics; it must never lose an answer, so it
    degrades to one warning — the same contract as :func:`record_event`,
    ``ingest_events.record`` and ``history.record_turn``.

    ⚠️ **ONE TRANSACTION, because a HALF-flushed turn is worse than an unflushed
    one.** These were four separate statements until a live run caught it: the
    pool closed at shutdown between the inserts and the UPDATE, so a turn landed
    with all its ``tool_calls`` and ``llm_calls`` rows but ``gave_up`` left NULL.
    That reads as "instrumented, and nobody assessed the answer" when the truth
    is "assessed, and the write was lost" — an inconsistency no reader could
    detect, in the exact table meant to be the ground truth. All of it commits or
    none of it does.
    """

    try:
        turn_id = int(turn_id)
        gave_up = capture.gave_up

        for call in capture.llms:
            _finalize_llm(call)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if capture.tools:
                    await conn.executemany(
                        """INSERT INTO tool_calls
                               (turn_id, seq, name, arguments, outcome,
                                error_message, attempt, duration_ms)
                           VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8)""",
                        [
                            (
                                turn_id,
                                t.seq,
                                t.name,
                                json.dumps(t.arguments) if t.arguments else None,
                                t.outcome,
                                t.error_message,
                                t.attempt,
                                t.duration_ms,
                            )
                            for t in capture.tools
                        ],
                    )

                if capture.llms:
                    await conn.executemany(
                        """INSERT INTO llm_calls
                               (turn_id, seq, model, prompt_tokens, completion_tokens,
                                reasoning_tokens, cache_read_tokens,
                                cache_creation_tokens, ttft_ms, duration_ms,
                                cost_usd, cost_is_estimated, finish_reason)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                                   $11::text::numeric,$12,$13)""",
                        [
                            (
                                turn_id,
                                c.seq,
                                c.model,
                                c.prompt_tokens,
                                c.completion_tokens,
                                c.reasoning_tokens,
                                c.cache_read_tokens,
                                c.cache_creation_tokens,
                                c.ttft_ms,
                                c.duration_ms,
                                # NUMERIC is bound through text: asyncpg maps
                                # `numeric` to Decimal and refuses a bare float.
                                # Same reason as log_chat.
                                None if c.cost_usd is None else str(c.cost_usd),
                                bool(c.cost_is_estimated),
                                c.finish_reason,
                            )
                            for c in capture.llms
                        ],
                    )

                if capture.actor_email or capture.actor_role or gave_up is not None:
                    await conn.execute(
                        """UPDATE chat_logs
                              SET actor_email = COALESCE($2, actor_email),
                                  actor_role  = COALESCE($3, actor_role),
                                  gave_up     = COALESCE($4, gave_up)
                            WHERE id = $1""",
                        turn_id,
                        (capture.actor_email or "").strip().lower() or None,
                        capture.actor_role or None,
                        gave_up,
                    )
    except Exception:  # noqa: BLE001 — the answer outranks its own analytics
        logger.warning("could not flush turn calls for turn %s", turn_id, exc_info=True)


__all__ = [
    "METRIC_FIELDS", "NO_METRICS", "extract_metrics", "ensure_turn_metrics",
    "ensure_app_events", "record_event", "unified_feed",
    "action_for", "match_route", "summarize_body", "should_record",
    "MUTATING", "REDACTED",
    # per-call capture (0008 / 0009)
    "SUCCEEDED", "REFUSED", "FAILED", "OUTCOMES",
    "ToolCall", "LlmCall", "TurnCapture",
    "begin_turn", "current_turn", "end_turn",
    "record_tool_call", "record_llm_call", "open_llm_call", "set_turn_actor",
    "evaluate_gave_up", "ensure_turn_calls", "ensure_chat_logs_actor",
    "ensure_chat_feedback_turn",
    "flush_turn", "flush_turn_now", "drain_flushes",
]
