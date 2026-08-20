-- ============================================================================
-- 0007_app_events.sql
-- ============================================================================
-- The app has three audit trails and a hole between them. `chat_logs` records
-- what the AGENT did. `auth_events` (0004) records who signed IN. `ingest_log`
-- records what happened to a FILE. Nothing anywhere records that a human
-- changed the running system — that someone repointed OIDC at another realm,
-- promoted an account to super_admin, deleted an embed credential, widened
-- CORS, or turned off certificate validation on the directory bind. Those are
-- the changes an incident review actually needs, and every one of them was
-- invisible the moment the request returned 200.
--
-- `app_events` is that trail: one row per MUTATING admin request.
--
--   action       a stable slug derived from the route, e.g. `admin.users.update`,
--                `admin.auth-config.update`. It is what you filter a feed on;
--                `method`/`path` are what you read when the slug is not enough.
--   target       the identifying path parameter (user id, embed_id, key label),
--                pulled from an explicit per-route capture — never guessed out
--                of the last path segment, because `/admin/graph/rebuild` would
--                then record "rebuild" as the thing that was changed.
--   detail       JSONB, and DELIBERATELY ALMOST EMPTY. See the warning below.
--   ip           first hop of X-Forwarded-For (we sit behind a proxy), same
--                rule and same helper as auth_events.
--   duration_ms  wall time of the request.
--
-- ⚠️ **THE REQUEST BODY IS NEVER STORED.** It cannot be, and this table is the
-- reason to say so in SQL as well as in code. The bodies that flow through
-- these very routes are:
--
--     PUT  /admin/auth-config  -> oidc_client_secret, ldap_bind_password
--     POST /admin/users        -> the new account's cleartext password
--     PATCH /admin/users/{id}  -> a password reset, same
--     POST /admin/credentials  -> an embed public key
--     POST /admin/sftp/keys    -> a partner's public key
--
-- An audit table that swallowed those would be a credential store with a
-- convenient time index, readable by every future `SELECT *` on this table, and
-- copied into every database dump and every support bundle. So `detail` is
-- filled from a **per-route allowlist of non-sensitive summary fields**
-- (`app.activity._ROUTES`); a route not in that list stores route + status and
-- NOTHING else. Key-name redaction (`*secret*`, `*password*`, `*token*`,
-- `*key*`) is the second line, not the first — it exists to catch the field
-- somebody adds to an allowlisted model next year, not to sanitise a body we
-- had no business reading. `tests/test_activity.py` PUTs a real secret through
-- the real route and asserts neither the value nor its 8-character prefix
-- appears anywhere in the stored row.
--
-- ⚠️ **Login is NOT recorded here.** `login_ok` / `login_fail` /
-- `login_locked` / `sso_*` already live in `auth_events`, where the lockout
-- counter counts them — duplicating them here would either double every
-- sign-in in the feed or, worse, tempt someone to move them and break the
-- lockout. The activity feed READS BOTH TABLES and merges on `ts`; see
-- `app.activity.unified_feed`.
--
-- Safe to run more than once: CREATE TABLE / CREATE INDEX IF NOT EXISTS.
--
-- `app.activity.ensure_app_events()` performs the SAME statements on every
-- boot, for the same reason `ensure_auth_events` does: a fresh container and a
-- database someone already migrated by hand have to converge on one schema.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0007_app_events.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS app_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT now(),
    actor_email TEXT,
    actor_role  TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    method      TEXT,
    path        TEXT,
    status      INT,
    detail      JSONB,
    ip          TEXT,
    duration_ms INT
);

-- The feed itself: a plain time window, newest first.
CREATE INDEX IF NOT EXISTS idx_app_events_ts ON app_events (ts DESC);

-- "What has this person changed?" — the question asked when an account is
-- suspected, and the one `auth_events` can only answer up to the login.
CREATE INDEX IF NOT EXISTS idx_app_events_actor_ts ON app_events (actor_email, ts DESC);

-- "Show me every auth-config change" — filtering the feed to one kind of change
-- is how the security-relevant subset is found without reading the rest.
CREATE INDEX IF NOT EXISTS idx_app_events_action_ts ON app_events (action, ts DESC);
