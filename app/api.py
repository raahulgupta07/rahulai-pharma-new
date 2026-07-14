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
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from contextlib import asynccontextmanager

from app.agent import get_agent
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


def _subject_of(rows: list, tool_args) -> Optional[Dict[str, str]]:
    """The drug an answer is ABOUT — code + display name — or ``None``.

    Sent as the additive ``subject`` field on an SSE ``result`` frame so the UI
    can offer follow-up questions ("price of X?", "substitutes for X?"). It
    cannot be read off the rows alone: ``get_stock`` and ``find_at_other_stores``
    select ``site_code, site_name, stock_qty`` — branch rows, no drug. The row
    scan below therefore falls back to the tool's own ``code`` argument.
    """

    for row in rows[:8]:
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
    try:
        from app.auth import ensure_users_table, seed_super_admin

        await ensure_users_table()
        await seed_super_admin()
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


app = FastAPI(title="CitCare Pharmacy Agent", lifespan=lifespan)


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


@app.post("/auth/login")
async def login(req: LoginRequest) -> Dict[str, Any]:
    """Local login; falls back to LDAP (email as username) if enabled."""

    try:
        return await authmod.login_local(req.email, req.password)
    except authmod.AuthError:
        if get_settings().ldap_enabled:
            try:
                return await authmod.login_ldap(req.email, req.password)
            except authmod.AuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc))
        raise HTTPException(status_code=401, detail="invalid credentials")


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

    try:
        # Proves this callback belongs to a login *this browser* started.
        authmod.verify_state(state, request.cookies.get(authmod.SSO_NONCE_COOKIE, ""))
        result = await authmod.oidc_callback(code)
    except authmod.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    # Hand the token to the SPA in the URL *fragment*. A fragment is never sent
    # to a server, so it stays out of access logs and out of the Referer header
    # on the SPA's next request — unlike the ?sso_token= query param this used to
    # use. The SPA strips it from the address bar immediately on read.
    resp = RedirectResponse(f"/admin#sso_token={result['token']}")
    resp.delete_cookie(authmod.SSO_NONCE_COOKIE, path="/auth")  # single use
    return resp


from app.admin import router as admin_router  # noqa: E402

app.include_router(admin_router, dependencies=[Depends(require_admin)])


# Serve the built admin SPA at /admin when present (single-deploy option).
def _mount_admin() -> None:
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException

    class SPAStatics(StaticFiles):
        """StaticFiles that serves index.html for unknown paths.

        The build is a client-routed SPA: adapter-static emits one index.html
        and no per-route file. Plain StaticFiles 404s on a deep link like
        /admin/chat, so the app only worked while you never reloaded. Client
        routes are indistinguishable from typos here, so every miss falls back
        to the shell and the router sorts it out.

        The /admin/* API routes are registered on `app` before this mount, so
        they take precedence and never reach this fallback.
        """

        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != 404:
                    raise
                # Never mask a missing asset as the shell — a 200 text/html for
                # a .js request breaks in confusing ways.
                if "." in path.rsplit("/", 1)[-1]:
                    raise
                return await super().get_response("index.html", scope)

    for cand in (Path(__file__).parent.parent / "admin" / "build",
                 Path("/app/admin_build")):
        if cand.is_dir():
            app.mount("/admin", SPAStatics(directory=str(cand), html=True), name="admin")
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


async def _answer(
    message: str,
    store_id: Optional[str],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    client_session: Optional[str] = None,
):
    """Run the agent for one message, force-scoped to ``store_id``.

    Returns ``(content, was_cached)``. Checks the Redis query cache first; on a
    miss runs the agent and caches the answer. ``user_id``/``session_id`` (when
    given) drive Agno self-learning memory. ``model`` selects the chat model.

    ``client_session`` is the session id the CLIENT sent, if any — distinct from
    ``session_id``, which ``_learn_ids`` may have defaulted to a shared value.
    Only a real client session gets conversation history, and only its first turn
    may use the shared answer cache.
    """

    from app import metrics

    follow_up = await _is_follow_up(client_session)

    # A follow-up ("which other shop has it?") is meaningless without its
    # history, and the cache key contains no history — so it must not be read
    # from or written to the shared cache.
    if not follow_up:
        cached = await get_cached_answer(message, store_id, model)
        if cached is not None:
            metrics.incr("cache_hits")
            # A cache hit runs no agent, so nothing would record this turn and
            # the NEXT turn would not know it happened.
            await _remember(client_session, model, session_id, user_id, message, cached)
            return cached, True
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
            content = getattr(out, "content", str(out))
            await set_cached_answer(message, store_id, content, model=model, version=version)
            await _remember(client_session, model, session_id, user_id, message, content)
            return content, False

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
        content = getattr(out, "content", str(out))
    finally:
        reset_store_scope(token)

    if not follow_up:
        await set_cached_answer(message, store_id, content, model=model, version=version)
    return content, False


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
        return {"status": "ready", "data_version": version, **c}
    except Exception as exc:  # noqa: BLE001 - surface as 503
        raise HTTPException(status_code=503, detail=f"not ready: {exc}")


@app.post("/api/embed/reload")
async def reload_data() -> Dict[str, Any]:
    """Reload data from the data dir (article merge + inventory replace) and bust cache.

    Loads whatever article/balance xlsx are present in the configured data dir,
    then bumps the data version so all cached answers miss. Missing files are
    skipped safely (existing data is preserved).
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

    from app.admin import log_chat

    claims = _claims(req.session_token)
    rl_id = claims.get("uid") or claims.get("store_id") or claims.get("embed_id") or "anon"
    if not await check_rate_limit(str(rl_id)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    store_id = claims.get("store_id")
    user_id, session_id = _learn_ids(claims, req.session_id)
    t0 = _t.time()
    content, was_cached = await _answer(
        req.message, store_id, user_id, session_id, req.model,
        client_session=req.session_id,
    )
    await log_chat(req.message, content, store_id, was_cached, int((_t.time() - t0) * 1000))
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

        from app.admin import log_chat
        from app import metrics

        t0 = _t.time()
        # A follow-up needs its conversation; the cache key has none. See _answer().
        follow_up = await _is_follow_up(req.session_id)
        if not follow_up:
            cached = await get_cached_answer(req.message, store_id, req.model)
            if cached is not None:
                metrics.incr("cache_hits")
                yield f"data: {json.dumps({'delta': cached})}\n\n"
                await log_chat(req.message, cached, store_id, True, int((_t.time() - t0) * 1000))
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
                    frame: dict = {"tool": facts["tool"], "rows": rows[:8]}
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
                try:
                    async for event in fastpath.get_phrasing_agent(req.model).arun(
                        phrase_prompt, stream=True, stream_events=True,
                    ):
                        if type(event).__name__ == "RunContentEvent":
                            delta = getattr(event, "content", None)
                            if isinstance(delta, str) and delta:
                                full += delta
                                yield f"data: {json.dumps({'delta': delta})}\n\n"
                except Exception as exc:  # noqa: BLE001
                    yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
                if full:
                    await log_chat(req.message, full, store_id, False, int((_t.time() - t0) * 1000))
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
        # Last drug seen in a tool result, carried across steps: get_stock and
        # find_at_other_stores return branch rows only, so a turn that ends on
        # one of them would otherwise name no subject at all.
        subject: Optional[Dict[str, str]] = None
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
                if name == "ToolCallStartedEvent":
                    tobj = getattr(event, "tool", None)
                    tool = getattr(tobj, "tool_name", "") or ""
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
                        payload: dict = {"tool": tname, "rows": rows[:8]}
                        if subject:
                            payload["subject"] = subject
                        yield f"event: result\ndata: {json.dumps(payload)}\n\n"
                elif name == "RunContentEvent":
                    delta = getattr(event, "content", None)
                    if isinstance(delta, str) and delta:
                        full += delta
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
        finally:
            reset_store_scope(scope)
            if full:
                await log_chat(req.message, full, store_id, False, int((_t.time() - t0) * 1000))
                if not follow_up:
                    await set_cached_answer(
                        req.message, store_id, full, model=req.model, version=version
                    )
            yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
