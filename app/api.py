"""FastAPI HTTP surface — CityAgent embed-compatible.

Implements the same contract the existing PHP ``CityAgentClient`` and
``widget.js`` already speak, so they are drop-in against this backend:

    POST /api/embed/session/create  {embed_id, public_key, user?, signature?}
                                    -> {session_token, expires_in}
    POST /api/embed/chat            {session_token, message} -> {content}
    POST /api/embed/chat/stream     -> SSE: event:step {label,icon}
                                            data: {delta}
                                            data: [DONE]
    GET  /health   /ready

Security: an optional signed ``user`` payload (HMAC, verified server-side)
binds a ``store_id`` into the short-lived session token. ``/chat`` decodes the
token and force-scopes every tool call to that store — the model cannot read
another branch's data.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from contextlib import asynccontextmanager

from app.agent import get_agent
from app.answer_filter import (
    LeakFilter,
    contains_reasoning,
    fallback_for,
    filter_answer,
)
from app.disclaimer import apply_policy as apply_disclaimer
from app.cache import (
    bump_data_version,
    bump_session_turn,
    check_rate_limit,
    close_client,
    get_cached_answer,
    get_data_version,
    is_valid_credential,
    set_cached_answer,
)
from app import fastpath
from app import cache as cache_mod
from app.cache import ensure_dev_credential, ensure_internal_credential
from app.config import Settings, get_settings
from app.db import close_pool, counts, get_pool
from app.embeddings import close as close_embeddings
from app.ingest import reload_from_data_dir

import logging
import re as _re

logger = logging.getLogger("pharmacy.api")

_MY_CHARS = _re.compile(r"[က-႟]")   # Burmese block


def _step_detail(tool_args) -> str:
    """A short, human detail for a tool step, pulled from its arguments.

    Turns three identical 'Looking up article info' rows into distinct lines
    ('Looking up RELYTE', 'Searching for fever medicine'). Store scope is not in
    the args (it rides a contextvar), so nothing leaks a sibling branch here.
    """

    if not isinstance(tool_args, dict):
        return ""
    for k in ("query", "name", "mention", "term", "condition", "keyword"):
        v = tool_args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:48]
    for k in ("code", "article_code"):
        v = tool_args.get(k)
        if v:
            return str(v)[:20]
    for k in ("store", "store_id", "site"):
        v = tool_args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:20]
    return ""


_CODE_RE = _re.compile(r"^\d{10,14}$")


# How many rows a `result` frame may carry. Was a silent `rows[:8]`, which is
# what acceptance rule 6 from the 2026-07-28 feedback explicitly rejects:
# "return all matching results... do not summarize, reduce, or limit the result
# unless user requests a specific limit". A ceiling still exists — an SSE frame
# holding 100k inventory rows helps nobody — but it is high enough not to bite
# real queries, and when it does bite it SAYS SO rather than quietly dropping
# rows the pharmacist needed.
RESULT_ROW_LIMIT = 250


def _result_frame(tool: str, rows: list) -> Dict[str, Any]:
    """Build an SSE ``result`` payload, reporting any truncation."""

    frame: Dict[str, Any] = {"tool": tool, "rows": rows[:RESULT_ROW_LIMIT]}
    if len(rows) > RESULT_ROW_LIMIT:
        # Additive fields; the embed widget ignores what it does not know.
        frame["total_rows"] = len(rows)
        frame["truncated"] = True
        logger.info(
            "result frame truncated: %s returned %s rows, sent %s",
            tool, len(rows), RESULT_ROW_LIMIT,
        )
    return frame



def _finish_stream(full: str, tail: str, force: Optional[bool] = None):
    """Apply the disclaimer policy at the end of a stream.

    Returns ``(text_to_emit, final_full)``.

    Streaming cannot un-send a token, so the policy can only suppress a safety
    line that has NOT been streamed yet. That covers the normal case: the line
    is the last paragraph, LeakFilter holds a paragraph until it is complete,
    so the final one is still in its buffer at flush time and can simply be
    dropped. When the model wrote the line inline instead ("800 MMK. Please
    consult..."), it has already gone out — the text is corrected for the cache
    and the conversation, and the reader sees one redundant sentence. Redundant
    beats wrong, and the non-streaming route has no such limit.
    """

    final = apply_disclaimer(full + (tail or ""), force=force)
    if final.startswith(full):
        return final[len(full):], final
    # The policy rewrote text already on screen; emit the tail as-is and keep
    # the corrected version for everything downstream.
    return (tail or ""), final


def _subject_of(rows: list, tool_args) -> Optional[Dict[str, str]]:
    """The drug an answer is ABOUT — code + display name — or ``None``.

    Sent as the additive ``subject`` field on an SSE ``result`` frame so the UI
    can offer follow-up questions ("price of X?", "substitutes for X?"). It
    cannot be read off the rows alone: ``get_stock`` and ``find_at_other_stores``
    select ``site_code, site_name, stock_qty`` — branch rows, no drug. The row
    scan below therefore falls back to the tool's own ``code`` argument.
    """

    for row in rows[:8]:  # a subject scan, not a display cap — first hit wins
        if not isinstance(row, dict):
            continue
        code = next(
            (
                str(v)
                for k, v in row.items()
                if "code" in k.lower() and _CODE_RE.match(str(v or ""))
            ),
            None,
        )
        if not code:
            continue
        name = next(
            (
                v.strip()
                for k, v in row.items()
                if isinstance(v, str)
                and _re.search(r"name|brand|product|desc", k, _re.I)
                and len(v.strip()) > 2
            ),
            None,
        )
        return {"code": code, "name": name or code}

    if isinstance(tool_args, dict):
        for k in ("code", "article_code"):
            v = tool_args.get(k)
            if v and _CODE_RE.match(str(v)):
                return {"code": str(v), "name": str(v)}
    return None


def _plan_line(message: str) -> str:
    """A one-line plan for the answer, chosen by intent from the question.

    Template, not a model call: it reads like a plan without adding a round trip
    on a stack where per-call LLM cost dominates latency. Bilingual to match the
    question. Deliberately honest and generic — it says what the agent is about
    to do, it does not promise a specific finding.
    """

    m = (message or "").strip()
    if not m:
        return ""
    my = bool(_MY_CHARS.search(m))
    low = m.lower()

    def has(*words):
        return any(w in low for w in words) or any(w in m for w in words)

    if has("price", "cost", "ဈေး", "စျေး", "ဈေးနှုန်း"):
        return "ဈေးနှုန်းရှာဖွေပြီး ဆိုင်များအလိုက် နှိုင်းယှဉ်ပါမည်။" if my \
            else "I'll find the item, then read its price across branches."
    if has("substitute", "alternative", "instead", "အစား", "အစားထိုး"):
        return "ရောဂါတူ/ဆေးတူ အစားထိုးများ ရှာဖွေပါမည်။" if my \
            else "I'll identify the drug, then find substitutes for the same use."
    if has("branch", "store", "which shop", "where", "ဆိုင်", "ဘယ်မှာ", "ဘယ်ဆိုင်"):
        return "ဆေးကို ဖော်ထုတ်ပြီး ဆိုင်များအလိုက် လက်ကျန်စစ်ပါမည်။" if my \
            else "I'll resolve the item, then check stock at each branch."
    # symptom before plain "have/stock": "medicine for fever" is a need, not a
    # named item, so the condition search is the more honest plan.
    if has("fever", "pain", "cough", "cold", "headache", "ဖျား", "အဖျား", "ချောင်း", "ဝေဒနာ"):
        return "ရောဂါလက္ခဏာအတွက် သင့်လျော်သောဆေးများ ရှာဖွေပါမည်။" if my \
            else "I'll look for medicines that treat this, then check what's in stock."
    if has("stock", "have", "available", "ရှိ", "လက်ကျန်"):
        return "ဆေးကို ရှာဖွေပြီး လက်ကျန်ပမာဏ စစ်ဆေးပါမည်။" if my \
            else "I'll match the item in the catalog, then check its stock."
    return "မေးခွန်းကို နားလည်ပြီး ဒေတာဘေ့စ်တွင် ရှာဖွေပါမည်။" if my \
        else "I'll interpret the question, then search the catalog and stock."
from app.security import (
    create_session_token,
    decode_session_token,
    verify_user_signature,
)
from app.tools import reset_store_scope, set_store_scope


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Warm the Postgres pool on startup; close pool + redis on shutdown.

    Creating the pool inside the serving loop (uvicorn's persistent loop, or the
    TestClient's portal loop) ensures every request shares one loop-bound pool.
    """

    # First, before anything is served: refuse to run a production deploy whose
    # signing secret is the shipped placeholder, and warn about the other
    # postures that are merely wrong. Deliberately NOT inside a try/except —
    # this one is meant to stop the process (app_env=production only).
    from app.auth import boot_security_checks

    await boot_security_checks()

    await get_pool()
    # Seed the database on first boot so the stack comes up with data. In
    # production this is where real Excel would be loaded instead.
    try:
        need_load = True
        try:
            need_load = (await counts()).get("catalog_rows", 0) == 0
        except Exception:  # noqa: BLE001 - tables not created yet
            need_load = True
        if need_load:
            result = await reload_from_data_dir()  # loads article/balance xlsx if present
            logger.info("loaded data on startup: %s", result)
    except Exception as exc:  # noqa: BLE001 - don't block startup on load
        logger.warning("startup data load skipped: %s", exc)
    try:
        from app.admin import ensure_chat_logs, ensure_feedback

        await ensure_chat_logs()
        await ensure_feedback()
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_logs init skipped: %s", exc)
    # Observability capture. `ensure_turn_metrics` adds the chat_logs cost
    # columns (migration 0006) and `ensure_app_events` creates the activity
    # trail (0007). Kept OUT of admin.ensure_chat_logs on purpose so this half
    # deploys independently of the log_chat signature change: the columns may
    # exist before anything writes them, and a writer that names them on a
    # database missing them still degrades to the old column set inside
    # log_chat.
    try:
        from app import activity

        await activity.ensure_turn_metrics()
        await activity.ensure_app_events()
        # 0008 / 0009: per-call detail (tool_calls, llm_calls) and the turn's
        # actor + give-up verdict. AFTER ensure_chat_logs above, not before —
        # both new tables carry a foreign key to chat_logs(id), so the parent
        # table has to exist or CREATE TABLE fails and takes the two ensure_*
        # calls above down with it on a fresh database.
        await activity.ensure_turn_calls()
        await activity.ensure_chat_logs_actor()
        # 0010: chat_feedback.turn_id. ⚠️ MUST stay after `ensure_feedback()`
        # above, which creates the same column ON DELETE CASCADE — and
        # `prune_chat_logs()` (further down this lifespan) then deletes every
        # rating attached to a turn older than the log retention, corrections
        # included. This re-points the delete rule at SET NULL, and whichever
        # runs LAST wins. See the warning on ensure_chat_feedback_turn.
        await activity.ensure_chat_feedback_turn()
    except Exception as exc:  # noqa: BLE001 — audit schema is never worth a failed boot
        logger.warning("activity/metrics schema init skipped: %s", exc)
    try:
        from app import ingest_events

        await ingest_events.ensure_schema()
        # Trim on boot rather than on a timer: the table only grows when files
        # arrive, and a restart is the one moment we are certainly not mid-scan.
        await ingest_events.prune()
    except Exception as exc:  # noqa: BLE001 — history is not worth a failed boot
        logger.warning("ingest_events init skipped: %s", exc)
    try:
        from app.auth import ensure_auth_events, ensure_users_table, seed_super_admin

        await ensure_users_table()
        await seed_super_admin()
        # The login lockout counts rows in this table; a fresh boot must create
        # it the same way migrations/0004_auth_events.sql would.
        await ensure_auth_events()
    except Exception as exc:  # noqa: BLE001
        logger.warning("user auth init skipped: %s", exc)
    # After ensure_users_table: adds users.store_id (branch-scoped admins) and
    # drug_alias. Ordering matters — the column is added to a table that must
    # already exist.
    try:
        from app.admin import ensure_admin_schema

        await ensure_admin_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin schema init skipped: %s", exc)
    # White-label branding. Same statements as migrations/0005_branding.sql, so
    # a fresh container and a hand-migrated database converge — and so the login
    # screen's GET /brand hits a table that exists on first boot rather than
    # falling back to defaults until someone runs psql.
    try:
        from app.brand import ensure_brand_tables

        await ensure_brand_tables()
    except Exception as exc:  # noqa: BLE001 — branding is never worth a failed boot
        logger.warning("branding schema init skipped: %s", exc)
    # The embed credential check is fail-closed, so an empty store rejects every
    # embed. Seed the documented dev credential — flag-gated, and only into an
    # empty store. Never silently: it logs a warning when it fires.
    try:
        await ensure_dev_credential()
    except Exception as exc:  # noqa: BLE001 — Redis down must not block startup
        logger.warning("embed credential seed skipped: %s", exc)
    # The console chat is a first-party embed client with a fixed credential;
    # once ANY credential exists the fail-closed check would 403 it. Always seed.
    try:
        await ensure_internal_credential()
    except Exception as exc:  # noqa: BLE001 — Redis down must not block startup
        logger.warning("internal chat credential seed skipped: %s", exc)
    try:
        from app.admin import prune_chat_logs

        removed = await prune_chat_logs()
        if removed:
            logger.info("pruned %d old chat logs", removed)
    except Exception:  # noqa: BLE001
        pass
    # Keep the runtime CORS allowlist warm. is_allowed_origin() is sync and on the
    # request hot path, so it cannot await Redis — this loop refreshes an
    # in-process copy instead, and a UI change lands within one interval.
    cors_task = asyncio.create_task(_refresh_cors_loop())
    yield
    cors_task.cancel()
    await close_pool()
    await close_client()
    await close_embeddings()


async def _refresh_cors_loop() -> None:
    """Mirror the Redis CORS set into ``_EXTRA_CORS`` every few seconds."""

    global _EXTRA_CORS
    while True:
        try:
            _EXTRA_CORS = {o.lower() for o in await cache_mod.get_cors_origins()}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a Redis blip must not kill the loop
            pass
        try:
            await asyncio.sleep(_CORS_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise


from app.version import VERSION as _APP_VERSION

app = FastAPI(title="CitCare Pharmacy Agent", version=_APP_VERSION, lifespan=lifespan)


def cors_origins() -> list[str]:
    """Resolve ALLOWED_ORIGINS to the list handed to CORSMiddleware.

    Two rules, both learned the hard way:

    * ``*`` is honoured only when an operator actually wrote it. It is no longer
      the default, and — critically — it is no longer the *fallback*. The old
      code said ``allow_origins=_origins or ["*"]``, so an empty or
      whitespace-only ALLOWED_ORIGINS silently reopened the API to every site on
      the internet: the one value an operator is most likely to leave behind
      while tightening the config was the one that undid the tightening.
    * An empty/blank setting therefore falls back to the safe localhost default,
      never to a wildcard.

    A wildcard still logs a warning, because the embed API mints store-scoped
    session tokens and any origin being allowed to ask for one is a decision, not
    an accident.
    """

    raw = get_settings().allowed_origins
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        origins = [
            o.strip()
            for o in Settings.model_fields["allowed_origins"].default.split(",")
            if o.strip()
        ]
        logger.warning(
            "ALLOWED_ORIGINS is empty; falling back to the dev default %s "
            "(NOT a wildcard). Set ALLOWED_ORIGINS to your real domains.",
            origins,
        )
    if "*" in origins:
        logger.warning(
            "CORS IS OPEN TO ALL ORIGINS (ALLOWED_ORIGINS=*). Any website can call "
            "the embed API from a visitor's browser. Set ALLOWED_ORIGINS to the "
            "customer domains before exposing this deployment."
        )
    return origins


# Origins added at runtime from the admin UI (Redis-backed), refreshed into this
# set every few seconds by the lifespan loop. is_allowed_origin() (sync, on the
# hot path) reads it without touching Redis; a change lands within one refresh.
_EXTRA_CORS: set[str] = set()
_CORS_REFRESH_SECONDS = 3


class DynamicCORS(CORSMiddleware):
    """CORSMiddleware whose allowlist is the env origins PLUS the runtime set.

    Origins used to be ONLY ``ALLOWED_ORIGINS`` (env, read once at boot), so
    allowing a new customer site meant editing env and restarting. This keeps
    Starlette's battle-tested preflight/header machinery and only widens *which*
    origins pass — env origins (``self.allow_origins``, fixed at init) unioned
    with ``_EXTRA_CORS`` (managed live at ``/admin/cors-origins``). A wildcard in
    env still short-circuits via ``allow_all_origins``.
    """

    def is_allowed_origin(self, origin: str) -> bool:
        if self.allow_all_origins:
            return True
        o = origin.lower()
        return o in self.allow_origins or o in _EXTRA_CORS


app.add_middleware(
    DynamicCORS,
    allow_origins=[o.lower() for o in cors_origins()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observability(request, call_next):
    """Per-request id, latency, structured log, and metrics counters.

    Never logs request bodies, tokens, or secrets — only method, path, status,
    latency, and a short request id.
    """

    import time
    import uuid

    from app import metrics

    rid = uuid.uuid4().hex[:8]
    start = time.time()
    metrics.incr("requests_total")
    metrics.record_request()
    try:
        response = await call_next(request)
    except Exception:
        metrics.incr("errors_total")
        logger.exception("rid=%s %s %s -> 500", rid, request.method, request.url.path)
        raise
    elapsed_ms = (time.time() - start) * 1000
    metrics.observe_latency(elapsed_ms)
    if response.status_code >= 500:
        metrics.incr("errors_total")
    response.headers["X-Request-Id"] = rid
    logger.info(
        "rid=%s %s %s -> %s %.0fms",
        rid, request.method, request.url.path, response.status_code, elapsed_ms,
    )
    return response


@app.get("/metrics")
async def metrics_endpoint() -> Dict[str, Any]:
    """Operational metrics: volume, errors, cache hit rate, LLM calls, latency."""

    from app import metrics

    return metrics.snapshot()


@app.get("/metrics/history")
async def metrics_history() -> Dict[str, Any]:
    """Last 12 minutes of request volume (requests vs llm) for the dashboard chart."""

    from app import metrics

    return {"buckets": metrics.history(12)}


from fastapi import Header  # noqa: E402

from app import auth as authmod  # noqa: E402


def _bearer(authorization: str) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1]


async def current_user(authorization: str = Header(default="")) -> Dict[str, Any]:
    """Resolve the signed-in user from the Authorization: Bearer <jwt> header."""

    import jwt as _jwt

    try:
        claims = authmod.decode_token(_bearer(authorization))
    except _jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return claims


async def require_admin(authorization: str = Header(default="")) -> Dict[str, Any]:
    """Gate /admin/* — a valid JWT whose account is admin, active AND approved.

    Approval is re-checked against the DB on every call, not read from the token,
    so revoking approval takes effect immediately rather than at token expiry.
    """

    claims = await current_user(authorization)
    u = await authmod.get_by_email(claims.get("email", ""))
    if not u or not u["active"]:
        raise HTTPException(status_code=401, detail="account inactive")
    if not u.get("approved"):
        raise HTTPException(status_code=403, detail="account pending administrator approval")
    if u["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="admin access required")
    return claims


# ---- auth routes -----------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


@app.get("/auth/config")
async def auth_config() -> Dict[str, Any]:
    """Public — tells the login screen which methods are available.

    Reads the effective config (env + runtime overrides) so toggling SSO from
    the admin panel shows the button without a restart.
    """

    return await authmod.auth_config_public()


def client_ip(request: Request) -> str:
    """The caller's address, honouring the first hop of X-Forwarded-For.

    The app sits behind a proxy, so ``request.client.host`` is the proxy for
    every caller on earth — throttling on it would throttle everyone at once.
    XFF is appended left-to-right, so the ORIGINAL client is the first entry;
    later entries are proxies. It is client-controlled and therefore only ever
    used for rate limiting and the audit trail, never for authorisation.
    """

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    return (request.client.host if request.client else "")[:64]


# ---- the activity trail ------------------------------------------------------
#
# One row in `app_events` per MUTATING /admin/* request: who, what, when, from
# where, and what it answered. See migrations/0007_app_events.sql for why this
# exists and app/activity.py for the allowlist that decides what of a body may
# be kept.
#
# ⚠️ **Reading the body here is the dangerous part, and it is bounded three
# ways.** Only `application/json`, only under 64 KB, and only when the route is
# in the allowlist at all — so a multipart logo upload or a 40 MB inventory
# xlsx is never pulled into memory by the audit layer. Starlette's
# BaseHTTPMiddleware caches a body read in dispatch and replays it downstream
# (`_CachedRequest.wrapped_receive`), so the handler still sees its body; that
# is a property of starlette >= 0.28 and the reason this is safe at all. Without
# it, reading here would hang every POST.

_AUDIT_BODY_LIMIT = 64 * 1024


async def _audit_body(request) -> Any:
    """The parsed JSON body, or None. Never raises, never blocks on a big upload."""

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return None
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        return None
    if length <= 0 or length > _AUDIT_BODY_LIMIT:
        return None
    try:
        raw = await request.body()
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — an unparseable body is simply not summarised
        return None


def _audit_actor(request) -> tuple:
    """``(email, role)`` from the bearer token, best-effort.

    Decoded, not verified against the database: this is a record of who the
    request CLAIMED to be, and `require_admin` has already decided whether that
    claim was honoured — the `status` column says which. A token that will be
    rejected still names the account someone tried to act as, which is exactly
    the row a review wants to see.
    """

    try:
        header = request.headers.get("authorization") or ""
        if not header.lower().startswith("bearer "):
            return None, None
        claims = authmod.decode_token(header.split(" ", 1)[1])
        return claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an invalid/expired token is an anonymous actor
        return None, None


@app.middleware("http")
async def activity_audit(request, call_next):
    """Record every mutating admin request in `app_events`. Never breaks one.

    A failed request is recorded too, with its status: a rejected attempt to
    promote an account is more interesting than a successful one, and an audit
    trail that only shows the system working is the failure mode this exists to
    avoid.
    """

    from app import activity

    method = request.method.upper()
    path = request.url.path
    if not activity.should_record(method, path):
        return await call_next(request)

    import time as _t

    target, spec = activity.match_route(method, path)
    body = await _audit_body(request) if spec else None
    actor_email, actor_role = _audit_actor(request)
    ip = client_ip(request)
    t0 = _t.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        # An unhandled error is still a change attempt, and the one most worth
        # having a row for. Recorded as 500, then re-raised unchanged.
        raise
    finally:
        try:
            await activity.record_event(
                activity.action_for(method, path, target),
                actor_email=actor_email,
                actor_role=actor_role,
                target=target,
                method=method,
                path=path,
                status=status,
                detail=activity.summarize_body(method, path, body),
                ip=ip,
                duration_ms=int((_t.time() - t0) * 1000),
            )
        except Exception:  # noqa: BLE001 — record_event already swallows; this is the net
            logger.warning("activity audit failed for %s %s", method, path, exc_info=True)


@app.post("/auth/login")
async def login(req: LoginRequest, request: Request) -> Dict[str, Any]:
    """Local login; falls back to LDAP (email as username) if enabled.

    Rate limited two ways (see app.auth and config.login_*): per email, which
    stops guessing at one account, and per IP at a much higher threshold, which
    stops spraying one guess across many accounts. Without the second, the
    per-email counter never fires and the endpoint stays an open oracle — and
    with LDAP on, every guess is also proxied into the directory, so an attacker
    could drive real AD accounts into THEIR lockout from here.
    """

    s = get_settings()
    email = (req.email or "").strip().lower()
    ip = client_ip(request)

    fails = await authmod.failed_logins_for_email(email, s.login_lock_minutes)
    if fails >= s.login_max_fail:
        await authmod.record_auth_event(
            authmod.EV_LOGIN_LOCKED, email=email, ip=ip,
            detail=f"{fails} failed attempts in {s.login_lock_minutes}m (email)",
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many failed sign-in attempts for this account. "
                f"Try again in {s.login_lock_minutes} minutes, or ask an "
                f"administrator to reset the password."
            ),
        )

    ip_fails = await authmod.failed_logins_for_ip(ip, s.login_lock_minutes)
    if ip_fails >= s.login_ip_max_fail:
        await authmod.record_auth_event(
            authmod.EV_LOGIN_LOCKED, email=email, ip=ip,
            detail=f"{ip_fails} failed attempts in {s.login_lock_minutes}m (ip)",
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many failed sign-in attempts from this network. "
                f"Try again in {s.login_lock_minutes} minutes."
            ),
        )

    async def _ok(result: Dict[str, Any], how: str) -> Dict[str, Any]:
        # The login_ok row is also what RESETS the per-email fail counter — the
        # counter reads "fails since the last success", so there is no separate
        # reset write that could be skipped on one of the two success paths.
        await authmod.record_auth_event(
            authmod.EV_LOGIN_OK, email=email, ip=ip, detail=how)
        return result

    async def _fail(reason: str):
        await authmod.record_auth_event(
            authmod.EV_LOGIN_FAIL, email=email, ip=ip, detail=reason)

    async def _blocked(exc: Exception):
        # The password WAS correct — this is policy, so it is logged as its own
        # event and must not feed the lockout counter (see EV_LOGIN_BLOCKED).
        await authmod.record_auth_event(
            authmod.EV_LOGIN_BLOCKED, email=email, ip=ip, detail=str(exc))
        raise HTTPException(status_code=403, detail=str(exc))

    try:
        result = await authmod.login_local(req.email, req.password)
    except authmod.AuthError:
        # ⚠️ The EFFECTIVE layer, not get_settings(). ldap_enabled can be turned
        # on at runtime from /admin/auth (a Redis override), and both
        # /auth/config and login_ldap already read it that way. Reading env here
        # made "enable LDAP" show the banner on the login screen while this
        # fallthrough silently stayed off.
        cfg = await authmod.effective_auth()
        if cfg.ldap_enabled:
            try:
                ldap_result = await authmod.login_ldap(req.email, req.password)
            except authmod.AuthError as exc:
                await _fail(str(exc))
                raise HTTPException(status_code=401, detail=str(exc))
            try:
                ldap_result = await authmod.enforce_signin_mode(ldap_result, cfg)
            except authmod.SigninModeError as exc:
                await _blocked(exc)
            return await _ok(ldap_result, "ldap")
        await _fail("invalid credentials")
        raise HTTPException(status_code=401, detail="invalid credentials")

    # Authenticated. `signin_mode` is applied only now, so the super_admin
    # carve-out can be decided from the row we just proved the caller owns.
    try:
        result = await authmod.enforce_signin_mode(result)
    except authmod.SigninModeError as exc:
        await _blocked(exc)
    return await _ok(result, "local")


@app.get("/auth/me")
async def me(authorization: str = Header(default="")) -> Dict[str, Any]:
    claims = await current_user(authorization)
    u = await authmod.get_by_email(claims["email"])
    if not u or not u["active"]:
        raise HTTPException(status_code=401, detail="account inactive")
    return {"id": u["id"], "email": u["email"], "name": u.get("name"),
            "role": u["role"], "approved": bool(u.get("approved")),
            "auth_sources": list(u.get("auth_sources") or [])}


@app.get("/auth/sso/login")
async def sso_login():
    from fastapi.responses import RedirectResponse

    # `signin_mode=local` means this deployment does not do SSO at all. Answer
    # with a clear 403 rather than a redirect into a realm nobody maintains —
    # and rather than the 400 "oidc disabled" that hides the real reason.
    cfg = await authmod.effective_auth()
    if authmod.signin_mode(cfg) == "local":
        raise HTTPException(
            status_code=403,
            detail="single sign-on is disabled (sign-in mode is local)",
        )

    try:
        state, nonce = authmod.make_state()
        url = await authmod.oidc_authorize_url(state=state)
    except authmod.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    resp = RedirectResponse(url)
    # SameSite=lax, not strict: the browser arrives back here via Keycloak's
    # top-level redirect, and a strict cookie would not be sent on that request.
    resp.set_cookie(
        authmod.SSO_NONCE_COOKIE, nonce,
        max_age=get_settings().oidc_state_ttl_seconds,
        httponly=True, samesite="lax", secure=get_settings().cookie_secure, path="/auth",
    )
    return resp


@app.get("/auth/sso/callback")
async def sso_callback(request: Request, code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse

    nonce = request.cookies.get(authmod.SSO_NONCE_COOKIE, "")
    ip = client_ip(request)
    try:
        # Proves this callback belongs to a login *this browser* started.
        authmod.verify_state(state, nonce)
        # The same nonce is enforced a second time against the id_token's own
        # `nonce` claim, which is what ties the *token* to this login rather
        # than only tying the redirect to this browser.
        result = await authmod.oidc_callback(code, expected_nonce=nonce)
    except authmod.AuthError as exc:
        await authmod.record_auth_event(
            authmod.EV_SSO_FAIL, ip=ip, detail=str(exc))
        raise HTTPException(status_code=401, detail=str(exc))

    await authmod.record_auth_event(
        authmod.EV_SSO_OK, email=result.get("user", {}).get("email"), ip=ip,
        detail="oidc")

    # Hand the token to the SPA in the URL *fragment*. A fragment is never sent
    # to a server, so it stays out of access logs and out of the Referer header
    # on the SPA's next request — unlike the ?sso_token= query param this used to
    # use. The SPA strips it from the address bar immediately on read.
    resp = RedirectResponse(f"/admin#sso_token={result['token']}")
    resp.delete_cookie(authmod.SSO_NONCE_COOKIE, path="/auth")  # single use
    return resp


from app.admin import router as admin_router  # noqa: E402

app.include_router(admin_router, dependencies=[Depends(require_admin)])


# ---- shareable embed preview (public by signed token, NOT by Bearer) ---------
#
# This route is on `app`, not on `admin_router`, and that placement is the whole
# feature. The include_router above puts `require_admin` on EVERY /admin/* path,
# and require_admin reads an Authorization header — which a browser navigating
# to a URL, or an <iframe src=>, never sends. A preview page registered there
# would be unopenable by exactly the people it exists for.
#
# So the only credential here is the token in the query string, minted by
# `POST /admin/embed/preview-link` (super_admin) and verified by
# `_decode_preview_token`, which insists on `purpose == "embed_preview"` — the
# widget's own chat session tokens are HS256 over the SAME secret and would
# otherwise decode cleanly.
_PREVIEW_GONE_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Not found</title></head><body style=\"font:16px/1.6 system-ui,sans-serif;"
    "max-width:640px;margin:15vh auto;padding:0 20px;color:#111\">"
    "<h1>Preview link not available</h1>"
    "<p>This preview link is not valid. Ask your CityAgent administrator for a new one.</p>"
    "</body></html>\n"
)


@app.get("/embed/preview")
async def embed_preview(request: Request, t: str = ""):
    """Render the outlet demo page for a signed, short-lived preview token.

    Missing, malformed, forged, expired and wrong-purpose tokens all answer the
    SAME 404 with the same body. Distinguishing them would turn this route into
    an oracle: "expired" confirms a link once existed for that store, and a
    forger could tell a bad signature from a bad payload while grinding.
    """

    from fastapi.responses import HTMLResponse

    from app.admin import (
        OutletSnippetRequest,
        _decode_preview_token,
        _demo_page,
        _outlet_user,
        _snippet_html,
    )
    from app.security import sign_user

    headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

    claims = _decode_preview_token(t)
    if claims is None:
        return HTMLResponse(_PREVIEW_GONE_HTML, status_code=404, headers=headers)

    # base_url comes from the request, not the token: the page is being served
    # by the very host the widget must call, so it can state its own origin. A
    # base_url claim would be a self-asserted URL we would then have to trust.
    req = OutletSnippetRequest(
        store_id=claims["store_id"],
        embed_id=claims.get("embed_id", ""),
        public_key=claims.get("public_key", ""),
        base_url=str(request.base_url).rstrip("/"),
        title=claims.get("title"),
        accent=claims.get("accent") or "#2F3293",
        stream=bool(claims.get("stream", True)),
    )
    # The same server-side signature the downloadable snippet carries — the
    # store is HMAC-locked, so the preview can answer for that branch and no
    # other, exactly like the real embed.
    snippet = _snippet_html(req, sign_user(_outlet_user(req.store_id)))
    return HTMLResponse(_demo_page(req, snippet), headers=headers)


# ---- the admin SPA -----------------------------------------------------------
#
# Path alone CANNOT tell an SPA deep link from a mistyped API call under /admin:
# /admin/users, /admin/stores, /admin/graph, /admin/conversations and
# /admin/learning are each BOTH a real API route and a real page in
# admin/src/routes/. So a prefix convention (/admin/api/*) or a hardcoded route
# allowlist would either break a page or drift the moment someone adds one.
#
# What does separate them is what the CLIENT asked for. A deep link is a browser
# document navigation: it sends `Sec-Fetch-Dest: document` (every current
# browser) and an Accept that names text/html. An API miss — the SPA's own fetch
# wrapper, curl, a scanner — sends neither; browser fetch() defaults to
# `Accept: */*` and `Sec-Fetch-Dest: empty`. Negotiating on that is the only rule
# here that cannot mislabel a legitimate deep link.
_ADMIN_INDEX = None  # set by _mount_admin() when a build is present

# The SPA shell must never be served from the browser cache without asking us
# first. Vite fingerprints everything under _app/immutable/, so the shell is the
# ONLY file whose contents change while its URL stays the same — and it is the
# file that names which fingerprinted bundle to load. StaticFiles sends an ETag
# and a Last-Modified but no Cache-Control, which leaves a browser free to apply
# heuristic freshness (a fraction of the document's age) and reuse the old shell
# for hours without revalidating. That is how a deploy lands on the server and
# the user still sees the previous console. `no-cache` does not mean "do not
# store" — it means "revalidate every time", so the usual answer is a cheap 304.
_SHELL_CACHE_HEADERS = {"Cache-Control": "no-cache"}

# The fingerprinted assets are the exact opposite: their URL changes whenever
# their contents do, so they can be cached hard and forever.
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _is_document_navigation(headers) -> bool:
    """True when this request is a browser navigating to a page, not a data call.

    Sec-Fetch-Dest wins when present: a browser fetch() to an API route sends
    `empty`, and trusting Accept alone there would hand it the HTML shell.
    Accept is the fallback for clients that send no Sec-Fetch-* at all.
    """

    dest = headers.get("sec-fetch-dest")
    if dest:
        return dest == "document"
    return "text/html" in headers.get("accept", "")


def _is_spa_route_path(path: str) -> bool:
    """A page path under /admin — i.e. not an asset request.

    A request for a *file* (anything with an extension in its last segment) is
    never a page. Keeping it out means a missing .js still 404s instead of
    returning the shell, which fails in confusing ways.
    """

    if path != "/admin" and not path.startswith("/admin/"):
        return False
    return "." not in path.rsplit("/", 1)[-1]


@app.middleware("http")
async def spa_deep_link(request, call_next):
    """Serve the SPA shell for browser navigations under /admin, before routing.

    This runs ahead of the router on purpose, and it is the ONLY reason
    /admin/users survives a cold reload: the API route of the same name is
    registered before the static mount (correctly — the SPA's fetch calls must
    win), so without this a refresh of that page rendered raw `{"detail":...}`
    from require_admin instead of the console.

    Only GET/HEAD navigations are diverted, and only to the public shell, which
    /admin/ already serves unauthenticated — no API route loses its auth check,
    because a fetch() is never `Sec-Fetch-Dest: document`.
    """

    if (
        _ADMIN_INDEX is not None
        and request.method in ("GET", "HEAD")
        and _is_spa_route_path(request.url.path)
        and _is_document_navigation(request.headers)
    ):
        from fastapi.responses import FileResponse

        return FileResponse(_ADMIN_INDEX, headers=_SHELL_CACHE_HEADERS)
    return await call_next(request)


# Serve the built admin SPA at /admin when present (single-deploy option).
def _mount_admin() -> None:
    global _ADMIN_INDEX
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    class SPAStatics(StaticFiles):
        """StaticFiles for the built SPA. Misses 404 — they are API misses.

        This used to fall back to index.html for any extensionless miss, which
        is how `GET /admin/nonexistent-xyz` answered **200 text/html** and how a
        traversal attempt that failed to match the SFTP download route came back
        200 with the admin shell. Nothing leaked, but a scanner reading status
        codes flags it and a broken API call looks like a working page.

        Deep links are handled ahead of routing by spa_deep_link(), which only
        the browser's own navigation headers can trigger, so everything that
        reaches this mount and misses is a data request for something that does
        not exist. It gets the JSON 404 it asked for.

        It also sets Cache-Control, which StaticFiles does not: the shell must
        revalidate on every load, the fingerprinted bundles never need to. See
        _SHELL_CACHE_HEADERS above for why the shell is the one that matters.
        """

        def file_response(self, full_path, stat_result, scope, status_code=200):
            resp = super().file_response(full_path, stat_result, scope, status_code)
            path = scope.get("path", "")
            if "/_app/immutable/" in path:
                resp.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
            elif path.endswith(".html") or path.endswith("/") or "." not in path.rsplit("/", 1)[-1]:
                # html=True serves index.html for a directory request, so the
                # extensionless/trailing-slash cases are the shell too.
                resp.headers["Cache-Control"] = "no-cache"
            return resp

    for cand in (Path(__file__).parent.parent / "admin" / "build",
                 Path("/app/admin_build")):
        if cand.is_dir():
            app.mount("/admin", SPAStatics(directory=str(cand), html=True), name="admin")
            _ADMIN_INDEX = str(cand / "index.html")
            break


_mount_admin()


# ---- request/response models ----------------------------------------------


class SessionCreateRequest(BaseModel):
    embed_id: str
    public_key: str
    user: Optional[Dict[str, Any]] = None
    signature: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_token: str
    expires_in: int


class ChatRequest(BaseModel):
    session_token: str
    message: str
    model: str = ""  # optional chat-model override (must be in the allowlist)
    session_id: str = ""  # stable per-conversation id (drives self-learning memory)


class ChatResponse(BaseModel):
    content: str


# ---- helpers ---------------------------------------------------------------


def detect_lang(text: str) -> str:
    """Return 'MY' if the text contains any Burmese-script char (U+1000..U+109F), else 'EN'. Deterministic, per-message."""
    return "MY" if any("က" <= ch <= "႟" for ch in (text or "")) else "EN"


def _claims(session_token: str) -> Dict[str, Any]:
    """Decode a session token or raise 401."""

    try:
        return decode_session_token(session_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="session expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid session token")


# Per-store "brains": each shop learns into, and answers from, its own memory
# keyed by f"brain:{store_id}", so one shop (or a test batch) can't poison
# another. Unscoped sessions (no store_id) fall back to this shared global brain.
SHARED_BRAIN_ID = "citcare-shared-brain"


def _learn_ids(claims: Dict[str, Any], session_id: str) -> tuple[str, str]:
    """Derive (user_id, session_id) for Agno self-learning.

    user_id is per-store (f"brain:{store_id}") so each shop keeps its OWN memory
    that its answers read from and write to — one shop's learning can't leak into
    or poison another's. Unscoped sessions fall back to SHARED_BRAIN_ID.
    session_id stays per-conversation so chat history context doesn't bleed
    across chats.
    """

    store_id = claims.get("store_id")
    user_id = f"brain:{store_id}" if store_id else SHARED_BRAIN_ID
    sid = session_id or user_id
    return user_id, sid


def _scoped_message(message: str, store_id: Optional[str]) -> str:
    """Prefix a deterministic, per-message language directive (computed from the
    ORIGINAL user message's script) plus, when scoped, store context so the model
    treats 'here'/'this store' as the scoped branch and answers without asking
    which site (tool scope still enforces it)."""

    if detect_lang(message) == "MY":
        lang_directive = (
            "[Reply ONLY in Burmese (မြန်မာ). The user's message is in Burmese, "
            "so your entire answer must be in Burmese, regardless of any remembered preference.]"
        )
    else:
        lang_directive = (
            "[Reply ONLY in English. The user's message is in English, "
            "so your entire answer must be in English, regardless of any remembered preference.]"
        )

    lines = [lang_directive]
    if store_id:
        lines.append(
            f"[Context: you are assisting pharmacy store {store_id}. "
            f"'here', 'this store', 'my branch' all mean {store_id}. "
            f"Answer for this store; do not ask which site.]"
        )
    return "\n".join(lines) + f"\n\n{message}"


async def _remember(
    client_session: Optional[str],
    model: Optional[str],
    session_id: Optional[str],
    user_id: Optional[str],
    question: str,
    answer: str,
) -> None:
    """Record a turn that no agent run wrote for us.

    The fast path answers with a tool-less phrasing agent, and a cache hit runs
    no agent at all — neither leaves a trace in the conversation. Without this,
    those turns are invisible to the next one, and "which other shop has it?"
    has nothing to resolve "it" against.

    Only for real conversations; a client without a session_id has no history to
    keep. Best-effort: ``record_turn`` never raises.
    """

    if not _conversational(client_session):
        return
    from app.history import record_turn

    # Write through the SAME agent that will later read the history back.
    await record_turn(
        get_agent(model, with_history=True), session_id, user_id, question, answer
    )


def _conversational(client_session: Optional[str]) -> bool:
    """True when this client keeps a multi-turn conversation we must preserve.

    Only a real, client-supplied session id counts. The embed widget sends none,
    so it stays single-turn — and keeps the fast path.
    """

    return bool(client_session) and get_settings().history_enabled


async def _is_follow_up(client_session: Optional[str]) -> bool:
    """True when this is turn 2+ of a real client conversation.

    Clients that send no ``session_id`` (the embed widget today) never have
    history, so every one of their turns is a first turn. Redis errors resolve to
    False: a missed cache is cheap, a cross-conversation cache hit is not — but
    the safe direction here is to treat the turn as fresh and self-contained.
    """

    if not client_session or not get_settings().history_enabled:
        return False
    try:
        return await bump_session_turn(client_session) > 1
    except Exception:   # noqa: BLE001 — Redis must never break chat
        logger.exception("Session turn counter failed; treating as first turn")
        return False


from app.activity import METRIC_FIELDS as _METRIC_FIELDS  # noqa: E402
from app.activity import NO_METRICS as _NO_METRICS  # noqa: E402
from app.activity import extract_metrics as _extract_metrics  # noqa: E402


# ---- chat_logs writes, with or without the metric columns --------------------
#
# `log_chat` lives in app/admin.py, which is owned elsewhere. This wrapper lets
# the two halves of the token/cost change deploy in either order:
#
#   * against a `log_chat` that takes the metric kwargs, they are passed through
#     and land in the 0006 columns;
#   * against the current one, they are dropped here and the turn is logged
#     exactly as before.
#
# The decision is made by INSPECTING the signature once, not by catching a
# TypeError from the call. `log_chat` swallows its own exceptions, so a TypeError
# raised inside it would be indistinguishable from a signature mismatch, and
# retrying a call that had already written a row would double every turn.

_LOG_CHAT_TAKES_METRICS: Optional[bool] = None


def _log_chat_takes_metrics() -> bool:
    global _LOG_CHAT_TAKES_METRICS
    if _LOG_CHAT_TAKES_METRICS is None:
        import inspect

        from app.admin import log_chat

        try:
            params = inspect.signature(log_chat).parameters
            _LOG_CHAT_TAKES_METRICS = "total_tokens" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except Exception:  # noqa: BLE001
            _LOG_CHAT_TAKES_METRICS = False
        if not _LOG_CHAT_TAKES_METRICS:
            logger.info(
                "admin.log_chat does not accept token/cost kwargs yet; per-turn "
                "metrics are captured but not persisted"
            )
    return _LOG_CHAT_TAKES_METRICS


async def _log_turn(*args, metrics: Optional[Dict[str, Any]] = None, **kwargs) -> None:
    """`admin.log_chat`, plus the six metric columns when it will take them.

    Then the turn's per-call detail. ``tool_calls`` and ``llm_calls`` both carry
    a foreign key to ``chat_logs(id)``, so nothing captured DURING the turn has
    a row to point at until this one is written — which is why capture buffers
    in memory (``activity.begin_turn``) and is flushed from here, once, with the
    real id that ``log_chat`` now returns.

    ⚠️ **This is the only place the buffer is flushed, and `begin_turn` is the
    only place it is opened.** Miss either and the capture layer is dead code
    that fails silently: `record_tool_call` returns immediately on an empty
    contextvar, so `tool_calls` simply stays empty and the Diagnostics tab
    renders a clean, permanently blank page with no error anywhere.

    ⚠️ Everything after ``log_chat`` is analytics and must never cost an answer.
    ``flush_turn`` is fire-and-forget and swallows its own errors.
    """

    from app import activity
    from app.admin import log_chat

    if metrics and _log_chat_takes_metrics():
        kwargs.update({k: v for k, v in metrics.items() if k in _METRIC_FIELDS})
    turn_id = await log_chat(*args, **kwargs)
    # args are (question, answer, store_id, cached, latency_ms) — log_chat's
    # positional contract. The answer is what the USER SAW, which is the only
    # thing that can tell us whether the turn gave up.
    activity.flush_turn(turn_id, answer=args[1] if len(args) > 1 else None)


async def _answer(
    message: str,
    store_id: Optional[str],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    client_session: Optional[str] = None,
):
    """Run the agent for one message, force-scoped to ``store_id``.

    Returns ``(content, was_cached, run_metrics)``. Checks the Redis query cache
    first; on a miss runs the agent and caches the answer. ``user_id``/
    ``session_id`` (when given) drive Agno self-learning memory. ``model``
    selects the chat model.

    ``run_metrics`` is the six-key dict from ``activity.extract_metrics`` for
    whichever model call actually happened. **On a cache hit every value is
    None** — no model ran, and `path='cache'` already records why. Filling those
    columns with 0 there would make a month of free cache hits read as a month
    of zero-cost model calls.

    ``client_session`` is the session id the CLIENT sent, if any — distinct from
    ``session_id``, which ``_learn_ids`` may have defaulted to a shared value.
    Only a real client session gets conversation history, and only its first turn
    may use the shared answer cache.
    """

    from app import activity, metrics

    # Open the capture buffer, but ONLY if nothing upstream already did.
    # `begin_turn` RESETS rather than appends — deliberately, so a turn can
    # never be credited with a previous turn's calls. The consequence is that
    # calling it unconditionally here would DISCARD whatever the streaming path
    # captured after it opened the turn in `chat_stream.gen`. This is the entry
    # point for the non-streaming route only; for the streaming route it is a
    # continuation, so it checks rather than resets.
    if activity.current_turn() is None:
        activity.begin_turn()

    follow_up = await _is_follow_up(client_session)

    # A follow-up ("which other shop has it?") is meaningless without its
    # history, and the cache key contains no history — so it must not be read
    # from or written to the shared cache.
    if not follow_up:
        cached = await get_cached_answer(message, store_id, model)
        if cached is not None:
            # Answers cached before the leak filter shipped can still contain
            # reasoning, and they survive for a full TTL. Filter on read too.
            # Entries cached before the policy shipped still carry the line
            # on stock answers, and survive a full TTL.
            cached = apply_disclaimer(filter_answer(cached) or cached)
            metrics.incr("cache_hits")
            # A cache hit runs no agent, so nothing would record this turn and
            # the NEXT turn would not know it happened.
            await _remember(client_session, model, session_id, user_id, message, cached)
            # No model ran: every metric column stays NULL. See the docstring.
            return cached, True, dict(_NO_METRICS)
    metrics.incr("cache_misses")

    # Pin the data version we are about to answer against. If an ingest lands
    # while the agent is thinking, set_cached_answer drops the answer instead of
    # filing stale stock under the new version.
    version = await get_data_version()

    # The fast path's phrasing agent has no db and no session, so it records
    # nothing. A follow-up also names no drug, so it could not be resolved from
    # this message alone. Hence: fast path only for self-contained turns, and we
    # write the turn into the conversation ourselves afterwards.
    # Operator-selected answer length (crisp/standard/detailed), applied to both
    # the fast-path phrasing and the full agent. A change bumps data_version at
    # the admin layer, so this never serves an old-style answer from cache.
    style = await cache_mod.get_answer_style()

    if get_settings().fast_path_enabled and not follow_up:
        facts = await fastpath.answer(message, store_id)
        if facts is not None:
            phrase_prompt = fastpath.build_phrasing_input(
                _scoped_message(message, store_id), facts, style
            )
            metrics.incr("llm_calls")
            metrics.record_llm()
            out = await fastpath.get_phrasing_agent(model).arun(phrase_prompt)
            # Tokens + cost of the ONE phrasing call this path makes. Extraction
            # is defensive by construction (activity.extract_metrics never
            # raises), so a renamed agno attribute costs a metric, not an answer.
            run_metrics = _extract_metrics(out)
            # The fast path answers only HOT_HAVE / HOT_WHERE — stock and
            # price — so the safety line never belongs on it.
            raw = getattr(out, "content", str(out))
            content = apply_disclaimer(filter_answer(raw), force=False)
            # Never cache an answer that showed reasoning. See the note in
            # answer_filter.contains_reasoning: caching one turns a rare glitch
            # into the permanent reply for that question.
            if not contains_reasoning(raw):
                await set_cached_answer(message, store_id, content, model=model, version=version)
            await _remember(client_session, model, session_id, user_id, message, content)
            return content, False, run_metrics

    prompt = _scoped_message(message, store_id)
    token = set_store_scope(store_id)
    run_kw: Dict[str, Any] = {}
    if user_id:
        run_kw["user_id"] = user_id
    if session_id:
        run_kw["session_id"] = session_id
    try:
        metrics.incr("llm_calls")
        metrics.record_llm()
        out = await get_agent(model, with_history=bool(client_session), style=style).arun(prompt, **run_kw)
        # The full agent may make several provider calls in its tool loop;
        # RunMetrics is the aggregate over the whole run, which is the number
        # this turn actually cost.
        run_metrics = _extract_metrics(out)
        raw_answer = getattr(out, "content", str(out))
        content = apply_disclaimer(filter_answer(raw_answer))
    finally:
        reset_store_scope(token)

    if not follow_up and not contains_reasoning(raw_answer):
        await set_cached_answer(message, store_id, content, model=model, version=version)
    return content, False, run_metrics


# ---- routes ----------------------------------------------------------------


@app.get("/")
async def root():
    """Root goes to the admin console — the one UI. The old standalone test chat
    (``static/index.html``) confused operators into thinking it was a second app;
    the embed widget is tested from the Embed page, not here."""

    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/admin/", status_code=307)


@app.get("/api/embed/widget.js")
async def widget_js():
    """Serve the drop-in embed widget script (script-tag integration)."""

    from pathlib import Path

    from fastapi.responses import Response

    js = (Path(__file__).parent / "static" / "widget.js").read_text(encoding="utf-8")
    return Response(content=js, media_type="application/javascript")


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> Dict[str, Any]:
    """Which build is this, and what changed in it.

    PUBLIC on purpose, and therefore deliberately thin: version, build stamp,
    and the newest release note only. Whoever is debugging a deployment needs
    to read it without an admin token — including on the CMHL host we cannot
    reach — and "which version are you on?" has been unanswerable until now.

    The full history is admin-only (`GET /admin/releases`). A version string
    tells an attacker nothing they could not infer from behaviour; the full
    changelog tells them which fixes are absent, which is worth a token.
    """

    from app import release_notes
    from app.version import version_info

    return {**version_info(), "latest_release": release_notes.latest()}


# ---- white-label branding (public) ------------------------------------------
#
# PUBLIC, like `/auth/config`, and for the same reason: the login screen renders
# BEFORE any token exists, and it renders from this. Requiring auth would mean
# the product could only be named once you were already signed in to it.
#
# Nothing here is sensitive — it is the text and the logos every visitor to the
# sign-in page already sees.


@app.get("/brand")
async def brand_document() -> Dict[str, Any]:
    """Branding for the login screen and the console chrome.

    ``version`` is one hash over the text config AND every asset's content hash,
    so the SPA cache-busts its whole brand surface with a single value — a
    replaced logo moves it just as a renamed product does.

    **This endpoint must never 500.** It is on the hot path of every sign-in
    render, so a Postgres blip here would blank the login page of a deployment
    whose database is otherwise fine, and nobody could sign in to fix it. Any
    failure degrades to the shipped defaults (``version: "default"``), which is
    exactly how the product looked before branding existed. The
    defaults-on-error path lives in ``app.brand.public_document``.
    """

    from app import brand

    return await brand.public_document()


@app.get("/brand/asset/{key}")
async def brand_asset(key: str):
    """Serve one stored logo. Public, immutable, and never sniffed.

    Three headers are load-bearing:

    * ``Cache-Control: immutable`` for a year is safe ONLY because the URL the
      document hands out carries the content hash (``?v=<sha256>``). Replacing a
      logo produces a different URL; nothing ever has to expire.
    * ``Content-Type`` comes from the STORED mime, which was decided from the
      file's magic bytes at upload — not from the filename and not from what the
      uploader's browser claimed.
    * ``X-Content-Type-Options: nosniff`` stops a browser from second-guessing
      that and executing the bytes as something else. Belt and braces with the
      PNG/JPEG-only rule in ``app.brand.validate_image``: this is served
      same-origin with the admin console, so a file that renders as script here
      reaches a super_admin's ``localStorage`` token.
    """

    from fastapi.responses import Response

    from app import brand

    row = await brand.get_asset(key)
    if not row:
        raise HTTPException(status_code=404, detail="asset not set")
    ext = brand.extension_for(row["mime"])
    return Response(
        content=bytes(row["bytes"]),
        media_type=row["mime"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{key}.{ext}"',
        },
    )


@app.get("/api/embed/models")
async def list_models() -> Dict[str, Any]:
    """Selectable chat models for the in-app picker (id, name, price)."""

    from app.agent import SELECTABLE_MODELS

    settings = get_settings()
    return {"default": settings.openrouter_model, "models": SELECTABLE_MODELS}


@app.get("/ready")
async def ready() -> Dict[str, Any]:
    """Readiness: Postgres reachable + row counts."""

    try:
        await get_pool()
        c = await counts()
        version = await get_data_version()
        # Row counts alone cannot distinguish "5,292 products" from "5,292
        # nameless codes" — the state behind the 2026-07-28 field reports, where
        # every catalog row was a stub and the agent answered "not found" for
        # stocked products. Surface the stub ratio so a health check can see it.
        from app.ingest import catalog_health

        health = await catalog_health()
        return {
            "status": "ready",
            "data_version": version,
            **c,
            "catalog_health": health,
            **({} if health["healthy"] else {"warning": "catalog is mostly stub rows"}),
        }
    except Exception as exc:  # noqa: BLE001 - surface as 503
        raise HTTPException(status_code=503, detail=f"not ready: {exc}")


@app.post("/api/embed/reload")
async def reload_data() -> Dict[str, Any]:
    """Reload data from the data dir and bust cache. BOTH kinds replace.

    Loads whatever article/balance xlsx are present in the configured data dir,
    then bumps the data version so all cached answers miss. Catalog is
    ``full_sync`` — rows the file omits are deleted, not kept; the "article
    merge" this docstring used to claim has not been the behaviour since
    2026-08-02. Inventory is truncate-and-reload.

    A missing file is skipped and an empty one is refused by ``ingest_inventory``
    / ``ingest_catalog``, so neither can empty a table through this endpoint.
    """

    result = await reload_from_data_dir()
    version = await bump_data_version()
    return {"status": "reloaded", "data_version": version, **result}


@app.post("/api/embed/ingest")
async def ingest_now() -> Dict[str, Any]:
    """Manually trigger ingestion of files sitting in the SFTP incoming dir.

    Processes any uploaded article/balance xlsx immediately (the watcher also
    does this automatically on its poll interval), then reports what happened.
    """

    from app.watcher import scan_once

    summary = await scan_once(stable_only=False)
    version = await get_data_version()
    return {"status": "ingested", "data_version": version, **summary}


@app.post("/api/embed/session/create", response_model=SessionCreateResponse)
async def session_create(req: SessionCreateRequest) -> SessionCreateResponse:
    """Verify the (optional) signed user and mint a short-lived session token.

    Public mode: no ``user`` -> unscoped session. HMAC mode: ``user`` +
    ``signature`` are verified; ``user.store_id`` becomes the locked store.
    """

    settings = get_settings()

    if not await is_valid_credential(req.embed_id, req.public_key):
        raise HTTPException(status_code=403, detail="invalid embed credentials")

    store_id: Optional[str] = None

    if req.user is not None:
        if not verify_user_signature(req.user, req.signature or ""):
            raise HTTPException(status_code=401, detail="bad user signature")
        store_id = (
            str(req.user.get("store_id"))
            if req.user.get("store_id") is not None
            else None
        )

    minted = create_session_token(
        user_id=(str(req.user.get("id")) if req.user else None),
        store_id=store_id,
        embed_id=req.embed_id,
        ttl_seconds=settings.session_ttl_seconds,
    )
    return SessionCreateResponse(**minted)


@app.post("/api/embed/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Blocking chat — returns the full answer, scoped to the token's store."""

    import time as _t

    claims = _claims(req.session_token)
    rl_id = claims.get("uid") or claims.get("store_id") or claims.get("embed_id") or "anon"
    if not await check_rate_limit(str(rl_id)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    store_id = claims.get("store_id")
    user_id, session_id = _learn_ids(claims, req.session_id)
    t0 = _t.time()
    content, was_cached, run_metrics = await _answer(
        req.message, store_id, user_id, session_id, req.model,
        client_session=req.session_id,
    )
    # Audit attribution: this endpoint knows who asked (the embed credential on
    # the session token), in which conversation, and with which model. It does
    # NOT know whether _answer() went through the fast path or the full agent —
    # that would mean changing what _answer returns — so `path` is recorded only
    # for the one route it can prove, the cache hit. A guess would be worse than
    # a NULL: NULL reads as "unattributed", "agent" reads as a fact.
    await _log_turn(
        req.message, content, store_id, was_cached, int((_t.time() - t0) * 1000),
        embed_id=claims.get("embed_id"),
        session_id=req.session_id or None,
        model=req.model or None,
        path="cache" if was_cached else None,
        metrics=run_metrics,
    )
    return ChatResponse(content=content)


@app.post("/api/embed/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Streaming chat over SSE — token deltas plus agent-activity step events."""

    claims = _claims(req.session_token)
    rl_id = claims.get("uid") or claims.get("store_id") or claims.get("embed_id") or "anon"
    if not await check_rate_limit(str(rl_id)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    store_id = claims.get("store_id")
    user_id, session_id = _learn_ids(claims, req.session_id)

    async def gen():
        import time as _t

        from app import activity, metrics

        # Open this turn's capture buffer BEFORE anything can call a tool or a
        # model. `record_tool_call` no-ops on an empty contextvar, so opening
        # late does not error — it silently drops the calls made before this
        # line, and the loss is invisible in every log.
        activity.begin_turn()

        t0 = _t.time()
        # A follow-up needs its conversation; the cache key has none. See _answer().
        follow_up = await _is_follow_up(req.session_id)
        if not follow_up:
            cached = await get_cached_answer(req.message, store_id, req.model)
            if cached is not None:
                metrics.incr("cache_hits")
                yield f"data: {json.dumps({'delta': cached})}\n\n"
                # No agent, no model call, no tools — so `tools`/`model` stay
                # NULL rather than echoing the model that was merely REQUESTED,
                # and every metric column stays NULL for the same reason. A 0
                # here would read as "this turn cost nothing to generate", which
                # is a claim about a model call that never happened.
                await _log_turn(
                    req.message, cached, store_id, True, int((_t.time() - t0) * 1000),
                    embed_id=claims.get("embed_id"),
                    session_id=req.session_id or None,
                    path="cache",
                )
                # No agent ran; record the turn or the next one cannot see it.
                await _remember(
                    req.session_id, req.model, session_id, user_id, req.message, cached
                )
                yield "data: [DONE]\n\n"
                return
        metrics.incr("cache_misses")

        # A one-line plan up front (template, no LLM call) — the agentic "here's
        # what I'll do" beat before any tool runs. Additive SSE event; consumers
        # that don't know it (the embed widget) ignore it.
        plan = _plan_line(req.message)
        if plan:
            yield f"event: plan\ndata: {json.dumps({'text': plan})}\n\n"

        # See _answer(): pin the version we answer against, so an ingest that
        # lands mid-stream invalidates this answer instead of being masked by it.
        version = await get_data_version()

        # Operator-selected answer length; see _answer().
        style = await cache_mod.get_answer_style()

        # See _answer(): fast path only for self-contained turns; we record the
        # turn ourselves afterwards, since its phrasing agent never does.
        if get_settings().fast_path_enabled and not follow_up:
            facts = await fastpath.answer(req.message, store_id)
            if facts is not None:
                _detail = facts.get("brand_name") or facts.get("mention") or ""
                step = json.dumps({
                    "label": facts["tool"], "icon": "search",
                    "args": {"name": _detail} if _detail else {},
                })
                yield f"event: step\ndata: {step}\n\n"
                rows = fastpath.result_rows(facts)
                if rows:
                    # `subject` is additive: the hot tools (get_stock,
                    # find_at_other_stores) select only site_code/site_name/
                    # stock_qty, so the drug the answer is ABOUT appears in no
                    # row. The UI needs it to offer follow-up questions.
                    frame: dict = _result_frame(facts["tool"], rows)
                    if facts.get("article_code"):
                        frame["subject"] = {
                            "code": facts["article_code"],
                            "name": facts.get("brand_name") or facts["article_code"],
                        }
                    yield f"event: result\ndata: {json.dumps(frame)}\n\n"
                phrase_prompt = fastpath.build_phrasing_input(
                    _scoped_message(req.message, store_id), facts, style
                )
                metrics.incr("llm_calls")
                metrics.record_llm()
                full = ""
                # The phrasing agent has no tools, so it has least reason to
                # narrate — but Feedback 5 leaked from exactly this route.
                leak = LeakFilter()
                # A streamed run returns no RunOutput, so usage arrives on an
                # event instead — RunCompletedEvent carries `.metrics`. Taking
                # the last event that has any means we do not have to name the
                # event class here and break on an agno rename.
                run_metrics = dict(_NO_METRICS)
                try:
                    async for event in fastpath.get_phrasing_agent(req.model).arun(
                        phrase_prompt, stream=True, stream_events=True,
                    ):
                        if getattr(event, "metrics", None) is not None:
                            run_metrics = _extract_metrics(event)
                        if type(event).__name__ == "RunContentEvent":
                            delta = getattr(event, "content", None)
                            if isinstance(delta, str) and delta:
                                safe = leak.feed(delta)
                                if safe:
                                    full += safe
                                    yield f"data: {json.dumps({'delta': safe})}\n\n"
                    # Fast path answers only stock and price, so force removal.
                    emit, full = _finish_stream(full, leak.flush(), force=False)
                    if emit:
                        yield f"data: {json.dumps({'delta': emit})}\n\n"
                    if leak.leaked:
                        metrics.incr("reasoning_leaks_suppressed")
                        logger.warning(
                            "fast path: suppressed leaked reasoning (%s)", leak.dropped[:1]
                        )
                        if not full.strip():
                            full = fallback_for(" ".join(leak.dropped))
                            yield f"data: {json.dumps({'delta': full})}\n\n"
                except Exception as exc:  # noqa: BLE001
                    yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
                # Logged unconditionally, OUTSIDE `if full` — a turn that
                # produced nothing is the most interesting row in an audit log
                # and used to write no row at all, so the silences were
                # invisible. Caching and remembering stay guarded: an empty
                # answer must never be served again or replayed as history.
                await _log_turn(
                    req.message, full, store_id, False, int((_t.time() - t0) * 1000),
                    embed_id=claims.get("embed_id"),
                    session_id=req.session_id or None,
                    model=req.model or None,
                    tools=[facts["tool"]] if facts.get("tool") else [],
                    path="fast_path",
                    metrics=run_metrics,
                )
                if full:
                    await set_cached_answer(
                        req.message, store_id, full, model=req.model, version=version
                    )
                    await _remember(
                        req.session_id, req.model, session_id, user_id, req.message, full
                    )
                yield "data: [DONE]\n\n"
                return

        scope = set_store_scope(store_id)
        full = ""
        # This is the route that leaked in Feedback 7, 10, 11 and 12 — the
        # tool-using agent narrating its search strategy, tool names and all.
        leak = LeakFilter()
        # Last drug seen in a tool result, carried across steps: get_stock and
        # find_at_other_stores return branch rows only, so a turn that ends on
        # one of them would otherwise name no subject at all.
        subject: Optional[Dict[str, str]] = None
        # Which tools this turn actually called, in call order. They were already
        # being computed for `event: step` and thrown away the moment the frame
        # was yielded, so the audit log could say a turn happened but never which
        # retrieval mode answered it. Collected here, written to chat_logs.tools;
        # the SSE frames below are untouched (the wire contract is frozen).
        tools_used: List[str] = []
        # Usage for the whole run, off the terminal stream event. Initialised to
        # all-NULL so a turn that errors before any event still logs honestly
        # ("unknown") rather than logging a zero-cost turn.
        run_metrics = dict(_NO_METRICS)
        metrics.incr("llm_calls")
        metrics.record_llm()
        try:
            async for event in get_agent(
                req.model, with_history=bool(req.session_id), style=style
            ).arun(
                _scoped_message(req.message, store_id),
                stream=True,
                stream_events=True,
                user_id=user_id,
                session_id=session_id,
            ):
                name = type(event).__name__
                if getattr(event, "metrics", None) is not None:
                    run_metrics = _extract_metrics(event)
                if name == "ToolCallStartedEvent":
                    tobj = getattr(event, "tool", None)
                    tool = getattr(tobj, "tool_name", "") or ""
                    if tool:
                        tools_used.append(tool)
                    detail = _step_detail(getattr(tobj, "tool_args", None))
                    frame = json.dumps({
                        "label": tool or "Searching", "icon": "search",
                        "args": {"detail": detail} if detail else {},
                    })
                    yield f"event: step\ndata: {frame}\n\n"
                elif name == "ToolCallCompletedEvent":
                    # forward the structured tool result (list of rows) so the UI
                    # can render the data the agent actually saw.
                    tool_obj = getattr(event, "tool", None)
                    tname = getattr(tool_obj, "tool_name", "") or ""
                    raw = getattr(tool_obj, "result", None)
                    rows = None
                    if isinstance(raw, str) and raw.strip().startswith(("[", "{")):
                        try:
                            rows = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            try:
                                import ast

                                rows = ast.literal_eval(raw)
                            except Exception:  # noqa: BLE001
                                rows = None
                    if isinstance(rows, dict):
                        rows = [rows]
                    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                        subject = _subject_of(rows, getattr(tool_obj, "tool_args", None)) or subject
                        payload: dict = _result_frame(tname, rows)
                        if subject:
                            payload["subject"] = subject
                        yield f"event: result\ndata: {json.dumps(payload)}\n\n"
                elif name == "RunContentEvent":
                    delta = getattr(event, "content", None)
                    if isinstance(delta, str) and delta:
                        safe = leak.feed(delta)
                        if safe:
                            full += safe
                            yield f"data: {json.dumps({'delta': safe})}\n\n"
            emit, full = _finish_stream(full, leak.flush())
            if emit:
                yield f"data: {json.dumps({'delta': emit})}\n\n"
            if leak.leaked:
                metrics.incr("reasoning_leaks_suppressed")
                logger.warning(
                    "agent stream: suppressed leaked reasoning (%s)", leak.dropped[:1]
                )
                if not full.strip():
                    full = fallback_for(" ".join(leak.dropped))
                    yield f"data: {json.dumps({'delta': full})}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
        finally:
            reset_store_scope(scope)
            # Unconditional, and before the cache write: an errored or empty turn
            # is exactly the row an audit log must not be missing. `if full:`
            # meant every failed answer left NO trace at all, so the log could
            # only ever show the system working. Caching stays guarded by `full`.
            await _log_turn(
                req.message, full, store_id, False, int((_t.time() - t0) * 1000),
                embed_id=claims.get("embed_id"),
                session_id=req.session_id or None,
                model=req.model or None,
                tools=tools_used,
                path="agent",
                metrics=run_metrics,
            )
            if full and not follow_up:
                await set_cached_answer(
                    req.message, store_id, full, model=req.model, version=version
                )
            yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
