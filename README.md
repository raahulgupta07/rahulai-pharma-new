# City Pharma Agent

A bilingual (**English / Burmese**) pharmacy AI assistant over a real drug
catalog and multi-site inventory. The agent answers stock, price, substitute,
and indication questions by **calling tools that read exact numbers from the
database** — quantities and prices are never hallucinated. It ships with an
embeddable chat widget (store-scoped per client) and a full admin console.

```
Client site ──widget──┐
                      ▼
              FastAPI :8088 ──> Gemini 3.5 Flash agent (10 tools)
   admin SPA (/admin) ──┘            │
                                     ├─ SQL    (exact: stock, price, substitutes)
                                     ├─ RAG    (pgvector semantic search)
                                     └─ Graph  (drug_edges: related / same-condition)
                                     │
                        Postgres 16 + pgvector   ·   Redis (cache / sessions / rate-limit)
```

The agent is a **router** — for each question it picks SQL, vector RAG, or the
drug graph, then cites the source in its answer.

## Status

**Demo / UAT ready** — runs end-to-end locally on real data (5,292 catalog ·
111,654 inventory), 38 tests passing, all features verified. An engineer can embed
the widget today via [INTEGRATION.md](INTEGRATION.md).

**Before going to production** (operator tasks, see checklist at the bottom):
rotate the OpenRouter key · set the prod `SECRET_KEY` · deploy behind TLS · tighten
CORS · SFTP key-auth · connect real LDAP/Keycloak.

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI (async) + Agno agent framework |
| LLM | **Gemini 3.5 Flash** (chat) + **Gemini embeddings** (semantic), both via OpenRouter |
| Data | Postgres 16 + pgvector (catalog, inventory, drug graph, materialized views) |
| Cache/state | Redis (query cache, sessions, rate limit, embedding cache) |
| Admin UI | SvelteKit 5 + Tailwind v4 ("Aurora" design), served at `/admin` |
| Ingestion | pandas + openpyxl; SFTP drop-zone auto-loader |
| Auth | JWT (local) + LDAP + Keycloak/OIDC SSO, merge-by-email |

## Quick start

```bash
cp .env.example .env          # set OPENROUTER_API_KEY and a 32-byte SECRET_KEY
docker compose up -d          # api:8088 · postgres:5433 · redis:6380 · sftp:2222

curl localhost:8088/ready     # {catalog_rows, inventory_rows, sites}
open http://localhost:8088/admin
```

First run seeds a super-admin from `ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env`.

### Local dev (without rebuilding the image)

```bash
# backend
./venv/bin/uvicorn app.api:app --reload --port 8088
# admin SPA (hot reload, proxies API)
cd admin && npm run dev        # http://localhost:5173
```

## Loading data

Two Excel files drive everything: an **articles export** (catalog) and a
**balance stock** file (inventory). Load via either:

- **SFTP** — drop `articles*.xlsx` / `balance*.xlsx` into the SFTP upload dir;
  the ingest worker auto-loads and busts the cache.
- **Admin** — Data → Upload, or `POST /api/embed/reload`.

Catalog fields: `article_code, brand_name, generic_name, composition, category,
indication, dosage, side_effect, mm_reg, mm_label, status`. Inventory:
`article_code, site_code, stock_qty, price`. (Burmese text lives in
indication/dosage/side_effect — store as UTF-8.)

## Embedding the chat widget

One line for a public widget, or store-scoped with an HMAC-signed user so answers
are locked to one branch's data:

```html
<script src="https://YOUR_HOST/api/embed/widget.js"
        data-embed-id="web" data-stream="true" async></script>
```

Full contract (session/create, chat, chat/stream SSE, HMAC signing rules,
Laravel/PHP example) → **[INTEGRATION.md](INTEGRATION.md)**.

## Admin console (`/admin`)

- **Overview** — agent health rings, status pills, usage, top stock
- **Chat tester** — Claude-style chat with live tool-use trace + clickable sources
- **Data** — catalog & inventory with search/category/stock filters + detail drawer
- **Knowledge graph** — interactive drug graph (ingredients, conditions)
- **Settings** — answer behaviour toggles (citations, bilingual, disclaimer)
- **Users / Tenants / SFTP / Evaluation**

## Docs

- **[CLAUDE.md](CLAUDE.md)** — architecture, file map, conventions, deploy gotchas
- **[RUNBOOK.md](RUNBOOK.md)** — ops: startup, secrets/rotation, data refresh, monitoring
- **[INTEGRATION.md](INTEGRATION.md)** — embed widget + raw API for engineers

## Tests

```bash
./venv/bin/python -m pytest -q                  # fast suite (no LLM, no network)
RUN_LIVE=1 ./venv/bin/python -m evals.run_eval  # live accuracy eval (costs $)
```

## Security checklist (before public deploy)

- [ ] Rotate the OpenRouter API key
- [ ] `SECRET_KEY` = 32+ random bytes, matching the Laravel `CITYAGENT_SECRET_KEY`
- [ ] SFTP key-based auth only
- [ ] Tighten `ALLOWED_ORIGINS` (CORS) to the host domain
