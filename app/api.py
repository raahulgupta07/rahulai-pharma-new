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

import json
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException
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
from app.config import get_settings
from app.db import close_pool, counts, get_pool
from app.embeddings import close as close_embeddings
from app.ingest import reload_from_data_dir

import logging

logger = logging.getLogger("pharmacy.api")
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
    try:
        from app.admin import prune_chat_logs

        removed = await prune_chat_logs()
        if removed:
            logger.info("pruned %d old chat logs", removed)
    except Exception:  # noqa: BLE001
        pass
    yield
    await close_pool()
    await close_client()
    await close_embeddings()


app = FastAPI(title="CitCare Pharmacy Agent", lifespan=lifespan)

_origins = [o.strip() for o in get_settings().allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],   # set ALLOWED_ORIGINS in prod
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
    """Gate /admin/* — requires a valid JWT with role admin or super_admin."""

    user = await current_user(authorization)
    if user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="admin access required")
    return user


# ---- auth routes -----------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


@app.get("/auth/config")
async def auth_config() -> Dict[str, Any]:
    """Public — tells the login screen which methods are available."""

    s = get_settings()
    return {
        "ldap_enabled": s.ldap_enabled,
        "oidc_enabled": s.oidc_enabled,
        "oidc_provider_name": s.oidc_provider_name,
    }


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
            "role": u["role"], "auth_sources": list(u.get("auth_sources") or [])}


@app.get("/auth/sso/login")
async def sso_login():
    from fastapi.responses import RedirectResponse

    try:
        url = await authmod.oidc_authorize_url(state="citcare")
    except authmod.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url)


@app.get("/auth/sso/callback")
async def sso_callback(code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse

    try:
        result = await authmod.oidc_callback(code)
    except authmod.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    # hand the token to the SPA via URL fragment
    return RedirectResponse(f"/?sso_token={result['token']}")


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
            return cached, True
    metrics.incr("cache_misses")

    # Pin the data version we are about to answer against. If an ingest lands
    # while the agent is thinking, set_cached_answer drops the answer instead of
    # filing stale stock under the new version.
    version = await get_data_version()

    # The fast path resolves the drug from THIS message alone. A follow-up names
    # no drug, so it must go to the agent, which can see the conversation.
    if get_settings().fast_path_enabled and not follow_up:
        facts = await fastpath.answer(message, store_id)
        if facts is not None:
            phrase_prompt = fastpath.build_phrasing_input(
                _scoped_message(message, store_id), facts
            )
            metrics.incr("llm_calls")
            metrics.record_llm()
            out = await fastpath.get_phrasing_agent(model).arun(phrase_prompt)
            content = getattr(out, "content", str(out))
            await set_cached_answer(message, store_id, content, model=model, version=version)
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
        out = await get_agent(model, with_history=bool(client_session)).arun(prompt, **run_kw)
        content = getattr(out, "content", str(out))
    finally:
        reset_store_scope(token)

    if not follow_up:
        await set_cached_answer(message, store_id, content, model=model, version=version)
    return content, False


# ---- routes ----------------------------------------------------------------


@app.get("/")
async def chat_ui():
    """Serve the test chat UI."""

    from pathlib import Path

    from fastapi.responses import HTMLResponse

    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


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
                yield "data: [DONE]\n\n"
                return
        metrics.incr("cache_misses")

        # See _answer(): pin the version we answer against, so an ingest that
        # lands mid-stream invalidates this answer instead of being masked by it.
        version = await get_data_version()

        if get_settings().fast_path_enabled and not follow_up:
            facts = await fastpath.answer(req.message, store_id)
            if facts is not None:
                step = json.dumps({"label": facts["tool"], "icon": "search"})
                yield f"event: step\ndata: {step}\n\n"
                rows = fastpath.result_rows(facts)
                if rows:
                    result = json.dumps({"tool": facts["tool"], "rows": rows[:8]})
                    yield f"event: result\ndata: {result}\n\n"
                phrase_prompt = fastpath.build_phrasing_input(
                    _scoped_message(req.message, store_id), facts
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
                yield "data: [DONE]\n\n"
                return

        scope = set_store_scope(store_id)
        full = ""
        metrics.incr("llm_calls")
        metrics.record_llm()
        try:
            async for event in get_agent(
                req.model, with_history=bool(req.session_id)
            ).arun(
                _scoped_message(req.message, store_id),
                stream=True,
                stream_events=True,
                user_id=user_id,
                session_id=session_id,
            ):
                name = type(event).__name__
                if name == "ToolCallStartedEvent":
                    tool = getattr(getattr(event, "tool", None), "tool_name", "") or ""
                    frame = json.dumps({"label": tool or "Searching", "icon": "search"})
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
                        frame = json.dumps({"tool": tname, "rows": rows[:8]})
                        yield f"event: result\ndata: {frame}\n\n"
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
