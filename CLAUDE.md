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

## Status (2026-07-10)

**Functionally complete + running locally.** Services up (api/postgres/redis/sftp/
ingest-worker healthy), real data loaded (5,292 catalog · 111,654 inventory). Run
`pytest -q` for the test count; 4 skip without a live `OPENROUTER_API_KEY`. Aurora
UI (Overview/Settings/Chat/Data), Claude-style chat with tool-use trace + rich
rendering, redesigned Data page, GraphRAG, auth, embed API — all live + verified.

**Accuracy is UNMEASURED.** `evals/bench.py` grades nothing — it measures latency
only. `evals/run_eval.py` (the accuracy eval, `RUN_LIVE=1`) has **never been run
in this repo**, so there is no per-question pass/fail record anywhere. Three
recent changes alter what the model *says* (the `FORMATTING` prompt block, the
fast path, `NULLS LAST`) and none has been graded. Do not cite a correctness
number; there isn't one.

**Git:** repo under version control since 2026-07-09. `main` holds the
pre-optimization baseline (`1610801`); `feature/optimize` holds the speed +
accuracy fixes and the optimization design. There is **NO REMOTE** — every commit
is local only. Nothing is pushed anywhere.

### Measured latency (real, not estimates)

`evals/bench.py` has now run live, side-by-side (see "Two stacks" below). n=20,
cold = first ask, warm = exact cache hit:

| stack | port | cold p50 | cold p95 | cold mean | warm |
|-------|------|---------:|---------:|----------:|-----:|
| baseline               | :8088 | 9,797ms | 12,955ms | 10,234ms | ~3ms |
| optimize, flags off    | :8091 | 6,498ms | 13,921ms |  8,065ms | ~3ms |
| optimize + fast path   | :8091 | 6,164ms | 11,773ms |  7,020ms | ~3ms |

Median per intent, baseline → fast path ON (n=4 per intent — noisy, single run):

| intent | baseline | fast path OFF | fast path ON | |
|--------|---------:|--------------:|-------------:|--|
| `hot_have`   | 10,858ms | 6,409ms | **5,084ms** | fast path claims it |
| `hot_where`  |  9,798ms | 7,288ms | **6,166ms** | fast path claims it |
| `catalog`    | 12,608ms | 5,070ms |  4,940ms | falls through |
| `substitute` | 12,406ms | 13,061ms | 12,102ms | falls through |
| `semantic`   |  9,212ms | 13,921ms | 10,395ms | falls through |
| `site`       | 12,299ms |  6,896ms |  6,164ms | falls through |

**Hot intents: 9,798ms → 5,757ms median, −41%.** Those two are most real traffic.

### The correction that matters — read before optimizing further

The fast path was predicted to land hot intents near **~700ms**. It lands at
**~5,700ms**. Deleting two of three sequential LLM calls bought only ~1,300ms,
because **one `gemini-3.5-flash` call through OpenRouter is itself ~5 seconds.**

**Per-call cost dominates, not round-trip count.** Any plan that reasons about
"N legs → 1 leg" and expects a proportional win is wrong on this stack. The
remaining ~5s is a single unavoidable LLM call.

The next lever follows directly: the fast path's phrasing call has **no tools**
and one job — restate a FACTS block in the user's language. It does not need
`gemini-3.5-flash`. Run it on `flash-lite` and re-bench; that is the cheapest
experiment left.

Secondary honesty: p95 is noise at n=20. Most of the *flags-off* p50 win is just
`learning_enabled=False` (no 3-run history replay, no second extraction model per
turn) — not the fast path, not the cache. Do not attribute gains to features that
were not running.

**Built but OFF by default:** the semantic answer cache and the router/answer
model split are coded behind flags defaulting to `False`. The fast path is also
OFF by default; `docker-compose.optimized.yml` sets `FAST_PATH_ENABLED=true` on
the `:8091` stack so it can be benched.

**Blocking production (operator-only, NOT code):**
1. Rotate the OpenRouter key (was shared in chat).
2. Set prod `SECRET_KEY` (32-byte) = Laravel `CITYAGENT_SECRET_KEY`.
3. Deploy + expose behind TLS / real domain (localhost now).
4. Tighten CORS `ALLOWED_ORIGINS` — it **defaults to `*`** (config.py:67).
5. SFTP key-auth only (password now).
6. Point LDAP/Keycloak at real servers to test SSO.
7. **`is_valid_credential` (cache.py) returns `True` for ANY credentials when
   none are registered** — dev-open by design, but a prod deploy with an empty
   `pharmacy:credentials` hash accepts every embed. Seed a credential (or gate it)
   before exposing the embed API.

**Optional polish (not blocking):** label chat trace by mode (SQL/RAG/Graph);
graph-page label de-clutter; wire Data Export-CSV / Upload buttons; settings
toggles → real runtime behaviour (needs a `/admin/config` POST; currently
local-only UI prefs); a prod readiness-check script for items 1–7.

**Not yet visually verified:** nobody has laid eyes on the *authenticated* admin
UI in a browser. Headless Chrome hangs on the authed route, so every claim about
how the logged-in pages actually render is unverified. The dark palette is
derived, not reviewed (see "Design").

## Architecture

```
SvelteKit admin (admin/)  ──serves──>  /admin  (built into the api image)
        │ fetch, SAME-ORIGIN (apiBase.js) — never a hardcoded port
        ▼
FastAPI (app/api.py)  ──────> Agno agent (app/agent.py, 12 tools)
   :8088 baseline             │
   :8091 optimize             ▼
        │              Redis (cache, sessions, rate limit)
        ▼              (app/cache.py — answers keyed by data_version)
Postgres 16 + pgvector
(catalog, inventory, drug_edges, MVs)
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

## ⚠️ Deploy gotcha — backend AND admin SPA are BAKED into the image

`docker-compose.yml` has **no source volume mount** for `api` — both `app/` and
the built `admin/build` are copied in at image build. Editing either on the host
changes nothing in a running container.

**Always rebuild.** Never touch the baseline stack unless you mean to — it is the
benchmark's control:

```bash
cd admin && ./node_modules/.bin/vite build && cd ..   # only if admin/src changed
docker compose -p pharmacy-opt -f docker-compose.yml -f docker-compose.optimized.yml \
  build api ingest-worker
docker compose -p pharmacy-opt -f docker-compose.yml -f docker-compose.optimized.yml \
  up -d api ingest-worker
```

`docker cp app/x.py pharmacy-agent-api-1:/app/app/x.py && docker restart …` works
for a quick probe but is **debug-only**: the next rebuild silently erases it, and
it will not update `/app/admin_build` at all.

The vite dev server (`:5173`, HMR) picks up `admin/src` changes; the docker-served
`/admin` does not.

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

## ⚠️ Cache freshness — anything that writes stock must bump `data_version`

Answers are cached in Redis under a key containing `data_version`; bumping it
invalidates every cached answer at once. **A writer that forgets to bump serves
stale stock for up to `CACHE_TTL_SECONDS` (600).**

Bumps: the SFTP watcher / `scan_once`, `POST /api/embed/reload`,
`POST /api/embed/ingest`, `POST /admin/upload`, `POST /admin/sync/mysql`,
`POST /admin/graph/rebuild`. **Does not bump: any direct SQL write to Postgres**
— psql, cron, another service. The app cannot detect it. Call
`POST /api/embed/reload` afterwards.

**The subtle half.** A writer must pin the version it *read*, not the version at
write time. `set_cached_answer` used to key on `get_data_version()` at write time;
an agent run takes ~5s, so an ingest landing inside that window filed an
old-stock answer under the *new* version, where it looked fresh and survived a
full TTL. A bump could not evict it — the entry was written *after* the bump. Now
callers pass `version=` captured before the run, and the answer is dropped rather
than cached if the data moved. Pinned by
`tests/test_cache.py::test_ingest_during_run_does_not_poison_cache`.

⚠️ **That test was vacuous on the first attempt.** The fix has two independent
halves (pin the key, skip the write); disabling one left the other covering for
it, and the test still passed. To verify a guard like this, revert **all** of the
fix, not part of it.

## ⚠️ Admin SPA — same-origin, and deep links need a fallback

Two bugs, both fixed, both easy to reintroduce:

- The build **hardcoded `http://localhost:8088` in 16 files**, so the SPA served
  from the optimize stack on `:8091` drove the **baseline** backend. Every UI
  observation of the optimize stack was really of baseline, and the fast path was
  never once exercised from a browser. The base now comes from
  `admin/src/lib/apiBase.js` (`window.location.origin`) — the backend serves this
  build, so same-origin is always right. Do not reintroduce a literal port.
- `/admin/<route>` **404'd on reload.** `adapter-static` emits one `index.html`
  and no per-route file; the mount was plain `StaticFiles`, which has no SPA
  fallback, so deep links worked only if you never refreshed. `SPAStatics`
  (`app/api.py`) falls back to the shell for extensionless misses; a missing `.js`
  must still 404, or a broken asset returns HTML and fails confusingly. The
  `/admin/*` API routes are registered **before** the mount, so they win.

## Two stacks — side-by-side benchmarking

`docker-compose.optimized.yml` overlays the baseline compose so **both stacks run
at once** and can be benched against each other:

```bash
docker compose -p pharmacy-opt \
  -f docker-compose.yml -f docker-compose.optimized.yml up -d --build
#   baseline   api :8088  postgres :5433  redis :6380  sftp :2222
#   optimize   api :8091  postgres :5434  redis :6381  sftp :2223
BENCH_BASE_URL=http://localhost:8091 RUN_LIVE=1 ./venv/bin/python -m evals.bench
```

`!override` on every `ports:` block is **required**: Compose MERGES sequences like
`ports` by appending, so without it each service would publish the baseline port
*as well* and fail with "port is already allocated". Volumes are namespaced by the
`-p pharmacy-opt` project, so the optimize stack gets its own pgdata and never
touches the baseline's.

## Optimization notes (2026-07-09)

- **Provider is OpenRouter, always.** Do not propose a direct Google/OpenAI
  client to shave the proxy hop. Win latency by deleting LLM round trips.
- **A3 landed.** `inventory.stock_qty` / `inventory.price` are now **NULLABLE**
  (`migrations/0001_inventory_nullable_stock_price.sql` drops `NOT NULL` +
  `DEFAULT`). `NULL` means **UNKNOWN — never zero**; a blank cell in the Excel
  export no longer masquerades as "zero on hand". The migration is idempotent but
  has been applied to the **:8091 optimize DB ONLY, NOT :8088** — the baseline
  still has the old `NOT NULL DEFAULT 0` schema.
  - **Consequence — `NULLS LAST`.** Postgres sorts NULLs *first* in `ORDER BY …
    DESC`, so `get_stock` and `top_by_stock` (tools.py) now say `DESC NULLS LAST`
    to keep unknown-quantity rows from floating to the top. `find_at_other_stores`
    filters `stock_qty > 0`, which already excludes NULL, so it needs no change.
- Per question the agent makes 2–3 sequential LLM calls (pick tool → run →
  phrase). The **fast path** (`app/fastpath.py` + `app/resolver.py`, flag
  `fast_path_enabled`, default OFF) collapses that to one phrasing call for the
  two hottest intents only:
  - **HOT_HAVE** ("do I have X" / "X ရှိလား") and **HOT_WHERE** ("who else has X"
    / "ဘယ်ဆိုင်မှာ X ရှိလဲ"). Anything else — or an AMBIGUOUS resolution — falls
    through to the full agent.
  - Intent detection is **regex**, tuned so **false negatives are fine, false
    positives are not**: a wrong fast-path answer in a pharmacy is worse than a
    slow one, so an unresolvable or ambiguous mention falls through rather than
    guessing.
  - Resolution (`resolver.py`) is zero-LLM, three layers cheapest-first: exact
    article code → **`drug_alias`** table lookup → trigram similarity (GIN index).
    The single phrasing agent has **no tools**, so it cannot fetch or invent a
    number — it only restates the FACTS block.
  - `drug_alias` exists (`migrations/0002_drug_alias.sql`) but **has no write path
    yet** — nothing populates it, so the alias layer is currently always a miss
    and resolution falls to trigram. Wiring the "pharmacist clarified → learn the
    alias" write is the next step.
- **The semantic answer cache is a KNOWN-BAD idea as built. Leave it OFF.**
  (`semantic_cache_enabled`, `semantic_cache_threshold`, default OFF.) Measured on
  `gemini-embedding-2`, whole-question cosine **cannot** separate "same question"
  from "different strength": `"do I have Panadol"` scores **0.947** against
  `"…Panadol 1g"` (a genuinely *different* product) but only **0.927** against
  `"Do we have Panadol?"` (the *same* question). The dangerous pair is CLOSER than
  the benign one — **no threshold is safe**, and a false hit serves the wrong
  drug's stock. The fix is to **pin the resolved `article_code` into the cache
  scope key** (via `resolver.py`) so strength variants land in different buckets.
  Until that exists, keep it off.
- The **router/answer split** (`router_split_enabled`, `router_model`, default
  OFF) uses Agno's `output_model` (`agno/agent/agent.py:297`): the cheap
  `router_model` drives the tool-selection loop, then the strong model regenerates
  the final answer. It saves **COST, not latency** — the round trips remain.
- `learning_enabled` defaults to **False** (config.py). When on, it adds
  `num_history_runs=3` replays plus a second extraction model to every turn — this
  is the single biggest driver of the baseline's p50 (see "Measured latency").
- The exact answer cache key is `(data_version, model, store_id,
  normalized_message)` — a SHA-256 hash, so near-miss phrasings do not hit. It is
  free and ~3ms on a hit. The semantic layer above was the intended near-match
  upgrade; it is unsafe as built.
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
- **The system prompt is the only thing that asks for tables.** `renderMarkdown()`
  has always parsed GFM pipe tables and `app.css` has always styled them; before
  the `FORMATTING` block in `BILINGUAL_SYSTEM_PROMPT`, the only style rule was
  "be concise", which the model read as "write a sentence". A rendering gap in
  chat is usually a *prompt* gap, so check there before touching the renderer.

## Ground truth (verified against the running `:8091` DB, 2026-07-10)

Check facts here before writing a test fixture or an example question.

- Tables are **`catalog`** and **`inventory`** — there is no `articles` or `sites`
  table. Catalog columns: `article_code, brand_name, generic_name, composition,
  category, indication, dosage, side_effect, mm_reg, mm_label, status, embedding`.
- Admin login: `admin@citcare.local` / `Admin123!` (the only user, `super_admin`).
- Real site codes: `20003-CCJ8`, `20005-CCYK`, `20024-CC73`, `20026-CC19`,
  `20052-CCTLKK`, `20059-CCGMPMTN`, …
- Real quantities: `RELYTE ORAL REHYDRATION SALTS 20.5G` @ `20026-CC19` = **6533**;
  `ROYAL-D 25G` @ `20052-CCTLKK` = **4154**, @ `20024-CC73` = **2298**.
- **`0` of 111,654 inventory rows have a NULL `stock_qty`.** Migration 0001 made
  the column nullable, but the existing data was ingested with blanks already
  coerced to `0`. `NULLS LAST` is correct and currently **untested against real
  NULLs** — they appear only after a re-ingest.
- **There is no Panadol in this catalog** (nearest: `PARAGEN`, `PARASAFE`,
  `P-125`). "Do you have Panadol?" is therefore the best probe for a fast-path
  false positive: the correct answer is "no Panadol", and silently resolving to a
  paracetamol sibling and reporting its stock is the failure mode to watch for.

## The embed widget (`app/static/widget.js`)

Restyled to the teal design. It is injected into **arbitrary customer sites**, so
it cannot use the admin's CSS vars or Tailwind — inline styles and a scoped style
block are correct there, and colours are hex, not tokens.

Three things must never change without breaking production embeds:

1. **The SSE wire contract** — `event: step`, `event: result`,
   `data: {"delta": …}`, `data: [DONE]`, frames split on `\n\n`.
2. **The session flow** — `POST /api/embed/session/create` →
   `/api/embed/chat/stream`, including the 401 re-mint-once retry.
3. **Every `data-*` attribute** — `data-embed-id`, `data-public-key`, `data-user`,
   `data-user-sig`, `data-title`, `data-greeting`, `data-accent`, `data-stream`.
   Customers have these in live HTML (see `INTEGRATION.md`). Only the *default*
   value of `data-accent` changed: `#c96342` → `#006869`.

The design mock's tool-trace chips, citation pills, typing indicator, and quick
replies were **deliberately skipped** — each needs new SSE parsing or state, and
the contract above outranks the design.

**Known gaps vs the admin chat** (unfixed, in rough order of effort):

1. Answers render with `textContent`, so Markdown tables arrive as literal `|`
   pipes. The admin's `renderMarkdown` is an ES module and the widget is a
   dependency-free classic script — copy-pasting it guarantees drift; prefer
   serving `widget.js` as a concatenation of one shared source.
2. The SSE loop reads only `j.delta`. It parses `event: step` and `event: result`
   and **discards** them, so the structured tool rows never reach the DOM.
3. **The admin source drawer cannot be reused.** It calls
   `GET /admin/catalog/{code}`, which is admin-authenticated *and* returns every
   branch's stock and price with **no store scoping** (`app/admin.py`,
   `catalog_one`). Wiring it into a store-scoped widget would hand a scoped
   pharmacist every sibling branch's inventory — the same leak class already fixed
   in `search_by_meaning` / `related_drugs`. A widget drawer needs its own
   session-scoped endpoint filtering through `_site_clause()`. While there, note
   `catalog_one` computes `total_stock` as `sum(s["stock_qty"] or 0)`, which
   coerces NULL (unknown) to zero and contradicts the `NULLS LAST` invariant.

## Conventions

- **Svelte 5 runes** everywhere: `$state`, `$derived`, `$props`, `{@render}`.
  Render dynamic components directly (`{@const Icon = x}<Icon/>`), NOT
  `<svelte:component>` (deprecated). Use actions for delegated DOM handlers to
  stay a11y-clean (no inline `onclick` on static divs).
- **Tailwind v4** with `@theme` tokens mapped to CSS vars (`--c-*`) for dark mode.
  Use semantic classes: `bg-surface`/`bg-surface-2`, `text-ink`/`text-ink-2`/
  `text-ink-3`, `border-line`, `bg-accent`, `*-soft`. `.elev` for card shadow.
  Display headings via `.page-title` (**Space Grotesk**); body is **IBM Plex
  Sans**; Burmese renders in Noto Sans Myanmar. **There are only two surface
  levels** (`surface`, `surface-2`) — there is no `--color-surface-3` token. Any
  `bg-surface-3` is a phantom that renders as no colour; repoint it to `bg-surface`
  or `bg-surface-2`. Every colour class MUST resolve to a `--color-*` token in
  `admin/src/app.css`.

- **Design (teal rebrand).** The app was rebranded to a **teal** accent
  (`--c-accent`) + Space Grotesk / IBM Plex Sans. **`text-on-accent` exists on
  purpose:** in dark mode the accent lightens past ~70% L, where white text on it
  fails AA contrast, so `--c-on-accent` flips dark. **NEVER put `text-white` on an
  accent fill** — use `text-on-accent`. The design mock ships **light only**; the
  entire **dark palette in `app.css` is DERIVED** (same hues, inverted lightness,
  eased chroma) and has **NOT been reviewed by a human**. Treat dark-mode colour as
  provisional.
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
