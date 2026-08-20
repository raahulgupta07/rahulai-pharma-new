#!/usr/bin/env bash
#
# Build (or refresh) the TEMPLATE the test suite copies its per-run database
# from. `tests/conftest.py` runs this automatically whenever the live schema
# has moved, so you rarely need to; run it by hand to force a rebuild.
#
#   tests/setup_test_db.sh
#
# Nothing ever runs tests against this database directly. Each pytest session
# does `CREATE DATABASE pharmacy_test_s<pid>_<rand> TEMPLATE pharmacy_test_tpl`
# (0.49s measured) and drops it at the end, so concurrent runs cannot delete
# each other's rows and never queue behind one another. That is also why the
# template must stay idle: `CREATE DATABASE ... TEMPLATE` fails outright with
# "source database is being accessed by other users" if anything is connected
# to it.
#
# Why a CLONE and not an empty database
# -------------------------------------
# This is an integration suite pinned to REAL data. tests/test_tools.py asserts
# ROYAL-D 25G is stocked at 53 sites totalling 37605 units, with 4154 at
# 20052-CCTLKK; test_admin_scope / test_embed_preflight lean on real site codes.
# An empty schema would fail dozens of tests for reasons that have nothing to do
# with the code under test, and the pressure to "just point it back at live"
# would be immediate — which is exactly the bug being fixed.
#
# So the test database is a byte-for-byte copy of the live one at the moment you
# run this, on the SAME server. Only the database name differs, which keeps the
# tests honest (same Postgres version, same extensions, same pgvector indexes)
# while making every DELETE they run land somewhere disposable.
#
# CREATE DATABASE ... TEMPLATE would be faster but requires zero connections to
# the source; the running stack holds a pool open, so it always fails here.
# pg_dump | psql works against a live database.
#
# Re-run this whenever the live data changes enough to matter. It DROPs and
# rebuilds the test database and never writes to the live one.
#
# Overridable: PG_CONTAINER, PGUSER, LIVE_DB, TEST_DB.

set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-pharmacy-opt-postgres-1}"
PGUSER="${PGUSER:-pharmacy}"
LIVE_DB="${LIVE_DB:-pharmacy}"
TEST_DB="${TEST_DB:-pharmacy_test_tpl}"

if [ "$TEST_DB" = "$LIVE_DB" ]; then
  echo "refusing: TEST_DB and LIVE_DB are both '$LIVE_DB'" >&2
  exit 1
fi

if ! docker inspect "$PG_CONTAINER" >/dev/null 2>&1; then
  echo "no such container: $PG_CONTAINER (set PG_CONTAINER=...)" >&2
  exit 1
fi

# Nothing is ever supposed to be connected to the template — runs execute in
# their own copies — but a stray psql makes `DROP DATABASE` fail with "is being
# accessed by other users", and that used to abort the whole pytest run. Evict
# first. Callers inside the suite already hold the template lock exclusively, so
# this only ever sees connections from outside the suite.
echo "==> dropping and recreating '$TEST_DB'"
docker exec "$PG_CONTAINER" psql -q -U "$PGUSER" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname = '$TEST_DB' AND pid <> pg_backend_pid()" >/dev/null
docker exec "$PG_CONTAINER" psql -q -v ON_ERROR_STOP=1 -U "$PGUSER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"$TEST_DB\"" \
  -c "CREATE DATABASE \"$TEST_DB\" OWNER \"$PGUSER\""

echo "==> cloning '$LIVE_DB' -> '$TEST_DB' (schema + data; ~230 MB, takes a minute)"
docker exec "$PG_CONTAINER" sh -c \
  "pg_dump -U '$PGUSER' -d '$LIVE_DB' | psql -q -v ON_ERROR_STOP=1 -U '$PGUSER' -d '$TEST_DB'"

# The live database is currently carrying pytest debris of its own (144
# app_events rows written through the TestClient, and two leaked appr-* users).
# Purging it from the CLONE is free and keeps the copy from re-seeding the
# Activity-feed noise. Purging it from the LIVE database is the owner's call and
# is deliberately not done anywhere in this repo.
echo "==> clearing inherited pytest debris from the clone"
docker exec "$PG_CONTAINER" psql -q -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$TEST_DB" \
  -c "DELETE FROM app_events WHERE ip = 'testclient'" \
  -c "DELETE FROM users WHERE email LIKE '%@corp.mm'"

# Stamp the template with the live schema fingerprint it was built from, as the
# database COMMENT. conftest compares that against live on every run and rebuilds
# on any difference; storing it in the comment rather than in a table means the
# freshness check needs no CONNECTION to the template, which would otherwise
# intermittently break the `CREATE DATABASE ... TEMPLATE` it is checking for.
echo "==> stamping the schema fingerprint"
# MUST stay identical to _FINGERPRINT_SQL in tests/dbguard.py — that one reads
# live to decide whether to rebuild, this one stamps the result. If they diverge
# the template rebuilds on every run. Pinned by
# test_postgres_isolation.py::test_the_template_matches_the_live_schema.
FP=$(docker exec "$PG_CONTAINER" psql -tA -U "$PGUSER" -d "$LIVE_DB" -c \
  "SELECT md5(string_agg(t,'|' ORDER BY t)) FROM (
     SELECT 'c:'||table_name||'.'||column_name||':'||data_type
            ||':'||is_nullable||':'||coalesce(column_default,'') AS t
       FROM information_schema.columns WHERE table_schema='public'
     UNION ALL
     SELECT 'i:'||indexname||':'||indexdef
       FROM pg_indexes WHERE schemaname='public'
     UNION ALL
     SELECT 'k:'||conname||':'||pg_get_constraintdef(oid)
       FROM pg_constraint WHERE connamespace='public'::regnamespace) s")
docker exec "$PG_CONTAINER" psql -q -v ON_ERROR_STOP=1 -U "$PGUSER" -d postgres \
  -c "COMMENT ON DATABASE \"$TEST_DB\" IS 'fingerprint:$FP'"

echo "==> done. Row counts in '$TEST_DB':"
docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$TEST_DB" -c \
  "SELECT 'catalog' t, count(*) FROM catalog
   UNION ALL SELECT 'inventory', count(*) FROM inventory
   UNION ALL SELECT 'users', count(*) FROM users
   UNION ALL SELECT 'app_events', count(*) FROM app_events"
