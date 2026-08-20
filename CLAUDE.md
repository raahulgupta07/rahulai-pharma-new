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

## Status (2026-08-03)

**Functionally complete + running locally.** Services up (api/postgres/redis/sftp/
ingest-worker healthy), real data loaded (5,292 catalog · 111,654 inventory · 400
stubs, `stub_ratio` 0.076). Run `pytest -q` for the test count (**358 pass, 4
skip** without a live `OPENROUTER_API_KEY`). Aurora UI, Claude-style chat with
tool-use trace, GraphRAG, auth, embed API — all live + verified.

**2026-08-02/03 sessions — FIELD FEEDBACK + SFTP OPS (on `main`, baked into
`:8091`, HEAD `8e8ed54`).** 14 CMHL feedback forms (`~/Downloads/28-07-2026 3/`)
were diagnosed down to a handful of causes and worked through. Shipped:

- **Validate-then-replace ingest** (`app/validation.py`) — three passes (format →
  columns → data) then a shrink guard; a refused file returns 422 with the
  reasons and never reaches the drop folder. **Merge mode is gone**: an upload
  always replaces.
- **0, negative and blank are real values.** Blank stock is NULL = *unknown*, not
  0. Negatives (79 in the current file) are stored and shown as sent.
- **Leak filter** (`app/answer_filter.py`) — the model was narrating its own
  process ("Let me search… actually, wait…"). Paragraph-buffered, so nothing
  escapes mid-sentence.
- **Deterministic disclaimer** (`app/disclaimer.py`) — see the landmine below.
- **Pharmacist voice** — prompt rewrite; `ALWAYS NAME THE PRODUCT`, no provenance
  narration, answer every question asked.
- **`RESULT_ROW_LIMIT = 250`** replacing a silent `rows[:8]`, and the answer says
  when it truncated. The field forms asked for this explicitly.
- **File history + file actions** (`app/ingest_events.py`, `8e8ed54`) — every
  ingest step is stored instead of going only to `logger`; `/admin/sftp/files`
  merges the three folders into one listing; download / retry / delete per file.
  The SFTP admin page was rebuilt around it (5 tabs, drawer with the timeline).

**Accuracy baseline exists now.** `evals/eval_set_field_feedback.json` (22 cases)
passes 22/22 against `:8091`. That is a *baseline, not coverage* — 22 questions,
one store. Do not quote it as an accuracy figure.

⚠️ **The customer host `192.168.2.46:8090` is untouched and unreachable from
here.** It still has no catalog loaded, which is the root cause behind ~8 of the
14 field tickets. Nothing in this repo fixes that until the file is loaded there;
check `/ready` for `catalog_health.stub_ratio` near 0 afterwards.

⚠️ **Four product decisions are blocked on CMHL** (see
`docs/What_We_Fixed_2026-08-03.docx`): cross-branch stock visibility (forms 4/9
ask the agent to *refuse*, and it currently answers), default answer length (form
5 wants "Found/Not Found", form 10 wants everything), whether an out-of-stock
answer may suggest an alternative, and whether the public may be given dosage.

**2026-07-14 session — EMBED OPS + repo now has a REMOTE (on `main`, baked into
`:8091`, pushed to `github.com/raahulgupta07/rahulai-pharma-new`, HEAD `c8f1636`):**
The baseline `:8088` stack was torn down — only the optimize `:8091` stack
(project `pharmacy-opt`) remains. Shipped: **UI-configurable CORS** (`DynamicCORS`
subclass + Redis set, refreshed every 3s, no restart — `8de3adf`); **answer length**
Crisp/Standard/Detailed (`/admin/answer-style`, re-tunes prompt + fast-path,
bumps `data_version`); **ready-to-use PHP** embed snippet + **per-outlet embed
generator** (`GET /admin/embed/outlets`, `POST /admin/embed/snippet`,
`POST /admin/embed/snippets.zip` — pre-signed, store-locked, download-and-go;
NO per-store URL, the store is the HMAC-signed `user.store_id` — `d3f9a29`); fixed
the **cross-store "which OTHER stores have X" regex** (adjective slot; it was
answering from the own store — `d3f9a29`); in-admin **Integration guide** docs
page (`/admin/docs`, audience tabs, live values — `8ee64e3`); **console chat 403
fix** — `ensure_internal_credential()` auto-seeds `admin-chat`/`admin` on every
boot, since fail-closed + a polluted credential store had knocked it offline
(`839785e`); **root `/` → `/admin` redirect**, dropping the old standalone test
chat page (`c8f1636`). ⚠️ **A code change that alters answers does NOT bump
`data_version`** — a stale cached answer masked the cross-store fix on redeploy;
`POST /api/embed/reload` (or bump `data_version`) after any answer-affecting
deploy. ✅ **The pytest-hits-live-data hazard is FIXED (2026-08-17)** — it used to
share the live `:6381` Redis and rewrite `pharmacy:credentials` (any embed using
`emb1` 401'd the moment someone ran the suite, demonstrated twice on 2026-08-03),
and it wrote into the live Postgres, where `tests/test_branding.py`'s autouse
fixture `DELETE`d `brand_assets` and erased the deployed logos twice. The suite
now runs against **its own database and Redis DB `/15`** via `tests/dbguard.py`,
with **no fallback**: a missing test database aborts the run with the command
that creates it, and `dbguard.install()` patches asyncpg so a module with a
hardcoded DSN still cannot reach live. Template database + schema fingerprint +
per-session clone took the suite from **727s for 11 tests to 0.44s**; the whole
suite is ~1,050 tests in ~5½ min. The debris the old behaviour left in live
`app_events`/`users`/`auth_events` is catalogued, with its SQL, in
`docs/PYTEST_DEBRIS_PURGE.md` — **not executed; it is the owner's call**.
⚠️ **AWS shows the
OLD UI until the image is rebuilt THERE** — GitHub push ≠ deploy; `docker/Dockerfile`
is multi-stage and builds the SPA itself (no manual `vite build`), so AWS update =
`git pull` → `docker compose … build api` → `up -d api` → `/api/embed/reload`.

**2026-07-10 session (on `feature/optimize`, baked into `:8091`, verified live):**
Keycloak SSO + LDAP existed since baseline but shipped with two auth bypasses —
fixed (`ec5c35d`, live-tested against a real OpenLDAP). SSO/LDAP now
UI-configurable (`386f41b`, `/admin/auth`). Admin-approval gate on console access
(`da1d819`, CMHL hold screen). Chat follow-ups became suggested *questions*
(`8b5115a`); agentic trace — plan line, distinct arg-bearing step labels, timing
fold, prompt-level self-correction (`8ac2bb2`). One blank-page regression from an
unimported icon, fixed + guarded (`a9a83be`). Nobody has still opened the authed
UI in a browser except via my curl checks.

**Accuracy is UNMEASURED.** `evals/bench.py` grades nothing — it measures latency
only. `evals/run_eval.py` (the accuracy eval, `RUN_LIVE=1`) has **never been run
in this repo**, so there is no per-question pass/fail record anywhere. Three
recent changes alter what the model *says* (the `FORMATTING` prompt block, the
fast path, `NULLS LAST`) and none has been graded. Do not cite a correctness
number; there isn't one.

**Git:** repo under version control since 2026-07-09. Active branch is now
**`main`**, pushed to **`github.com/raahulgupta07/rahulai-pharma-new`** (HEAD
`c8f1636`, 2026-07-14). The old local-only `feature/optimize` history was merged
forward; `main` now carries the speed + accuracy fixes, the auth work, and the
2026-07-14 embed-ops features. `.env` is NOT tracked (verified). `admin/build` is
gitignored — the Docker image rebuilds the SPA from source.

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
the `:8091` stack so it can be benched. **`HISTORY_ENABLED` defaults ON** — it
adds no LLM call, only prompt tokens for `HISTORY_TURNS` (3) replayed turns.

**Conversational latency (`:8091`, streaming, measured 2026-07-10):** turn 1 via
fast path **8.3s**; a conversational cache hit **25ms**; the widget (no
`session_id`) 7.3s cold / 3ms cached. Before `record_turn`, a conversational turn
1 cost **15.7s**, because conversations could not use the fast path at all.

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
derived, not reviewed (see "Design"). **This is now more true, not less**: the
SFTP page was rewritten from scratch on 2026-08-03 (1,445 lines, five tabs, a
drawer) and verified only by `curl`, a clean `vite build`, and
`scripts/check_svelte_icons.py`. That guard catches the known blank-page trap; it
is not the same as looking. Open `/admin/ftp` before trusting any of it.

**Client-facing docs** live in `docs/`. `What_We_Fixed_2026-08-03.docx` is the
short version sent to CMHL (the issues table only — deliberately no advice, no
test plan, no next steps; the client asked for exactly that and nothing more).
`Field_Feedback_Response_2026-08-03.docx` is the long version with causes,
questions and upgrade notes, for their IT team. Both are generated by scripts, so
regenerate rather than hand-editing if the facts change.

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
| `app/api.py` | FastAPI app, lifespan, auth routes, embed chat + **SSE stream** (`event: plan`, `event: step` tool-trace w/ `args`, `event: result` rows, `data:` deltas), `_plan_line`/`_step_detail`, `require_admin` approval gate |
| `app/agent.py` | `build_agent()` / `build_history_agent()` / `build_learning_agent()` — OpenRouter model, 12 tools, bilingual system prompt (FORMATTING + SEARCH STRATEGY) |
| `app/history.py` | `record_turn()` — writes a fast-path / cache-hit turn into the Agno session (private agno APIs; see landmine) |
| `app/tools.py` | the 12 agent tools (store-scope contextvar) |
| `app/admin.py` | admin router: catalog/inventory/categories, stores, conversations, graph, users (+approval), upload, sftp files + keys, `GET/PUT /admin/auth-config`, `_resolve_sftp_file` (path safety) |
| `app/validation.py` | three-pass file validation (format → columns → data) + `check_shrink`; `ValidationReport` carries `errors`/`warnings`/`notes`/`stats` |
| `app/ingest_events.py` | the file history: `record()`, `history()`, `latest()`, `prune()`. Every write degrades to a warning — see landmine |
| `app/answer_filter.py` | strips the model's own reasoning out of a streamed answer, a paragraph at a time |
| `app/disclaimer.py` | decides the safety line in code, not by prompt — see landmine |
| `app/auth.py` | users table (+`approved`), bcrypt, JWT, local + LDAP + OIDC, merge-by-email, `effective_auth()` (env+Redis override), `make_state`/`verify_state` (OIDC CSRF) |
| `docs/SSO.md` | operator guide: Keycloak client setup, LDAP/AD, the failure modes |
| `scripts/check_svelte_icons.py` | static guard: unimported `.svelte` component/icon → blank page (see landmine); run by `tests/test_svelte_builds.py` |
| `app/graph.py` | `drug_edges`, `build_edges`, recursive `related()`, LLM `build_treats_edges` |
| `app/security.py` | HMAC canonical-JSON signer (matches PHP `json_encode` flags) |
| `app/config.py` | pydantic-settings (`extra="ignore"`) |
| `admin/src/routes/` | SvelteKit pages (Overview `/`, chat, data, settings, graph, users, …) |
| `admin/src/lib/aurora/` | shared UI: Ring, Toggle, StatusPill, AlertChip, HeroMetric, Modal, ToastHost, markdown.js |
| `admin/DESIGN.md` | **the design system.** Token values and why each is that value; the component contract; the four rules that outlive it. Read before changing anything visual — the console is themed from ~20 `--c-*` tokens in `app.css`, so one value edits 23 pages |
| `admin/src/lib/TabStrip.svelte` | the ONE underline tab strip. Seven pages had near-identical copies; do not write an eighth |

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
./venv/bin/python scripts/check_svelte_icons.py       # ALWAYS after editing admin/src
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

## ⚠️ The drop folder is `/incoming`, NOT `/app/data/incoming`

Both api and ingest-worker mount the `sftp_data` volume at **`/incoming`**
(`INCOMING_DIR=/incoming`). The image ALSO contains a stale `/app/data/incoming`
with an `archive/` and a months-old xlsx, copied in from the repo's `data/`
directory at build time. It looks exactly like the real thing and is read by
nothing. A `docker cp` into it silently does nothing; you will watch the watcher
ignore your file and conclude the watcher is broken. Verify with
`docker inspect --format '{{range .Mounts}}…'` before believing a path.

## ⚠️ The safety line is decided in code, not asked for in the prompt

`app/disclaimer.py` runs **after** the model has written its answer: clinical
content (a dose, how to take it, what it treats, a side effect) gets exactly one
line, last, in the answer's language; stock/price/code/brand/category gets none.

The prompt asked for exactly this and the model obeyed *most of the time*.
Measured live, the same question class differed run to run — an English price
question got a medical warning while the same question in Burmese, and as a
follow-up, did not. **Whether a medical warning appears cannot depend on
sampling.** Both my hypotheses (Burmese, follow-ups) were wrong; it was plain
sampling non-determinism, and the case I was using as the control was the one
that failed.

Two details that will re-break it if touched:

- **Detection matches phrases about USE, never bare units.** `mg` appears in
  almost every brand name here (`BIOGESIC 500MG 10\`S`), so matching it marks
  every stock answer clinical and puts the warning back on all of them. Bold runs
  (product names) and article codes are stripped before matching, because
  `AIR-X DROP` contains "drop".
- **Streaming can only suppress what it has not sent.** `LeakFilter` holds a
  paragraph until complete, so a disclaimer written as the final paragraph is
  still buffered at flush and can be dropped. Written *inline*, it is already on
  screen; `_finish_stream()` corrects the text for the cache and conversation and
  accepts the one redundant sentence rather than pretending it can un-send it.

Pinned by `tests/test_disclaimer.py` (24 tests, no LLM, so they cannot flake).

## ⚠️ Ingest history: never let recording break an ingest, and summarise the RUN

`app/ingest_events.py` mirrors `history.record_turn`: **every write is wrapped
and degrades to a warning.** A file loading but not being recorded is a bad day;
a file failing to load *because* the recorder was down is much worse. If a
failure ever starts propagating out of `record()`, a Postgres blip stops being a
missing timeline and becomes a refused upload.

`latest()` summarises the whole run, **not the last event**. Taking the last row
looked right and was wrong: a successful load ends on `cache_cleared`, which
carries no `kind` and no row count — so every file that loaded correctly listed
as "Unknown, — rows", and the files with the most to say looked like the ones we
knew nothing about. Caught live, not in review. A rejection also outranks the
`set_aside` that follows it, or the listing shows "Kept so you can look at it"
where the reason belongs.

## ⚠️ `_resolve_sftp_file` needs BOTH defences

The download name is doubly untrusted: a URL segment naming a file a partner
uploaded over SFTP. `app/admin.py::_resolve_sftp_file` has two independent
checks and both are load-bearing:

1. `Path(name).name` discards directory parts, killing traversal.
2. The resolved path's parent must **be** one of the three folders, compared
   after `resolve()` on both sides.

Check 1 alone is bypassed by a **symlink planted in the drop folder** — a plain
single-segment filename, no traversal in the URL at all, which check 1 happily
passes through. Verified live against a real symlink to `/etc/passwd`: 404.

Note that multi-segment traversal never reaches the handler at all — it fails to
match the route and falls through to the SPA mount, which answers **200 with the
admin shell**. Nothing leaks, but a scanner reading status codes will flag it.
API 404s under `/admin/*` returning HTML rather than JSON is pre-existing.

## ⚠️ Two settings that reported success and did nothing

- **`catalog_mode`** was a live merge/full_sync control. It POSTed, Redis stored
  it, and the console printed "Catalog mode set to Merge — nothing is
  auto-deleted" — while `get_catalog_mode()` has hard-returned `full_sync` since
  2026-08-02, so the next file deleted rows anyway. A setting that lies about
  DATA LOSS is worse than no setting. `set_ingest_config` now raises on any
  attempted change (re-sending the current value is a no-op, so the page's
  GET→POST round trip still works), and the config exposes `catalog_mode_locked`.
- **`allow_shrink`** had no UI at all — a parameter on `POST /admin/upload` and
  nothing else, so the only way to override the "would delete more than half the
  rows" guard was hand-written curl. The one person overriding it was the one
  person who never saw the warning. It now lives in the SFTP page's per-file
  drawer, next to the count it would delete, and expires with that file.

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

## ⚠️ Conversation history — three ways a turn disappears

`HISTORY_ENABLED` (default ON) gives the chat multi-turn memory: `build_history_agent`
adds `add_history_to_context` + `db`, and nothing else — no LearningMachine, no
second extraction model. Only clients that send a **real `session_id`** get it
(`_conversational()`); the embed widget sends none and stays single-turn.

**1. `agent_id=None` stores the turn and then loses it, silently.**
`AgentSession.from_dict` only revives a stored run when the key `"agent_id"` is
present, and `RunOutput.to_dict` drops `None` fields. Agno assigns `agent.id`
*during* a run, so an agent that has not run yet writes `agent_id=None` — the row
lands in `agno_sessions` and `get_messages()` steps straight over it. No error.
Hence `build_history_agent` pins `id=HISTORY_AGENT_ID` ("city-pharma-agent")
instead of Agno's per-process uuid. This also makes a session written by one
worker readable by another, and survives a restart; before it, history was
leaning on Agno's **in-process session cache**.

Two sibling invariants, same silent failure, both pinned by `tests/test_history.py`:
a run whose `status` is not `completed`, or whose `parent_run_id` is set, is
stored and never replayed.

**2. The fast path and a cache hit record nothing.**
The fast path answers with a tool-less phrasing agent that has no `db` and no
`session_id`; a cache hit runs no agent at all. Both used to leave the
conversation empty, so the next turn ("which other shop has it?") had nothing to
resolve "it" against. `app/history.py::record_turn` now writes those turns —
called from `_remember()` at every such exit in `app/api.py`.

Do **not** "simplify" this by giving the phrasing agent a `db`. Agno persists what
it is given, and that is `build_phrasing_input()`: a language directive plus a
FACTS JSON blob of every row the SQL returned. It would replay as a user turn the
user never sent, and carry a 53-row payload into the next three prompts.

**3. A follow-up must never touch the shared answer cache.**
The cache key is `(data_version, model, store_id, message)` — no conversation in
it. Cache "which other shop" and the next conversation to type those three words
is served an answer about a different drug. `bump_session_turn()` makes turn 1
cacheable and turn 2+ bypass. The fast path is likewise skipped on follow-ups: it
resolves the drug from the message alone, and a follow-up names none.

`record_turn` calls agno's **private** `_session` / `_storage`. Funnelled through
one function; every failure degrades to "the chat forgot this turn", never a
failed answer. Re-verify after any agno upgrade.

## ⚠️ SSO/LDAP already exist — and shipped with two auth bypasses

`app/auth.py` has had Keycloak OIDC and LDAP since the baseline commit. Don't
rebuild them. Both are off by default (`OIDC_ENABLED`, `LDAP_ENABLED`), which is
the only reason the bypasses below were never exploitable. Operator guide:
`docs/SSO.md`.

**An empty LDAP password.** A simple bind with a valid DN and a zero-length
password is an *unauthenticated simple bind* (RFC 4513 §5.1.2). Servers configured
to allow it — some AD — answer **success**, which is login-as-anyone; a default
OpenLDAP refuses it server-side and ldap3 refuses it client-side (verified live).
Independently, the *old* code let ldap3's client-side refusal escape as an HTTP
**500** on any wrong password, because `LDAPBindError` is not `AuthError`.
`login_ldap` now rejects blank credentials before binding, closing both. Note
`/auth/login` falls through to LDAP whenever local auth fails.

**`ldap3.Connection` is always truthy.** It defines neither `__bool__` nor
`__len__`. The original `if not ldap3.Connection(..., auto_bind=True)` was dead
code; a wrong password only failed because `auto_bind=True` *raised* — and
`LDAPBindError` is not `AuthError`, so it escaped as a 500. Bind explicitly with
`auto_bind=False` and check `.bound`.

**OIDC `state` was the constant `"citcare"` and was never read.** That is
login-CSRF: replay your own `code` at a victim's callback and their browser is
signed into your account. Now a signed, time-boxed nonce, matched against an
httponly cookie (`SameSite=lax` — `strict` is not sent on the IdP's redirect back).

**The SSO token rides back in the URL *fragment*, not a query param.** Fragments
never reach a server, so the token stays out of access logs and `Referer`. The SPA
scrubs it via `history.replaceState` on read.

`id_token` signatures are deliberately not verified: the code is redeemed over TLS
against `token_endpoint` authenticated with `client_secret`, and the profile comes
from `userinfo`. **Make the Keycloak client public (no secret) and that reasoning
collapses** — you'd then have to verify against the realm JWKS.

Roles live in the `users` table, never in the token, and `_merge_external` never
INSERTs. So a Keycloak realm admin cannot mint a pharmacy admin. Keep it that way.

**SSO/LDAP are now UI-configurable** (2026-07-10). `ldap_*`/`oidc_*` read from an
*effective* layer — env defaults overlaid with a Redis override hash (`auth.*`
keys in `pharmacy:config`), read fresh every login so a change needs no restart.
`app/auth.py::effective_auth()` builds it; the ldap/oidc helpers take an explicit
`cfg`, not `get_settings()`. Admin page: **Configuration → Authentication**
(`/admin/auth`), backed by `GET/PUT /admin/auth-config`. Secrets are write-only:
GET masks `oidc_client_secret`/`ldap_bind_password` to `""` + a `_set` bool; PUT
skips a secret sent empty (blank field = keep current). `.env` still works as the
boot default.

## ⚠️ Console access needs admin approval (2026-07-10)

`users.approved` gates the admin console. A new account authenticates but is held
on the **CMHL Secure Platform** notice until an admin approves it (Users page →
**Access** column → Approve). Enforcement is server-side: `require_admin`
re-checks `approved`+`active` against the DB **per request**, not from the token,
so approval takes effect on the account's existing session (no re-login) and
revocation is immediate. The hold screen polls `/auth/me` every 5s.

- **Migration must not lock out the current admin.** `ensure_users_table` adds the
  column AND `UPDATE users SET approved=TRUE` for every existing row, in a block
  guarded on "column did not exist" so it runs exactly once. Skip that guard and
  either everyone gets re-approved every boot, or the existing super_admin is
  locked out. `seed_super_admin` inserts `approved=TRUE`.
- Admin-created users default to **pending**, so the gate is exercised. If you'd
  rather they be auto-approved, flip the `create_user(approved=…)` default.

## ⚠️ An unimported icon blanks the WHOLE SPA — and the build won't catch it

Referencing a component/icon in a `.svelte` file without importing it (e.g.
`icon: KeyRound` in a nav entry) is **not** a Vite build error: it becomes an
undefined global, the bundle builds clean, then the SPA throws `ReferenceError`
at startup and **every page renders blank**. `svelte-check` does not catch it
either (a plain-JS `<script>` is not checked for undefined identifiers — verified:
0 errors with the bug present). `scripts/check_svelte_icons.py` does; it runs as
`tests/test_svelte_builds.py`. After editing any `.svelte`, and always after a
rebuild, **open the page in a browser** — a 200 on `/admin/` is only the HTML
shell and says nothing about whether the JS runs.

## ⚠️ The SSE trace is additive-only — don't touch the frozen fields

The chat stream now also emits `event: plan` (a one-line template plan, chosen by
intent+language in `app.api._plan_line`, **no LLM call**) and an `args` object on
`event: step` (the tool's argument, for distinct labels like "Searching for X").
Both are **additive** — the embed widget ignores unknown events and JSON fields.
The frozen contract — `event: step`/`event: result`, `data:{delta}`, `[DONE]`,
`\n\n` frame split, every `data-*` widget attribute — must not change. Store scope
is NOT in `args` (it rides a contextvar), so a step label cannot leak a sibling
branch.

## ⚠️ Product names contain backticks

2,790 of 5,292 catalog rows use a backtick as an apostrophe (`PARACAP
PARACETAMOL 10`S`). That is Markdown's inline-code delimiter. A permissive
`` `([^`]+)` `` pairs the apostrophe with the backtick opening an article code
later on the line and eats the bold marker between them. `renderMarkdown()`
(`admin/src/lib/aurora/markdown.js`) therefore forbids `*` and whitespace inside a
code span, and the system prompt tells the model to write article codes **bare**
— `inline()` chips any 10–14 digit run on its own. Emitted HTML is parked behind a
sentinel before the bare-code pass, or that pass re-wraps digits inside a chip it
just made and produces nested `<button>`s. Pinned by `tests/test_markdown_render.py`
(runs the real module through `node`; skips if node is absent).

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
  - `drug_alias` has an **API** write path (`GET/POST/DELETE /admin/aliases`,
    `ensure_admin_schema` creates the table) but **nothing populates it**: no
    admin UI references those endpoints and nothing learns an alias
    automatically, so the alias tier is still always a miss and resolution falls
    to trigram. The two things that would change that: a "pharmacist clarified →
    learn it" write, and a list of the local shorthand staff actually use — which
    is question 9 in the client doc, currently unanswered.
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
- Admin login: `admin@citcare.local` / `Admin123!` (`super_admin`). **NOT the only user** — the
`users` table also holds two `admin`-role accounts left behind by
`tests/test_approval.py` (`appr-188413e477@corp.mm`, `appr-150e95de29@corp.mm`,
ids 6 and 9). They are `approved`, so if LDAP or SSO is ever enabled they are
real accounts someone could authenticate as. See `docs/PYTEST_DEBRIS_PURGE.md`.
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
  Display headings via `.page-title` (**Nunito** — matched to the CityCare
  wordmark; it is NOT Space Grotesk any more); body is **IBM Plex Sans**; Burmese
  renders in Noto Sans Myanmar. **There are only two surface
  levels** (`surface`, `surface-2`) — there is no `--color-surface-3` token. Any
  `bg-surface-3` is a phantom that renders as no colour; repoint it to `bg-surface`
  or `bg-surface-2`. Every colour class MUST resolve to a `--color-*` token in
  `admin/src/app.css`.

- **Design (CityCare white-label, 2026-08-17).** The accent is **indigo
  `#2F3293`** with a `#00ADEF` secondary, taken from the CityCare logo, and the
  display face is **Nunito**. The earlier teal (`#006869`) and Space Grotesk are
  GONE from the console — if you find either in a doc, the doc is stale. Logo,
  product name and parent name are **admin-editable** and live in Postgres
  (`brand_assets` / `brand_config`, served by `GET /brand` and
  `/brand/asset/{key}`; keys are `icon`, `lockup`, `lockup_dark`, `parent`).
  They are in the DATABASE, not the image, because a rebuild would otherwise
  erase an uploaded logo. The widget default `data-accent` is a separate
  contract — see the embed section. **`text-on-accent` exists on
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

## ⚠️ Analytics instrumentation (2026-08-17) — read before touching the console

The console now measures the agent per CALL, not per turn. Contract:
`docs/ANALYTICS_CONTRACT.md` — normative, and written before the code so several
agents could work disjointly. Source of the design:
`~/Desktop/AI-AGENT-ANALYTICS-PLAYBOOK.md`.

**Schema** (migrations 0008–0010, mirrored by `ensure_*` in `app/activity.py`,
because this codebase applies schema at BOOT — a `.sql` file alone never reaches
a running database):

- `tool_calls` — one row per tool invocation, with a **three-state** outcome:
  `succeeded` / `refused` / `failed`. **A refusal is not a failure.** Today the
  only deliberate refusal is a store-scope decline (`_effective_site` answering
  for the session's own store instead of the one asked for). Classify at the
  return site where the reason is known, never by string-matching an error
  later. Collapsing refusals into failures is exactly how a working tool
  acquires a 56% "failure rate".
- `llm_calls` — one row per model call: prompt / completion / reasoning /
  **cache_read** / cache_creation tokens, ttft, cost, `cost_is_estimated`. Tokens
  belong HERE, not on the turn: turn #20777 made 9 calls, of which seq 8 cost
  $0.05 with 0 cached against ~$0.006 for its siblings which read ~3–5k from
  cache. At turn grain that lever is invisible. **The cache split cannot be
  backfilled** — capture it from day one.
- `chat_logs.actor_email` / `actor_role` / `gave_up`, `chat_feedback.turn_id`.
  `gave_up` is checked against the ANSWER TEXT (the two `answer_filter`
  fallbacks), because a turn can record success while apologising on screen.

**The capture layer is wired in exactly two places and fails silently if either
is missed.** `activity.begin_turn()` opens the buffer (`chat_stream.gen`, and
`_answer` only when `current_turn() is None` — `begin_turn` RESETS, so calling it
unconditionally in `_answer` would discard everything the streaming path already
captured); `_log_turn` flushes it with the id `log_chat` now returns. Miss either
and `record_tool_call` no-ops on an empty contextvar, `tool_calls` stays empty
forever, and nothing errors. This shipped broken once and all 804 tests passed.

**Never resolve the turn id by matching question text.** An early version looked
it up by `question + store + session, newest first`; two identical questions in
flight put both turns' calls on one turn. `log_chat` returns the id from all
three of its fallback INSERT tiers — use that.

### ⚠️ Timezone: buckets are cut in the CALLER's zone

Postgres runs `Etc/UTC`. `date_trunc('day', ts)` therefore cut every chart's
"day" at 06:30 local for a GMT+6:30 reader — six and a half hours of each
morning landed on the previous day, and nobody noticed because the chart still
looked plausible. Every bucketing endpoint now takes `tz` (IANA, declared on
`SharedFilters` so no handler can forget it) and buckets on
`ts AT TIME ZONE $tz`. An unknown zone is a **400**, never a silent UTC fallback.

**Every bucketing payload ECHOES the zone it used**, and the UI renders its
header chip from the echo, never from what it sent. Without that, an endpoint
that failed to declare `tz` would answer 200 with UTC buckets under a chip
saying "GMT+6:30" — the same bug wearing a nicer hat.

**`tzdata` is a REQUIRED dependency** (requirements.txt). `python:3.12-slim`
ships an incomplete `/usr/share/zoneinfo`: `Asia/Yangon` resolves but
`Asia/Rangoon` — what Chrome actually reports on this machine — does not, nor do
`Asia/Calcutta` or the whole `US/*` family. Without it the validator rejected the
zone the user's own browser sent and **every panel on both console pages
rendered a 400**. `tests/test_console_v2.py` pins the aliases, so dropping the
dependency fails loudly instead of blanking the console.

### Two verification traps, both hit on the same day

- **A 200 is not a working product.** Every curl check passed while both console
  pages were entirely dead, because they all sent the canonical zone name. It was
  found by screenshotting the page and looking at it.
- **A missing scrollbar is not missing data.** Headless Chrome screenshots taken
  with `--hide-scrollbars` hide the scroll affordance; a correctly scrolling
  heatmap was reported as clipped. Probe `scrollWidth` vs `clientWidth` before
  believing a layout defect in a screenshot.

### Building the admin SPA: use the lock

`scripts/build-admin.sh` (`--wait` to queue). Two processes running
`npm run build` in `admin/` clobber `.svelte-kit/output` and the loser dies with
`ENOENT … manifest-full.js`, which reads exactly like a real failure. Exit **75**
means "someone else is building", not a defect.

Two substitutes that do NOT work, both measured rather than assumed:
`vite build --outDir …` is ignored by the SvelteKit plugin (it writes
`.svelte-kit/output/**` anyway AND rewrites `build/`), and `svelte-check` cannot
see a missing export — a named import of a non-exported symbol reports
`0 errors`. `svelte-check` answers "does my syntax compile"; only a real build
answers "does the bundle link".

### Shared chart components

`admin/src/lib/charts/` (Kpi, Section, LineChart, StackedBars, RankBars, Donut,
Heatmap, Funnel, Table, TurnDrawer, TraceView, GapCard, WarnBar, DeltaChip,
TzChip + `format.js`, `geom.js`). Every panel imports from there. **Do not fork
one into a route folder** — a chart that renders an unknown as `0` in one place
and `—` in another is what this directory exists to prevent. `FilterBar` stays
in `routes/analytics/`; it knows analytics params.

## The dev loop

Three loops, all measured on this laptop. Use them; the slow paths below are
what they replace.

| change | slow path | fast path |
|---|---|---|
| Python | `docker compose build` + `up`, 2-4 min | dev overlay, **1.8s** |
| Console | `npm run build` + `docker cp`, ~12s | `npm run dev`, **1.3s** |
| Full suite | `pytest`, **449s** | `./scripts/test.sh`, **85s** |

### Backend — `docker-compose.dev.yml`

```
docker compose -p pharmacy-opt \
  -f docker-compose.yml -f docker-compose.optimized.yml -f docker-compose.dev.yml \
  up -d api ingest-worker
```

Bind-mounts `./app` read-only over the copy baked into the image and runs
uvicorn with `--reload`. Nothing about the image or the shipping path changes —
it is a third file you opt into, and **leaving it out of the `up` puts the baked
code back**, which is exactly what a release deploy must do.

A container started this way is running the WORKING TREE. Never read `/version`
off it as evidence that a build contains something; it reports `git_sha: "dev"`
and `is_release_build: false` for that reason.

### Console — the vite dev server

```
cd admin && npm run dev          # http://localhost:5173
```

The dev server mounts the app at the ROOT, not at `/admin`, and proxies the API
to :8091. That is not cosmetic: 84 of the backend's routes are `/admin/...`, so
a dev server mounted at `/admin` answers `/admin/analytics/summary` with the
SPA's HTML fallback and the console looks like it is talking to a broken
backend. `svelte.config.js` keys the base off `NODE_ENV`, so `vite build` — the
shipped bundle — still gets `/admin`.

`/version` is the one path both sides claim. `vite.config.js` splits them on the
`Accept` header the browser sets itself: a navigation gets the Version page, the
console's own fetch gets the JSON.

For a production-shaped check, `./scripts/build-admin.sh --wait` still works and
the dev overlay mounts `admin/build`, so no `docker cp` is needed any more.

⚠️ **Rebuilding the console while the dev overlay is up needs a `docker restart
pharmacy-opt-api-1`.** `build-admin.sh` REPLACES the `admin/build` directory
rather than writing into it, so the container's bind mount is left pointing at
the old, deleted inode. The host has `admin/build/index.html`; the container
raises `FileNotFoundError: /app/admin_build/index.html` and every `/admin` route
answers **500**. Nothing about the build is wrong and nothing in its output says
so. Bring the overlay up AFTER a build, or restart the api container after one.

### Where the console's pages are, and how the links between them resolve

The rail is 15 rows in 6 groups; the sections of the old Analytics page are
routes. `admin/src/lib/analytics/AnalyticsPage.svelte` is the one component that
draws all fourteen analytics sections, and a page mounts it with the sections it
wants:

| route | sections |
| --- | --- |
| `/analytics` Health & usage | overview, performance, cache, health |
| `/conversations` | questions, users |
| `/quality?tab=answers` | quality, diagnostics |
| `/cost` | cost |
| `/embed?tab=analytics` | embeds |
| `/activity` (off-rail) | feed, audit, trends, explore |
| `/security-log` | audit, pinned to `source=auth` |

⚠️ **A link between sections must name the SECTION, never a page or a tab.**
`crossTo('cost')` / `drillTo('model', x, 'cost')` resolve through
`$lib/analytics/routes.js`; a hand-written href does not, and will not follow
the section when it moves.

This rule exists because the alternative shipped. The sections began as ten TABS
whose ids were section names (`?tab=cost`). When they were grouped into six, the
tab ids became group names and **all 46 links that named a section were left
naming it** — 26 `drillTo`, 8 `setTab`, 6 health tiles, 6 direct `p.set('tab',…)`.
`tab` falls back to `overview` for an unrecognised value, so every one of them
navigated, applied its filter, and drew the WRONG panel. Measured by clicking:
14 of 14 clickable links on Overview landed back on Overview. **The numbers on
screen changed each time, which is exactly why nobody reported it for a
release.** `tests/test_console_routes.py` pins it.

⚠️ **A section must appear in `SECTION_FEEDS`.** Splitting one page into six
means six loads, so `loadAll` fetches only what the page's sections read.
A section left out of that map is not an error — every panel renders `blank()`
as an em-dash, so the page reads as a quiet day rather than as a page that asked
for nothing. Pinned by the same test file.

### Tests — `./scripts/test.sh`

Parallel by default (8 workers), and it uses `./venv/bin/python`. Both matter:

* Under the SYSTEM python the suite does not fail — it **skips the three LDAP
  bind/TLS security guards** and reports green, with one line in the header as
  the only sign.
* `-n 8` needed no test changes because the suite was already built for process
  isolation: a database per PID (`tests/dbguard.py`) and one of 15 Redis indexes
  per PID (`tests/conftest.py::_claim_redis_db`). Eight rather than "auto"
  leaves indexes spare for a second run.

Paths or `-k` run serially — forking eight workers for twelve tests is slower
than not forking. `--serial` for an order-dependent failure or for `pdb`, which
has no terminal under xdist.

⚠️ **Seeding goes through `tests/pgconn.py`. Never open your own connection.**

Sixteen modules each carried a byte-identical `_pg` that opened a fresh asyncpg
connection AND a fresh event loop for every single statement. The seeding
fixtures make dozens of calls each, so the suite spent most of its wall clock on
Postgres handshakes: `--durations` showed its slowest twenty-five entries were
all fixture SETUP, none of them a test body. `test_console_v2.py` alone was
about thirty connections per test across forty-nine tests. Routing them through
one connection per PROCESS — still private, still never `app.db`'s pool — took
`./scripts/test.sh` from **70s to 24s with every test still running**.

This paragraph used to say the flat per-test SETUP was `api_client` running the
app lifespan, and that parallelism was therefore the only lever. That was wrong,
and it is the kind of wrong that stops anybody looking again: it named a cause
that cannot be fixed. Measure before believing it.

Two modules keep their own connection on purpose — `run_isolated` in
`test_catalog_full_sync.py` and `test_ingest_replace.py` opens a transaction and
rolls it back, which is what keeps a full-sync DELETE off the real catalog. They
are listed in `_MAY_CONNECT` in `tests/test_postgres_isolation.py`, which pins
the rule; the pattern is one copy-paste from returning and returns invisibly.

### One console page for "what has this system been doing?"

`/admin/activity` was folded into `/admin/analytics` — fourteen tabs across two
pages became **six groups on one**, and `routes/activity/+page.js` is now a
redirect that carries every filter across. The four old Activity tabs live in
`$lib/activity/` (FeedTab, AuditTab, TrendsTab, ExploreTab, ActivityFilters,
`shared.js`) and are rendered by the Analytics page.

Three things about that page are load-bearing:

* **`?tab=` names a GROUP, not a section.** An unknown value falls back to
  Overview WITHOUT erroring, so a link that writes a section name there
  navigates, renders, and lands somewhere else. Cross-links go through
  `openSection()` in `$lib/activity/shared.js`.
* **The section parameter is `sec`, never `sub`.** `sub` is already Explore's
  stacking subgroup and its own links carry `sub=source`.
* **The two halves have separate filter bars** and separate URL keys, and
  crossing between them clears the other side's. `actor` and `q` are spelled the
  same in both and mean different things — an actor typed against chat turns
  would silently narrow the event feed. The eighteen analytics endpoints are
  also not fetched while an activity group is on screen.

### The honesty rules the whole console is built on

`—` for absent, `0` only for measured zero. An unpriced cost is "not configured",
never `$0.00` — a zero reads as free and nobody notices for months. Every rate
ships with its denominator ("92% of 25 rated"). A block that cannot honour the
active filters says so per card (`filters_applied: false`). No placeholder KPIs.
Pre-instrumentation rows are `not recorded`, never folded into a bucket.
