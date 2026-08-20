"""Shared pytest fixtures for the pharmacy-agent test suite.

The async DB layer uses a module-global asyncpg pool. Different tests run on
different event loops (the FastAPI TestClient uses its own loop; the tool tests
use a per-module loop), and an asyncpg pool is bound to the loop that created
it. Reusing a pool across loops raises "attached to a different loop" errors.

The autouse fixture below resets the pool reference before each test so every
test recreates a fresh pool on its own running loop. This is a test-only
concern — in production the pool is created once inside the serving loop.
"""

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests import dbguard

# --- Postgres isolation: this must run before anything opens a pool ---------
#
# The suite is an integration suite. Twenty-one modules open real connections
# and run real DELETEs, two of them unqualified (`DELETE FROM brand_assets` in
# test_branding, `DELETE FROM app_events` in test_activity). Every one of them
# built its DSN from `get_settings().postgres_url` — the DSN the running stack
# serves from. Consequences, both observed rather than theorised:
#
#   * the deployed CityCare / CMHL logos were erased twice by someone running
#     `pytest`, and had to be re-uploaded by hand;
#   * `app_events` — the table the Activity feed and the analytics Actors panel
#     read — ended up 144/149 pytest rows, so generated actors like
#     `brand-df8b605e13@corp.mm` out-ranked `admin@citcare.local`, the only real
#     admin on the instance.
#
# So every run gets its OWN DATABASE on the same server: same host, same port,
# same Postgres, a database named for this process. It is copied from an idle
# template (`pharmacy_test_tpl`, built from live by `tests/setup_test_db.sh`)
# because the tests are pinned to real data — test_tools asserts ROYAL-D's 37605
# units across 53 sites — and an empty schema would fail dozens of tests for
# unrelated reasons.
#
# Per RUN, not one shared test database, and the reason is measured. The first
# version shared one and serialised runs with a file lock; with three agents
# running `pytest tests/` concurrently a two-file run took 727s, 247s of it a
# single test's `setup` waiting on that lock (`user 0.97s` against 248s wall —
# asleep, not working). Copying the template costs 0.49s and runs never queue.
# See the block comment in tests/dbguard.py for the full numbers.
#
# There is deliberately NO fallback to live. The database name is derived from
# the PID before anything connects, so it cannot be the live one; the template is
# rebuilt whenever its recorded schema fingerprint differs from live, so a stale
# clone cannot quietly test the wrong shape. `dbguard.install` then patches
# asyncpg so no connection can reach any other database even if a module
# hardcodes a DSN.
LIVE_POSTGRES_URL = os.environ.get("POSTGRES_URL") or get_settings().postgres_url
POSTGRES_STATUS, TEST_POSTGRES_URL, TEMPLATE_STATUS = dbguard.bootstrap(
    LIVE_POSTGRES_URL
)

os.environ["POSTGRES_URL"] = TEST_POSTGRES_URL
get_settings.cache_clear()  # the live URL is already cached by the read above
dbguard.install(dbguard.dbname_of(TEST_POSTGRES_URL))

# --- Redis isolation: this must run before anything builds a client ---------
#
# The suite writes real keys — the emb1/pk1 embed credential below, cache
# entries, rate-limit counters. Pointed at the URL the running stack serves
# from, `_register_test_credential` rewrites `pharmacy:credentials` on the LIVE
# instance, and every deployed embed starts 401-ing until someone re-seeds it.
# That happened three times before this fix; it is silent, and it looks like an
# auth bug rather than a test-suite bug.
#
# So the whole suite is redirected to a scratch DB **on the same server**: same
# host, same port, different DB index. Nothing else about the environment
# changes, and no test needs to know.
#
# The index is CLAIMED per run, not fixed, for the same reason each run gets its
# own Postgres database. Every run used to take DB 15, so two overlapping runs
# shared one keyspace and deleted each other's state. Measured with two full
# suites started together, both green apart from exactly this:
#
#   test_cache.py::test_dev_credential_seeds_only_into_an_empty_store
#       assert {'emb1', 'real-tenant'} == {'real-tenant'}   <- the OTHER run's emb1
#   test_cors_origins.py::test_add_get_remove_round_trip     assert 0 == 1
#   test_ingest_config.py::test_automatic_loading_defaults_on_and_round_trips
#
# `pharmacy:credentials`, the CORS origin set and `pharmacy:config` are all
# single global keys, so there is nothing to namespace — the whole DB has to be
# private. The claim is a SET NX on a key inside the candidate DB itself, so it
# is self-describing and cannot be orphaned by a coordinator dying.
#
# Set TEST_REDIS_DB to pin one index and skip claiming.
REDIS_CLAIM_KEY = "pytest:suite-claim"
REDIS_CLAIM_TTL = 7200  # matches the Postgres reaper's 2h
REDIS_CANDIDATE_DBS = range(1, 16)  # 0 is the live stack's; never a candidate


def _db_index_of(url: str) -> str:
    m = re.search(r"/(\d+)$", url.rstrip("/"))
    return m.group(1) if m else "0"


def _claim_redis_db(live_url: str) -> str:
    """Take an unused Redis DB index for this run, or raise.

    A stale claim from a run that was SIGKILLed would otherwise hold an index for
    two hours. The claim records the owning PID, and since every run is on this
    same host a dead PID means the claim is free — so it is stolen rather than
    waited out.
    """

    import redis as _redis_sync

    base = re.sub(r"/\d+$", "", live_url.rstrip("/"))
    live_index = _db_index_of(live_url)
    mine = str(os.getpid())

    for n in REDIS_CANDIDATE_DBS:
        if str(n) == live_index:
            continue
        client = _redis_sync.from_url(f"{base}/{n}", decode_responses=True)
        try:
            if client.set(REDIS_CLAIM_KEY, mine, nx=True, ex=REDIS_CLAIM_TTL):
                return str(n)
            holder = client.get(REDIS_CLAIM_KEY)
            if holder and holder != mine and _pid_is_dead(holder):
                # Steal it, but only by replacing the exact value we read, so two
                # runs reclaiming the same corpse cannot both win.
                if client.delete(REDIS_CLAIM_KEY) and client.set(
                    REDIS_CLAIM_KEY, mine, nx=True, ex=REDIS_CLAIM_TTL
                ):
                    return str(n)
        except Exception:  # noqa: BLE001 — no Redis at all is handled by callers
            return "15"
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    raise RuntimeError(
        f"every Redis DB in {list(REDIS_CANDIDATE_DBS)} is claimed by another "
        f"pytest run. Set TEST_REDIS_DB=<n> to force one, or wait."
    )


def _pid_is_dead(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ValueError, TypeError):
        return False   # unrecognisable: leave it alone
    except ProcessLookupError:
        return True
    except PermissionError:
        return False   # alive, owned by someone else
    return False


LIVE_REDIS_URL = os.environ.get("REDIS_URL") or get_settings().redis_url
TEST_REDIS_DB = os.environ.get("TEST_REDIS_DB") or _claim_redis_db(LIVE_REDIS_URL)


def _isolate_redis_db() -> str:
    """Rewrite REDIS_URL to this run's scratch DB and drop the cached Settings."""

    isolated = re.sub(r"/\d+$", "", LIVE_REDIS_URL.rstrip("/")) + f"/{TEST_REDIS_DB}"
    os.environ["REDIS_URL"] = isolated
    get_settings.cache_clear()  # the live URL is already cached by the read above
    return isolated


TEST_REDIS_URL = _isolate_redis_db()

import app.cache as cache  # noqa: E402 — must import after the URL is rewritten
import app.db as db  # noqa: E402


# Markers for rows the suite generates as a SIDE EFFECT rather than on purpose.
#
# `ip = 'testclient'` is the FastAPI TestClient's client host: every audit row
# the middleware writes during a suite run carries it, and no real request can
# (real ones carry an address). It is the exact fingerprint that identifies the
# 144 debris rows sitting in the production `app_events` table.
#
# `%@corp.mm` is the domain every generated account in this suite uses; the real
# admin is `@citcare.local`.
#
# Nothing sweeps these any more: the database they land in is dropped whole at
# the end of the run, which is a stronger guarantee than any DELETE and costs
# 0.75s. They stay defined because `tests/test_postgres_isolation.py` uses them
# to assert the LIVE database is not accumulating either.
SUITE_EVENT_MARKER = "testclient"
SUITE_EMAIL_DOMAIN = "@corp.mm"


# --- a skipped SECURITY guard has to be impossible to scroll past -------------
#
# `tests/test_auth_sso.py` skips three tests when `ldap3` is missing. Those pin
# two auth bypasses that actually shipped — `ldap3.Connection` being
# unconditionally truthy, and `LDAPBindError` escaping as a 500 — so them
# quietly becoming part of a "5 skipped" tally is how a guard goes missing for a
# year. The reason string is only visible under `-rs`, which nobody passes.
#
# These two hooks put it on screen in every run, before the first test and again
# next to the final counts, without `-rs` and without turning it into a failure
# (the suite must stay runnable on a machine that genuinely lacks the package).
_OPTIONAL_GUARDS = [
    (
        "ldap3",
        "LDAP bind + TLS certificate guards (3 tests) NOT exercised — "
        "ldap3 is in requirements.txt, so this usually means pytest is running "
        "under the system python instead of ./venv/bin/python",
    ),
]


def _missing_guards():
    """Modules that will actually raise ImportError, not merely be absent.

    `importlib.util.find_spec` is the obvious choice and is wrong here: it
    locates a package without executing it, so a module that is present but
    fails on import reads as available. This banner then stayed silent while the
    tests skipped — the exact mismatch it exists to prevent. Import it the same
    way `pytest.importorskip` does, so the banner and the skip can never
    disagree.
    """

    import importlib

    missing = []
    for mod, why in _OPTIONAL_GUARDS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append((mod, why))
    return missing


def pytest_report_header(config):
    missing = _missing_guards()
    if not missing:
        return None
    return [f"!! SECURITY GUARD SKIPPED: {why}" for _mod, why in missing]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    missing = _missing_guards()
    if not missing:
        return
    for _mod, why in missing:
        terminalreporter.write_line("")
        terminalreporter.write_line(
            f"!! SECURITY GUARD SKIPPED: {why}", red=True, bold=True
        )


@pytest.fixture(scope="session", autouse=True)
def _own_database():
    """Drop this run's database and release its Redis DB when the run ends.

    Cleanup is by DESTRUCTION, not by tidying: whatever the suite created,
    altered or forgot goes with the database. `test_activity` DROPs `app_events`
    and `test_approval` DROPs a column mid-test, so "put it back the way you
    found it" was never really achievable — dropping the whole copy is.

    A run killed with SIGKILL never reaches this. Those leftovers are reaped by
    `dbguard.reap_stale_session_dbs` at the start of a later run, which is why
    it requires the database to be BOTH unused and two hours old.
    """

    yield

    # Redis first: FLUSHDB on THIS RUN'S claimed index only. Same reasoning as
    # dropping the database — destroying the whole keyspace is stronger than
    # deleting the keys we remember writing, and it releases the claim (the claim
    # key lives in the DB it claims) so the index is reusable immediately rather
    # than after the 2h TTL.
    if not os.environ.get("TEST_REDIS_DB"):   # a pinned index is not ours to wipe
        try:
            import redis as _redis_sync

            client = _redis_sync.from_url(TEST_REDIS_URL, decode_responses=True)
            try:
                if client.get(REDIS_CLAIM_KEY) == str(os.getpid()):
                    client.flushdb()
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 — cleanup must not fail a run
            print(f"\n[conftest] could not release Redis DB {TEST_REDIS_DB}: {exc!r}")

    if POSTGRES_STATUS != "ok":
        return

    db._pool = None  # nothing may hold a connection while the database is dropped
    dbguard.drop_session_db(LIVE_POSTGRES_URL, dbguard.dbname_of(TEST_POSTGRES_URL))


@pytest.fixture(autouse=True)
def _reset_db_pool():
    """Drop stale pool/redis refs so each test binds clients to its own loop."""

    db._pool = None
    cache._client = None
    yield
    db._pool = None
    cache._client = None


# The embed credential check is fail-closed (cache.is_valid_credential), so the
# emb1/pk1 pair the API and security tests have always used must actually be
# registered or every /session/create in the suite would 403.
#
# Seeded with a SYNCHRONOUS redis client on purpose: the async client in
# app.cache is bound to whichever event loop first touched it, and this fixture
# runs outside any test loop. Reaching for asyncio.run() here would bind the
# module-global client to a loop that is closed before the test body runs.
TEST_EMBED_ID = "emb1"
TEST_PUBLIC_KEY = "pk1"


@pytest.fixture(autouse=True)
def _register_test_credential():
    """Register the suite's embed credential in Redis (fail-closed API needs it)."""

    import redis as _redis_sync

    client = _redis_sync.from_url(get_settings().redis_url, decode_responses=True)
    try:
        client.hset(cache._CRED_KEY, TEST_EMBED_ID, TEST_PUBLIC_KEY)
    except Exception:  # noqa: BLE001 — Redis-less collection must not error here
        pass
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    yield


@pytest.fixture
def api_client():
    """Context-managed TestClient: one persistent portal loop for all requests.

    Using TestClient as a context manager runs the app lifespan and keeps a
    single event loop for the duration, so the loop-bound asyncpg/redis clients
    are reused across requests (mirroring uvicorn's single-loop runtime).

    A current event loop must exist before that starts. On Python 3.9,
    ``asyncio.run`` CLOSES its loop and leaves the thread with none set, so any
    test using it (several here call it directly through ``_pg``) poisons every
    later ``api_client`` in the same process::

        RuntimeError: There is no current event loop in thread 'MainThread'.

    It surfaced as an ERROR at fixture setup, not a failure, so the test never
    ran and the suite still reported "17 passed, 1 error" — green enough to
    scroll past. Installing a fresh loop when none is current makes the fixture
    independent of whatever ran before it.
    """

    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    from app.api import app

    with TestClient(app) as client:
        yield client
