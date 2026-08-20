-- ============================================================================
-- 0004_auth_events.sql
-- ============================================================================
-- `/auth/login` had no throttle and no audit trail. Two consequences:
--
--   1. Unlimited online password guessing against bcrypt, and — once LDAP is
--      enabled — every guess is proxied into the directory, so a guessing run
--      also drives the real AD account into ITS lockout (amplification: one
--      attacker, one endpoint, every corporate account).
--   2. Nothing anywhere recorded that it happened. `users.last_login` says a
--      login succeeded; nothing said 4,000 failed first.
--
-- This table is both halves. The lockout counts rows in it, and the same rows
-- are the audit trail.
--
--   event       'login_ok' | 'login_fail' | 'login_locked' | 'sso_ok' | 'sso_fail'
--   email       the account the attempt was AGAINST (may not exist — that is
--               itself the interesting case). NULL when unknown.
--   actor_email who caused the event, when that differs from `email`.
--   ip          client address, first hop of X-Forwarded-For (we sit behind a
--               proxy, so request.client.host is the proxy, not the caller).
--   detail      free text: the AuthError message, the lock reason, …
--
-- Safe to run more than once: CREATE TABLE / CREATE INDEX IF NOT EXISTS.
--
-- `app.auth.ensure_auth_events()` performs the SAME statements on every boot,
-- for the same reason `ensure_chat_logs` does: a fresh boot and a database
-- someone already migrated by hand have to converge on one schema.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0004_auth_events.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS auth_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT now(),
    event       TEXT NOT NULL,
    email       TEXT,
    actor_email TEXT,
    ip          TEXT,
    detail      TEXT
);

-- The lockout query is "recent failures for THIS email, newest first".
CREATE INDEX IF NOT EXISTS idx_auth_events_email_ts ON auth_events (email, ts DESC);

-- The audit listing and the retention prune are plain time windows.
CREATE INDEX IF NOT EXISTS idx_auth_events_ts ON auth_events (ts DESC);

-- The per-IP spray throttle windows on the address instead of the account.
CREATE INDEX IF NOT EXISTS idx_auth_events_ip_ts ON auth_events (ip, ts DESC);
