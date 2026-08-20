"""The suite must never write to the database the running stack serves from.

Twenty-one test modules open real asyncpg connections and run real DELETEs, two
of them unqualified. Built from `get_settings().postgres_url`, those landed on
the LIVE database:

* `tests/test_branding.py` ran `DELETE FROM brand_assets` / `DELETE FROM
  brand_config`, erasing the deployed CityCare and CMHL logos. Twice. Each time
  it looked like a branding bug rather than a test-suite bug.
* `tests/test_activity.py` ran `DELETE FROM app_events`, wiping the audit trail
  the Activity feed and the analytics Actors panel read — and refilling it with
  generated actors. 144 of the 149 rows in the production table were pytest
  debris, so `brand-df8b605e13@corp.mm` out-ranked `admin@citcare.local`, the
  only real admin on the instance.
* Accounts leaked out of `tests/test_approval.py` and were still in the
  production `users` table months later.

`tests/conftest.py` now redirects `POSTGRES_URL` to a separate database on the
same server before anything reads settings, and `tests/dbguard.py` patches
asyncpg so nothing can reach any other one. These tests pin that at four levels:
the settings, the pool the app actually builds, the guard's refusal, and — the
one that matters — the LIVE database itself, checked for the suite's
fingerprints after the suite has run.

No LLM and no network beyond localhost Postgres, so they cannot flake. The
live-DB tests skip when Postgres is unreachable (CI without a stack).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.config import get_settings
from tests import dbguard
from tests.conftest import (
    LIVE_POSTGRES_URL,
    POSTGRES_STATUS,
    SUITE_EMAIL_DOMAIN,
    SUITE_EVENT_MARKER,
    TEMPLATE_STATUS,
    TEST_POSTGRES_URL,
)

pytestmark = pytest.mark.skipif(
    POSTGRES_STATUS != "ok", reason="no Postgres server reachable"
)


def test_settings_point_at_the_test_database_not_the_live_one():
    """Every `_pg` copy in the suite reads its DSN from settings."""

    assert get_settings().postgres_url == TEST_POSTGRES_URL
    assert dbguard.dbname_of(TEST_POSTGRES_URL) != dbguard.dbname_of(
        LIVE_POSTGRES_URL
    )


def test_the_test_database_is_on_the_same_server_as_the_live_one():
    """Only the database name may differ.

    Isolating by pointing at some other host would hide real integration
    failures — a wrong port would satisfy the test above while testing nothing,
    and the suite's value is that it runs against the real Postgres version,
    the real extensions and the real pgvector indexes.
    """

    from urllib.parse import urlsplit

    live, test = urlsplit(LIVE_POSTGRES_URL), urlsplit(TEST_POSTGRES_URL)
    assert (live.hostname, live.port) == (test.hostname, test.port)


def test_this_run_has_a_database_to_itself():
    """Per-run, not shared — the property that lets runs overlap safely.

    A shared test database plus a lock was the first design; it made a two-file
    run take 727s behind other agents' suites. Concurrency is only safe because
    the name below is unique to this process, so `DELETE FROM app_events` in one
    run is invisible to another.
    """

    name = dbguard.dbname_of(TEST_POSTGRES_URL)
    assert name.startswith(dbguard.SESSION_DB_PREFIX)
    assert str(os.getpid()) in name
    assert name != dbguard.TEMPLATE_DB      # never run inside the template
    assert name != dbguard.dbname_of(LIVE_POSTGRES_URL)


def test_this_runs_database_matches_the_schema_bootstrap_validated():
    """THE invariant that stops a test lying, and it is order-independent.

    An earlier version of this test re-read LIVE and asserted it still equalled
    the template stamp. That was order- and timing-dependent for two reasons,
    both since fixed and both worth remembering:

    1. A rebuild DROPs and recreates the template, so for ~3 seconds it does not
       exist and the stamp reads NULL. A concurrent run's check then saw
       `None`, called the template stale, and failed — and the next run passed,
       because the window had closed. Measured directly (`stamp=<NULL>` at t+1,
       t+2, t+3). Fixed by the shared/exclusive lock in `dbguard`.
    2. Live is not ours. The stack gets redeployed and other agents run
       migrations while the suite runs, so "live has not changed since we
       started" is an assertion about other people's timing, not about this run.

    What actually has to be true is narrower and completely deterministic: the
    database this run is executing against carries the schema that `bootstrap`
    validated against live before the run began. If live moves afterwards, this
    run's results are still true of the code it started with, and the NEXT
    bootstrap rebuilds. That is the strongest guarantee available, and this
    asserts exactly it — no live read, nothing to race.
    """

    assert TEMPLATE_STATUS in ("current", "rebuilt")
    assert dbguard.BOOTSTRAP_FINGERPRINT, "bootstrap recorded no fingerprint"
    assert dbguard.fingerprint_of(TEST_POSTGRES_URL) == dbguard.BOOTSTRAP_FINGERPRINT


def test_a_template_that_does_not_match_live_is_rebuilt_not_tolerated():
    """The staleness decision itself: reuse only on an exact match.

    Pins the rule rather than the current state, so it cannot pass by accident on
    a day when nothing has drifted. `ensure_template` returns "current" if and
    only if the stamp equals live; every other case — missing template, missing
    stamp, one differing column, one changed FK — rebuilds.
    """

    assert dbguard.template_is_current("abc", "abc") is True
    assert dbguard.template_is_current("abc", "abd") is False
    assert dbguard.template_is_current(None, "abc") is False   # never stamped
    assert dbguard.template_is_current("abc", None) is False   # live unreadable
    assert dbguard.template_is_current(None, None) is False    # not "both absent, fine"


def test_the_app_pool_is_built_from_the_isolated_url():
    """Settings being right is not enough — pin what the app actually opened.

    A pool built before the rewrite, or from a hardcoded DSN, would still write
    to the live database.
    """

    import app.db as db

    async def go():
        db._pool = None
        pool = await db.init_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchval("SELECT current_database()")
        finally:
            await db.close_pool()

    assert asyncio.run(go()) == dbguard.dbname_of(TEST_POSTGRES_URL)


def test_the_guard_refuses_a_connection_to_the_live_database():
    """The layer that survives someone hardcoding a DSN.

    Non-vacuous by construction: it asks for the LIVE url, which is a real,
    reachable database. Without the guard this connects happily.
    """

    import asyncpg

    async def go():
        return await asyncpg.connect(LIVE_POSTGRES_URL)

    with pytest.raises(dbguard.LiveDatabaseRefused):
        asyncio.run(go())


def test_a_destructive_fixture_asserts_before_deleting():
    """`assert_test_database` is what test_branding/test_activity call inline."""

    dbguard.assert_test_database()  # the configured URL — must not raise
    with pytest.raises(dbguard.LiveDatabaseRefused):
        dbguard.assert_test_database(LIVE_POSTGRES_URL)


def test_the_live_database_gains_no_rows_from_this_suite():
    """The guard that would have caught the original bug.

    Reads the LIVE database directly — not through settings, which the suite has
    moved — and fails if the suite's own fingerprints are being added to it.
    `admin@citcare.local` is the real super admin and is never in this set: the
    marker is the TestClient's client host, which no real request can carry.
    """

    async def go():
        import asyncpg

        conn = await asyncpg.connect(LIVE_POSTGRES_URL, timeout=5)
        try:
            events = await conn.fetchval(
                "SELECT count(*) FROM app_events WHERE ip = $1", SUITE_EVENT_MARKER
            )
            users = await conn.fetch(
                "SELECT id, email FROM users WHERE email LIKE $1",
                "%" + SUITE_EMAIL_DOMAIN,
            )
            return events, [dict(r) for r in users]
        finally:
            await conn.close()

    # The one place in the suite allowed to open the live database, and it only
    # reads. Deliberately noisy to grep for.
    with dbguard.allow_live():
        try:
            events, users = asyncio.run(go())
        except dbguard.LiveDatabaseRefused:
            raise
        except Exception as exc:  # noqa: BLE001 — no stack running is not a failure
            pytest.skip(f"live postgres unreachable: {exc!r}")

    # Both counts are the PRE-EXISTING debris, recorded when isolation landed:
    # 144 app_events rows and the two leaked `appr-*` accounts (ids 6 and 9).
    # Purging them is the instance owner's call, not this suite's — so the
    # assertion is that the suite adds nothing, not that the table is empty.
    assert events <= 144, (
        f"the live app_events table has grown to {events} rows written through "
        f"the TestClient. The suite is writing to the running stack's database; "
        f"check the POSTGRES_URL rewrite in tests/conftest.py."
    )
    assert len(users) <= 2, (
        f"generated accounts in the LIVE users table: "
        f"{[u['email'] for u in users]}. The suite is writing to the running "
        f"stack's database."
    )


# --------------------------------------------------------------------------
# One connection per process, not one per statement.
#
# Sixteen modules each carried a byte-identical `_pg` that opened a fresh
# asyncpg connection and a fresh event loop for EVERY statement. The seeding
# fixtures make dozens of calls each, so the suite spent most of its wall clock
# on Postgres handshakes: 70s of a 70s run, with `--durations` showing its
# slowest twenty-five entries were fixture SETUP rather than any test body.
# Routing them through tests/pgconn.py took the suite to ~30s with every test
# still running.
#
# The pattern is one copy-paste away from coming back, and it comes back
# invisibly — nothing fails, the suite just gets slower every time a module is
# added. So it is pinned.
# --------------------------------------------------------------------------

import pathlib
import re as _re

_TESTS = pathlib.Path(__file__).resolve().parent

# `run_isolated` in the ingest tests genuinely needs its OWN connection: it
# opens a transaction and rolls it back, which is the whole mechanism that keeps
# a full-sync DELETE off the real catalog. A shared connection would leak that
# transaction into every other statement in the process.
_MAY_CONNECT = {
    "pgconn.py",
    "dbguard.py",
    "test_postgres_isolation.py",
    "test_catalog_full_sync.py",   # run_isolated — rolled-back transaction
    "test_ingest_replace.py",      # run_isolated — rolled-back transaction
}


def test_no_module_opens_a_connection_per_statement():
    offenders = {}
    for p in sorted(_TESTS.glob("*.py")):
        if p.name in _MAY_CONNECT:
            continue
        src = p.read_text(encoding="utf-8")
        for m in _re.finditer(r"^.*asyncpg\.connect\(.*$", src, _re.M):
            line = m.group(0).strip()
            if line.startswith("#"):
                continue
            offenders.setdefault(p.name, []).append(line)
    assert not offenders, (
        "these modules open their own asyncpg connections. A per-statement "
        "connection is what made the suite spend its wall clock on handshakes "
        "instead of assertions — use `from tests.pgconn import pg`, which keeps "
        "one private connection per process (still never app.db's pool). If a "
        "module genuinely needs its own connection — a transaction it intends "
        "to roll back — add it to _MAY_CONNECT with the reason: " + str(offenders)
    )
