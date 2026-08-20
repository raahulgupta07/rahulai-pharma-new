-- ============================================================================
-- 0006_turn_metrics.sql
-- ============================================================================
-- chat_logs records THAT a turn happened, who asked, which model answered and
-- by which route (0003). It records nothing about what the turn COST. There is
-- no per-turn token count anywhere in this system, so "what is this deployment
-- spending" and "which questions are expensive" have never been answerable —
-- and the one number the repo does have (`latency_ms`) says nothing about
-- tokens, because a slow turn and a large turn are different failures.
--
-- These six columns come straight off `agno.metrics.RunMetrics`, which rides on
-- `RunOutput.metrics` (and on the streaming `RunCompletedEvent`):
--
--   input_tokens      INT            prompt tokens the provider billed
--   output_tokens     INT            completion tokens
--   total_tokens      INT            provider's own total, NOT input+output —
--                                    they differ when the provider bills cache
--                                    reads or reasoning separately, and the
--                                    provider's number is the billable one
--   reasoning_tokens  INT            thinking tokens, when the model reports any
--   cost_usd          NUMERIC(12,6)  provider-reported cost. NUMERIC, not FLOAT:
--                                    these get summed into a spend figure and a
--                                    binary float drifts. 12,6 holds a single
--                                    turn to the microdollar and a rollup to
--                                    ~1e6 USD.
--   ttft_ms           INT            time to first token, ms
--
-- ⚠️ **NULL means UNKNOWN and 0 means ZERO, and they are not the same thing.**
-- This is the same invariant `inventory.stock_qty` carries (migration 0001,
-- CLAUDE.md "A3 landed"): a blank cell is not a zero. Applied here:
--
--   * The **cache-hit** path ran no model. Every column above stays NULL on
--     those rows, and `path='cache'` (0003) already records why. Writing 0 there
--     would make a month of cache hits look like a month of free model calls,
--     and any "average tokens per turn" would be silently halved.
--   * OpenRouter does not return a per-generation price on the completion
--     response, so agno leaves `cost` as `None`. A 0 in `cost_usd` would be a
--     claim — "this turn was free" — that nothing in this stack can back.
--     `app.activity.extract_metrics` therefore maps a missing OR zero cost to
--     NULL. It never invents one.
--   * Every row written before this migration is NULL in all six, i.e.
--     unattributed. It is not backfilled with a guess, for the reason 0003
--     gives at length.
--
-- No new index. Every reader of these columns is the existing analytics window
-- ("last N days, newest first"), which `idx_chat_logs_ts` (0003) already serves;
-- the aggregate is a SUM over that window, not a lookup by token count.
--
-- Safe to run more than once: ADD COLUMN IF NOT EXISTS only. Existing rows are
-- untouched.
--
-- ``app.activity.ensure_turn_metrics()`` performs the SAME statements on every
-- boot, called from the lifespan. That is deliberate, not duplication, and it is
-- the same trap `ensure_chat_logs` documents: `chat_logs` is created with
-- CREATE TABLE IF NOT EXISTS, so on any database that already has the table a
-- column added to that CREATE would never appear.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0006_turn_metrics.sql
-- ============================================================================

ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS input_tokens     INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS output_tokens    INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS total_tokens     INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS reasoning_tokens INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS cost_usd         NUMERIC(12,6);
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS ttft_ms          INT;
