# City Pharma Agent — Runbook

Bilingual (English / Burmese) pharmacy inventory & catalog agent.
Claude Sonnet 4.5 chat + Gemini embeddings (both via OpenRouter) + Postgres/pgvector + Redis, behind an embed-compatible API.

## Stack

```
api  :8088   FastAPI + Agno agent (anthropic/claude-sonnet-4.5), 8 tools, EN/Burmese
postgres :5433  pgvector/pgvector:pg16 — catalog (+embedding) + inventory
redis :6380  query cache, sessions, rate limit, query-embedding cache
sftp :2222   drop article*/balance* xlsx → auto-ingested
ingest-worker  polls SFTP, loads, backfills, busts cache
```

## Run

```bash
cp .env.example .env          # fill secrets (see below)
docker compose up -d --build  # boots, auto-loads data/ on first run
curl localhost:8088/ready     # {catalog_rows, inventory_rows}
curl localhost:8088/metrics   # ops counters
```

## Secrets (.env — never commit; .env is gitignored)

| Var | Notes |
|-----|-------|
| `OPENROUTER_API_KEY` | LLM + embeddings. **Rotate the one shared in chat.** |
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` (chat) |
| `EMBEDDING_MODEL` | `google/gemini-embedding-2` (best Burmese, 3072-dim) |
| `SECRET_KEY` | HMAC + session tokens. **32+ random bytes.** Must match Laravel `CITYAGENT_SECRET_KEY`. |
| `SFTP_PASSWORD` | strong; or prefer key-only auth in prod |

### Key rotation (do now — the OpenRouter key was shared in plaintext)
1. OpenRouter dashboard → revoke the exposed key → create new.
2. Update `OPENROUTER_API_KEY` in `.env`.
3. `docker compose up -d api ingest-worker` (reloads env).
4. `SECRET_KEY`: generate `python -c "import secrets;print(secrets.token_hex(32))"`, set same value in Laravel.

## Daily data refresh

Two ways, both "replace old, add new" (catalog merges, inventory full-replaces):
- **SFTP**: upload `articles-export*.xlsx` + `balance_stock*.xlsx` → worker auto-ingests within ~15s → archives file → busts cache. New articles auto-embedded.
- **Manual**: `curl -X POST localhost:8088/api/embed/reload` (from data dir) or `/api/embed/ingest` (from SFTP dir).

## API (embed contract — drop-in for the Laravel widget)

```
POST /api/embed/session/create  {embed_id, public_key, user?, signature?} → {session_token, expires_in}
POST /api/embed/chat            {session_token, message} → {content}
POST /api/embed/chat/stream     → SSE (event:step + data delta + [DONE])
POST /api/embed/reload | /ingest
GET  /health /ready /metrics
```
Point Laravel `CITYAGENT_BASE_URL` at `http://<host>:8088`.

## Tests & checks

```bash
./venv/bin/python -m pytest tests/ -q          # 38 pass (fast, no LLM, no cost)
RUN_LIVE=1 ./venv/bin/python -m evals.run_eval # live LLM accuracy (Claude, costs $)
./venv/bin/locust -f evals/locustfile.py --host http://localhost:8088 \
   --users 50 --spawn-rate 10 --run-time 60s --headless   # load
```

Last results: pytest 38/38; live eval 13/13; load 50 users 0 errors, p95 ~20ms, cache hit 95.6%.

## Security model

- `store_id` comes from the **signed** session token, force-scoped into every tool — a user cannot read another branch's stock.
- HMAC user signature verified server-side (canonical JSON matches the PHP client).
- Logs never include bodies, tokens, or secrets — only method, path, status, latency, request id.
- Prod: tighten CORS `allow_origins` to the host domain; expose `:2222` only to trusted uploaders; SFTP key-auth only.

## Monitoring (GET /metrics)

```
requests_total, errors_total, llm_calls,
cache_hits, cache_misses, cache_hit_rate,
latency_ms {p50, p95, p99, max}
```
Watch: error rate, cache_hit_rate (cost driver), p95 latency, llm_calls/day (spend).

## Scaling to 1000+ users

- API is stateless → run N replicas behind a load balancer.
- Postgres + Redis shared; asyncpg pool + Redis cache absorb load (95%+ hit rate observed).
- Inventory = SQL + indexes (exact). Catalog = SQL + vector (semantic). Never vectorize inventory.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/ready` 503 | Postgres up? `docker compose logs postgres` |
| 429 on /chat | rate limit (per user/min) — expected under abuse |
| empty answers | data loaded? `/ready` counts; re-`/reload` |
| Burmese garbled | ensure Unicode (not Zawgyi) input |
| semantic off | catalog embeddings present? re-run `embed_catalog` |
| SFTP upload denied | `sftp/chown.sh` ran? upload dir owned by uid 1001 |
