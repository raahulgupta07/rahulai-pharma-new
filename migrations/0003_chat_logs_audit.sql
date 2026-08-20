-- ============================================================================
-- 0003_chat_logs_audit.sql
-- ============================================================================
-- chat_logs was (id, ts, lang, store_id, question, answer, cached, latency_ms).
-- That records THAT a turn happened and nothing about WHO asked, WHICH model
-- answered, or HOW — so an answer could never be attributed to an embed
-- credential, a model, a tool call or a code path. This adds the attribution
-- columns the analytics/audit page needs, plus the two indexes every one of its
-- queries sorts or filters on.
--
--   embed_id   TEXT   which embed credential asked (session token claim)
--   session_id TEXT   the conversation id
--   model      TEXT   the answering model id
--   tools      JSONB  tool names actually called, in order
--   path       TEXT   'agent' | 'fast_path' | 'cache'
--
-- Existing rows keep NULL in all five. NULL means UNATTRIBUTED — a turn logged
-- before this migration — and must be shown as such, never backfilled with a
-- guess: inventing an embed_id for 122 historical turns would make the audit log
-- lie about exactly the thing it exists to answer.
--
-- Safe to run more than once: every statement is ADD COLUMN IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS. Existing rows are untouched.
--
-- ``app.admin.ensure_chat_logs()`` performs the SAME statements on every boot.
-- That is deliberate, not duplication: the table is created with CREATE TABLE IF
-- NOT EXISTS, so on a database that already HAS chat_logs the create is a no-op
-- and new columns would never appear. A fresh boot and an existing database have
-- to converge on the same schema whether or not anyone ran this file by hand.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0003_chat_logs_audit.sql
-- ============================================================================

ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS embed_id   TEXT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS model      TEXT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS tools      JSONB;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS path       TEXT;

-- Every analytics query is time-windowed and newest-first.
CREATE INDEX IF NOT EXISTS idx_chat_logs_ts ON chat_logs (ts DESC);

-- The per-embed rollup groups on embed_id and orders by time within it.
CREATE INDEX IF NOT EXISTS idx_chat_logs_embed_ts ON chat_logs (embed_id, ts DESC);
