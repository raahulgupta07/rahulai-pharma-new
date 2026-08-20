"""Refuse — at the connection layer — any test statement against the live DB.

Why this exists
---------------
The suite is an *integration* suite: ~21 test modules open real asyncpg
connections and run real INSERTs and DELETEs, and two of those DELETEs are
unqualified::

    tests/test_branding.py   DELETE FROM brand_assets / brand_config
    tests/test_activity.py   DELETE FROM app_events

Every one of them built its connection from ``get_settings().postgres_url`` —
the same DSN the running stack serves from. Running ``pytest`` therefore erased
the deployed CityCare/CMHL logos (twice) and filled ``app_events`` with pytest
actors that then out-ranked the only real admin in the Activity feed.

The fix has two layers, and both are load-bearing:

1. ``tests/conftest.py`` rewrites ``POSTGRES_URL`` to a **separate database on
   the same server** before anything reads settings, and refuses to start at all
   if that database is missing while the server is up (see
   :func:`resolve_test_url` / :func:`preflight`).

2. This module then patches ``asyncpg.connect`` / ``asyncpg.create_pool`` so a
   connection to any *other* database raises. Layer 1 alone would be undone by
   one test module that hardcodes a DSN, or by a future ``_pg`` copy that reads
   the URL before conftest moved it. Layer 2 makes "assert we are on the test
   database before deleting" true for **every** fixture at once, including ones
   nobody has written yet — which is strictly stronger than adding the assert to
   the fifteen ``_pg`` copies individually.

The refusal is a hard ``RuntimeError``, never a skip and never a warning: a test
run that quietly targets production data is the thing being fixed here, so the
failure mode has to be louder than the bug, not quieter.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

# Set once by :func:`install`. Until then nothing is enforced.
_ALLOWED_DB: Optional[str] = None
_ALLOW_LIVE = False

# The live schema fingerprint this run was validated against, set by bootstrap.
BOOTSTRAP_FINGERPRINT: Optional[str] = None

# Saved originals, so install() is idempotent and uninstall() is possible.
_orig_connect = None
_orig_create_pool = None


class LiveDatabaseRefused(RuntimeError):
    """Raised when the suite tries to open a connection outside the test DB."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def dbname_of(url: str) -> str:
    """Return the database name in a ``postgresql://`` DSN (``''`` if absent)."""

    return urlsplit(url).path.lstrip("/")


def with_dbname(url: str, name: str) -> str:
    """Return *url* with its database name replaced by *name*."""

    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


def redact(url: str) -> str:
    """Return *url* with any password removed — these strings reach tracebacks."""

    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.replace(":" + parts.password + "@", ":***@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_test_url(live_url: str) -> str:
    """Return the DSN the suite must use, or raise if it cannot be made safe.

    Resolution order:

    * ``TEST_POSTGRES_URL`` — a full DSN, for anyone who wants the test database
      on a different server entirely;
    * ``TEST_POSTGRES_DB`` — just the database name, on the live server;
    * otherwise ``<live database>_test`` on the live server.

    The derived default is deliberately **not** a fallback to the live DSN. It
    names a database that cannot be the live one (:func:`preflight` then proves
    it exists), so there is no configuration — present, absent, or typo'd — that
    lands the suite on production data.
    """

    if not live_url:
        raise LiveDatabaseRefused(
            "POSTGRES_URL is empty, so the test database cannot be derived. "
            "Set TEST_POSTGRES_URL explicitly."
        )

    explicit = os.environ.get("TEST_POSTGRES_URL")
    if explicit:
        test_url = explicit
    else:
        name = os.environ.get("TEST_POSTGRES_DB") or (dbname_of(live_url) + "_test")
        test_url = with_dbname(live_url, name)

    live_db, test_db = dbname_of(live_url), dbname_of(test_url)
    if not test_db:
        raise LiveDatabaseRefused(
            f"the test DSN {redact(test_url)} names no database."
        )
    if test_db == live_db and _same_server(live_url, test_url):
        raise LiveDatabaseRefused(
            f"the test database resolved to {test_db!r} on the same server as the "
            f"live one ({redact(live_url)}). That is the bug this guard exists to "
            f"prevent. Point TEST_POSTGRES_URL or TEST_POSTGRES_DB somewhere else."
        )
    return test_url


def _same_server(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.hostname, pa.port) == (pb.hostname, pb.port)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _run(coro):
    """asyncio.run, restoring a current loop afterwards.

    `asyncio.run` CLOSES its loop and, on 3.9, leaves the thread with none set —
    which later breaks the TestClient fixtures. Every helper here goes through
    this rather than calling asyncio.run directly.
    """

    import asyncio

    try:
        return asyncio.run(coro)
    finally:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())


# ---------------------------------------------------------------------------
# Template + per-session databases
# ---------------------------------------------------------------------------
#
# Why per-session databases rather than one shared test database
# --------------------------------------------------------------
# The first version of this gave the whole suite ONE test database and a file
# lock, so concurrent runs could not corrupt each other. It was correct and
# unusable: with three agents running `pytest tests/` continuously, a two-file
# run measured 727s, of which 247s was a single test's `setup` — the lock wait.
# `user 0.97s` against 248s wall: the process was asleep, not working. A suite
# nobody can afford to run protects nothing.
#
# The measurements that decided the redesign, all against this stack:
#
#     pg_dump | psql clone from live          3.75s
#     CREATE DATABASE ... TEMPLATE (idle)     0.49s
#     DROP DATABASE                           0.75s
#     schema fingerprint query                0.11s
#
# So every session gets its OWN database, copied from an idle template by
# Postgres itself in half a second. Runs stop queueing because they stop
# sharing: the unqualified `DELETE FROM app_events` that made two runs delete
# each other's rows now happens in a database only that run can see. The lock is
# gone; the only remaining exclusion is a short advisory lock around a template
# REFRESH, which is rare.
#
# The template is never connected to by tests — that restriction is load-bearing
# (`CREATE DATABASE ... TEMPLATE` fails with "source database is being accessed
# by other users", measured), and it is exactly why the template is a separate
# database from the one sessions run in.

TEMPLATE_DB = os.environ.get("TEST_POSTGRES_TEMPLATE", "pharmacy_test_tpl")
SESSION_DB_PREFIX = "pharmacy_test_s"

# Leftover session databases are only reaped when they are BOTH unused and older
# than this. "No connections" alone is not safe: between tests the pool is reset
# and a live run momentarily has none, so a concurrent sweep would drop a
# database out from under a running suite. The full suite takes ~4 minutes.
SESSION_DB_REAP_AFTER = 2 * 3600

# The schema's shape, hashed. Three sources, and each is here because leaving it
# out was shown to miss a real change on this database:
#
#   columns      new/dropped tables and columns, and type changes. Caught
#                `tool_calls`, `llm_calls`, `chat_logs.actor_email` and
#                `chat_feedback.turn_id` arriving — verified by dropping each
#                from a throwaway clone and watching the hash move.
#   nullability  a NOT NULL that exists on live but not in the clone lets a test
#                insert a NULL, pass, and ship a row production would reject.
#   defaults     same class: the clone would supply a different value.
#   indexes      cheap to include; a missing one only costs speed, but a test
#                that asserts an index exists would pass against the wrong shape.
#   constraints  THE one that caught real drift. A columns-only fingerprint
#                called the template current while live had quietly replaced
#                `chat_feedback_turn_id_fkey ... ON DELETE CASCADE` with
#                `chat_feedback_turn_fk ... ON DELETE SET NULL`. Deleting a turn
#                then removes the feedback row on one and nulls it on the other —
#                a test would not fail, it would assert the wrong behaviour.
#
# Keep this IDENTICAL to the query in tests/setup_test_db.sh: the script stamps
# the template and this reads live, so a divergence would rebuild on every run.
# `test_the_template_matches_the_live_schema` fails if they ever disagree.
_FINGERPRINT_SQL = """
    SELECT md5(string_agg(t, '|' ORDER BY t))
    FROM (
        SELECT 'c:' || table_name || '.' || column_name || ':' || data_type
               || ':' || is_nullable || ':' || coalesce(column_default, '') AS t
        FROM information_schema.columns
        WHERE table_schema = 'public'
        UNION ALL
        SELECT 'i:' || indexname || ':' || indexdef
        FROM pg_indexes WHERE schemaname = 'public'
        UNION ALL
        SELECT 'k:' || conname || ':' || pg_get_constraintdef(oid)
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace
    ) s
"""


# One advisory lock guards the template's whole lifecycle.
#
# It has to be shared/exclusive, not a plain mutex, and the reason was measured
# rather than reasoned about. Rebuilding DROPs and recreates the template, so for
# ~3 seconds it DOES NOT EXIST:
#
#     t+0s  stamp=fingerprint:fc8ed4b2…
#     t+1s  stamp=<NULL>          <-- template absent
#     t+2s  stamp=<NULL>
#     t+3s  stamp=<NULL>
#     t+4s  stamp=fingerprint:fc8ed4b2…
#
# A concurrent run reading the stamp in that window got `None` and its template
# check failed; the next run passed, because the window had closed. That is the
# order-dependent flake, and it is a synchronisation bug in this file — not a
# stale template and not a comparison that is too strict.
#
# A second run trying to REBUILD in that window failed harder still: its
# `DROP DATABASE` hit "database is being accessed by other users" and aborted
# collection.
#
# So: a rebuild takes the lock EXCLUSIVE, and everything that observes the
# template — reading its stamp, copying it into a per-run database — takes it
# SHARED. Readers never see a half-built template, rebuilds never overlap, and
# concurrent runs still copy in parallel because shared locks do not block each
# other.
_TEMPLATE_LOCK = "hashtext('pharmacy-agent-test-template')"


async def _lock(conn, exclusive: bool) -> None:
    fn = "pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
    await conn.execute(f"SELECT {fn}({_TEMPLATE_LOCK})")


async def _unlock(conn, exclusive: bool) -> None:
    fn = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    try:
        await conn.execute(f"SELECT {fn}({_TEMPLATE_LOCK})")
    except Exception:  # noqa: BLE001 — the connection is closing anyway
        pass


def _sql_literal(value: str) -> str:
    """Quote a string for statements that take no bind parameters.

    `COMMENT ON DATABASE` and `CREATE/DROP DATABASE` are utility statements:
    Postgres rejects `$1` in them outright (`syntax error at or near "$1"`), so
    the value has to be inlined and therefore has to be quoted here.
    """

    return "'" + value.replace("'", "''") + "'"


def admin_url(url: str) -> str:
    """A DSN for the server's `postgres` database — for CREATE/DROP DATABASE."""

    return with_dbname(url, "postgres")


async def _fingerprint(conn) -> str:
    return await conn.fetchval(_FINGERPRINT_SQL)


def live_fingerprint(live_url: str) -> str:
    """The live schema's shape: every public column and its type, hashed.

    Deliberately SCHEMA only, not data. The two drift for different reasons and
    fail differently:

    * **Schema** drift is the one that makes a test LIE — another agent's
      migration adds `tool_calls`, the clone lacks it, and tests either error
      somewhere unrelated or quietly exercise a shape that no longer exists.
      Undetectable from inside a test, so it is what the fingerprint covers.
    * **Data** drift fails LOUDLY on its own: `test_tools.py` asserts ROYAL-D at
      exactly 37605 units across 53 sites, so a re-ingest breaks that test by
      name. Folding row counts into the fingerprint would force a rebuild after
      every ingest to catch something already self-reporting.
    """

    async def go():
        import asyncpg

        conn = await asyncpg.connect(live_url, timeout=10)
        try:
            return await _fingerprint(conn)
        finally:
            await conn.close()

    return _run(go())


def fingerprint_of(url: str) -> str:
    """The schema fingerprint of whatever database *url* names."""

    async def go():
        import asyncpg

        conn = await asyncpg.connect(url, timeout=10)
        try:
            return await _fingerprint(conn)
        finally:
            await conn.close()

    return _run(go())


def template_fingerprint(live_url: str) -> Optional[str]:
    """The fingerprint the template was built from, or None if absent.

    Stored as the template's database COMMENT rather than in a table inside it,
    on purpose: `shobj_description` is readable from ANY database, so checking
    freshness needs no connection to the template. A read connection held while
    another session issues `CREATE DATABASE ... TEMPLATE` would make that
    session fail with "being accessed by other users" — the check would
    intermittently break the thing it is checking.
    """

    async def go():
        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=10)
        try:
            # SHARED: never read the stamp while a rebuild has the template
            # dropped, or this returns None and the caller concludes "stale".
            await _lock(conn, exclusive=False)
            try:
                return await conn.fetchval(
                    """SELECT shobj_description(oid, 'pg_database')
                       FROM pg_database WHERE datname = $1""",
                    TEMPLATE_DB,
                )
            finally:
                await _unlock(conn, exclusive=False)
        finally:
            await conn.close()

    stored = _run(go())
    if not stored or not stored.startswith("fingerprint:"):
        return None
    return stored.split(":", 1)[1]


def template_is_current(stamp: Optional[str], live: Optional[str]) -> bool:
    """Reuse the template only on an exact match. Everything else rebuilds.

    Split out as a pure function so the RULE can be pinned by a test that cannot
    pass by accident on a day when nothing happens to have drifted. Note that two
    Nones are NOT "equal, therefore fine": an unreadable fingerprint is a reason
    to rebuild, not a reason to trust.
    """

    return bool(stamp) and bool(live) and stamp == live


def ensure_template(live_url: str) -> str:
    """Make the template match the live schema. Returns "current" or "rebuilt".

    Refreshing is the DEFAULT and reuse is the exception: the template is only
    kept when its recorded fingerprint equals the live one right now. Anything
    else — missing template, missing comment, any schema difference — rebuilds.
    So the fast path is the provably-current case, not the hopeful one.

    The rebuild takes the template lock EXCLUSIVE, so several sessions starting at
    once produce one rebuild rather than several racing `pg_dump`s — and, more
    importantly, no other session can read the stamp or copy the template while it
    briefly does not exist. Before that lock existed, a concurrent run saw a NULL
    stamp mid-rebuild and reported the template as stale; the run after it passed.
    """

    want = live_fingerprint(live_url)
    if template_is_current(template_fingerprint(live_url), want):
        return "current"

    import subprocess

    # The lock connection and the rebuild must share one event loop, and the
    # rebuild shells out, so the whole critical section runs inside one _run.
    async def go():
        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=10)
        try:
            await _lock(conn, exclusive=True)
            # Re-check under the lock: another session may have rebuilt it while
            # we waited, in which case there is nothing to do. Without this, N
            # sessions that all saw a stale template would all rebuild it.
            stored = await conn.fetchval(
                """SELECT shobj_description(oid, 'pg_database')
                   FROM pg_database WHERE datname = $1""",
                TEMPLATE_DB,
            )
            recheck = None
            if stored and stored.startswith("fingerprint:"):
                recheck = stored.split(":", 1)[1]
            if template_is_current(recheck, want):
                return "current"

            script = os.path.join(os.path.dirname(__file__), "setup_test_db.sh")
            proc = subprocess.run(
                [script], capture_output=True, text=True,
                env={**os.environ, "TEST_DB": TEMPLATE_DB},
            )
            if proc.returncode != 0:
                raise LiveDatabaseRefused(
                    f"the test template {TEMPLATE_DB!r} is stale or missing and "
                    f"rebuilding it failed:\n\n{proc.stdout}\n{proc.stderr}\n"
                    f"Refusing to run rather than testing against a schema that "
                    f"does not match live."
                )
            # COMMENT takes no bind parameters, so the value is inlined. `want`
            # is an md5 hex digest from the server, but quote it properly rather
            # than relying on that staying true.
            await conn.execute(
                f'COMMENT ON DATABASE "{TEMPLATE_DB}" IS '
                f"{_sql_literal('fingerprint:' + want)}"
            )
            return "rebuilt"
        finally:
            try:
                await _unlock(conn, exclusive=True)
            finally:
                await conn.close()

    return _run(go())


def create_session_db(live_url: str, name: str) -> None:
    """Copy the template into this run's own database (~0.5s, measured)."""

    import time as _time

    async def go():
        import asyncio

        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=10)
        try:
            # SHARED: blocks only against a rebuild, never against another run's
            # copy — so concurrent suites still start in parallel, they just
            # cannot catch the template mid-drop.
            await _lock(conn, exclusive=False)
            try:
                # Still retried: a stray psql session on the template (not ours,
                # so not covered by the lock) produces the same transient error.
                last = None
                for _ in range(20):
                    try:
                        await conn.execute(
                            f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"'
                        )
                        break
                    except Exception as exc:  # noqa: BLE001
                        if "being accessed by other users" not in str(exc):
                            raise
                        last = exc
                        await asyncio.sleep(0.25)
                else:
                    raise LiveDatabaseRefused(
                        f"could not copy {TEMPLATE_DB!r}: {last!r}"
                    )
                await conn.execute(
                    f'COMMENT ON DATABASE "{name}" IS '
                    f"{_sql_literal(f'session:{int(_time.time())}')}"
                )
            finally:
                await _unlock(conn, exclusive=False)
        finally:
            await conn.close()

    _run(go())


def drop_session_db(live_url: str, name: str) -> None:
    """Remove this run's database. Best-effort — a leftover is reaped later."""

    async def go():
        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=10)
        try:
            await conn.execute(
                """SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname = $1 AND pid <> pg_backend_pid()""",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    with allow_live():
        try:
            _run(go())
        except Exception as exc:  # noqa: BLE001 — never fail a run on cleanup
            print(f"\n[dbguard] could not drop {name}: {exc!r}")


def reap_stale_session_dbs(live_url: str) -> int:
    """Drop databases left behind by killed runs. Returns how many.

    Both conditions are required — unused AND old. "Unused" alone would drop a
    running suite's database during the moment between two tests when the pool
    has been reset and no connection is open.
    """

    import time as _time

    cutoff = int(_time.time()) - SESSION_DB_REAP_AFTER

    async def go():
        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=10)
        try:
            rows = await conn.fetch(
                """SELECT d.datname, shobj_description(d.oid, 'pg_database') AS note
                   FROM pg_database d
                   WHERE d.datname LIKE $1
                     AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a
                                     WHERE a.datname = d.datname)""",
                SESSION_DB_PREFIX + "%",
            )
            dropped = 0
            for r in rows:
                note = r["note"] or ""
                if not note.startswith("session:"):
                    continue
                try:
                    born = int(note.split(":", 1)[1])
                except ValueError:
                    continue
                if born > cutoff:
                    continue
                await conn.execute(f"DROP DATABASE IF EXISTS \"{r['datname']}\"")
                dropped += 1
            return dropped
        finally:
            await conn.close()

    try:
        return _run(go())
    except Exception:  # noqa: BLE001 — housekeeping must never fail a run
        return 0


def server_reachable(live_url: str) -> bool:
    """True when something answers on the configured Postgres server."""

    async def go():
        import asyncpg

        conn = await asyncpg.connect(admin_url(live_url), timeout=5)
        await conn.close()
        return True

    try:
        return _run(go())
    except Exception:  # noqa: BLE001
        return False


def bootstrap(live_url: str):
    """Prepare this run's database. Returns ``(status, test_url, template)``.

    ``status`` is ``"ok"`` or ``"no-server"``; ``template`` is ``"current"``,
    ``"rebuilt"``, ``"explicit"`` or ``None``.

    Order matters. The test DSN is derived — and proved not to be the live one —
    BEFORE anything connects, so there is no window in which a failure part-way
    through leaves the suite pointed at production.
    """

    if not live_url:
        raise LiveDatabaseRefused(
            "POSTGRES_URL is empty, so the test database cannot be derived. "
            "Set TEST_POSTGRES_URL explicitly."
        )

    # An explicit DSN is an advanced escape hatch (a test database on another
    # server). It is taken as given and only checked, never created.
    explicit = os.environ.get("TEST_POSTGRES_URL")
    if explicit:
        test_url = resolve_test_url(live_url)
        return preflight(live_url, test_url), test_url, "explicit"

    name = f"{SESSION_DB_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:6]}"
    test_url = with_dbname(live_url, name)
    # Belt and braces: the derived name cannot collide with live, but assert it
    # rather than assume the DSN parsed the way we expect.
    if dbname_of(test_url) == dbname_of(live_url):
        raise LiveDatabaseRefused(
            f"the per-run database name resolved to the live one "
            f"({redact(live_url)}). Refusing to run."
        )

    if not server_reachable(live_url):
        # Nothing to damage. Collection proceeds and the DB-backed tests fail on
        # their own, exactly as they did before isolation existed. Refusing here
        # would only teach people to set an env var that disables the guard.
        return "no-server", test_url, None

    template = ensure_template(live_url)

    # The schema this run was validated against, captured AFTER the template is
    # known good and BEFORE the copy. This — not "live right now" — is what the
    # suite's results are true of. Live belongs to other people: the stack gets
    # redeployed and other agents run migrations mid-run, so re-reading live at
    # test time compares against a moving target and fails for reasons that say
    # nothing about this run.
    global BOOTSTRAP_FINGERPRINT
    BOOTSTRAP_FINGERPRINT = template_fingerprint(live_url)

    reap_stale_session_dbs(live_url)
    create_session_db(live_url, name)
    return "ok", test_url, template


def preflight(live_url: str, test_url: str) -> str:
    """Prove the test database is usable, or fail loudly. Returns a status word.

    Three outcomes, and the middle one is the whole point:

    ``"ok"``
        The test database answered. The suite runs against it.
    raise
        The **server** is up but the test database is missing or unusable. This
        is the dangerous state — the one where a silent fallback would put the
        suite back on live data — so it aborts the run with the command that
        fixes it.
    ``"no-server"``
        Nothing is listening at all (CI with no stack). There is no live data to
        damage, so collection proceeds and the DB-backed tests fail on their own
        exactly as they did before. Refusing here would only teach people to set
        an env var that turns the guard off.
    """

    import asyncpg

    async def _probe(url):
        conn = await asyncpg.connect(url, timeout=5)
        try:
            return await conn.fetchval("SELECT current_database()")
        finally:
            await conn.close()

    try:
        actual = _run(_probe(test_url))
    except Exception as exc:  # noqa: BLE001 — every failure is handled below
        try:
            _run(_probe(live_url))
        except Exception:  # noqa: BLE001 — no server at all
            return "no-server"
        raise LiveDatabaseRefused(
            f"the Postgres server at {redact(live_url)} is up, but the test "
            f"database {dbname_of(test_url)!r} is not usable ({exc!r}).\n"
            f"\n"
            f"Refusing to run: without it the suite would write to the database "
            f"the product is serving from — it has already destroyed the live "
            f"branding twice.\n"
            f"\n"
            f"Create it with:\n"
            f"    tests/setup_test_db.sh\n"
        ) from exc

    if actual != dbname_of(test_url):
        raise LiveDatabaseRefused(
            f"connected to {actual!r} while asking for "
            f"{dbname_of(test_url)!r} — refusing to run."
        )
    return "ok"


# ---------------------------------------------------------------------------
# The connection-layer guard
# ---------------------------------------------------------------------------

def _check(dsn, kwargs, what: str) -> None:
    if _ALLOWED_DB is None or _ALLOW_LIVE:
        return

    name = None
    if dsn:
        name = dbname_of(dsn)
    if not name:
        name = kwargs.get("database") or kwargs.get("db")
    if not name:
        raise LiveDatabaseRefused(
            f"{what} was called with no database name. The guard cannot tell "
            f"which database that reaches, so it is refused. Pass an explicit DSN."
        )
    if name != _ALLOWED_DB:
        raise LiveDatabaseRefused(
            f"{what} tried to open database {name!r}; the suite is only allowed "
            f"to touch {_ALLOWED_DB!r}.\n"
            f"\n"
            f"Something built its DSN before tests/conftest.py redirected "
            f"POSTGRES_URL, or hardcoded one. Read it from "
            f"app.config.get_settings().postgres_url instead — the tests destroy "
            f"rows, and this is the database the product serves from."
        )


def install(allowed_db: str) -> None:
    """Patch asyncpg so only *allowed_db* is reachable. Idempotent."""

    global _ALLOWED_DB, _orig_connect, _orig_create_pool

    _ALLOWED_DB = allowed_db
    if _orig_connect is not None:
        return

    import asyncpg

    _orig_connect = asyncpg.connect
    _orig_create_pool = asyncpg.create_pool

    def connect(dsn=None, **kwargs):
        _check(dsn or kwargs.get("dsn"), kwargs, "asyncpg.connect")
        return _orig_connect(dsn, **kwargs)

    def create_pool(dsn=None, **kwargs):
        _check(dsn or kwargs.get("dsn"), kwargs, "asyncpg.create_pool")
        return _orig_create_pool(dsn, **kwargs)

    asyncpg.connect = connect
    asyncpg.create_pool = create_pool


@contextlib.contextmanager
def allow_live():
    """Temporarily permit a live connection — for tests that AUDIT live data.

    Only :mod:`tests.test_postgres_isolation` uses this, to read the live server
    and assert the suite left no fingerprints on it. Nothing that writes may use
    it, and it is deliberately noisy to grep for.
    """

    global _ALLOW_LIVE
    prev = _ALLOW_LIVE
    _ALLOW_LIVE = True
    try:
        yield
    finally:
        _ALLOW_LIVE = prev


def assert_test_database(url: Optional[str] = None) -> None:
    """Assert *url* (default: the configured DSN) is the test database.

    The connection-layer patch above already makes this impossible to get wrong,
    but the two fixtures with **unqualified** DELETEs call it anyway. Those two
    statements are the ones that erased production data, and a reader looking at
    ``DELETE FROM brand_assets`` should be able to see the guard on the line
    above it rather than having to trust a patch installed in another file.
    """

    if url is None:
        from app.config import get_settings

        url = get_settings().postgres_url
    name = dbname_of(url)
    if _ALLOWED_DB is None:
        raise LiveDatabaseRefused(
            "the database guard is not installed; refusing a destructive fixture."
        )
    if name != _ALLOWED_DB:
        raise LiveDatabaseRefused(
            f"destructive fixture is pointed at database {name!r}, not the test "
            f"database {_ALLOWED_DB!r}. Refusing to delete."
        )
