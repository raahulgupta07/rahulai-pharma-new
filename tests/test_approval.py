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


async def _fresh_user(approved=False, role="admin"):
    await authmod.ensure_users_table()
    email = f"appr-{uuid.uuid4().hex[:10]}@corp.mm"
    u = await authmod.create_user(email, "Test", "pw12345", role, approved=approved)
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
