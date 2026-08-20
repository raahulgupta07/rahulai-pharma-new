-- 0008 — per-call detail for a chat turn: tool_calls + llm_calls.
--
-- `chat_logs` records a turn as ONE row: one latency number, one token count,
-- and a JSON array of tool NAMES. That array says a tool ran. It cannot say
-- whether the tool did the thing, deliberately declined, or crashed — and it
-- cannot say which of a turn's several model calls got slow or expensive.
--
-- ⚠️ THIS FILE ALONE CHANGES NOTHING ON A RUNNING STACK. Schema here is applied
-- at boot by the idempotent `ensure_*` helpers (see `app/api.py`'s lifespan).
-- The matching helper is `app.activity.ensure_turn_calls()`; keep the two in
-- step or a fresh container and a hand-migrated database will diverge.
--
-- ⚠️ THE THREE-STATE OUTCOME IS THE ENTIRE POINT OF `tool_calls`.
-- A boolean `ok` collapses "the query was too broad, so I declined and
-- redirected" into the same bucket as "the database was unreachable". Every
-- dashboard built on that reports a working tool as broken: in the product this
-- design came from, a 56% "failure rate" was a tool refusing correctly. The
-- CHECK below is what stops a future writer from re-flattening it.
--
--   succeeded — the tool did the thing (an EMPTY result set is a success:
--               "no branch stocks this" is an answer, not a failure)
--   refused   — the tool deliberately declined and redirected. NOT a failure.
--   failed    — an exception, a timeout, a malformed result.
--
-- Classification happens at the raise/return site, where the reason is known.
-- Never by string-matching an error message afterwards.

CREATE TABLE IF NOT EXISTS tool_calls (
  id            bigserial PRIMARY KEY,
  turn_id       bigint      NOT NULL REFERENCES chat_logs(id) ON DELETE CASCADE,
  -- Order within the turn, 0-based. Shared with `llm_calls.seq`: a single
  -- counter runs across BOTH tables so `GET /admin/analytics/trace/{turn_id}`
  -- can merge them and ORDER BY seq to get real call order. Per-table sequences
  -- would each start at 0 and no merge could interleave them.
  seq           int         NOT NULL,
  name          text        NOT NULL,
  -- The tool's arguments, NOT the user's message. Store scope is deliberately
  -- absent: it rides a contextvar and is never a tool argument, so a row here
  -- cannot name a sibling branch.
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

-- Per LLM call. One turn is many calls — the agent's tool loop makes one
-- provider request per iteration — so token counts belong HERE, not on the turn.
--
-- ⚠️ NULL IS NOT ZERO, and the cache split is why this table exists on day 1.
-- A prompt-cache read cannot be backfilled: once the turn is over, nobody can
-- ever recover whether those 4,000 input tokens were billed at full rate or at
-- the cached rate. So the columns are captured now even where this provider
-- reports nothing for them — as NULL. A 0 would be a measurement claiming the
-- cache was cold, and a month of "free" turns nobody notices for months.
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
  -- NEVER 0.0 for "no price configured". A zero reads as "this call was free".
  cost_usd              numeric(12,6),
  cost_is_estimated     boolean     NOT NULL DEFAULT false,
  finish_reason         text,
  ts                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_turn_idx  ON llm_calls (turn_id, seq);
CREATE INDEX IF NOT EXISTS llm_calls_model_idx ON llm_calls (model, ts DESC);
