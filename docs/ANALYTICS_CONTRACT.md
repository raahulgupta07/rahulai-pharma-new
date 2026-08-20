# Analytics instrumentation contract — 2026-08-17

Fixed before any code was written, so four agents could work on disjoint files
without negotiating. Derived from `AI-AGENT-ANALYTICS-PLAYBOOK.md`.

Everything here is normative. If you find a reason to deviate, say so in your
report rather than deviating silently — another agent is coding against this.

## Why

`chat_logs` records a turn: one row, one latency number, one token count, and a
JSON array of tool *names*. That is not enough to answer any of:

- did a tool succeed, refuse, or fail? (`tools: ["get_stock"]` says only that it ran)
- which of a turn's several LLM calls got slow or expensive?
- who asked?

The playbook's first rule is that an outcome field needs at least three states,
because a tool that *correctly declines* and a tool that *crashed* both look
like "not a success" otherwise, and every dashboard built on that lies.

## 1. Schema

Two new tables and three new columns. Migration files AND the idempotent
`ensure_*` startup path must both be updated — this codebase applies schema at
boot (see `app/api.py` around the `ensure_app_events` / branding calls), and a
`.sql` file alone will not reach a running database.

`migrations/0008_turn_calls.sql`:

```sql
-- Per tool call. The three-state outcome is the point of this table.
CREATE TABLE IF NOT EXISTS tool_calls (
  id            bigserial PRIMARY KEY,
  turn_id       bigint      NOT NULL REFERENCES chat_logs(id) ON DELETE CASCADE,
  seq           int         NOT NULL,          -- order within the turn, 0-based
  name          text        NOT NULL,
  arguments     jsonb,
  outcome       text        NOT NULL CHECK (outcome IN ('succeeded','refused','failed')),
  error_message text,
  attempt       int         NOT NULL DEFAULT 1,
  duration_ms   int,
  ts            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_turn_idx    ON tool_calls (turn_id, seq);
CREATE INDEX IF NOT EXISTS tool_calls_outcome_idx ON tool_calls (outcome, ts DESC);
CREATE INDEX IF NOT EXISTS tool_calls_name_idx    ON tool_calls (name, ts DESC);

-- Per LLM call. One turn can be many calls; token counts belong HERE, not on
-- the turn. The cache split cannot be backfilled, so it is captured from day 1.
CREATE TABLE IF NOT EXISTS llm_calls (
  id                    bigserial PRIMARY KEY,
  turn_id               bigint      NOT NULL REFERENCES chat_logs(id) ON DELETE CASCADE,
  seq                   int         NOT NULL,
  model                 text,
  prompt_tokens         int,
  completion_tokens     int,
  reasoning_tokens      int,
  cache_read_tokens     int,
  cache_creation_tokens int,
  ttft_ms               int,
  duration_ms           int,
  cost_usd              numeric(12,6),
  cost_is_estimated     boolean     NOT NULL DEFAULT false,
  finish_reason         text,
  ts                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_turn_idx  ON llm_calls (turn_id, seq);
CREATE INDEX IF NOT EXISTS llm_calls_model_idx ON llm_calls (model, ts DESC);
```

`migrations/0009_chat_logs_actor.sql`:

```sql
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_email text;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_role  text;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS gave_up     boolean;
CREATE INDEX IF NOT EXISTS chat_logs_actor_idx ON chat_logs (actor_email, ts DESC);
```

`visitor_id` for anonymous widget users is deliberately NOT in this contract.
It is a disclosure decision, not an engineering one, and the product owner has
not made it.

## 2. Outcome semantics — the rule everything else rests on

| outcome | meaning |
|---|---|
| `succeeded` | the tool did the thing |
| `refused` | the tool deliberately declined and redirected — **not a failure** |
| `failed` | something broke |

A tool signalling a deliberate refusal through the same channel as a crash is
what produced a 56% "failure rate" on a tool that was working correctly in the
product this playbook came from. Classify at the raise/return site, where the
reason is known — never by string-matching an error later.

`chat_logs.gave_up` covers the second rule: a turn can record success while the
answer text apologises for giving up. Set it by checking what the user actually
saw, and leave it `NULL` when not evaluated (never `false`).

## 3. Absent vs zero

- A metric with no data returns `null`, and the UI renders `—`.
- `0` means measured zero.
- A cost with no configured price is `null` + `cost_is_estimated` semantics —
  **never `0.0`**. A zero reads as "free" and nobody notices for months.
- Any endpoint returning a rate must also return its denominator, so the UI can
  say "92% of 25 rated" rather than a bare percentage over a tiny sample.

## 4. Shared query params — every analytics endpoint takes the same object

Retrofitting consistent filtering across twenty endpoints is miserable. All of
these are optional:

```
start   ISO8601      inclusive
end     ISO8601      see the date rule below
store   text         comma-separated store ids
lang    text         comma-separated
path    text         comma-separated (fast_path|agent)
embed   text         comma-separated embed ids; the literal `none` selects unattributed turns
model   text         comma-separated
actor   text         comma-separated actor emails
cached  true|false
rated   up|down|any
```

### The date rule (amended 2026-08-17)

`end` was originally specified as a plain exclusive bound. That collided with
the legacy `to`, which treats a bare date as the whole day, so `to=2026-08-17`
included the 17th while `end=2026-08-17` excluded it — a silent one-day
difference between two spellings on the same endpoint.

The bound now depends on whether a time was given, and both spellings agree:

- **bare date** (`2026-08-17`) → the WHOLE day is included, i.e. `ts < date + 1 day`
- **date with a time** (`2026-08-17T14:00:00Z`) → exclusive at that instant

Rationale: every date picker a human touches means "through the 17th" when it
says 17 Aug. A bound that quietly drops the most recent day is the kind of error
nobody reports, because the chart still looks plausible.

`from`/`to` remain supported for existing callers and now mean exactly what
`start`/`end` mean. If a request supplies both spellings with conflicting
values, that is a caller bug: return 400 rather than silently preferring one.

### The store predicate is `tools._site_clause`, not `=`

Do not "simplify" this to equality or `= ANY`. A site token is legitimately the
full code (`20005-CCYK`), its numeric prefix (`20005`), or its alpha suffix
(`CCYK`) — everywhere in this codebase, including `users.store_id`, which is
the pin `caller_store_scope` reads. Plain equality would make the same string
mean two different things depending on whether it was typed into a filter box
or into a user's store pin.

`_site_clause` is anchored on all three forms: `20005` matches `20005-CCYK`,
and `2000` matches nothing. The predecessor was `store_id ILIKE '%'||$n||'%'`,
which matched a sibling branch — `CMHL-1` returned CMHL-10, CMHL-19, CMHL-100.
On a pharmacy chain that is wrong data, not a cosmetic issue.

### Lists

`store`, `lang`, `path`, `embed`, `model`, `actor` are comma-separated lists on
EVERY analytics endpoint, old and new. A single value must produce byte-for-byte
the predicate it produced before the list support was added, so this is not a
behaviour change for any existing caller.

**Every one of these must be a declared FastAPI parameter.** An undeclared query
param is silently dropped by FastAPI, and the endpoint then answers 200 with
unfiltered data — a whole product in this account shipped 19 endpoints that
ignored their scope filter that way, with a green test suite.

Store scoping is not one of these filters: it is enforced server-side from the
caller's own scope (`caller_store_scope`) and a caller must never be able to
widen it by passing `store`.

## 5. Endpoints

Existing, keep working: `/admin/analytics/summary`, `/questions`,
`/question/{id}`, `/embeds`, `/repeats`, `/data-health`, `/timeseries`,
`/tools`, `/paths`, `/cost`.

New:

```
GET /admin/analytics/tool-outcomes   bars: {name, succeeded, refused, failed, avg_ms}[]
GET /admin/analytics/llm-usage       per model: {model, calls, prompt_tokens, completion_tokens,
                                     cache_read_tokens, cache_creation_tokens, cost_usd|null,
                                     cost_is_estimated, p50_ttft_ms|null}[]
GET /admin/analytics/trace/{turn_id} one turn, every call in order:
                                     {turn:{…}, calls:[{kind:'tool'|'llm', seq, …}]}
GET /admin/analytics/diagnosis       failed ∪ negative feedback ∪ gave_up, each row carrying
                                     issue_type: 'failed_tool'|'negative_feedback'|'gave_up'|'both'
GET /admin/analytics/actors          console activity by actor (from app_events + chat_logs)
GET /admin/analytics/intents         keyword-bucket clusters + the tool heatmap matrix
```

Keyword buckets, not embeddings — crude, cheap, debuggable, and good enough to
answer "what do people come here for". Buckets for this product:

```python
"stock":      ["stock", "available", "ရှိ", "ရှိလား", "in stock"],
"price":      ["price", "cost", "how much", "ဈေး", "စျေး"],
"substitute": ["substitute", "alternative", "instead", "အစား"],
"branch":     ["branch", "store", "which shop", "ဆိုင်"],
"dosage":     ["dose", "dosage", "how many", "mg", "သောက်"],
```

### A block that cannot obey the filters must say so

`chat_feedback` carries its own copies of question/answer and no `turn_id`, so
the feedback KPI cannot honour lang / embed / path / actor / cached / rated. A
number sitting under filter chips it silently ignores is the same lie as an
undeclared parameter, just further from the wire.

Two parts, and both are required:

1. `chat_feedback` gains `turn_id bigint REFERENCES chat_logs(id)`, populated
   going forward. Existing rows keep `NULL` — they are unattributable and must
   not be guessed at by matching question text, which is exactly the
   correlated-subquery mistake that made `?rated=down` return the whole table.
2. Until a block can honour the active filters, its payload carries
   `filters_applied: false` and the UI marks the number as unfiltered. This is
   not a temporary hack to remove later: any block that cannot obey a filter
   must keep declaring it.

## 6. Section isolation

Every panel computes independently and is wrapped, so one broken section
returns its empty shape rather than 500-ing the whole page. The empty shape must
be *shaped like the real one* — the frontend must never branch on a missing key.

## 7. What must NOT happen

- No placeholder KPI. A hardcoded `"90%"` renders as a real number on a real
  dashboard and nobody can tell it is fake. Return `null` instead.
- No backfill of the 122 pre-instrumentation turns. They are unknown, and
  unknown is a value the UI can render.
- No new dependency for charts. Hand-rolled inline SVG, as the page already does.

---

# Addendum — 2026-08-17 · console v2

Fixed before the second round of agents. Same rules as above still apply; these
are additions and one correctness fix.

## A. Timezone — a real defect, not a feature

The database runs `Etc/UTC` and buckets with `date_trunc('day', ts)`, while the
console labels those buckets with the browser's local time (GMT+6:30 in Yangon).
Every "day" on every chart therefore runs **06:30 → 06:30 local**, and the first
six and a half hours of each morning are attributed to the previous day.

Fix: every endpoint that buckets by time takes `tz` (an IANA name, e.g.
`Asia/Yangon`), defaults to UTC, and buckets as:

```sql
date_trunc('day', ts AT TIME ZONE $tz)
```

The UI sends the browser's zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`)
and shows it in the header so the reader knows which midnight they are looking at.
An invalid zone is a 400 naming the parameter — never a silent fallback to UTC,
because a silently wrong bucket is the bug being fixed.

## B. Period deltas

Every KPI ships its movement, and **absolute alongside percentage**:

```json
{"value": 136, "prev": 94, "delta": 42, "delta_pct": 44.7, "prev_period": {"start":"…","end":"…"}}
```

`prev` is the immediately preceding window of the same length. When there is no
prior window, `delta` and `delta_pct` are `null` and the UI prints "no prior
period" — never `0%`, which reads as "no change". A percentage without its
absolute is banned: a rise from 1 to 4 is +3, and calling it 300% is noise.

## C. New endpoints

```
GET /admin/activity/summary    KPI row for the feed: events, browser vs testclient,
                               failures by status class, sign-in outcomes, files set aside
GET /admin/activity/trends     series + movers: {series[], movers:[{key, spark[], delta, delta_pct}]}
GET /admin/activity/explore    the pivot: measure × dimension × rollup × top-N →
                               {series[], table:[{key, n, min, max, avg, sum, share}]}
GET /admin/activity/audit      sign-in outcomes over time, 403s by action, the event list
GET /admin/analytics/llm-calls one row per model call: turn_id, seq, model, prompt,
                               cache_read, completion, reasoning, ttft_ms, cost_usd,
                               cost_is_estimated
GET /admin/analytics/economics blended $/1M, cost per turn, cache-read share,
                               prompt:completion ratio — each with its denominator
```

`explore` must whitelist its `measure` and `by` values server-side and 400 on
anything else. It builds SQL from a fixed map, never from the raw string.

## D. Cost, restated now that it is real

Cost is no longer hypothetical: 22 calls, $0.261554, `cost_is_estimated = false`.
The rules that matter for the UI:

- **completion is 3.7% of tokens** (6,797 of 182,187). Any UI that implies
  shortening answers saves money is wrong.
- **cache read is 26.5% of prompt tokens** and is the largest available lever.
- The per-call table must show the cache split, because turn #20772 contains
  seq 2 at $0.0026 (5,597 cached) and seq 4 at $0.0388 (0 cached) — 15× apart
  inside one turn. At turn grain that is one number and the lever is invisible.

## E. Shared chart components

`admin/src/lib/charts/` now holds Kpi, Section, LineChart, StackedBars, RankBars,
Donut, Heatmap, Funnel, Table, TurnDrawer, TraceView, GapCard, WarnBar, plus
`format.js` and `geom.js`. Both the analytics page and the activity page import
from `$lib/charts/…`. Do not fork a second copy of any of these; a chart that
renders an unknown as `0` in one place and `—` in another is the failure this
directory exists to prevent.

`FilterBar` stays in `routes/analytics/` — it knows analytics-specific params.

## F. Two amendments — 2026-08-17, after the v2 agents started

### F1. Every bucketing payload echoes the zone it actually used

```json
{"tz": "Asia/Yangon", "rows": [...]}
```

Not optional, and not cosmetic. If an endpoint fails to DECLARE `tz`, FastAPI
drops the parameter silently and answers 200 with UTC buckets — and the header
chip would then tell the reader "GMT+6:30" over UTC data. That is the original
timezone bug wearing a nicer hat, and it is harder to spot because the UI now
looks like it handles timezones.

The UI renders the chip from the ECHO, never from what it sent. When the echo is
absent or differs from the request, the chip must say `buckets: UTC` (or the zone
actually used) rather than repeating the request back to the reader.

The same rule generalises: **a payload that reports what it did beats a UI that
assumes the request was honoured.** Any parameter that changes the meaning of the
numbers — not merely which rows are returned — should be echoed.

### F2. The ingest funnel needs an endpoint

`/admin/analytics/data-health` returns catalog / inventory / freshness / by_day
and says nothing about files arriving. The Data health tab's funnel needs:

```json
"funnel": {"arrived": 573, "detected": 286, "checked": 281, "loaded": 281, "set_aside": 190}
```

sourced from `ingest_events.step`. Until it exists the funnel renders every stage
as `—` with a note naming the missing key — it does not render the mockup's
numbers, which would be fabrication.

Why it matters: measured today, 573 arrived and 286 were detected. **Half of
everything arriving is not recognised**, and nothing in the console says so.
