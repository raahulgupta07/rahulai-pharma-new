# CLAUDE.md — City Pharma Agent

Guidance for Claude Code working in this repo. Read this first.

## What this is

Bilingual (English / Burmese) pharmacy AI agent over a real catalog + multi-site
inventory. A FastAPI backend exposes an **embed-compatible chat API** (drop-in
widget for client sites, store-scoped via signed tokens) plus an admin API. A
SvelteKit admin SPA ("Aurora" UI) is served at `/admin`.

- **Chat model:** `google/gemini-3.5-flash` via OpenRouter (default). Selectable
  per-message in the chat UI — see `SELECTABLE_MODELS` in `app/agent.py` (3 Gemini
  Flash variants, A/B picker). Override with `OPENROUTER_MODEL` env.
- **Embeddings:** `google/gemini-embedding-2` (3072-dim, pgvector, exact scan).

## Status (2026-06-23)

**Functionally complete + running locally.** Services up (api/postgres/redis/sftp/
ingest-worker healthy), real data loaded (5,292 catalog · 111,654 inventory, v22),
38 tests pass. Aurora UI (Overview/Settings/Chat/Data), Claude-style chat with
tool-use trace + rich rendering, redesigned Data page, GraphRAG, auth, embed API —
all live + verified.

**Blocking production (operator-only, NOT code):**
1. Rotate the OpenRouter key (was shared in chat).
2. Set prod `SECRET_KEY` (32-byte) = Laravel `CITYAGENT_SECRET_KEY`.
3. Deploy + expose behind TLS / real domain (localhost now).
4. Tighten CORS `ALLOWED_ORIGINS` to host domain.
5. SFTP key-auth only (password now).
6. Point LDAP/Keycloak at real servers to test SSO.

**Optional polish (not blocking):** label chat trace by mode (SQL/RAG/Graph);
graph-page label de-clutter; wire Data Export-CSV / Upload buttons; settings
toggles → real runtime behaviour (needs a `/admin/config` POST; currently
local-only UI prefs); a prod readiness-check script for items 1–6.

## Architecture

```
SvelteKit admin (admin/)  ──serves──>  /admin  (built into the api image)
        │ fetch
        ▼
FastAPI (app/api.py) :8088 ──> Agno agent (app/agent.py, 12 tools)
        │                            │
        ▼                            ▼
Postgres 16 + pgvector         Redis (cache, sessions, rate limit)
(catalog, inventory,           (app/cache.py)
 drug_edges, MVs)
```

The agent is a **router**: per question it picks among three retrieval modes —
- **SQL** (exact/keyword): `search_by_name` (ILIKE), `get_substitutes` (same
  `generic_name`), `get_stock`, `top_by_stock`, `filter_by_price`, `get_article_info`
- **RAG** (pgvector semantic): `search_by_meaning` — `embedding <=> query`
- **Graph** (recursive CTE on `drug_edges`): `related_drugs`, `drugs_for_same_condition`

## Key files

| Path | Role |
|------|------|
| `app/api.py` | FastAPI app, lifespan, auth routes, embed chat + **SSE stream** (`event: step` tool-trace, `event: result` rows, `data:` deltas) |
| `app/agent.py` | `build_agent()` — OpenRouter model, 12 tools, bilingual system prompt |
| `app/tools.py` | the 12 agent tools (store-scope contextvar) |
| `app/admin.py` | admin router: catalog/inventory/categories, stores, conversations, graph, users, upload, sftp |
| `app/auth.py` | users table, bcrypt, JWT, local + LDAP + OIDC, merge-by-email |
| `app/graph.py` | `drug_edges`, `build_edges`, recursive `related()`, LLM `build_treats_edges` |
| `app/security.py` | HMAC canonical-JSON signer (matches PHP `json_encode` flags) |
| `app/config.py` | pydantic-settings (`extra="ignore"`) |
| `admin/src/routes/` | SvelteKit pages (Overview `/`, chat, data, settings, graph, users, …) |
| `admin/src/lib/aurora/` | shared UI: Ring, Toggle, StatusPill, AlertChip, HeroMetric, Modal, ToastHost, markdown.js |

## Commands

```bash
# Full stack (recommended)
docker compose up -d              # api:8088, postgres:5433, redis:6380, sftp:2222
curl localhost:8088/ready         # {catalog_rows, inventory_rows, sites}

# Backend dev (needs local postgres/redis or compose ones)
./venv/bin/uvicorn app.api:app --reload --port 8088

# Admin SPA dev
cd admin && npm run dev            # vite :5173, proxies API at localhost:8088
cd admin && npm run build          # production build (the api image bakes this)

# Tests
./venv/bin/python -m pytest -q     # fast, no LLM, no network
RUN_LIVE=1 ./venv/bin/python -m evals.run_eval   # live accuracy (costs $)
```

## ⚠️ Deploy gotcha — backend code is BAKED into the image

`docker-compose.yml` has **no source volume mount** for `api` — `app/` is copied
in at build. After editing any `app/*.py`, the running container does NOT pick it
up on its own. Fast path (avoids a full multi-stage rebuild):

```bash
docker cp app/api.py pharmacy-agent-api-1:/app/app/api.py
docker restart pharmacy-agent-api-1
until [ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8088/health)" = 200 ]; do sleep 1; done
```

Admin SPA changes are picked up by the vite dev server (HMR) at :5173, but the
docker-served `/admin` needs a rebuild.

## Conventions

- **Svelte 5 runes** everywhere: `$state`, `$derived`, `$props`, `{@render}`.
  Render dynamic components directly (`{@const Icon = x}<Icon/>`), NOT
  `<svelte:component>` (deprecated). Use actions for delegated DOM handlers to
  stay a11y-clean (no inline `onclick` on static divs).
- **Tailwind v4** with `@theme` tokens mapped to CSS vars (`--c-*`) for dark mode.
  Use semantic classes: `bg-surface`, `text-ink`/`text-ink-2`/`text-ink-3`,
  `border-line`, `bg-accent`, `*-soft`. `.elev` for card shadow. Serif headings via
  `.page-title` (Fraunces). Burmese renders in Noto Sans Myanmar.
- **Admin API auth:** every `/admin/*` call needs a Bearer JWT. The layout's fetch
  wrapper injects it from `localStorage.auth_token`. A 401 shows as "backend
  offline" in the UI — usually a stale/expired token; re-login.
- **Store scoping:** chat answers are locked to the token's `store_id`; tools read
  it from a contextvar. Never bypass `set_store_scope`.

## Security (before any public deploy)

- Rotate the OpenRouter key if it was ever shared/committed.
- `SECRET_KEY` = 32+ random bytes, must match the Laravel `CITYAGENT_SECRET_KEY`.
- SFTP key-auth only in prod; tighten CORS (`ALLOWED_ORIGINS`) to the host domain.

See `RUNBOOK.md` (ops), `INTEGRATION.md` (embed widget), `README.md` (overview).
