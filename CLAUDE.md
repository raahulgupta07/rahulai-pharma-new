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

## Status (2026-07-09)

**Functionally complete + running locally.** Services up (api/postgres/redis/sftp/
ingest-worker healthy), real data loaded (5,292 catalog · 111,654 inventory),
47 tests pass. Aurora UI (Overview/Settings/Chat/Data), Claude-style chat with
tool-use trace + rich rendering, redesigned Data page, GraphRAG, auth, embed API —
all live + verified.

**Git:** the repo went under version control on 2026-07-09. `main` holds the
pre-optimization baseline (`1610801`); `feature/optimize` holds the speed +
accuracy fixes (`27a4f94`). There is **no remote** — both commits are local only.

**Pending code work (see "Optimization" below):**
- **A3 — blocked, and it can mislead a pharmacist.** `stock_qty` and `price` are
  `NOT NULL DEFAULT 0` (schema.sql:49-50), so a blank cell in the Excel export is
  indistinguishable from "zero on hand". Needs a migration dropping `NOT NULL` so
  `NULL` can mean *unknown*. Not done — schema change against 111k live rows.
- **Nothing is benchmarked.** `evals/bench.py` (20 questions, EN + Burmese,
  cold/warm, p50/p95, cache-hit rate) exists but has never run live — it is gated
  behind `RUN_LIVE=1` and spends OpenRouter credit. There is no measured
  before/after latency. Do not quote a speedup number until it runs.
- **The container runs the OLD baked code.** Nothing on `feature/optimize` is live.
- Not built: the deterministic fast path, the semantic answer cache, the
  router/answer model split.

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
RUN_LIVE=1 ./venv/bin/python -m evals.bench      # live latency p50/p95 (costs $)
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

## ⚠️ Site scoping — always go through `_site_clause`

A site token may be a full code (`20005-CCYK`), its numeric prefix (`20005`), or
its alpha suffix (`CCYK`). **Never** match a site with `ILIKE '%' || $n || '%'`
and never with bare `=`. Both have shipped as bugs:

- `ILIKE '%x%'` on the *enforced* store scope let a prefix-shaped `store_id`
  substring-match sibling branches — one store reading another's stock.
- Bare `=` in `get_article_info` / `summarize_article` disagreed with
  `get_stock`'s `_site_clause`, so the same store got "not stocked" from one tool
  and a real quantity from another.

`_site_clause(col, param)` (tools.py) is the only correct matcher. The one
legitimate `ILIKE` on `site_code` is the **unscoped** branch of `list_sites`,
where the token is a user's search string, not a scope.

Scope reaches tools via the `_STORE_SCOPE` contextvar. Never bypass
`set_store_scope`.

## Optimization notes (2026-07-09)

- **Provider is OpenRouter, always.** Do not propose a direct Google/OpenAI
  client to shave the proxy hop. Win latency by deleting LLM round trips.
- Per question the agent currently makes 2–3 sequential LLM calls (pick tool →
  run → phrase). The intended fix is a deterministic fast path: resolve the drug
  via the trigram GIN index + a `drug_alias` table (no LLM), run one SQL query,
  then one LLM call purely to phrase the answer in the user's language.
- `learning_enabled` now defaults to **False** (config.py). When on, it adds
  `num_history_runs=3` replays plus a second extraction model to every turn.
- The answer cache key is `(data_version, model, store_id, normalized_message)`.
  It is an exact hash — near-miss phrasings do not hit. A semantic (embedding)
  cache is the intended upgrade.
- A shared `lru_cache`'d Agno `Agent` **is** safe under concurrent `arun()` with
  different `session_id`s — `agno/agent/_session.py` only writes
  `agent.session_id` when it is `None`, and the app always passes one. Pinned by
  `tests/test_agent_concurrency.py`. Do not "fix" this by rebuilding agents.
- Never call pandas (`read_excel`, `iterrows`) directly from an `async def` — it
  blocks the event loop and freezes every concurrent chat for the whole parse.
  Use `asyncio.to_thread`.
- Refresh materialized views `CONCURRENTLY` (both have the required UNIQUE
  index); a plain `REFRESH` takes `ACCESS EXCLUSIVE` and blocks all readers.
- A catalog upsert must set `embedding = NULL` when the embedded source text
  changes, or `embed_catalog(only_missing=True)` will keep answering semantic
  searches from a stale vector.

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
