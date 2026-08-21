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

### Two stacks, two sets of ports

The ports above are the BASE stack (`docker-compose.yml` alone). `docker-compose.optimized.yml`
overlays a second set so both can run at once — it is the one currently deployed, and every
port in it differs:

| | base | optimized |
|---|---|---|
| api | 8088 | **8091** |
| postgres | 5433 | **5434** |
| redis | 6380 | **6381** |
| sftp | 2222 | **2223** |

```bash
docker compose -f docker-compose.yml -f docker-compose.optimized.yml up -d   # project: pharmacy-opt
```

Every `localhost:8088` in this runbook is the base stack. If you are on the deployed one,
read 8091. Check which you have with `docker ps` — the optimized containers are named
`pharmacy-opt-*`.

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

**The catalog file is authoritative — it does not merge.** `ingest_catalog` runs in
`mode="full_sync"` by default (`app/ingest.py:294`): every row in the file is upserted and
stamped with this run's timestamp, and then

```sql
DELETE FROM catalog WHERE last_seen IS DISTINCT FROM <this run>
```

removes everything the file did not contain, as discontinued. Uploading a partial catalog
export therefore **deletes every product missing from it**. This runbook used to say
"catalog merges", which is the opposite, and acting on it would empty most of the catalog.
Inventory full-replaces as well.

Two ways to trigger it:
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
./venv/bin/python -m pytest tests/ -q          # ~1320 pass in ~2min (no LLM, no cost)
./scripts/test.sh                              # same, 8 xdist workers, ~30s
RUN_LIVE=1 ./venv/bin/python -m evals.run_eval # live LLM accuracy (Claude, costs $)
./venv/bin/locust -f evals/locustfile.py --host http://localhost:8088 \
   --users 50 --spawn-rate 10 --run-time 60s --headless   # load
```

Last results: pytest 38/38; live eval 13/13; load 50 users 0 errors, p95 ~20ms, cache hit 95.6%.

### Data pipeline smoke test (needs a running stack, not pytest)

```bash
ADMIN_EMAIL=... ADMIN_PASSWORD=... ./scripts/pipeline_smoke.sh \
  --base https://citycareagent.citygpt.xyz --sftp-host 127.0.0.1
```

Registers its own partner key, uploads over SFTP as a partner would, waits for
the watcher, checks the catalog survived and the assistant still answers, then
revokes the key. Run it FROM the host running sshd unless the SFTP port is
reachable from where you are — on production it is closed at the security
group, so `--sftp-host 127.0.0.1` on the box is the only way in.

By default it replays the newest catalog already in the archive, so the load
nets to zero. Catalog mode is `full_sync`: any OTHER file you pass replaces the
product list for real, and an inventory file additionally needs
`--i-know-this-replaces-stock`.

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
