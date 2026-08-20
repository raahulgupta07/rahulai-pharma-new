"""One private Postgres connection per test process.

Sixteen test modules each carried a byte-identical copy of this:

    def _pg(query, *args, fetch=False):
        async def go():
            conn = await asyncpg.connect(get_settings().postgres_url)
            try:
                ...
            finally:
                await conn.close()
        return asyncio.run(go())

A fresh connection AND a fresh event loop for every single statement. The
seeding fixtures make dozens of calls each, across hundreds of tests, so the
suite spent most of its wall clock on Postgres handshakes rather than on
assertions — `--durations` showed its slowest twenty-five entries were fixture
SETUP, not a single test body.

What made the original correct is preserved exactly: this is a PRIVATE
connection, never `app.db`'s pool, so these tests cannot exhaust or disturb the
pool the endpoints are using while they run. Short-lived was never the point.

Per PROCESS is the unit that matters. Under xdist each worker is its own
process and gets its own connection, so workers still cannot observe each
other's work — the isolation the suite already relies on is unchanged.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings

_LOOP: asyncio.AbstractEventLoop | None = None
_CONN = None


def _connect_coro():
    import asyncpg

    return asyncpg.connect(get_settings().postgres_url)


def pg(query: str, *args, fetch: bool = False):
    """Run one statement on this process's private connection."""

    global _LOOP, _CONN

    async def run(conn):
        if fetch:
            return [dict(r) for r in await conn.fetch(query, *args)]
        await conn.execute(query, *args)
        return None

    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
    if _CONN is None:
        _CONN = _LOOP.run_until_complete(_connect_coro())
    try:
        return _LOOP.run_until_complete(run(_CONN))
    except Exception:
        # A connection that has died — the server restarted, an idle timeout —
        # must not turn every remaining test in the process red. Reconnect once;
        # a genuine query error raises again on the retry and is reported as
        # itself.
        try:
            _LOOP.run_until_complete(_CONN.close())
        except Exception:
            pass
        _CONN = _LOOP.run_until_complete(_connect_coro())
        return _LOOP.run_until_complete(run(_CONN))
