"""Guard the admin-approval gate on console access.

An authenticated account only reaches the admin console once an admin approves
it. The load-bearing, easy-to-break invariants:

* the migration that adds `approved` must APPROVE everyone already in the table,
  or upgrading the app locks out the existing super_admin;
* a pending account authenticates (200) but is refused by `require_admin` (403);
* approval is re-checked against the DB per request, so it takes effect on the
  account's EXISTING token — no re-login — and revoking is immediate.

Needs live Postgres, like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.pgconn import pg

from app import auth as authmod

# Import at collection time, not inside a test. app.api pulls in agno, which
# builds an asyncio.Lock() at import — that calls get_event_loop(), and on py3.9
# after a prior asyncio.run() has closed its loop the call raises. Importing here
# runs it once, before any test loop exists.
from app.api import require_admin


def run(coro):
    """Run one coroutine on a fresh loop, then close the DB pool bound to it so
    its connections do not leak into the next test's loop (asyncpg pools are
    loop-bound; a leaked one raises RuntimeError on a later, closed loop)."""

    from app import db

    async def _wrapped():
        try:
            return await coro
        finally:
            await db.close_pool()

    return asyncio.run(_wrapped())


# Every account this module creates, registered the instant it exists.
#
# Cleanup used to be the LAST statement of each test coroutine — so any test
# that raised before reaching it left its account behind for good. It did:
# `appr-188413e477@corp.mm` (id 6) and `appr-150e95de29@corp.mm` (id 9) were
# still sitting in the production `users` table months later, and every request
# they had made was still in `app_events`, out-ranking the real admin in the
# Activity feed. The teardown below runs whether the test passed, failed, or
# blew up in the middle, which is the only version of this that holds.
_CREATED: set = set()
_CREATED_EMAILS: set = set()


def _pg(query: str, *args):
    """Run one statement on a private connection. Never touches app.db's pool.

    Teardown cannot use `app.db.execute`: `run()` above closes the pool at the
    end of every test, and the loop it was bound to is closed with it.

    One connection per PROCESS, not per statement — see tests/pgconn.py for
    why the previous arrangement was the suite's whole wall clock.
    """

    pg(query, *args)


@pytest.fixture(autouse=True)
def _drop_created_users():
    """Remove every account this module made, however the test ended."""

    _CREATED.clear()
    _CREATED_EMAILS.clear()
    yield
    ids, emails = sorted(_CREATED), sorted(_CREATED_EMAILS)
    _CREATED.clear()
    _CREATED_EMAILS.clear()
    if ids:
        _pg("DELETE FROM users WHERE id = ANY($1::int[])", ids)
    if emails:
        # `test_migration_approves_preexisting_rows` inserts by email and never
        # learns an id — and it DROPs the `approved` column halfway through, so
        # it is the likeliest test here to die mid-body.
        _pg("DELETE FROM users WHERE email = ANY($1::text[])", emails)


async def _fresh_user(approved=False, role="admin"):
    await authmod.ensure_users_table()
    email = f"appr-{uuid.uuid4().hex[:10]}@corp.mm"
    u = await authmod.create_user(email, "Test", "pw12345", role, approved=approved)
    _CREATED.add(u["id"])
    return email, u


def test_new_user_is_pending_by_default():
    async def go():
        _, u = await _fresh_user()
        approved = u["approved"]
        await authmod.delete_user(u["id"])
        return approved

    assert run(go()) is False


def test_seeded_super_admin_is_approved():
    """The seed and the migration must both leave the super_admin usable."""

    async def go():
        await authmod.ensure_users_table()
        await authmod.seed_super_admin()
        from app.config import get_settings
        return await authmod.get_by_email(get_settings().admin_email)

    u = run(go())
    if u is None:
        pytest.skip("no seeded super_admin in this DB")
    assert bool(u["approved"]) is True


def test_migration_approves_preexisting_rows():
    """Simulate an upgrade: a row created before the column existed must end up
    approved, never locked out."""

    async def go():
        from app.db import execute, q
        await authmod.ensure_users_table()
        email = f"legacy-{uuid.uuid4().hex[:8]}@corp.mm"
        _CREATED_EMAILS.add(email)
        # insert, then force it to pending as if it predated approval
        await execute(
            "INSERT INTO users (email, name, role, approved) VALUES ($1,$2,'admin',TRUE)",
            email, "Legacy",
        )
        await execute("UPDATE users SET approved=FALSE WHERE email=$1", email)
        # drop the column and re-run ensure_users_table -> migration re-adds + approves
        await execute("ALTER TABLE users DROP COLUMN approved")
        await authmod.ensure_users_table()
        row = (await q("SELECT approved FROM users WHERE email=$1", email))[0]
        await execute("DELETE FROM users WHERE email=$1", email)
        return row["approved"]

    assert run(go()) is True


def test_pending_user_is_refused_by_require_admin_then_allowed():
    """The full gate, through the real dependency, on one unchanged token."""

    async def go():
        email, u = await _fresh_user(approved=False)
        token = authmod.make_token(u)["token"]
        header = f"Bearer {token}"
        results = {}
        # pending -> 403
        try:
            await require_admin(header)
            results["pending"] = "allowed"
        except Exception as exc:  # HTTPException
            results["pending"] = getattr(exc, "status_code", "err")
        # approve, same token -> allowed
        await authmod.update_user(u["id"], approved=True)
        try:
            await require_admin(header)
            results["approved"] = "allowed"
        except Exception as exc:
            results["approved"] = getattr(exc, "status_code", "err")
        # revoke, same token -> 403 again
        await authmod.update_user(u["id"], approved=False)
        try:
            await require_admin(header)
            results["revoked"] = "allowed"
        except Exception as exc:
            results["revoked"] = getattr(exc, "status_code", "err")
        await authmod.delete_user(u["id"])
        return results

    r = run(go())
    assert r["pending"] == 403
    assert r["approved"] == "allowed"     # took effect with no re-login
    assert r["revoked"] == 403            # revocation is immediate
