# City Pharma Agent

A bilingual (**English / Burmese**) pharmacy AI assistant over a real drug catalog
and multi-site inventory. A pharmacist asks *"do I have Panadol?"* or *"where can I
find it?"* and gets an exact answer — because the agent **calls tools that read the
database**, it never guesses a quantity or a price.

Read-only by design. The agent queries and explains; it never transfers stock,
places orders, or writes to inventory.

```
Client site ──widget──┐
                      ▼
              FastAPI :8088 ──> Gemini 3.5 Flash agent (12 tools)
   admin SPA (/admin) ──┘            │
                                     ├─ SQL    (exact: stock, price, substitutes)
                                     ├─ RAG    (pgvector semantic search)
                                     └─ Graph  (drug_edges: related / same-condition)
                                     │
                        Postgres 16 + pgvector   ·   Redis (cache / sessions / rate-limit)
```

The agent is a **router** — per question it picks SQL, vector RAG, or the drug
graph, then cites the source in its answer.

---

## What matters most

Five things, if you read nothing else.

**1. Numbers come from tools, never from the model.**
Text-to-SQL was rejected deliberately. Twelve hand-written tools in `app/tools.py`
own every query. Agno builds the tool schema from each function's **signature and
docstring**, so those docstrings are load-bearing — reword one and you change how
the model picks tools.

**2. Store scoping is fail-closed, and `_site_clause()` is the only correct matcher.**
When a widget session is store-scoped, every query must go through
`_site_clause()` in `app/tools.py`. Never `site_code = $2` (misses sites whose code
carries a suffix) and never `ILIKE '%'||$2||'%'` (a store id that is a prefix of a
sibling branch's code **leaks that branch's stock**). Both bugs existed; both are
fixed; both are easy to reintroduce.

**3. `NULL` stock means *unknown*, not zero.**
A blank cell in the Excel export used to land as `0` and get reported as
"out of stock". `stock_qty` and `price` are now nullable. Consequence: Postgres
sorts `NULL` **first** in `ORDER BY … DESC`, so every such sort needs
`NULLS LAST`. Applied via `migrations/0001`.

**4. Per-call LLM cost dominates, not round-trip count.**
One `gemini-3.5-flash` call through OpenRouter costs **~5 seconds** by itself. The
deterministic fast path deletes two of three LLM legs and still only saves
~1,300 ms. Any optimization plan whose argument is "N round trips → 1 round trip"
is wrong on this stack. The lever that remains is a **cheaper model per call**.

**5. Anything that writes stock must bump `data_version`.**
Answers are cached in Redis under a key containing a `data_version` counter;
bumping it invalidates every cached answer at once. **A writer that forgets to
bump serves stale stock for up to `CACHE_TTL_SECONDS` (600).** The watcher,
`/api/embed/reload`, `/admin/sync/mysql`, and `/admin/graph/rebuild` all bump.
Writes made directly to Postgres — `psql`, cron, another service — bump nothing,
and the app cannot detect them.

Related and easy to get wrong: a writer must pin the version it *read*, not the
version at write time. An agent run takes seconds; an ingest can land inside that
window, and an answer computed against old stock would otherwise be filed under
the new version, where it looks fresh. `set_cached_answer(..., version=…)` drops
such an answer instead of caching it. Pinned by
`tests/test_cache.py::test_ingest_during_run_does_not_poison_cache`.

---

## Status

**Demo / UAT ready.** Runs end-to-end on real data (5,292 catalog · 111,654
inventory · 53 sites). The full suite passes offline — `pytest -q` for the count.

Measured latency (`evals/bench.py`, n=20, real data, both stacks):

| stack | cold p50 | cold p95 | mean | warm (cached) |
|---|---|---|---|---|
| baseline `:8088` | 9,797 ms | 12,955 ms | 10,234 ms | ~2.9 ms |
| optimize `:8091`, flags off | 6,498 ms | 13,921 ms | 8,065 ms | ~3.4 ms |
| optimize + `FAST_PATH_ENABLED` | 6,164 ms | 11,773 ms | 7,020 ms | ~3.3 ms |

Per intent, median (n=4): `hot_have` 10,858 → 5,084 ms · `hot_where` 9,798 →
6,166 ms. **Hot intents −41% vs baseline.** p95 is noise at this sample size.

Most of the flags-off win is simply `LEARNING_ENABLED=false`.

**Before production** — see the [security checklist](#security-checklist-before-public-deploy).

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI (async) + Agno agent framework |
| LLM | **Gemini 3.5 Flash** (chat) + **Gemini embeddings** (semantic) — **both via OpenRouter, exclusively** |
| Data | Postgres 16 + pgvector + pg_trgm (catalog, inventory, drug graph, materialized views) |
| Cache/state | Redis (query cache, sessions, rate limit, embedding cache) |
| Admin UI | SvelteKit 5 + Tailwind v4, served at `/admin` |
| Ingestion | pandas + openpyxl; SFTP drop-zone auto-loader |
| Auth | JWT (local) + LDAP + Keycloak/OIDC SSO, merge-by-email |

**Provider constraint: OpenRouter only.** Do not add a direct Google, OpenAI, or
Anthropic SDK — not even to cut latency. Win by spending fewer or cheaper
OpenRouter calls.

---

## Installation

### Requirements

Docker + Docker Compose. An OpenRouter API key. Nothing else — Postgres, Redis,
and the SFTP drop zone all come up in the compose stack.

### 1. Configure

```bash
cp .env.example .env
```

Set these three at minimum:

| Var | Notes |
|---|---|
| `OPENROUTER_API_KEY` | from https://openrouter.ai/keys |
| `SECRET_KEY` | 32+ random bytes. Must match the Laravel `CITYAGENT_SECRET_KEY` if you embed the widget with signed users. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | seeds the first super-admin on first boot |

`.env` is gitignored. Keep it that way — it holds a live key.

### 2. Start

```bash
docker compose up -d --build
# api :8088 · postgres :5433 · redis :6380 · sftp :2222
```

`app/schema.sql` is applied automatically on startup (`app/db.py`).

### 3. Apply migrations

**Migrations are not auto-applied.** Run them once, in order, against a fresh DB:

```bash
for f in migrations/*.sql; do
  docker exec -i pharmacy-agent-postgres-1 \
    psql -U pharmacy -d pharmacy < "$f"
done
```

Both are idempotent, so re-running is safe.

- `0001_inventory_nullable_stock_price.sql` — `NULL` = unknown stock, not zero
- `0002_drug_alias.sql` — alias table for the deterministic resolver

### 4. Verify

```bash
curl localhost:8088/ready     # {catalog_rows, inventory_rows, sites}
open http://localhost:8088/admin
```

Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

### Local dev (no image rebuild)

```bash
./venv/bin/uvicorn app.api:app --reload --port 8088   # backend
cd admin && npm run dev                                # admin SPA :5173, proxies API
```

---

## Updating

### Updating the application

The backend is **baked into the image — there is no source volume mount.** Editing
a file on the host changes nothing in a running container.

```bash
docker compose up -d --build api ingest-worker
```

For a one-file hotfix during debugging only:

```bash
docker cp app/tools.py pharmacy-agent-api-1:/app/app/tools.py
docker restart pharmacy-agent-api-1
```

A `docker cp` is erased by the next rebuild. Never ship one.

### Updating the admin SPA

```bash
cd admin && npm run build     # → admin/build, copied in at image build
cd .. && docker compose up -d --build api
```

### Updating the data

Two Excel files drive everything: an **articles export** (catalog) and a **balance
stock** file (inventory). Either path re-ingests and busts the cache:

- **SFTP** — drop `articles*.xlsx` / `balance*.xlsx` into the upload dir; the
  `ingest-worker` polls every 15 s, loads, and invalidates.
  ```bash
  sftp -P 2222 pharma@localhost   # then: put articles.xlsx upload/
  ```
- **Admin** — Data → Upload, or `POST /api/embed/reload`.

Ingest bumps `data_version`, which is part of the cache key, so stale answers
cannot survive a reload. Rows whose embedded source text changed get
`embedding = NULL` and are re-embedded in the background.

Catalog columns: `article_code, brand_name, generic_name, composition, category,
indication, dosage, side_effect, mm_reg, mm_label, status`.
Inventory columns: `article_code, site_code, stock_qty, price`.
Burmese text lives in `indication` / `dosage` / `side_effect` — store as UTF-8.
A blank `stock_qty` or `price` is ingested as `NULL` (unknown), **not** `0`.

### Updating a database schema

Add `migrations/NNNN_description.sql`, make it idempotent (`IF NOT EXISTS`,
`DROP … IF EXISTS`), and mirror the end state into `app/schema.sql` so fresh
installs get it too. Apply with the loop in step 3.

---

## Feature flags

All in `app/config.py`, all read from env, **all default OFF**.

| Flag | Default | Effect |
|---|---|---|
| `LEARNING_ENABLED` | `false` | Agno long-term memory. Replays 3 prior runs into every prompt and runs a second extraction model per turn. Costs ~2 s on *every* call, including trivial lookups. Stores **preferences only — never stock facts.** |
| `FAST_PATH_ENABLED` | `false` | Deterministic route for the two hot intents (*do I have X* / *where is X*). Resolves the drug with trigram + `drug_alias` (zero LLM), runs one SQL query, spends one LLM call phrasing. Falls through to the agent when ambiguous. |
| `SEMANTIC_CACHE_ENABLED` | `false` | **Leave off — known broken.** See below. |
| `ROUTER_SPLIT_ENABLED` | `false` | Runs the tool loop on `ROUTER_MODEL` (flash-lite), regenerates the answer with the strong model. Saves cost, not latency. |

### The semantic cache is a dead end as designed

Measured on `gemini-embedding-2`:

| pair | cosine | same answer? |
|---|---|---|
| "do I have Panadol" ↔ "do I have Panadol **1g**" | **0.947** | ❌ different product |
| "do I have Panadol" ↔ "Do we have Panadol?" | 0.927 | ✅ same question |

The dangerous pair scores **higher** than the benign one. No cosine threshold is
safe. Fixing it requires keying the cache on the **resolved `article_code`**
(`app/resolver.py` now makes that possible), not on the whole-question embedding.

---

## Cache freshness

Answers live in Redis for `CACHE_TTL_SECONDS` (default 600), keyed by
`(data_version, model, store_id, normalised_message)`. Freshness is therefore
entirely a question of who bumps `data_version`.

| writer | bumps? |
|---|---|
| SFTP watcher / `scan_once` | yes |
| `POST /api/embed/reload` | yes |
| `POST /api/embed/ingest` | yes (via `scan_once`) |
| `POST /admin/upload` | yes (via `scan_once`) |
| `POST /admin/sync/mysql` | yes |
| `POST /admin/graph/rebuild` | yes |
| direct SQL against Postgres | **no** |

**If you write `inventory` outside the app, the cache will not notice.** Call
`POST /api/embed/reload` afterwards, or accept up to 10 minutes of stale stock.
Lowering `CACHE_TTL_SECONDS` bounds the damage but does not fix it, and cache hits
are what hide the ~5 s per-call LLM latency — so that trade is not free.

---

## Embedding the chat widget

```html
<script src="https://YOUR_HOST/api/embed/widget.js"
        data-embed-id="web" data-stream="true" async></script>
```

Store-scoped with an HMAC-signed user, answers are locked to one branch's data.

**Three things in `app/static/widget.js` must never change** — customers have them
pasted into live HTML:

1. The SSE wire contract — `event: step`, `event: result`, `data: {"delta": …}`,
   `data: [DONE]`, frames split on `\n\n`
2. The session flow — `POST /api/embed/session/create` → `chat` / `chat/stream`
3. Every `data-*` attribute name

Full contract (HMAC signing rules, Laravel/PHP example) →
**[INTEGRATION.md](INTEGRATION.md)**.

**Known gaps vs the admin chat** (`app/static/widget.js`), if you go to close them:

- It renders answers with `textContent`, so Markdown tables arrive as raw `|` pipes.
- It parses `event: step` and `event: result` frames and then discards them — the
  structured tool rows never reach the DOM.
- It has no source drawer, and **cannot reuse the admin one**. The drawer calls
  `GET /admin/catalog/{code}`, which is admin-authenticated *and* returns every
  branch's stock and price with no store scoping. Wiring that into a store-scoped
  widget would leak sibling branches' inventory to a scoped user. A widget drawer
  needs its own session-scoped endpoint that filters through `_site_clause()`.

## Admin console (`/admin`)

Six sections: **Overview** (health, usage, top stock) · **Assistant** (chat tester
with live tool-use trace and clickable sources) · **Data** (catalog & inventory,
filters, detail drawer, upload) · **Intelligence** (drug knowledge graph,
evaluation) · **Organization** (users, tenants) · **Configuration** (answer
behaviour, SFTP).

Teal theme, light and dark. The accent lightens past 70% lightness in dark mode,
where white text on it fails WCAG AA — so **never `text-white` on an accent fill,
always the `--c-on-accent` token.**

---

## Benchmarks and tests

```bash
./venv/bin/python -m pytest -q                  # 81 pass, 4 skip. no LLM, no network
RUN_LIVE=1 ./venv/bin/python -m evals.bench     # latency p50/p95, cold+warm. costs $
RUN_LIVE=1 ./venv/bin/python -m evals.run_eval  # accuracy. costs $
```

`evals/bench.py` runs 20 questions (10 EN / 10 Burmese) and reports p50/p95 per
stack and per intent.

### Running two stacks side by side

`docker-compose.optimized.yml` brings up a second, isolated stack for A/B work:

```bash
docker compose -p pharmacy-opt \
  -f docker-compose.yml -f docker-compose.optimized.yml up -d --build
```

| | api | postgres | redis | sftp |
|---|---|---|---|---|
| baseline | 8088 | 5433 | 6380 | 2222 |
| optimized | 8091 | 5434 | 6381 | 2223 |

Compose namespaces volumes by project name, so `-p pharmacy-opt` gets its own
`pgdata` and never touches the baseline's.

> **Gotcha:** Compose **merges `ports` sequences by appending** across `-f` files.
> Without `ports: !override` each service tries to publish the baseline port too
> and dies with `port is already allocated`.

Tear down (removes its volumes, leaves the baseline alone):

```bash
docker compose -p pharmacy-opt \
  -f docker-compose.yml -f docker-compose.optimized.yml down -v
```

---

## Security checklist (before public deploy)

Two defaults are **open by design for local dev and fatal in production**:

- `is_valid_credential` (`app/cache.py`) returns `True` for **any** credentials when
  none are registered.
- `allow_origins` (`app/api.py`) falls back to `["*"]`.

Deployed as-is, anyone can mint a session against your pharmacy data.

- [ ] Rotate the OpenRouter API key
- [ ] Register at least one embed credential (closes the open-dev-mode hole)
- [ ] `SECRET_KEY` = 32+ random bytes, matching the Laravel `CITYAGENT_SECRET_KEY`
- [ ] `ALLOWED_ORIGINS` narrowed to the host domain
- [ ] SFTP key-based auth only (drop pubkeys in `sftp/keys`, unset `SFTP_PASSWORD`)
- [ ] Deploy behind TLS
- [ ] Connect real LDAP / Keycloak
- [ ] Change `ADMIN_PASSWORD` from the seed value

## Docs

- **[CLAUDE.md](CLAUDE.md)** — architecture, file map, conventions, deploy gotchas
- **[RUNBOOK.md](RUNBOOK.md)** — ops: startup, secrets/rotation, data refresh, monitoring
- **[INTEGRATION.md](INTEGRATION.md)** — embed widget + raw API for engineers
