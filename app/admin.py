"""Admin API — backs the SvelteKit management panel.

Read-mostly endpoints over the existing data + a little CRUD for credentials
and config. Mounted under /admin in app.api.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import jwt
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel

from app import cache
from app.config import DEFAULT_SECRET_KEY, Settings, get_settings
from app.db import counts, execute, q
from app.tools import _site_clause

router = APIRouter(prefix="/admin", tags=["admin"])


# ---- "not created yet" is ONE error, not every error ------------------------
#
# This codebase applies schema at BOOT, so a first-boot console legitimately
# queries relations that do not exist yet: `chat_logs` is created by the DDL in
# `app.activity`, and `mv_article_summary` only after the first ingest. Those
# reads must answer an empty list quietly rather than 500 an operator on a fresh
# install.
#
# The tempting way to write that is `except Exception: return []`, and it is
# wrong: it also swallows a syntax error, a dropped column, a pool failure and a
# timeout, and hands every one of them back as "there is no data". On an AUDIT
# surface (`/admin/conversations`) that is not a degraded answer, it is a false
# statement — "no conversations took place" instead of "I could not answer" —
# and the console's ErrorState never renders, so nobody is told to look.
# Demonstrated by renaming the relation out from under the running code: both
# endpoints answered 200 with zero rows and looked healthy.
#
# So catch exactly the missing-relation condition and let everything else
# propagate to FastAPI as a 500. asyncpg raises `UndefinedTableError` for a
# missing table AND for a missing materialized view (both are `relation ... does
# not exist`, SQLSTATE 42P01); `UndefinedObjectError` covers a missing type or
# other object referenced by the same first-boot query. Narrow on purpose — do
# not widen this to `Exception`.
#
# `app.stores.ensure_stores_table` is the other shape of the same decision: it
# pre-checks with `to_regclass` where it must not let the error abort a larger
# block. Where a plain read is all that is at stake, catching the one exception
# is cheaper (no extra round trip) and has no check-then-act window.
_MISSING_RELATION = (
    asyncpg.exceptions.UndefinedTableError,
    asyncpg.exceptions.UndefinedObjectError,
)


def _int_or_none(v) -> Optional[int]:
    """int(v), preserving NULL. NULL stock is UNKNOWN — never coerce it to 0."""

    return None if v is None else int(v)


# ---- schema owned by the admin surface -------------------------------------


async def ensure_admin_schema() -> None:
    """Create/extend the tables this router owns. Idempotent; run at startup.

    * ``users.store_id`` — pins an admin account to ONE branch. NULL (the state
      every existing row is in) means the global view, so adding the column
      changes nobody's access.
    * ``drug_alias`` — the fast path's learned-alias table. It has existed as
      ``migrations/0002_drug_alias.sql`` since the fast path landed, but nothing
      applied it on boot and nothing wrote to it, so ``resolver._alias_lookup``
      was a permanent miss. Created here so the write path below has a table.
    """

    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    # last_seen: the run timestamp of the last article file to carry a catalog
    # row. Written by ingest_catalog, read by full_sync (delete rows not in the
    # latest file) and the manual stale purge. NULL on every existing row until
    # the first ingest after this deploy — treated as "unknown age".
    await execute("ALTER TABLE catalog ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
    await execute(
        """
        CREATE TABLE IF NOT EXISTS drug_alias (
            alias        TEXT PRIMARY KEY,
            article_code TEXT NOT NULL
                         REFERENCES catalog(article_code) ON DELETE CASCADE,
            source       TEXT,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_drug_alias_article ON drug_alias (article_code)"
    )


# ---- caller store scope ----------------------------------------------------


async def caller_store_scope(authorization: str = Header(default="")) -> Optional[str]:
    """The branch this admin caller is pinned to, or ``None`` for the global view.

    The chat layer scopes by taking ``store_id`` off a signed token and forcing
    every tool through ``tools._site_clause``. The admin layer had no equivalent
    at all, so ``GET /admin/catalog/{code}`` handed any caller every branch's
    stock. This is the admin-side half of the same mechanism: the scope comes
    from the server (the caller's ``users`` row), never from the request, and it
    is matched with the very same ``_site_clause``.

    * ``super_admin`` — always global. A pinned super_admin would be a way to
      lock the top account out of its own data.
    * anyone else — scoped iff their row carries a ``store_id``.

    Every existing account has ``store_id`` NULL, so today this is a no-op and
    the admin console is unchanged. Assigning a store to an ``admin`` row (see
    ``PATCH /admin/users/{id}``) turns them into a branch manager.

    ``require_admin`` has already authenticated this exact token at the router
    level, so a failure here means the token vanished between two dependencies —
    reject rather than fall through to the global view.
    """

    from app import auth as authmod

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = authmod.decode_token(authorization.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — any decode failure is a rejected caller
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user = await authmod.get_by_email(claims.get("email", ""))
    if not user:
        raise HTTPException(status_code=401, detail="account not found")
    if user["role"] == "super_admin":
        return None
    return (user.get("store_id") or "").strip() or None


async def require_super_admin(authorization: str = Header(default="")) -> Dict:
    """Narrow an /admin/* endpoint from "any admin" to super_admin only.

    The router-level ``api.require_admin`` already proved this token belongs to
    an active, approved ``admin`` **or** ``super_admin``. Endpoints that hand
    back a shared secret (see ``GET /sftp/connection``, which returns the SFTP
    password) need the stricter half, so this re-reads the caller's row exactly
    as ``caller_store_scope`` does — role from the ``users`` table, never from
    the token — and rejects a plain ``admin`` with 403.

    Re-reading the DB rather than trusting the JWT's claims is deliberate and
    matches ``require_admin``: a demoted account loses the password at its next
    request, not at token expiry.
    """

    from app import auth as authmod

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = authmod.decode_token(authorization.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — any decode failure is a rejected caller
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user = await authmod.get_by_email(claims.get("email", ""))
    if not user or not user["active"] or not user.get("approved"):
        raise HTTPException(status_code=401, detail="account not found")
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="super_admin access required")
    return user


async def caller_is_super_admin(authorization: str = Header(default="")) -> bool:
    """Is this caller a super_admin? A yes/no, where ``require_super_admin`` is a gate.

    Same mechanism as ``caller_store_scope`` and ``require_super_admin`` —
    decode the bearer, then read the ROLE off the caller's ``users`` row. Never
    off the token: the whole reason those two re-read the database is that a
    demotion has to take effect on the account's existing session, and a field
    projection that trusted the JWT would keep handing an ex-super_admin the
    operator notes until their token expired.

    This exists because ``GET /stores`` is not a gate — it answers for a plain
    ``user`` and for a super_admin, with DIFFERENT fields (see ``_store_row``).
    ``require_super_admin`` cannot express that: it raises 403, which would take
    the branch list away from the console roles that legitimately read it.

    An unreadable token is a rejected caller here exactly as it is there, rather
    than a quiet ``False``. ``require_admin`` has already authenticated this same
    token at the router level, so there is no legitimate way to arrive with a bad
    one, and "not a super_admin" is the wrong description of "not authenticated".
    """

    from app import auth as authmod

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = authmod.decode_token(authorization.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — any decode failure is a rejected caller
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user = await authmod.get_by_email(claims.get("email", ""))
    if not user:
        raise HTTPException(status_code=401, detail="account not found")
    return user["role"] == "super_admin"


# ---- catalog ---------------------------------------------------------------


@router.get("/catalog")
async def catalog(
    search: str = "", category: str = "", limit: int = 50, offset: int = 0
) -> List[Dict]:
    """List/search catalog (brand, generic, or exact code), paginated.

    Optional `category` filters by category substring (e.g. "PRESCRIPTION").
    """

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    conds, params = [], []
    if search:
        params.append(search)
        n = len(params)
        conds.append(
            f"(brand_name ILIKE '%'||${n}||'%' OR generic_name ILIKE '%'||${n}||'%' OR article_code = ${n})"
        )
    # Stub rows (brand_name == article_code) are NO LONGER hidden. Filtering
    # them out made a 100%-stub catalog render as a clean, ordinary-looking
    # table — which is why the broken load on the customer host went unnoticed
    # for weeks while the chat agent answered "not found" for stocked products.
    # They are returned and flagged; the UI badges them.
    if category:
        params.append(category)
        conds.append(f"category ILIKE '%'||${len(params)}||'%'")
    # Guarded, like every sibling listing: with no search and no category
    # `conds` is empty, and an unguarded "WHERE " emits `FROM catalog WHERE
    # ORDER BY` — a syntax error, i.e. a 500 on the Data page's default view.
    # It was safe until the stub filter above was removed, because that
    # condition was always present.
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    return await q(
        f"""SELECT article_code, brand_name, generic_name, category,
                   (brand_name = article_code) AS is_stub
              FROM catalog {where}
             ORDER BY (brand_name = article_code), brand_name
             LIMIT ${len(params)-1} OFFSET ${len(params)}""",
        *params,
    )


@router.get("/categories")
async def categories() -> List[Dict]:
    """Distinct catalog categories with counts (for the data-page filter)."""

    return await q(
        """SELECT category, count(*) AS n FROM catalog
            WHERE category IS NOT NULL AND brand_name <> article_code
            GROUP BY category ORDER BY n DESC"""
    )


@router.get("/catalog/{code}")
async def catalog_one(
    code: str, scope: Optional[str] = Depends(caller_store_scope)
) -> Dict:
    """Full article detail + per-site stock + summary, scoped to the caller's branch.

    Two bugs lived here, both of them a pharmacy reading a number that is not
    true:

    **Every branch's stock, to everyone.** The site query had no scope clause at
    all, so a branch-scoped account saw its siblings' inventory — the same leak
    class already fixed in ``search_by_meaning`` / ``related_drugs``. It now
    filters through ``tools._site_clause``, the one correct site matcher (a bare
    ``=`` or an ``ILIKE '%x%'`` here would reintroduce the two scoping bugs
    documented in CLAUDE.md). ``scope`` is ``None`` for admin/super_admin, and the
    ``$2 IS NULL`` guard keeps that the full view.

    **NULL stock counted as zero.** ``sum(s["stock_qty"] or 0)`` coerced UNKNOWN
    to 0, contradicting the repo-wide invariant that a NULL ``stock_qty`` means
    *we do not know*, never *none on hand*. A pharmacist reading "0" does not
    dispense. Unknown now stays unknown: ``total_stock`` sums only the branches
    we actually have a figure for, and is ``None`` when we have a figure for none
    of them. ``unknown_site_count`` says how many branches were left out, so a
    partial total is never mistaken for a complete one.
    """

    rows = await q("SELECT * FROM catalog WHERE article_code=$1", code)
    if not rows:
        raise HTTPException(status_code=404, detail="article not found")
    article = {k: v for k, v in rows[0].items() if k != "embedding"}
    sites = await q(
        """SELECT site_code, stock_qty, price FROM inventory
            WHERE article_code=$1
              AND ($2::text IS NULL OR """ + _site_clause("site_code", "$2") + """)
            ORDER BY stock_qty DESC NULLS LAST""",
        code,
        scope,
    )
    for s in sites:
        s["price"] = float(s["price"]) if s["price"] is not None else None

    known = [s["stock_qty"] for s in sites if s["stock_qty"] is not None]
    return {
        "article": article,
        "sites": sites,
        # None (not 0) when no branch in scope reports a quantity: UNKNOWN.
        "total_stock": sum(known) if known else None,
        "site_count": len(sites),
        "known_site_count": len(known),
        "unknown_site_count": len(sites) - len(known),
        "store_scope": scope,
    }


# ---- inventory -------------------------------------------------------------


@router.get("/inventory")
async def inventory(
    site: str = "",
    search: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
    scope: Optional[str] = Depends(caller_store_scope),
) -> List[Dict]:
    """Inventory rows, optionally filtered by site, article code and/or stock status.

    `status` is one of: in (>=20), low (1–19), out (0).

    Scoped exactly like ``catalog_one``: this endpoint had NO scope dependency at
    all, so a branch-pinned admin listing inventory read every sibling branch's
    stock and price row by row — the leak `catalog_one` was fixed for, reachable
    from the Data page's default view rather than a per-article drawer.

    The two site predicates below do different jobs and only one of them is a
    scope. ``site`` is the operator's own search box, so a substring ILIKE is
    right there (same reasoning as the unscoped branch of ``tools.list_sites``).
    ``scope`` is an enforced boundary, so it goes through ``_site_clause`` — and
    it is ANDed, never substituted, so a pinned caller who types a sibling's code
    into the filter narrows to nothing instead of crossing the boundary.
    """

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    conds, params = [], []
    if scope:
        params.append(scope)
        conds.append(_site_clause("i.site_code", f"${len(params)}"))
    if site:
        params.append(site)
        conds.append(f"i.site_code ILIKE '%'||${len(params)}||'%'")
    if search:
        params.append(search)
        conds.append(
            f"(i.article_code = ${len(params)} OR c.brand_name ILIKE '%'||${len(params)}||'%')"
        )
    if status == "out":
        conds.append("i.stock_qty = 0")
    elif status == "low":
        conds.append("i.stock_qty BETWEEN 1 AND 19")
    elif status == "in":
        conds.append("i.stock_qty >= 20")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    rows = await q(
        f"""SELECT i.article_code, COALESCE(c.brand_name, i.article_code) AS brand_name,
                   i.site_code, i.stock_qty, i.price
              FROM inventory i LEFT JOIN catalog c USING (article_code)
              {where}
             ORDER BY i.stock_qty DESC LIMIT ${len(params)-1} OFFSET ${len(params)}""",
        *params,
    )
    for r in rows:
        r["price"] = float(r["price"]) if r["price"] is not None else None
    return rows


# ---- stores ----------------------------------------------------------------
#
# A branch's EXISTENCE now comes from the `stores` registry (app/stores.py),
# which accumulates and never auto-deletes; its STOCK still comes from
# `inventory`, which is truncate-and-reload from the daily file. Before the
# registry, a branch dropped from one export vanished from this list, the embed
# picker and every chat answer at once.
#
# ⚠️ **Absence from the registry must never hide a branch — only an explicit
# disable may.** Every read below is therefore built from *everything the data
# knows about, minus what is explicitly disabled*, never from *what the registry
# lists as active*. The two differ only when the registry is incomplete, and
# then they are the difference between hiding one branch somebody disabled and
# hiding the entire company. Concretely, the shape to avoid is
#
#     JOIN stores s ON s.site_code = i.site_code AND s.status = 'active'
#
# which answers NOTHING when `stores` is empty. Agent A measured both against
# the real 53-branch dev inventory with the registry emptied: that join returned
# 0 branches, `stores.not_disabled_clause` / `active_codes()` still returned 53.
# Neither appears here. On top of that, every call into `app.stores` below falls
# back to the pre-registry `inventory` answer if it raises at all.


async def _registry_rows(scope: Optional[str] = None) -> Optional[List[Dict]]:
    """Every registry row, with stock aggregates, or ``None`` if it cannot answer.

    An EMPTY result is folded into ``None`` so the caller falls back. That is
    safe HERE and only here, because ``list_stores`` returns disabled rows too:
    "every branch is disabled" is a 53-row answer, not an empty one, so an empty
    result cannot be a deliberate operator decision being overridden. It can
    only mean no data or a shape that stopped working. Today it means no data —
    ``list_stores`` is a FULL OUTER JOIN against `inventory`, so it empties only
    when both tables do, and then the fallback returns ``[]`` as well. The guard
    costs one wasted query in a case that does not arise, and buys immunity to
    that join shape ever changing.

    **Do not copy this to a customer-facing filter.** Where an empty answer CAN
    be an operator decision — :func:`_embeddable_codes` — falling back on empty
    would silently re-enable every branch somebody disabled.

    ``scope`` is passed straight through, so the store pin is matched by
    ``tools._site_clause`` inside ``list_stores`` — the same SQL predicate as
    every other scoped query, rather than a Python re-implementation of it here.
    """

    try:
        from app import stores as stores_mod

        rows = list(await stores_mod.list_stores(scope))
    except Exception:  # noqa: BLE001 — module absent, table absent, DB blip
        return None
    return rows or None


_STORE_TS_FIELDS = ("first_seen", "last_seen_in_file", "missing_since")

# The three fields that describe WHO hid a branch and WHY. `note` is free text an
# operator types into a super_admin-only dialog ("licence suspended", "pharmacist
# under review"); `disabled_by` is that operator's email address.
_STORE_PRIVILEGED_FIELDS = ("disabled_by", "disabled_at", "note")


def _store_row(r: Dict, *, privileged: bool) -> Dict:
    """One registry row in the shape the console consumes, projected for the caller.

    Every path that answers ``GET /stores`` — the registry list, the live
    fallback, and the echo after a status write — goes through here, so the
    projection cannot be applied to one and forgotten on another. It is the only
    place that decides which fields leave this endpoint.

    ``privileged`` is the caller being a **super_admin**, and it gates
    ``_STORE_PRIVILEGED_FIELDS`` — nothing else. Everything a console user needs
    to read the branch list stays unconditional: the code, the name, the stock
    aggregates, the ``status`` the Disabled chip is drawn from, and the three
    timestamps ``routes/stores/status.js`` derives the other three states from
    (``first_seen`` for New, ``missing_since`` for "not in the latest file",
    ``last_seen_in_file`` for the row's sub-line). Dropping any of those would
    silently collapse the four-state chips into two.

    **Why super_admin and not "any admin".** Hiding a branch is a super_admin
    feature end to end: the two-step dialog, ``POST /stores/{code}/status`` and
    the ``/detail`` panel that shows the audit trail are all behind
    ``require_super_admin``. A plain ``admin`` can neither hide a branch, nor
    un-hide it, nor read the trail — so the operator's stated reason is not
    something they can act on, and "licence suspended" or "pending fraud
    investigation" about a named pharmacy is exactly the sentence whose audience
    should be the smallest one that keeps the feature working. The person typing
    it has no way to know who else can read it back, so the default has to be the
    narrow one. A plain admin's row still says the branch is disabled; the
    console already renders ``hidden by an admin`` when ``disabled_by`` is
    absent, so the page degrades to the fact without the accusation.

    The three fields are OMITTED rather than nulled. A ``null`` note is a real
    state — a branch hidden without a reason — and sending one to say "you may
    not see this" would put "hidden by an admin, no reason given" on screen for
    a branch that has a reason.

    Used by the two paths that do NOT come straight from ``list_stores`` — the
    fallback, whose rows carry no registry fields, and the echo after a status
    write, whose row carries no stock aggregates. Its job is to make those two
    indistinguishable from a ``list_stores`` row: same keys, same types, every
    field present, so the console never has to tell "absent" from "undefined".

    ``skus``/``units``/``value`` keep the types they have always had (int, int,
    float) so the existing page keeps working while the new one is built.

    Timestamps are left as ``datetime`` rather than stringified here. That is
    not laziness: ``list_stores`` rows are returned raw and serialised by
    FastAPI, and an ``isoformat()`` on this path rendered the SAME field as
    ``…05:46:04.471865+00:00`` from one endpoint and ``…05:46:04.471865Z`` from
    the other. Two spellings of one instant is a parsing bug waiting on the
    console side; let one encoder do it.
    """

    out = {
        "site_code": r["site_code"],
        "site_name": r.get("site_name"),
        "status": r.get("status") or "active",
        "skus": int(r.get("skus") or 0),
        "units": int(r.get("units") or 0),
        "value": float(r.get("value") or 0),
    }
    for f in _STORE_TS_FIELDS:
        out[f] = r.get(f)
    if privileged:
        for f in _STORE_PRIVILEGED_FIELDS:
            out[f] = r.get(f)
    return out


async def _stock_only_stores(scope: Optional[str], *, privileged: bool) -> List[Dict]:
    """The pre-registry answer: a per-site aggregate over `inventory`.

    Kept verbatim (view first, live aggregate second, both scope-filtered)
    because it is the fallback the paragraph at the top of this section
    describes, not dead code.
    """

    scope_sql = (" WHERE " + _site_clause("site_code", "$1")) if scope else ""
    args = [scope] if scope else []
    try:
        rows = await q(
            "SELECT site_code, skus, units, value FROM mv_store_summary"
            + scope_sql
            + " ORDER BY value DESC",
            *args,
        )
    except Exception:  # view missing -> live aggregate
        rows = await q(
            """SELECT site_code, COUNT(*) AS skus, SUM(stock_qty) AS units,
                      ROUND(SUM(price * stock_qty)) AS value
                 FROM inventory"""
            + scope_sql
            + " GROUP BY site_code ORDER BY value DESC",
            *args,
        )
    return [_store_row(dict(r), privileged=privileged) for r in rows]


@router.get("/stores")
async def stores(
    scope: Optional[str] = Depends(caller_store_scope),
    privileged: bool = Depends(caller_is_super_admin),
) -> List[Dict]:
    """Every branch in the registry, with its stock summary. Falls back to live.

    Registry-backed, so a branch with no rows in today's file is still listed —
    with zeroes and a `missing_since` — instead of vanishing. Disabled branches
    are returned too: this is the console's own branch list, and the page that
    can re-enable a branch has to be able to see it.

    Scoped for the same reason as ``inventory`` above: unscoped, this listed
    every branch's SKU count, unit count and stock VALUE to a branch-pinned
    admin. It is an aggregate rather than per-row stock, but a competitor
    branch's inventory value is exactly the kind of thing the store pin exists to
    withhold.

    **Both the registry path and the live fallback are filtered**, or the leak
    would reappear the moment the registry is unavailable — the branch nobody
    tests. That was already true of the two pre-registry paths and stays true of
    both paths here, and in both the matcher is ``tools._site_clause`` itself:
    ``list_stores`` applies it to the coalesced site code, so the scope pin is
    never re-implemented, only handed on.

    Field-projected by role as well as row-filtered by scope, and the two are
    independent questions. ``scope`` decides WHICH branches this caller may see;
    ``privileged`` decides which COLUMNS of them. An unpinned ``user`` account —
    the norm, since pinning is newer than the console roles — has a ``None``
    scope and therefore every branch, so the scope pin is no defence at all here
    and never was; the projection is. See ``_store_row`` for what moves and why
    the line is drawn at super_admin.

    ``list_stores`` rows now go through ``_store_row`` rather than being returned
    as they arrive, so this path and the two below cannot drift on what a branch
    row contains. That costs the raw rows nothing: ``_store_row`` copies the
    timestamps through untouched, exactly so that one encoder still renders them
    and the two spellings of one instant recorded in its docstring stay fixed.

    It aggregates live from
    `inventory` rather than from `mv_store_summary` — deliberate, on agent A's
    side: the MV carries no `site_name` and goes stale — and it has already
    coerced skus/units to int and value to float, which is what keeps the
    existing console page working through the migration.
    """

    rows = await _registry_rows(scope)
    if rows is None:                      # registry raised -> pre-registry answer
        return await _stock_only_stores(scope, privileged=privileged)
    return [_store_row(dict(r), privileged=privileged) for r in rows]


def _site_code_path(site_code: str) -> str:
    """The `{site_code}` path segment, normalised ONCE for every branch route.

    Every route under ``/stores/{site_code}/…`` takes its code through this
    dependency, so all three see the identical string and cannot drift on what
    counts as the same branch.

    They had drifted. A trailing space — `20043-CCSJ%20`, one copy-paste away —
    was accepted by ``/detail`` and ``/status`` and refused by ``/embed``, so the
    panel could open a branch it then could not fetch embed code for, and the
    refusal said "unknown store_id" about a branch visibly on screen. Nothing
    resolved to the WRONG branch (the two answers were "this branch" and "no
    branch", never "a different branch"), so it was the confusing shape rather
    than the dangerous one — but three routes disagreeing about which branch a
    request means is not a state to leave.

    The split was not a missing trim in one place; it was a trim in the wrong
    LAYER. ``stores.detail`` and ``stores.set_status`` each strip their own
    argument, which is right for them — they are public functions with other
    callers — but ``/embed`` reaches the registry through
    ``_embeddable_codes()``, a Python ``set`` of raw codes tested with ``in``.
    No amount of stripping inside `app.stores` can reach a set-membership test in
    this file. It has to be normalised before either path is chosen, which means
    here, at the HTTP boundary.

    So this is deliberately NOT a fourth ``.strip()`` next to the other three. It
    is the only one that runs before the routes diverge; the ones inside
    `app.stores` stay as defence for its non-HTTP callers (the ingest sync) and
    become no-ops for these three.

    Whitespace at the EDGES only. `20043 CCSJ` still 404s everywhere: a space in
    the middle is a different string, not the same code typed untidily.
    """

    return site_code.strip()


class StoreStatusUpdate(BaseModel):
    """Body of ``POST /admin/stores/{site_code}/status``."""

    status: str
    note: Optional[str] = None


@router.post("/stores/{site_code}/status", dependencies=[Depends(require_super_admin)])
async def store_set_status(
    body: StoreStatusUpdate,
    request: Request,
    site_code: str = Depends(_site_code_path),
) -> Dict:
    """Hide a branch from customers, or bring it back. super_admin only.

    ``disabled`` means *pretend this branch does not exist*: it is excluded from
    chat answers, from customer-facing lists and from company-wide totals. It is
    not "temporarily closed" and it is not a stock fact — which is exactly why it
    is a deliberate, audited, super-admin-only act rather than something an
    ingest can infer. A branch missing from the daily file is flagged, never
    disabled.

    Audit is recorded HERE, explicitly, for the same reason
    ``sftp_keys_generate`` does it: ``activity._ROUTES`` has no entry for this
    path, so the ``activity_audit`` middleware records route + status with an
    empty ``detail`` and loses both the branch and the new status — and, because
    an unlisted route captures no ``target``, the site code stays in the action
    slug and every branch becomes its own action. Passing ``target=`` to
    ``action_for`` keeps THIS row low-cardinality
    (``admin.stores.status.create``); the middleware's bare companion row is
    fixed on the other side by adding

        ("POST", re.compile(r"^/admin/stores/(?P<target>[^/]+)/status$"),
         ("keys", ("status", "note"))),

    to ``_ROUTES``. That file belongs to another agent; the entry is reported,
    not written. Nothing in the body is a secret, so the values are safe to keep.
    """

    from app import activity

    try:
        from app import stores as stores_mod
    except Exception:  # noqa: BLE001 — registry module not deployed
        raise HTTPException(status_code=503, detail="store registry unavailable")

    actor_email, actor_role = _actor_identity(request)

    try:
        updated = await stores_mod.set_status(
            site_code, body.status, actor_email=actor_email, note=body.note
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown site_code {site_code!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "invalid status")

    # ⚠️ **The cache is part of this contract.** Answers live in Redis under a key
    # containing `data_version`, and the ONLY thing that invalidates them is a
    # bump. Ingest bumps; a status change is not an ingest. Without this line the
    # filters that hide a disabled branch apply to NEW queries only, while every
    # cached answer keeps naming that branch, its stock and its address until the
    # next file happens to land — up to a full day on a site that loads once a
    # night. "Pretend it does not exist" cannot mean "for anyone who asks a
    # question nobody asked yesterday".
    #
    # Both directions bump, and re-enabling is not the harmless one: the branch is
    # back in the data while the cached answers are the ones written while it was
    # hidden, so a returning branch stays invisible exactly as long.
    #
    # AFTER the write, never before — the same ordering `watcher.py` bumps LAST
    # for. A bump while the row is still the old value lets a request in flight
    # re-file the pre-change answer under the NEW version, where it looks fresh
    # and survives a full TTL that no later bump can evict.
    await cache.bump_data_version()

    path = f"/admin/stores/{site_code}/status"
    await activity.record_event(
        activity.action_for("POST", path, target=site_code),
        actor_email=actor_email,
        actor_role=actor_role,
        target=site_code,
        method="POST",
        path=path,
        status=200,
        detail={"site_code": site_code, "status": updated.get("status"), "note": body.note},
    )

    # `set_status` RETURNS the registry row alone — it does not join stock, so
    # its `skus`/`units`/`value` would be absent and `_store_row` would fill them
    # with zeroes. A console that patched its table from this response would show
    # the branch it just disabled as holding no stock. Re-read through the same
    # function the list uses so the row handed back is the same shape as the one
    # being replaced; the registry row stands in only if the re-read misses.
    #
    # The echo is projected like the list is, and here the flag is a CONSTANT:
    # this route is `require_super_admin`-only (see the decorator), so the only
    # caller that can reach this line is one that may read `disabled_by` and the
    # note it just wrote. Deriving it from the caller again would suggest some
    # other role can get here.
    try:
        for row in await stores_mod.list_stores(updated["site_code"]):
            if row["site_code"] == updated["site_code"]:
                return _store_row(dict(row), privileged=True)
    except Exception:  # noqa: BLE001 — the write succeeded; never fail on the echo
        pass
    return _store_row(dict(updated), privileged=True)


async def _scope_permits(scope: Optional[str], site_code: str) -> bool:
    """Would a caller pinned to ``scope`` be allowed to see ``site_code``?

    ``None`` (the global view) permits everything; anything else is matched with
    ``tools._site_clause`` — **the same predicate**, not a Python lookalike. A
    store pin may be spelled as the full code, its numeric prefix or its alpha
    suffix (`20043-CCSJ` / `20043` / `CCSJ`), and the two scoping bugs recorded in
    CLAUDE.md were both a hand-written matcher disagreeing with that one. The
    clause takes a column name, so the site code is handed to it AS a parameter
    expression; no table is read, because there is no table to read — the
    question is about two strings.

    Every caller turns a ``False`` into the same 404 an unknown code gets. A 403
    would be a working oracle for "this branch exists, you just cannot have it".
    """

    if scope is None:
        return True
    rows = await q(
        "SELECT 1 WHERE " + _site_clause("$1::text", "$2::text"), site_code, scope
    )
    return bool(rows)


@router.get("/stores/{site_code}/detail", dependencies=[Depends(require_super_admin)])
async def store_detail(
    site_code: str = Depends(_site_code_path),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Everything the console knows about one branch, for the detail panel.

    The body is entirely ``stores.detail``'s: stock profile, rank and share of
    the estate, biggest holdings, conversation summary, recent questions, and the
    audit trail for this branch **including refused attempts**. This endpoint
    owns the boundary, not the content.

    **Store scope, twice over, deliberately.** `require_super_admin` already
    settles it — `caller_store_scope` returns ``None`` for a super_admin and a
    plain admin never reaches the handler — so the check below is dead code
    today. It is here because of what this route returns: `/stores` was leaking a
    sibling branch's SKU count, unit count and stock VALUE, and `catalog_one` was
    leaking its per-row stock. This route returns both of those AND the branch's
    customer questions, which the conversations endpoints are scoped for on their
    own. If this dependency is ever relaxed to `require_admin` — the obvious
    "branch managers should see their own branch" change — the scope check is
    what stops that from being the same leak a third time, and it fails to a 404
    so it cannot be used to enumerate branches either.

    An unknown code is a 404. So is a code outside the caller's scope: to a
    scoped caller those are the same fact.
    """

    if not await _scope_permits(scope, site_code):
        raise HTTPException(status_code=404, detail=f"unknown site_code {site_code!r}")

    # Function-local, like every other import of `app.stores` in this file:
    # `app.stores` and `app.tools` import each other, and this module imports
    # `app.tools` at module scope.
    try:
        from app import stores as stores_mod
    except Exception:  # noqa: BLE001 — registry module not deployed
        raise HTTPException(status_code=503, detail="store registry unavailable")

    fn = getattr(stores_mod, "detail", None)
    if fn is None:                      # older registry build without the panel
        raise HTTPException(status_code=503, detail="store registry unavailable")

    # `privileged=True` as a CONSTANT, exactly like the status echo above and for
    # the same reason: `require_super_admin` on the decorator means the only
    # caller who can reach this line may read `disabled_by` and the note. Deriving
    # it from the caller here would suggest another role can arrive.
    #
    # It is passed explicitly rather than left to a default because `stores.detail`
    # deliberately has no default (see its docstring): if the gate above is ever
    # relaxed to `require_admin`, this line is where the question gets answered,
    # and it should be visible at the call site rather than inherited.
    try:
        return await fn(site_code, privileged=True)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown site_code {site_code!r}")


# The panel's other half — the branch's embed snippet — is
# `GET /stores/{site_code}/embed`, and it lives down in the embed section beside
# the minting machinery it delegates to, because that is where its refusal rules
# are written.


def _actor_identity(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """``(email, role)`` off the bearer token, or ``(None, None)``.

    The same four lines the sftp key routes carry inline. ``require_super_admin``
    has already re-read this exact email's row from the ``users`` table and
    proved it is an active, approved super_admin, so the claim is not being
    trusted for authorisation — only for the `disabled_by` stamp and the audit
    row.
    """

    from app import auth as authmod

    try:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            claims = authmod.decode_token(header.split(" ", 1)[1])
            return claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an unreadable token is an anonymous actor
        pass
    return None, None


@router.get("/data-freshness")
async def data_freshness() -> Dict:
    """When the catalog and the stock file last landed. Nothing else.

    The console header carries a "Stock data — 14 min ago" pill on EVERY screen,
    so its source has to cost almost nothing. `/analytics/data-health` already
    reports these two timestamps, but it earns them alongside eight aggregates
    over `inventory` and `catalog`; running that on every navigation to render
    one pill would be the most expensive thing the console does.

    Deliberately NOT store-scoped, and deliberately not a permission boundary
    beyond being under `/admin`: an ingest timestamp says when a FILE arrived,
    not what was in it. A branch manager seeing that the stock file landed
    fourteen minutes ago learns nothing about another branch's stock.

    A missing table, or a table with no rows of that kind, answers `null` — the
    header then draws no pill at all. It must never answer "just now" for "never
    loaded": the whole point of the pill is to say whether the numbers under the
    answers are current.
    """

    out: Dict[str, Optional[str]] = {"catalog_at": None, "inventory_at": None}
    try:
        for row in await q(
            "SELECT kind, max(at) AS at FROM ingest_events "
            "WHERE kind IN ('catalog','inventory') GROUP BY kind"
        ):
            at = row["at"]
            out[f"{row['kind']}_at"] = at.isoformat() if at is not None else None
    except Exception:  # noqa: BLE001 — table not created yet
        pass
    return out


@router.get("/overview")
async def overview(limit: int = 10) -> List[Dict]:
    """Top articles by total stock, from the article-summary materialized view."""

    limit = min(max(limit, 1), 50)
    try:
        rows = await q(
            """SELECT article_code, brand_name, total_stock, weighted_avg_price, site_count
                 FROM mv_article_summary ORDER BY total_stock DESC LIMIT $1""",
            limit,
        )
    except _MISSING_RELATION:
        # The view is built by the first ingest, so a database that has never
        # been loaded genuinely has no top articles. Anything else is a broken
        # query and must reach the caller as a 500 — see `_MISSING_RELATION`.
        return []
    for r in rows:
        # NULL is UNKNOWN, not zero: `or 0` here would report a drug nobody has
        # counted as one nobody has. site_count is a COUNT(), so 0 is a real 0.
        ts, price = r["total_stock"], r["weighted_avg_price"]
        r["total_stock"] = int(ts) if ts is not None else None
        r["weighted_avg_price"] = float(price) if price is not None else None
        r["site_count"] = int(r["site_count"] or 0)
    return rows


@router.get("/views")
async def views_status() -> Dict:
    """Materialized view row counts (admin visibility)."""

    out = {}
    for mv in ("mv_store_summary", "mv_article_summary"):
        try:
            out[mv] = (await q(f"SELECT count(*) AS n FROM {mv}"))[0]["n"]
        except Exception:
            out[mv] = None
    return out


@router.post("/views/refresh")
async def views_refresh() -> Dict:
    """Manually refresh both materialized views."""

    from app.ingest import refresh_views

    await refresh_views()
    return await views_status()


# ---- conversations ---------------------------------------------------------


@router.get("/conversations")
async def conversations(
    limit: int = 50,
    lang: str = "",
    store: str = "",
    offset: int = 0,
    scope: Optional[str] = Depends(caller_store_scope),
) -> List[Dict]:
    """Recent chat logs (question, answer, lang, store, cached, latency), paginated.

    **Store-scoped.** This endpoint hands back customers' questions and the
    agent's answers verbatim — the most sensitive rows in the system — and until
    now it had no scope dependency at all, so a branch-pinned admin could read
    every other branch's conversations. The analytics block below (see rule 1 in
    its header comment) named this endpoint as "exactly the mistake not to copy";
    it is now fixed the same way those endpoints already were.

    ``scope`` and ``store`` are different things and are **ANDed, never
    substituted**. ``scope`` comes from the caller's ``users`` row and is an
    enforced boundary, so it goes through ``tools._site_clause`` (anchored: full
    code / numeric prefix / alpha suffix — never a bare ``=``, never a substring
    ``ILIKE``, both of which have shipped here as cross-branch leaks). ``store``
    is the operator's own filter box and keeps its original exact-match
    behaviour, so a pinned caller who types a sibling's code into it narrows to
    nothing rather than widening past their boundary.

    A row with a NULL ``store_id`` (an unscoped embed session) matches no
    ``_site_clause`` and is therefore invisible to a pinned admin. That is
    correct: it is not their branch's turn, and it might be anybody's.
    """

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    conds, params = [], []
    if scope:
        params.append(scope)
        conds.append(_site_clause("store_id", f"${len(params)}"))
    if lang:
        params.append(lang)
        conds.append(f"lang = ${len(params)}")
    if store:
        params.append(store)
        conds.append(f"store_id = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    try:
        return await q(
            f"""SELECT id, ts, lang, store_id, question, answer, cached, latency_ms
                  FROM chat_logs {where} ORDER BY id DESC
                 LIMIT ${len(params)-1} OFFSET ${len(params)}""",
            *params,
        )
    except _MISSING_RELATION:
        # `chat_logs` is created by the boot DDL, so a console opened during a
        # fresh install has genuinely had no conversations. Every other failure
        # propagates: this is the audit surface, and "no conversations took
        # place" is a materially worse answer than "I could not answer".
        # See `_MISSING_RELATION` — do not widen this back to `Exception`.
        return []


# ---- analytics / audit / diagnostics ---------------------------------------
#
# One page's worth of read-only questions about the turn log: how much traffic,
# how slow, from which embed, repeated how often, and is the DATA underneath it
# healthy. Three rules hold across every endpoint below.
#
# 1. **Every one is store-scoped.** These endpoints hand back customers'
#    questions and the agent's answers verbatim — the most sensitive rows in the
#    system, more so than the stock numbers `catalog_one` / `inventory` /
#    `stores` were already fixed for. A branch-pinned admin must not read another
#    branch's, so each takes `scope=Depends(caller_store_scope)` and ANDs a
#    `_site_clause` predicate. `GET /admin/conversations` (above) used to be the
#    one hole in that rule — no scope dependency at all — and this comment used
#    to name it as the mistake not to copy. It now carries the same dependency
#    and the same `_site_clause` predicate. The rule is now exceptionless: if you
#    add an endpoint over `chat_logs`, it takes `scope`.
#
# 2. **Bound parameters only.** Nothing a caller sends is ever interpolated into
#    SQL; the only f-string content is `$n` placeholder numbers and fixed column
#    names chosen in this file.
#
# 3. **Aggregate in Postgres.** 122 rows today, unbounded later. Percentiles come
#    from `percentile_cont`, counts from `count(*) FILTER`, and nothing pulls the
#    whole table into Python.


def _parse_ts_full(value: str, field: str) -> Tuple[str, bool, bool]:
    """Validate an ISO date/datetime filter.

    Returns ``(value, is_date_only, has_offset)``.

    Parsed here rather than handed to Postgres raw so a typo is a 400 naming the
    parameter, not a 500 out of asyncpg. The value itself is still bound and cast
    in SQL — this is validation, not string building.

    ``has_offset`` is what decides whether the bound is a naked wall-clock time
    (to be read in the request's ``tz``) or an absolute instant. ``2026-08-17``
    and ``2026-08-17T09:00:00`` name a moment only once you say *whose* midnight;
    ``2026-08-17T09:00:00Z`` already names one, and re-interpreting it in the
    caller's zone would move it. See :func:`_ts_expr`.
    """

    v = (value or "").strip()
    if not v:
        return "", False, False
    try:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"`{field}` is not an ISO date or datetime"
        )
    return v, len(v) == 10, parsed.tzinfo is not None


def _parse_ts(value: str, field: str) -> tuple[str, bool]:
    """``(value, is_date_only)`` — the two-value form the older callers use."""

    v, is_day, _ = _parse_ts_full(value, field)
    return v, is_day


# ---- timezone (addendum §A) -------------------------------------------------
#
# The database runs `Etc/UTC` and every chart used to bucket with
# `date_trunc('day', ts)`, while the console labelled those buckets in the
# browser's zone. In Yangon (GMT+6:30) each "day" therefore ran 06:30 -> 06:30
# local and the first six and a half hours of every morning were counted on the
# previous day. Nobody reported it because the chart still looked plausible —
# which is exactly why it survived.
#
# Two halves, and BOTH are required or the fix is worse than the bug:
#
#   1. buckets are cut in the caller's zone (`ts AT TIME ZONE $tz`), and
#   2. a naive date bound is READ in that same zone.
#
# Do only the first and `start=2026-08-17` still selects from UTC midnight while
# the axis is drawn from Yangon midnight — the window and its buckets disagree by
# 6h30 and the first bar is short by exactly that much.

DEFAULT_TZ = "UTC"


def _validate_tz(value: str) -> str:
    """An IANA zone name, or a 400 naming the parameter.

    Checked against the tz database through :class:`zoneinfo.ZoneInfo` rather
    than a hand-written list — a list goes stale, and "Asia/Yangon is not a real
    zone" is a lie a list can tell. **There is deliberately no fallback to UTC**:
    a silently wrong bucket is the defect being fixed here, so an unrecognised
    zone has to be loud.

    ⚠️ This depends on the tz database being COMPLETE, which is a packaging
    concern rather than a logic one. `python:3.12-slim` ships an incomplete
    `/usr/share/zoneinfo`: `Asia/Yangon` resolves and `Asia/Rangoon` — the name
    Chrome actually reports in Yangon — does not, nor does `Asia/Calcutta` or
    any of `US/*`. Those are legitimate tzdata LINKS, so this function rejected
    the zone the user's own browser sent and every panel on the console rendered
    a 400. `tzdata` in requirements.txt is the fix; the alias tests in
    tests/test_console_v2.py fail loudly if it is ever dropped again.

    The value is bound as a query parameter everywhere it is used, so this is a
    usability check rather than the injection defence.
    """

    tz = (value or "").strip() or DEFAULT_TZ
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        raise HTTPException(
            status_code=400,
            detail=f"`tz` is not a known IANA time zone: {tz!r}",
        )
    return tz



def _ts_expr(value: str, params: List, tz: str, *, plus_day: bool = False) -> str:
    """SQL for one bound instant, appending its binds to ``params``.

    * a value carrying an offset (``…Z``, ``…+06:30``) is already an instant and
      is cast straight to ``timestamptz``;
    * a bare date or a naive datetime is wall-clock and is read in ``tz``:
      ``($n::timestamp AT TIME ZONE $tz)``.

    ``plus_day`` implements the §4 bare-date rule — ``end=2026-08-17`` includes
    the whole of the 17th. The day is added in LOCAL space, before the zone
    conversion, so "the whole day" stays 24 wall-clock hours across a DST change
    rather than 23 or 25.
    """

    params.append(value)
    p = f"${len(params)}"
    if _parse_ts_full(value, "ts")[2]:
        base = f"{p}::text::timestamptz"
        return f"({base} + interval '1 day')" if plus_day else base
    naive = f"{p}::text::timestamp" + (" + interval '1 day'" if plus_day else "")
    params.append(tz)
    return f"(({naive}) AT TIME ZONE ${len(params)}::text)"


def _bucket_expr(col: str, unit: str, tz: str, params: List) -> str:
    """``date_trunc(unit, col AT TIME ZONE $tz)`` — the local-midnight bucket.

    ``unit`` is always a literal chosen in this file (a key of ``_BUCKETS_TS`` or
    ``_ROLLUPS``); ``tz`` is bound. The result is a NAIVE local timestamp, which
    is what the axis labels want: `to_char` on it prints the reader's own clock,
    and generate_series over it steps in local days.
    """

    params.append(tz)
    return f"date_trunc('{unit}', {col} AT TIME ZONE ${len(params)}::text)"


def _local_bound_expr(value: str, params: List, tz: str) -> str:
    """An axis bound as a NAIVE LOCAL timestamp, NULL-tolerant.

    The zero-fill axis lives in the same naive-local space as
    :func:`_bucket_expr`'s output, and an empty bound is bound as NULL so the
    query can fall back to the data's own span with COALESCE. That NULL is why
    this cannot just call ``_ts_expr``: the branch has to be chosen without a
    value to inspect, and an absent bound has no offset to honour.
    """

    v = (value or "").strip()
    params.append(v or None)
    p = f"${len(params)}"
    if v and _parse_ts_full(v, "ts")[2]:
        params.append(tz)
        return f"({p}::text::timestamptz AT TIME ZONE ${len(params)}::text)"
    return f"{p}::text::timestamp"


# The reserved value that selects turns with NO recorded value in a nullable
# column. An empty string cannot do it — empty means "no filter" for every other
# box on the page — and the "not recorded" band of each chart has to stay
# clickable like the rest.
#
# It applies to `path`, `embed_id` and `actor_email` alike. `embed` used to be
# the exception, and that was a defect rather than a decision: contract §4
# reserves `none` for every nullable filter, but the `embed` branch below matched
# `embed_id = 'none'` exactly, so `?embed=none` was DECLARED, accepted, and
# returned zero rows over the ~122 turns that really are unattributed. That is
# worse than a dropped parameter — an empty result reads as a measurement, and
# the Embeds tab's "Unattributed" drill-through led to a page saying there are
# none. Reported by the UI agent; the branch now mirrors `path`.
_NONE_TOKEN = "none"
_PATH_NONE = _NONE_TOKEN  # kept: the old name is referenced by the console


def _csv(value: str) -> List[str]:
    """`"a, b ,,c"` -> `["a","b","c"]`. Empty string means "no filter".

    Contract §4 spells `store`, `lang`, `path`, `embed`, `model` and `actor` as
    comma-separated lists. A single value splits to a one-element list and
    produces exactly the predicate the single-valued code produced before, so
    every caller sending one value is unaffected.
    """

    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _or_group(conds: List[str]) -> str:
    return "(" + " OR ".join(conds) + ")"


def _list_clause(col: str, value: str, params: List, *, ci: bool = False) -> str:
    """`col = ANY($n::text[])`, with the `none` sentinel lifted out as IS NULL.

    One bind for the whole list instead of one per token (contract §4). ``ci``
    folds case on both sides, which is what ``lang`` needs — ``EN`` and ``en`` are
    the same language and there is no third thing they could mean.

    ``none`` is handled OUTSIDE the array: an unattributed row has no value to
    compare, so `= ANY(…)` can never reach it however the array is spelled. This
    is the branch whose absence made `?embed=none` return zero rows over the ~122
    turns that are unattributed — an empty result that reads as a measurement.
    """

    tokens = _csv(value)
    literal = [t for t in tokens if t != _NONE_TOKEN]
    ors: List[str] = []
    if len(literal) < len(tokens):
        ors.append(f"{col} IS NULL")
    if literal:
        params.append([t.upper() for t in literal] if ci else literal)
        target = f"upper({col})" if ci else col
        ors.append(f"{target} = ANY(${len(params)}::text[])")
    return _or_group(ors)


def _store_clause(col: str, value: str, params: List) -> str:
    """The `store` filter — ANCHORED per token, never a substring.

    This used to be ``store_id ILIKE '%'||$n||'%'``, which is wrong-data on a
    pharmacy chain: ``store=CMHL-1`` also matched CMHL-10, CMHL-19 and CMHL-100,
    so a branch manager's own filter quietly showed them three other branches'
    turns.

    It goes through ``tools._site_clause`` rather than a bare ``=``, and that is
    a deliberate choice over plain equality: a site token is legitimately written
    as the full code (``20005-CCYK``), its numeric prefix (``20005``) or its alpha
    suffix (``CCYK``) everywhere else in this codebase, including in the
    ``users.store_id`` pin that drives ``caller_store_scope``. A filter that
    accepted only the full code while the scope pin accepted all three would be
    its own trap. ``_site_clause`` is anchored on every one of those forms — it
    matches ``20005-CCYK`` for ``20005`` and matches NOTHING for ``2000`` — so it
    is exact per token without being narrower than the rest of the system.

    **This is a narrowing filter, never the boundary.** The enforced scope is a
    separate predicate built from ``caller_store_scope`` and ANDed with this one;
    see ``_log_filters``.
    """

    ors: List[str] = []
    for tok in _csv(value):
        params.append(tok)
        ors.append(_site_clause(col, f"${len(params)}"))
    return _or_group(ors)


_RATED_VALUES = ("", "up", "down", "any")


def _upper_bound(col: str, value: str, is_day: bool, params: List,
                 tz: str = DEFAULT_TZ) -> str:
    """The end-of-window predicate, per the amended §4 date rule.

    * a **bare date** (``2026-08-17``) includes the WHOLE day → ``< date + 1 day``
    * a **date with a time** is exclusive at that instant → ``< value``

    Both spellings of the window (``end`` and the legacy ``to``) go through here,
    so they cannot drift apart again. The rule exists because every date picker a
    human touches means "through the 17th" when it says 17 Aug, and a bound that
    quietly drops the most recent day is the kind of error nobody reports — the
    chart still looks plausible, it is just missing today.

    ``tz`` decides WHOSE 17th (addendum §A). A date bound read in UTC against
    buckets cut in Asia/Yangon is the same class of error one layer down: the
    window and the axis would disagree by the offset.
    """

    return f"{col}ts < " + _ts_expr(value, params, tz, plus_day=is_day)


class SharedFilters:
    """The contract §4 filters that ``_log_filters``' positional signature predates.

    ``model``, ``actor``, ``cached`` and ``rated`` were in the contract from the
    start and in no endpoint, and ``start``/``end`` are the contract's spelling of
    the window that the older endpoints call ``from``/``to``. The console emits
    all of them on every request, so an endpoint that does not DECLARE them
    answers 200 with unfiltered rows underneath a filter chip that says otherwise
    — §4's whole warning, and the reason this object exists rather than six more
    parameters copied onto nine handlers.

    ``start``/``end`` and ``from``/``to`` now mean exactly the same thing (§4,
    amended 2026-08-17), and ``_log_filters`` rejects a request that sends both
    with conflicting values rather than silently preferring one.
    """

    __slots__ = ("start", "end", "end_is_day", "model", "actor", "cached",
                 "rated", "tz")

    def __init__(self, start="", end="", end_is_day=False, model="", actor="",
                 cached=None, rated="", tz=DEFAULT_TZ):
        self.start = start
        self.end = end
        self.end_is_day = end_is_day
        self.model = model
        self.actor = actor
        self.cached = cached
        self.rated = rated
        self.tz = tz

    def shifted(self, start: str, end: str) -> "SharedFilters":
        """A copy over a different window, everything else identical.

        Used for the previous-period half of a KPI (addendum §B). The two bounds
        are absolute instants (they carry an offset), so ``end_is_day`` is False:
        a previous window is computed, never typed by a human, and the bare-date
        courtesy would push it a day past the start of the current one.
        """

        return SharedFilters(start=start, end=end, end_is_day=False,
                             model=self.model, actor=self.actor,
                             cached=self.cached, rated=self.rated, tz=self.tz)

    def conds(self, params: List, col: str = "") -> List[str]:
        conds: List[str] = []

        if self.start:
            conds.append(f"{col}ts >= " + _ts_expr(self.start, params, self.tz))
        if self.end:
            conds.append(_upper_bound(col, self.end, self.end_is_day, params,
                                      self.tz))

        if self.model:
            conds.append(_list_clause(f"{col}model", self.model, params))
        if self.actor:
            conds.append(_list_clause(f"{col}actor_email", self.actor, params))

        if self.cached is not None:
            # IS TRUE / IS FALSE, not `= $n`: a NULL `cached` is UNKNOWN and
            # belongs to neither side. `COALESCE(cached,false)` would file every
            # unrecorded turn under "not cached" — a guess wearing a number.
            conds.append(f"{col}cached IS TRUE" if self.cached
                         else f"{col}cached IS FALSE")

        if self.rated:
            # Joined on `chat_feedback.turn_id`, the real foreign key (§5 as
            # amended). It is deliberately NOT matched on question+answer text.
            #
            # Text matching was the first implementation and it was wrong twice
            # over. It guessed — two turns with the same words are one rating —
            # and, because `chat_feedback` has its OWN `question`/`answer`
            # columns, the unqualified correlated reference resolved both sides
            # to `cf`: a predicate true for every feedback row, so `?rated=down`
            # returned the ENTIRE table under a chip saying "rated down". The
            # endpoint was filtering, on nothing.
            #
            # Rows written before `turn_id` existed keep NULL and match nothing.
            # That is the honest answer: they are unattributable, and the count
            # they would inflate is one somebody makes a decision on.
            outer = col or "chat_logs."
            verdict = ""
            if self.rated in ("up", "down"):
                params.append(self.rated)
                verdict = f" AND cf.verdict = ${len(params)}"
            conds.append(
                "EXISTS (SELECT 1 FROM chat_feedback cf"
                f" WHERE cf.turn_id = {outer}id{verdict})"
            )

        return conds


async def shared_filters(
    start: str = Query("", description="ISO8601, inclusive (contract §4)"),
    end: str = Query("", description="ISO8601, EXCLUSIVE (contract §4)"),
    model: str = Query("", description="comma-separated; `none` = unattributed"),
    actor: str = Query("", description="comma-separated; `none` = unattributed"),
    cached: Optional[bool] = Query(None),
    rated: str = Query("", description="up|down|any"),
    tz: str = Query(DEFAULT_TZ,
                    description="IANA zone for buckets AND date bounds (§A)"),
) -> SharedFilters:
    """Declare the four §4 filters + the contract time window, once.

    Every `/admin/analytics/*` endpoint depends on this, so supporting a subset
    is not something a handler can do by accident. `actor_email` may not exist on
    an un-migrated database; a filter naming it then raises inside the endpoint's
    own try/except, which is the honest failure — better an empty panel than one
    silently answering as if the filter had been applied.
    """

    if rated not in _RATED_VALUES:
        raise HTTPException(status_code=400, detail="`rated` must be up, down or any")
    tz_v = _validate_tz(tz)
    start_v, _ = _parse_ts(start, "start")
    end_v, end_day = _parse_ts(end, "end")
    return SharedFilters(start_v, end_v, end_day, model, actor, cached, rated,
                         tz_v)


# ---- period deltas (addendum §B) --------------------------------------------


def _resolve_bound(value: str, tz: str, field: str, *, upper: bool) -> datetime:
    """One window bound as an aware datetime, mirroring :func:`_ts_expr` exactly.

    It has to mirror it. This function decides where the PREVIOUS window starts,
    and the SQL decides where the current one does; if they disagree about what
    ``2026-08-17`` means, the two halves of a KPI measure different lengths of
    time and the delta is nonsense in a way no test of either half alone can see.
    """

    v, is_day, has_off = _parse_ts_full(value, field)
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if upper and is_day:
        dt = dt + timedelta(days=1)
    if not has_off:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def _prev_window(start: str, end: str, tz: str) -> Optional[Tuple[str, str]]:
    """The immediately preceding window of the same length, or ``None``.

    ``None`` when the request did not pin BOTH ends: without a length there is no
    "same length again", and inventing one (last 7 days, since the epoch) would
    put a delta on screen that answers a question nobody asked. The UI prints
    "no prior period" for it — never `0%`, which reads as "no change".
    """

    if not start or not end:
        return None
    lo = _resolve_bound(start, tz, "start", upper=False)
    hi = _resolve_bound(end, tz, "end", upper=True)
    span = hi - lo
    if span <= timedelta(0):
        return None
    return ((lo - span).isoformat(), lo.isoformat())


def _kpi(value, prev, prev_period: Optional[Dict]) -> Dict:
    """``{value, prev, delta, delta_pct, prev_period}`` — movement, both ways.

    A percentage never travels alone (§B): a rise from 1 to 4 is **+3**, and
    calling it 300% is noise dressed as a finding.

    Three absences, three different answers, and none of them is `0`:

    * no prior window at all → ``prev``/``delta``/``delta_pct`` all null;
    * a prior window that measured nothing (``prev = 0``) → ``delta`` is the
      whole of ``value`` and ``delta_pct`` stays **null**. Growth from zero has
      no percentage, and 0 there would read as "unchanged";
    * ``value`` itself unmeasured → everything downstream stays null.
    """

    out = {"value": value, "prev": prev, "delta": None, "delta_pct": None,
           "prev_period": prev_period}
    if value is None or prev is None:
        return out
    delta = value - prev
    out["delta"] = round(delta, 6) if isinstance(delta, float) else delta
    if prev:
        out["delta_pct"] = round(delta / prev * 100, 1)
    return out


def _log_filters(
    params: List,
    scope: Optional[str] = None,
    frm: str = "",
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    text: str = "",
    tool: str = "",
    path: str = "",
    col: str = "",
    extra: Optional["SharedFilters"] = None,
    tz: str = DEFAULT_TZ,
) -> List[str]:
    """Build the shared chat_logs predicates, appending binds to ``params``.

    ``col`` is an optional table alias prefix (``"l."``).

    ``extra`` carries the four contract §4 filters this signature predates —
    ``model``, ``actor``, ``cached``, ``rated`` — plus ``start``/``end``, the
    contract's spelling of the time window. It is a separate object rather than
    six more positional parameters so that adding a filter is one edit here and
    one dependency on each endpoint, not a signature change rippling through
    nine call sites. See :class:`SharedFilters`.

    ``tool`` and ``path`` are the chart drill-downs: the operator clicks a tool
    bar or a route segment and the row lists below narrow to the turns behind it.
    They live HERE rather than in one endpoint because the console sends them to
    every row query at once (``params()`` in ``admin/src/routes/analytics``:
    summary, questions, embeds, repeats), and a KPI row that ignores the filter
    the list below it applied is the same lie in a different place. Declaring
    them in one endpoint only is exactly how they went missing to begin with —
    FastAPI drops an undeclared query param without a word, so the endpoint
    answered UNFILTERED while the UI drew a filter chip.

    ``path`` matches exactly, and the reserved token ``"none"`` selects
    ``path IS NULL`` — the "not recorded" segment of the stacked bar, which is a
    real clickable band (pre-audit-column history, see ``/analytics/paths``) and
    otherwise unreachable, since an empty value means "no filter". The two are
    never conflated: ``?path=none`` returns NULL rows and NOT a row whose path is
    the literal string ``'none'``. Nothing writes such a row — ``app/api.py``
    emits only ``agent`` / ``fast_path`` / ``cache`` — and if a route ever were
    named ``none`` it must be renamed rather than made ambiguous here.

    ``tool`` matches turns whose ``tools`` JSONB ARRAY CONTAINS that name, via
    ``@>`` with a bound parameter (``to_jsonb($n::text)``) — never a built
    string, and never a ``LIKE`` over the serialised array, which would match
    ``get_stock`` inside ``get_stock_history``. A turn with ``tools IS NULL``
    matches no tool filter: ``NULL @> …`` is NULL, and the ``jsonb_typeof``
    guard makes that explicit. That is the honest answer — a cache hit ran no
    model, so nobody wrote down which tools it used, and it must not be swept
    into "used get_stock" or into "used nothing".

    ``scope`` and ``store`` are different things and are ANDed, never
    substituted — the same distinction `inventory` documents. ``store`` is the
    operator's own filter box, so a substring ILIKE is right there; ``scope`` is
    an enforced boundary, so it goes through ``_site_clause`` and a pinned caller
    who types a sibling's code into the box narrows to nothing rather than
    crossing the boundary.

    A row with a NULL ``store_id`` (an unscoped embed session) matches no
    ``_site_clause``, so it is invisible to a pinned admin. That is correct: it
    is not their branch's turn, and it might be anybody's.
    """

    conds: List[str] = []

    # -- the ENFORCED boundary, first and unconditionally -----------------------
    #
    # This is NOT the `store` filter and the two are never substituted for one
    # another. `scope` comes from the caller's own users row via
    # `caller_store_scope`; `store` is the operator's filter box. They are ANDed,
    # so a pinned caller who types a sibling's code into the box narrows to
    # nothing rather than crossing the boundary. This endpoint family leaked
    # store scope once already; keep them separate.
    if scope:
        params.append(scope)
        conds.append(_site_clause(f"{col}store_id", f"${len(params)}"))

    # -- the window, in either spelling ---------------------------------------
    #
    # `from`/`to` and `start`/`end` now mean exactly the same thing (§4 amended).
    # Sending both with DIFFERENT values is a caller bug, and it is answered with
    # a 400 rather than by quietly preferring one — a window silently narrower
    # than the one on screen is unfalsifiable from the outside.
    frm_v, _ = _parse_ts(frm, "from")
    to_v, to_day = _parse_ts(to, "to")
    if extra is not None:
        if frm_v and extra.start and frm_v != extra.start:
            raise HTTPException(
                status_code=400,
                detail="`from` and `start` were both sent with different values",
            )
        if to_v and extra.end and to_v != extra.end:
            raise HTTPException(
                status_code=400,
                detail="`to` and `end` were both sent with different values",
            )
        # Identical values: let `extra` own the bound so the rule lives in one
        # place. Blanking them here keeps the predicate from being added twice.
        if frm_v and frm_v == extra.start:
            frm_v = ""
        if to_v and to_v == extra.end:
            to_v = ""

    # The legacy spelling reads its bounds in the SAME zone as `start`/`end` —
    # `extra` owns it when present, since that is where the request's `tz` landed.
    tz_v = extra.tz if extra is not None else tz
    if frm_v:
        conds.append(f"{col}ts >= " + _ts_expr(frm_v, params, tz_v))
    if to_v:
        conds.append(_upper_bound(col, to_v, to_day, params, tz_v))

    if store:
        conds.append(_store_clause(f"{col}store_id", store, params))
    if embed:
        conds.append(_list_clause(f"{col}embed_id", embed, params))
    if lang:
        conds.append(_list_clause(f"{col}lang", lang, params, ci=True))
    if text:
        params.append(text)
        conds.append(
            f"({col}question ILIKE '%'||${len(params)}||'%'"
            f" OR {col}answer ILIKE '%'||${len(params)}||'%')"
        )
    if tool:
        params.append(tool)
        conds.append(
            f"(jsonb_typeof({col}tools) = 'array'"
            f" AND {col}tools @> to_jsonb(${len(params)}::text))"
        )
    if path:
        conds.append(_list_clause(f"{col}path", path, params))

    if extra is not None:
        conds.extend(extra.conds(params, col))
    return conds


def _where(conds: List[str]) -> str:
    return ("WHERE " + " AND ".join(conds)) if conds else ""


def _i(v) -> int:
    return int(v) if v is not None else 0


def _ms(v) -> Optional[int]:
    """percentile_cont returns a float (or NULL when no row had a latency)."""

    return int(round(float(v))) if v is not None else None


def _tools_of(raw) -> List[str]:
    """asyncpg hands JSONB back as text; decode it, never guess at it."""

    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return val if isinstance(val, list) else []


def _json_obj(raw) -> Optional[Dict]:
    """Same decode for a JSONB **object**, preserving NULL.

    ``{}`` and NULL are different answers here: an activity row with no detail
    recorded nothing, while ``{}`` is a detail object that happens to be empty.
    """

    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return val if isinstance(val, dict) else None


# Disjoint latency buckets, by upper bound. `lt100` is effectively the cache-hit
# band (a hit answers in ~3ms); `gte20000` is the tail a pharmacist gives up on.
# A row with a NULL latency_ms lands in no bucket, so the buckets sum to the
# number of TIMED turns, not necessarily to `turns`.
_BUCKETS = (
    ("lt100", "latency_ms < 100"),
    ("lt5000", "latency_ms >= 100 AND latency_ms < 5000"),
    ("lt10000", "latency_ms >= 5000 AND latency_ms < 10000"),
    ("lt20000", "latency_ms >= 10000 AND latency_ms < 20000"),
    ("gte20000", "latency_ms >= 20000"),
)


# The KPIs `/analytics/summary` reports movement for (§B). Named here rather
# than inline so the delta block and the payload cannot disagree about which
# numbers are KPIs, and so a metric added to one is added to both.
_SUMMARY_KPIS = ("turns", "distinct", "repeat_rate", "cache_hits", "cache_rate",
                 "p50_ms", "p95_ms", "refusals", "rated", "up", "down",
                 "corrections", "up_rate", "burmese_share", "tokens", "cost_usd")


async def _summary_scalars(scope, frm, to, store, embed, lang, q_text, tool,
                           path, extra: SharedFilters) -> Dict:
    """Every scalar `/analytics/summary` reports, over ONE window.

    Called twice — once for the requested window, once for the preceding one —
    and the endpoint's own top-level numbers are read from the first call rather
    than computed separately. That is deliberate: two code paths measuring "the
    same" thing is how the two halves of a delta end up defined differently, and
    a delta between two definitions is worse than no delta, because it looks
    like a finding.

    Each of the three queries degrades on its own. A database with no cost
    columns loses `tokens`/`cost_usd` and keeps its traffic numbers; the whole
    point of asking separately is that the blast radius of a missing column is
    the column.
    """

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)

    buckets_sql = ",\n".join(
        f"count(*) FILTER (WHERE {expr}) AS {name}" for name, expr in _BUCKETS
    )
    try:
        head = (
            await q(
                f"""SELECT count(*)                                   AS turns,
                           count(DISTINCT lower(btrim(question)))      AS distinct_q,
                           count(*) FILTER (WHERE cached)              AS cache_hits,
                           count(*) FILTER (WHERE upper(lang) = 'MY')  AS my_turns,
                           count(*) FILTER (WHERE answer IS NULL
                                              OR btrim(answer) = '')   AS refusals,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)  AS p50,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                           {buckets_sql}
                      FROM chat_logs {where}""",
                *params,
            )
        )[0]
    except Exception:  # noqa: BLE001 — table not created yet
        head = {}

    turns = _i(head.get("turns"))
    distinct = _i(head.get("distinct_q"))
    cache_hits = _i(head.get("cache_hits"))

    # ---- the feedback half (chat_feedback): scope + time + store + text only --
    fb_params: List = []
    fb_conds: List[str] = []
    if scope:
        fb_params.append(scope)
        fb_conds.append(_site_clause("store_id", f"${len(fb_params)}"))
    # Either spelling of the window, read in the request's zone. This block used
    # to look at `from`/`to` alone, so a console sending `start`/`end` — which is
    # what the contract's own spelling is — got an UNWINDOWED feedback KPI beside
    # windowed traffic numbers. The block already declares what it cannot honour;
    # the window is not one of those things.
    fb_from, _ = _parse_ts(frm, "from")
    if not fb_from:
        fb_from = extra.start
    fb_to, fb_to_day = _parse_ts(to, "to")
    if not fb_to:
        fb_to, fb_to_day = extra.end, extra.end_is_day
    if fb_from:
        fb_conds.append("ts >= " + _ts_expr(fb_from, fb_params, extra.tz))
    if fb_to:
        fb_conds.append(
            "ts < " + _ts_expr(fb_to, fb_params, extra.tz, plus_day=True)
            if fb_to_day
            else "ts <= " + _ts_expr(fb_to, fb_params, extra.tz)
        )
    if store:
        fb_params.append(store)
        fb_conds.append(f"store_id ILIKE '%'||${len(fb_params)}||'%'")
    if q_text:
        fb_params.append(q_text)
        fb_conds.append(
            f"(question ILIKE '%'||${len(fb_params)}||'%'"
            f" OR answer ILIKE '%'||${len(fb_params)}||'%')"
        )
    # Which of the active filters this block could actually honour, and which it
    # could not (contract §5, amended). `chat_feedback` has no lang, embed, path,
    # actor or cached column, so those chips cannot reach this number — and a KPI
    # sitting under a chip it silently ignores is the same lie as an undeclared
    # parameter, just further from the wire. The payload says so; the UI marks it.
    #
    # This is not a temporary hack pending the `turn_id` join. Any block that
    # cannot obey a filter keeps declaring that, permanently.
    _fb_blind = ("lang", "embed", "path", "actor", "cached", "rated")
    _fb_active = [
        name for name, value in (
            ("lang", lang), ("embed", embed), ("path", path),
            ("actor", extra.actor if extra else ""),
            ("cached", "" if not extra or extra.cached is None else "set"),
            ("rated", extra.rated if extra else ""),
        ) if name in _fb_blind and value
    ]
    feedback = {"up": 0, "down": 0, "corrections": 0, "rated": 0,
                "filters_applied": not _fb_active, "ignored_filters": _fb_active}
    try:
        fb = (
            await q(
                f"""SELECT count(*) FILTER (WHERE verdict='up')   AS up,
                           count(*) FILTER (WHERE verdict='down') AS down,
                           count(*) FILTER (WHERE correction IS NOT NULL
                                              AND correction <> '') AS corrections,
                           count(*) AS rated
                      FROM chat_feedback {_where(fb_conds)}""",
                *fb_params,
            )
        )[0]
        feedback = {
            "up": _i(fb["up"]),
            "down": _i(fb["down"]),
            "corrections": _i(fb["corrections"]),
            "rated": _i(fb["rated"]),
            "filters_applied": not _fb_active,
            "ignored_filters": _fb_active,
        }
    except Exception:  # noqa: BLE001 — table not created yet
        pass

    # ---- turn metrics: additive, and NULL when nothing was captured ----------
    # Asked as its OWN query rather than folded into the head SELECT above. On a
    # database that has not picked up `migrations/0006_turn_metrics.sql` the
    # column names raise, and inside the head query that would take `turns`,
    # `p50_ms` and every bucket down with them — a summary reporting NO TRAFFIC
    # because the cost columns are missing. Here the blast radius is the cost.
    #
    # `tokens` and `cost_usd` are None, never 0, when no matching row carries a
    # figure: 0 would say "these turns were free", which is a claim the log
    # cannot support for a turn logged before capture existed.
    tokens: Optional[Dict] = None
    cost_usd: Optional[float] = None
    try:
        m = (
            await q(
                f"""SELECT sum(input_tokens)  AS inp,
                           sum(output_tokens) AS outp,
                           sum(total_tokens)  AS tot,
                           sum(cost_usd)      AS cost,
                           count(*) FILTER (WHERE input_tokens  IS NOT NULL
                                              OR output_tokens IS NOT NULL
                                              OR total_tokens  IS NOT NULL) AS captured
                      FROM chat_logs {where}""",
                *params,
            )
        )[0]
        if _i(m["captured"]):
            tokens = {
                "input": _int_or_none(m["inp"]),
                "output": _int_or_none(m["outp"]),
                "total": _int_or_none(m["tot"]),
            }
        if m["cost"] is not None:
            cost_usd = float(m["cost"])
    except Exception:  # noqa: BLE001 — pre-0006 database, or no chat_logs at all
        pass

    rated = feedback["rated"]
    return {
        "turns": turns,
        "distinct": distinct,
        # How much of the traffic is the same question again. 0.0 when there is
        # no traffic at all — not 1.0, which a bare 1 - 0/0 guard would produce.
        "repeat_rate": round(1 - (distinct / turns), 4) if turns else 0.0,
        "cache_hits": cache_hits,
        "cache_rate": round(cache_hits / turns, 4) if turns else 0.0,
        "p50_ms": _ms(head.get("p50")),
        "p95_ms": _ms(head.get("p95")),
        "refusals": _i(head.get("refusals")),
        "rated": rated,
        "up": feedback["up"],
        "down": feedback["down"],
        "corrections": feedback["corrections"],
        # null, not 0.0, over an unrated window: "nobody rated anything" and
        # "everybody rated it down" must not render as the same number.
        "up_rate": round(feedback["up"] / rated, 4) if rated else None,
        # Roughly half this product's traffic is Burmese, and a shift in that
        # split changes who the product is failing — an English-only reading of
        # the numbers would report that half as nothing at all.
        #
        # The language is hardcoded because the product is bilingual EN/MY and
        # nothing else is written down; a third language makes this the wrong
        # shape and it should become a per-language block rather than gain a
        # sibling. null (not 0.0) over an empty window: no traffic is not a
        # measured share of zero, per §3 — note `cache_rate` above answers 0.0
        # in the same case, which predates that rule and is left alone rather
        # than changed under an existing caller.
        "burmese_share": round(_i(head.get("my_turns")) / turns, 4) if turns else None,
        # The delta-friendly scalar. The payload keeps the {input,output,total}
        # object; a movement needs one number and this is the one.
        "tokens": (tokens or {}).get("total"),
        "cost_usd": cost_usd,
        # -- not KPIs; carried so the endpoint can build its payload from one call
        "buckets": {name: _i(head.get(name)) for name, _ in _BUCKETS},
        "tokens_obj": tokens,
        "feedback": feedback,
    }


@router.get("/analytics/summary")
async def analytics_summary(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Headline traffic / latency / quality numbers over the filtered turn log.

    ``refusals`` counts turns that produced **no answer text at all**. That is a
    deliberately mechanical definition: the alternative — pattern-matching "sorry
    I could not find" in two languages — would put a number nobody can reproduce
    next to numbers everybody can. An empty answer is the one refusal shape the
    log can prove, and it is now recorded (it used to write no row at all, so the
    audit trail's silences were invisible).

    ``feedback`` comes from ``chat_feedback``, which carries only
    ``session_id``/``store_id``/``model``/``question``/``answer``/``verdict``.
    So ``lang``, ``embed``, ``path``, ``actor``, ``cached`` and ``rated`` cannot
    narrow it — there is no column to narrow on — and it is scoped, time-filtered
    and store-filtered like everything else. Said out loud because a filter chip
    over a KPI that the chip cannot reach is the page lying quietly; the honest
    fix is a ``turn_id`` on ``chat_feedback``, which is a schema change and not
    this endpoint's to make.

    ``deltas`` carries each KPI's movement against the immediately preceding
    window of the same length (§B), absolute AND percentage. It is a separate
    block rather than a reshaping of the existing keys so no current caller
    breaks: ``turns`` is still a bare integer, and ``deltas.turns`` is the object
    beside it.

    **The block is always present, with nulls inside it when there is no prior
    window** — never omitted. Absent and null mean different things to the UI: a
    missing block is "this build does not compute movement" and draws no chip at
    all, while `{"delta": null}` is "there was nothing before this window" and
    prints "no prior period". Omitting it would hide a real answer behind a
    capability question.
    """

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)

    cur = await _summary_scalars(scope, frm, to, store, embed, lang, q_text,
                                 tool, path, extra)

    # ---- the previous window (§B) -------------------------------------------
    #
    # Resolved from whichever spelling the caller used, then passed to the SAME
    # helper with `from`/`to` blanked — sending both a legacy window and a
    # shifted `start`/`end` would trip `_log_filters`' conflict guard, which is a
    # 400 the caller did nothing to deserve.
    eff_start = _parse_ts(frm, "from")[0] or extra.start
    eff_end, _eff_day = _parse_ts(to, "to")
    if not eff_end:
        eff_end = extra.end
    prev_win = _prev_window(eff_start, eff_end, extra.tz)
    prev: Optional[Dict] = None
    if prev_win:
        prev = await _summary_scalars(scope, "", "", store, embed, lang, q_text,
                                      tool, path, extra.shifted(*prev_win))
    prev_period = {"start": prev_win[0], "end": prev_win[1]} if prev_win else None
    deltas = {
        name: _kpi(cur[name], prev[name] if prev else None, prev_period)
        for name in _SUMMARY_KPIS
    }

    turns = cur["turns"]
    distinct = cur["distinct"]
    cache_hits = cur["cache_hits"]
    feedback = cur["feedback"]
    tokens = cur["tokens_obj"]
    cost_usd = cur["cost_usd"]

    by_lang: List[Dict] = []
    by_store: List[Dict] = []
    by_day: List[Dict] = []
    if turns:
        by_lang = [
            {
                "lang": r["lang"],
                "cached": bool(r["cached"]),
                "n": _i(r["n"]),
                "p50_ms": _ms(r["p50"]),
                "p95_ms": _ms(r["p95"]),
            }
            for r in await q(
                f"""SELECT COALESCE(lang,'?') AS lang, COALESCE(cached,false) AS cached,
                           count(*) AS n,
                           percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
                      FROM chat_logs {where}
                     GROUP BY 1,2 ORDER BY n DESC""",
                *params,
            )
        ]
        by_store = [
            {"store_id": r["store_id"], "n": _i(r["n"])}
            for r in await q(
                f"""SELECT store_id, count(*) AS n FROM chat_logs {where}
                     GROUP BY 1 ORDER BY n DESC""",
                *params,
            )
        ]
        # A day is the caller's day (§A). Its own copy of the bind list because
        # `_bucket_expr` BINDS the zone, and the sibling queries above share
        # `params` — appending to it would hand them an argument they never
        # reference, which asyncpg rejects outright.
        day_params = list(params)
        day_bucket = _bucket_expr("ts", "day", extra.tz, day_params)
        by_day = [
            {"day": r["day"], "n": _i(r["n"]), "p50_ms": _ms(r["p50"])}
            for r in await q(
                f"""SELECT to_char({day_bucket}, 'YYYY-MM-DD') AS day, count(*) AS n,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50
                      FROM chat_logs {where}
                     GROUP BY 1 ORDER BY 1""",
                *day_params,
            )
        ]

    return {
        "turns": turns,
        "distinct": distinct,
        "repeat_rate": cur["repeat_rate"],
        "cache_hits": cache_hits,
        "cache_rate": cur["cache_rate"],
        "p50_ms": cur["p50_ms"],
        "p95_ms": cur["p95_ms"],
        "refusals": cur["refusals"],
        "buckets": cur["buckets"],
        "by_lang": by_lang,
        "by_store": by_store,
        "by_day": by_day,
        "feedback": feedback,
        # Additive: existing keys keep their shape. `null` here means "not
        # captured" and the UI veils the panel; it must never be rendered as 0.
        "tokens": tokens,
        "cost_usd": cost_usd,
        # Movement for every KPI (§B). Always present; nulls inside it when
        # there is no prior window. See the docstring for why absent and null
        # must not be conflated.
        "deltas": deltas,
        "prev_period": prev_period,
        # The zone the buckets in `by_day` were actually cut at (§F1). Echoed
        # rather than assumed: an endpoint that forgot to declare `tz` would
        # answer 200 with UTC buckets under a header chip saying otherwise,
        # which is the original bug wearing a nicer hat.
        "tz": extra.tz,
        "store_scope": scope,
    }


# The list view's row shape. `session_id` is deliberately NOT here: a list of
# turns does not need the conversation key, and it is the one field on the row
# that is opaque to a human reading the page. The single-turn view below adds it,
# because that is where somebody is trying to reconstruct one conversation.
_LOG_COLS = (
    "id, ts, question, answer, lang, store_id, embed_id, model, tools, "
    "cached, path, latency_ms"
)
_LOG_COLS_ONE = _LOG_COLS + ", session_id"


def _log_row(r: Dict) -> Dict:
    out = dict(r)
    out["tools"] = _tools_of(out.get("tools"))
    out["latency_ms"] = _int_or_none(out.get("latency_ms"))
    return out


@router.get("/analytics/questions")
async def analytics_questions(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    limit: int = 50,
    offset: int = 0,
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """One page of turns plus the TOTAL the filter matches.

    ``total`` is the count before ``limit``/``offset``, which is what makes the
    pager honest: ``GET /admin/conversations`` returns a bare list, so a UI over
    it cannot tell "page 3 of 40" from "that's everything" and has to guess by
    asking for one more row than it shows.

    ``tool`` / ``path`` are the chart drill-downs (``_log_filters``). This
    endpoint is where they were MISSING: the console has always sent them and
    drawn a removable filter chip, and FastAPI dropped them silently, so the list
    came back whole under a chip claiming it was narrowed. ``total`` is counted
    with the same predicates as the page, so a drill-down that matches nothing
    reads as 0 rather than as page 1 of everything.
    """

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)
    try:
        total = _i((await q(f"SELECT count(*) AS n FROM chat_logs {where}", *params))[0]["n"])
        page = list(params)
        page.append(limit)
        page.append(offset)
        rows = await q(
            f"""SELECT {_LOG_COLS} FROM chat_logs {where}
                 ORDER BY id DESC LIMIT ${len(page)-1} OFFSET ${len(page)}""",
            *page,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — table not created yet
        return {"total": 0, "rows": []}
    return {"total": total, "rows": [_log_row(r) for r in rows]}


@router.get("/analytics/question/{log_id}")
async def analytics_question(
    log_id: int, scope: Optional[str] = Depends(caller_store_scope)
) -> Dict:
    """One full turn by id, or 404.

    The scope is part of the WHERE, not a filter applied after the fetch: a
    pinned admin who guesses a sibling branch's row id gets a 404, the same
    answer they would get for an id that does not exist. Fetching then hiding
    would leak the row's existence through the status code.

    **One of two analytics endpoints that deliberately take no §4 filters**, and
    the exemption is stated rather than left as an oversight: this row is
    addressed by primary key. A window or a lang filter could only ever turn a
    turn the caller already has the id of into a 404, which is not a filter, it
    is a trap. (The other is ``/analytics/data-health``.)
    """

    try:
        rows = await q(
            f"""SELECT {_LOG_COLS_ONE} FROM chat_logs
                 WHERE id = $1
                   AND ($2::text IS NULL OR """
            + _site_clause("store_id", "$2")
            + ")",
            log_id,
            scope,
        )
    except Exception:  # noqa: BLE001 — table not created yet
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="turn not found")
    return _log_row(rows[0])


@router.get("/analytics/embeds")
async def analytics_embeds(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> List[Dict]:
    """Traffic rolled up per (embed credential, branch).

    **A NULL ``embed_id`` is returned as NULL.** Those are turns logged before
    the audit columns existed; the honest label is "unattributed" and the UI
    renders it. Folding them into a real embed, or into a string like
    ``"unknown"`` that sorts among real ids, would make the one column this
    endpoint exists for untrustworthy.

    ``rated`` is best-effort attribution: ``chat_feedback`` carries no
    ``embed_id``, so a rating is matched to a group by (branch, question text).
    A rating on a question two embeds both asked from the same branch counts for
    both — it is a signal, not an audited figure.
    """

    params: List = []
    conds = _log_filters(
        params, scope, frm, to, store, embed, lang, q_text, tool, path,
        col="l.", extra=extra,
    )
    where = _where(conds)
    sql = f"""
        SELECT l.embed_id, l.store_id,
               count(*) AS turns,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms) AS p50,
               avg(CASE WHEN l.cached THEN 1.0 ELSE 0.0 END) AS cache_rate,
               count(*) FILTER (WHERE f.qq IS NOT NULL) AS rated,
               max(l.ts) AS last_seen
          FROM chat_logs l
          LEFT JOIN (SELECT DISTINCT store_id, lower(btrim(question)) AS qq
                       FROM chat_feedback) f
            ON f.qq = lower(btrim(l.question))
           AND f.store_id IS NOT DISTINCT FROM l.store_id
        {where}
         GROUP BY 1,2 ORDER BY turns DESC"""
    try:
        rows = await q(sql, *params)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — chat_feedback (or chat_logs) absent
        try:
            rows = await q(
                f"""SELECT l.embed_id, l.store_id, count(*) AS turns,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms) AS p50,
                           avg(CASE WHEN l.cached THEN 1.0 ELSE 0.0 END) AS cache_rate,
                           0 AS rated, max(l.ts) AS last_seen
                      FROM chat_logs l {where}
                     GROUP BY 1,2 ORDER BY turns DESC""",
                *params,
            )
        except Exception:  # noqa: BLE001
            return []
    return [
        {
            "embed_id": r["embed_id"],       # NULL stays NULL — unattributed
            "store_id": r["store_id"],
            "turns": _i(r["turns"]),
            "p50_ms": _ms(r["p50"]),
            "cache_rate": round(float(r["cache_rate"]), 4) if r["cache_rate"] is not None else 0.0,
            "rated": _i(r["rated"]),
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]


@router.get("/analytics/repeats")
async def analytics_repeats(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    limit: int = 50,
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> List[Dict]:
    """Questions asked more than once, grouped on ``lower(trim(question))``.

    This is the list that tells an operator what to put in a FAQ, what to teach
    the alias table, and which answers are worth being sure about — a question
    asked 40 times is 40 chances to be wrong once.
    """

    limit = min(max(limit, 1), 200)
    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)
    params.append(limit)
    try:
        rows = await q(
            f"""SELECT lower(btrim(question)) AS question,
                       count(*) AS asked,
                       count(*) FILTER (WHERE cached) AS cached,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS median
                  FROM chat_logs {where}
                 GROUP BY 1 HAVING count(*) > 1
                 ORDER BY asked DESC, question ASC LIMIT ${len(params)}""",
            *params,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — table not created yet
        return []
    return [
        {
            "question": r["question"],
            "asked": _i(r["asked"]),
            "cached": _i(r["cached"]),
            "median_ms": _ms(r["median"]),
        }
        for r in rows
    ]


# ---- the ingest funnel (§F2) ------------------------------------------------
#
# `ingest_events` records eleven step names. Only five of them describe a FUNNEL
# — a stage that can narrow. The rest are either the same population under
# another name or a reason for leaving, and putting them on a funnel chart would
# be padding:
#
#   arrived    -> seen in the drop folder                      the entry population
#   detected   -> the filename told us what it is              NARROWS
#   checked    -> validation passed                            NARROWS
#   loaded     -> rows replaced                                NARROWS
#   set_aside  -> moved to failed/                             a terminal OUTCOME
#
# `indexed`, `cache_cleared` and `stored` follow `loaded` unconditionally, and
# measured live they are the same number as it (281 each). Charting them would
# draw four identical bars and imply three stages where nothing can be lost.
# `waiting` is a poll, not a stage. `unrecognised` and `rejected` are the two
# places files LEAVE, so they are reported beside the funnel as drops rather
# than inside it — a funnel whose stages do not narrow monotonically cannot be
# read, and adding a drop to it as if it were a stage is how that happens.
#
# `set_aside` is in the funnel because the contract asks for it there, but it is
# NOT a narrowing stage: it is where the files that failed ended up, so it will
# routinely exceed `loaded`. `funnel_meta.terminal` names it as such, and a UI
# drawing it as the last bar of a descending funnel would be drawing a lie.
_FUNNEL_STAGES = ("arrived", "detected", "checked", "loaded", "set_aside")
_FUNNEL_DROPS = ("unrecognised", "rejected")


async def _ingest_funnel() -> Tuple[Optional[Dict], Dict]:
    """``({stage: n}, meta)`` over `ingest_events`, or ``(None, meta)``.

    Counted as **distinct `run_id`** — one attempt at one file — not as rows and
    not as distinct filenames. Rows would count a step recorded twice inside one
    run as two files. Filenames would collapse a file that failed on Monday and
    loaded on Tuesday into one success, which erases exactly the retry the
    operator came to the page to find. An attempt is the unit that survives both.
    `files` is reported alongside for the reader who wants the other reading.

    `None` when the table cannot be read, so the UI can say "we cannot see this"
    instead of drawing a funnel of zeroes — which would read as "no file has
    ever arrived", the most alarming wrong answer this panel can give.
    """

    wanted = _FUNNEL_STAGES + _FUNNEL_DROPS
    meta: Dict = {"unit": "run", "terminal": ["set_aside"],
                  "stages": list(_FUNNEL_STAGES), "files": None, "drops": None}
    try:
        rows = await q(
            """SELECT step, count(DISTINCT run_id) AS runs,
                      count(DISTINCT file) AS files
                 FROM ingest_events
                WHERE step = ANY($1::text[])
                GROUP BY 1""",
            list(wanted),
        )
    except Exception:  # noqa: BLE001 — table not created yet
        return None, meta

    runs = {r["step"]: _i(r["runs"]) for r in rows}
    files = {r["step"]: _i(r["files"]) for r in rows}
    # A stage with no rows is a measured 0, not an absence: the table was read
    # and nothing reached that stage. That is the whole finding on this panel —
    # 573 arrived against 286 detected says half of everything arriving is not
    # recognised, and it only says it if the missing half is a number.
    meta["files"] = {s: files.get(s, 0) for s in _FUNNEL_STAGES}
    meta["drops"] = {d: runs.get(d, 0) for d in _FUNNEL_DROPS}
    return {s: runs.get(s, 0) for s in _FUNNEL_STAGES}, meta


@router.get("/analytics/data-health")
async def analytics_data_health(
    tz: str = Query(DEFAULT_TZ, description="IANA zone for the `by_day` buckets"),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Is the data under the answers any good, and how old is it.

    The catalog half reuses ``ingest.catalog_health()`` — the stub ratio that is
    the single best predictor of "the agent says not-found for things on the
    shelf" (2026-07-28 field reports). It is deliberately NOT store-scoped:
    ``catalog`` has no site column, it is one shared product list, and pretending
    otherwise would report a per-branch number that does not exist.

    **The second endpoint that deliberately takes no §4 filters.** It reads
    ``catalog`` and ``inventory``, not ``chat_logs``: there is no turn here to
    filter by lang, embed or model, and a time window over "how many rows are
    stubs right now" would answer a question nobody asked. It reports current
    state, not history — declaring `start`/`end` here would put a date range on
    the page that changes nothing, which is the same lie as a dropped parameter
    wearing the opposite face.

    The inventory half IS scoped, because it counts rows per site. ``under_20``
    means 1–19 (the "low" band used by ``GET /admin/inventory``); it excludes 0
    and it excludes NULL, because NULL is UNKNOWN and never "nearly out".

    Freshness is the newest ``ingest_events`` row per ``kind``, i.e. when a file
    of that kind last moved through the pipeline.

    It takes exactly one §4-adjacent parameter, ``tz``, and that is not a
    contradiction of the paragraph above: a zone does not change WHICH rows are
    returned, it changes what the `by_day` labels MEAN. §F1's rule is that any
    parameter which changes the meaning of the numbers gets declared and echoed;
    a window, which would change which rows come back, still does not belong here.

    ``funnel`` (§F2) is the one thing on this page that speaks about files rather
    than rows. See :func:`_ingest_funnel` for what each stage counts and why the
    stage list is shorter than the set of steps the pipeline records.
    """

    from app.ingest import catalog_health

    tz_v = _validate_tz(tz)

    try:
        cat = await catalog_health()
        catalog = {
            "total": _i(cat.get("total")),
            "stubs": _i(cat.get("stubs")),
            "stub_ratio": cat.get("stub_ratio", 0.0),
        }
    except Exception:  # noqa: BLE001
        catalog = {"total": 0, "stubs": 0, "stub_ratio": 0.0}

    inv_where, inv_args = "", []
    if scope:
        inv_where = " WHERE " + _site_clause("site_code", "$1")
        inv_args = [scope]
    try:
        r = (
            await q(
                """SELECT count(*) AS rows,
                          count(DISTINCT site_code) AS sites,
                          count(*) FILTER (WHERE stock_qty = 0)                    AS zero,
                          count(*) FILTER (WHERE stock_qty < 0)                    AS negative,
                          count(*) FILTER (WHERE stock_qty BETWEEN 1 AND 19)       AS under_20,
                          count(*) FILTER (WHERE stock_qty IS NULL)                AS null_qty
                     FROM inventory"""
                + inv_where,
                *inv_args,
            )
        )[0]
        inventory = {k: _i(r[k]) for k in ("rows", "sites", "zero", "negative", "under_20", "null_qty")}
    except Exception:  # noqa: BLE001
        inventory = {k: 0 for k in ("rows", "sites", "zero", "negative", "under_20", "null_qty")}

    freshness = {"catalog_at": None, "inventory_at": None}
    try:
        for row in await q(
            "SELECT kind, max(at) AS at FROM ingest_events WHERE kind IS NOT NULL GROUP BY kind"
        ):
            if row["kind"] in ("catalog", "inventory"):
                freshness[f"{row['kind']}_at"] = row["at"]
    except Exception:  # noqa: BLE001 — table not created yet
        pass

    # ---- ingest over time ----------------------------------------------------
    #
    # The rest of this endpoint reports CURRENT state; this is the one historical
    # series on it, and it comes from `ingest_events` rather than from the data:
    # a table tells you how many rows it holds now, never how many arrived on
    # Tuesday or how many files were turned away.
    #
    # `rows` sums the row count each `loaded` step recorded; `rejected` counts
    # `rejected` steps — validation refusals, which are the thing an operator is
    # actually hunting when a branch says "we sent it and nothing changed".
    #
    # `by_day` is `null`, not `[]`, when the table cannot be read. An empty list
    # says "no ingests happened"; null says "we cannot see". Those are different
    # answers and only one of them should make somebody go and look.
    #
    # NOT store-scoped, for the same reason the catalog half is not: an ingest is
    # a file arriving for the whole estate, and `ingest_events` has no branch
    # column to scope by. Inventing a per-branch number here would be a figure
    # that does not exist.
    by_day: Optional[List[Dict]] = None
    try:
        day_params: List = []
        day_bucket = _bucket_expr("at", "day", tz_v, day_params)
        by_day = [
            {
                "day": r["day"],
                "rows": _int_or_none(r["rows"]),
                "rejected": _i(r["rejected"]),
                "files": _i(r["files"]),
            }
            for r in await q(
                f"""SELECT to_char({day_bucket}, 'YYYY-MM-DD') AS day,
                          sum((data->>'rows')::bigint)
                              FILTER (WHERE step = 'loaded')      AS rows,
                          count(*) FILTER (WHERE step = 'rejected') AS rejected,
                          count(DISTINCT file)                      AS files
                     FROM ingest_events
                    GROUP BY 1 ORDER BY 1""",
                *day_params,
            )
        ]
    except Exception:  # noqa: BLE001 — table absent, or a non-numeric `rows`
        by_day = None

    funnel, funnel_meta = await _ingest_funnel()
    return {"catalog": catalog, "inventory": inventory, "freshness": freshness,
            "by_day": by_day, "funnel": funnel, "funnel_meta": funnel_meta,
            "tz": tz_v}

# ---- governance: the proposal, and which of its claims are true here --------
#
# The redesign's Governance screen describes a process: an intake-to-operate
# flow with an owner and an SLA per step, a decision log, and four support
# tiers. None of it is established anywhere in this system — no document in
# this repository names a working group, an exec sponsor, an on-call rota or a
# response time, and nothing enforces one.
#
# Printing that flow as though it were operating would be the worst thing this
# console could do: it would tell a reader that somebody is on call.
#
# So the page renders the proposal AS a proposal, and this endpoint answers the
# subset of its claims that are checkable HERE, from the running system. A
# claim the process makes and the system contradicts is the useful output.

#: Claims the governance proposal rests on, each answered by a measurement.
#: `state` is one of:
#:   in_place  — checked just now, and true
#:   absent    — checked just now, and there is nothing
#:   unknown   — could not be established from here
_GOV_CLAIMS = ("read_only", "one_vendor", "blank_is_not_zero", "accuracy_graded", "alerting")


@router.get("/governance")
async def governance() -> Dict:
    """Which of the governance proposal's claims this deployment can support.

    Every entry carries `how` — what was actually done to answer it — because
    "no alerting" established by reading the code and "no alerting" assumed
    from silence are different claims, and only one of them is worth acting on.
    """

    out = []

    # 1. The assistant is read-only. Checked against the tool list it is given,
    #    not against a promise in a prompt: a prompt can be argued with.
    try:
        import inspect

        from app.agent import TOOLS

        # Read what each tool DOES, not what it is called. A name-based check
        # passes anything a writer is named — `reorder_stock` writes and starts
        # with no write verb — and a check with a blind spot it does not
        # disclose is worse than no check, because it is quoted as one.
        write_sql = re.compile(
            r"\b(insert\s+into|update\s+\w+\s+set|delete\s+from|truncate|"
            r"alter\s+table|drop\s+table|create\s+table)\b",
            re.I,
        )
        names, writes, unread = [], [], []
        for t in TOOLS:
            name = getattr(t, "__name__", str(t))
            names.append(name)
            try:
                if write_sql.search(inspect.getsource(t)):
                    writes.append(name)
            except (OSError, TypeError):
                # No source to read. Not a pass — say so.
                unread.append(name)
        out.append({
            "id": "read_only",
            "claim": "The assistant will never write inventory",
            "state": "absent" if writes else "unknown" if unread else "in_place",
            "how": "probed",
            "detail": (
                f"{len(names)} tools. Each one's own code was read just now and none "
                f"of them writes."
                if not writes and not unread
                else f"{len(writes)} of {len(names)} tools write: {', '.join(writes)}"
                if writes
                else f"{len(unread)} of {len(names)} tools could not be read: {', '.join(unread)}"
            ),
        })
    except Exception:  # noqa: BLE001
        out.append({
            "id": "read_only", "claim": "The assistant will never write inventory",
            "state": "unknown", "how": "not_checked",
            "detail": "the agent's tool list could not be read from here",
        })

    # 2. One model vendor. Read from the calls that were actually made — a
    #    second vendor appearing here is the decision being broken in practice,
    #    whatever anybody agreed.
    try:
        rows = await q(
            "SELECT split_part(model, '/', 1) AS vendor, count(*) AS n "
            "FROM llm_calls WHERE model IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        )
        vendors = [(r["vendor"], _i(r["n"])) for r in rows]
        out.append({
            "id": "one_vendor",
            "claim": "One model vendor, reached through one gateway",
            "state": "in_place" if len(vendors) <= 1 else "absent",
            "how": "observed",
            "detail": (
                "no model call has been recorded yet, so there is nothing to check"
                if not vendors
                else " · ".join(f"{v} ({n:,} calls)" for v, n in vendors)
            ),
        })
    except Exception:  # noqa: BLE001
        out.append({
            "id": "one_vendor", "claim": "One model vendor, reached through one gateway",
            "state": "unknown", "how": "not_checked",
            "detail": "the call log could not be read",
        })

    # 3. Blank is not zero. The decision that came out of the 2026-08-03 field
    #    reports. Counted, because the code being right and the data carrying
    #    the distinction are two different things.
    try:
        r = (await q(
            "SELECT count(*) FILTER (WHERE stock_qty IS NULL) AS unknown_n, "
            "       count(*) FILTER (WHERE stock_qty = 0) AS zero_n, "
            "       count(*) FILTER (WHERE stock_qty < 0) AS neg_n, "
            "       count(*) AS total FROM inventory"
        ))[0]
        u, z, neg, tot = _i(r["unknown_n"]), _i(r["zero_n"]), _i(r["neg_n"]), _i(r["total"])
        out.append({
            "id": "blank_is_not_zero",
            "claim": "A blank stock count means “not recorded”, never zero",
            # The column is nullable and the readers distinguish the two. A file
            # with no blanks in it does not disprove the rule, and reporting
            # that as a failure would be reading the DATA as the DECISION.
            "state": "in_place",
            "how": "observed",
            "detail": (
                f"{tot:,} rows: {u:,} not recorded, {z:,} a measured zero, "
                f"{neg:,} negative and shown as sent"
            ),
        })
    except Exception:  # noqa: BLE001
        out.append({
            "id": "blank_is_not_zero", "claim": "A blank stock count means “not recorded”, never zero",
            "state": "unknown", "how": "not_checked", "detail": "inventory could not be read",
        })

    # 4. Accuracy graded. There is an eval set in the repository and no result
    #    from it is stored anywhere, so nothing on this console says whether an
    #    answer was TRUE. The observability board says the same thing.
    out.append({
        "id": "accuracy_graded",
        "claim": "Accuracy is graded before the next rollout wave",
        "state": "absent",
        "how": "observed",
        "detail": (
            "no eval result is stored on this deployment. Every number on this "
            "console describes how fast and how cheaply the assistant answered; "
            "none says whether it was right"
        ),
    })

    # 5. Alerting. Step 5 of the proposal says "Runbook, alerts, monthly
    #    review", and the fourth support tier says nobody is on call. Only one
    #    of those can be true, and it is the second one.
    out.append({
        "id": "alerting",
        "claim": "Someone is told when it breaks",
        "state": "absent",
        "how": "observed",
        "detail": (
            "nothing pages, emails or posts. A failure is visible only to "
            "somebody who opens this console and looks"
        ),
    })

    return {
        "checks": out,
        "counts": {
            st: sum(1 for c in out if c["state"] == st)
            for st in ("in_place", "absent", "unknown")
        },
    }


# ---- architecture: what this is made of, and what each part is doing --------
#
# Three ways a board like this lies, all of them avoided here rather than
# documented afterwards:
#
# 1. **A green tick for something never checked.** Every part below carries how
#    it was established — `probed` (we asked it just now and timed the answer),
#    `observed` (we read a record it left behind) or `not_checked`. A part whose
#    state is `unknown` says so and stays grey; it never borrows the colour of
#    the parts around it.
#
# 2. **Probing something that costs money.** The model provider is NOT called.
#    A health check that spends a fraction of a cent per page view, and adds a
#    five-second leg to it, is a worse thing than not knowing. It is reported
#    from `llm_calls` — what the last real question actually cost and how long
#    it took — which is evidence about the same dependency and free.
#
# 3. **Claiming to see a process we cannot see.** The ingest worker is a
#    separate container. Nothing in this process can tell whether it is alive;
#    all we have is the trail it leaves in `ingest_events`. The row says that in
#    the words "last seen", never "healthy".

_ARCH_STALE_INGEST_H = 26    # a daily stock drop that has not run in a day + a bit
_ARCH_SLOW_STORE_MS = 250    # a datastore answering `SELECT 1` this slowly is not well


async def _arch_probe_postgres() -> Dict:
    """Time a trivial round trip, then report what is in there."""

    import time as _t

    t0 = _t.perf_counter()
    try:
        await q("SELECT 1 AS ok")
        ms = round((_t.perf_counter() - t0) * 1000, 1)
    except Exception as exc:  # noqa: BLE001
        return {"state": "down", "how": "probed", "metric": None,
                "detail": f"The query did not complete: {type(exc).__name__}."}

    counts = {}
    for key, sql in (
        ("catalog", "SELECT count(*) AS n FROM catalog"),
        ("inventory", "SELECT count(*) AS n FROM inventory"),
        ("sites", "SELECT count(DISTINCT site_code) AS n FROM inventory"),
    ):
        try:
            counts[key] = _i((await q(sql))[0]["n"])
        except Exception:  # noqa: BLE001
            counts[key] = None

    parts = []
    if counts.get("catalog") is not None:
        parts.append(f"{counts['catalog']:,} products")
    if counts.get("inventory") is not None:
        parts.append(f"{counts['inventory']:,} stock rows")
    if counts.get("sites") is not None:
        parts.append(f"{counts['sites']:,} branches")
    return {
        "state": "ok" if ms < _ARCH_SLOW_STORE_MS else "watch",
        "how": "probed",
        "metric": f"{ms} ms",
        "detail": " · ".join(parts) if parts else "Answering, but its tables could not be counted.",
    }


async def _arch_probe_redis() -> Dict:
    """PING, and read the data version the cache keys everything by."""

    import time as _t

    from app import cache

    t0 = _t.perf_counter()
    try:
        await cache.get_client().ping()
        ms = round((_t.perf_counter() - t0) * 1000, 1)
    except Exception as exc:  # noqa: BLE001
        return {"state": "down", "how": "probed", "metric": None,
                "detail": f"No reply to PING: {type(exc).__name__}. Answers are not being cached."}

    try:
        version = await cache.get_data_version()
    except Exception:  # noqa: BLE001
        version = None
    return {
        "state": "ok" if ms < _ARCH_SLOW_STORE_MS else "watch",
        "how": "probed",
        "metric": f"{ms} ms",
        "detail": (f"Data version {version} — every cached answer is filed under it, and a stock "
                   f"file landing bumps it." if version is not None
                   else "Answering, but the data version could not be read."),
    }


def _arch_probe_drop() -> Dict:
    """Look in the drop folder itself. It is a mounted volume, not a service."""

    import os
    from pathlib import Path as _P

    base = _P(get_settings().incoming_dir)
    if not base.is_dir():
        return {"state": "down", "how": "probed", "metric": None,
                "detail": f"{base} is not a directory in this container, so nothing can arrive."}
    try:
        files = [p for p in list(base.glob("*.xlsx")) + list(base.glob("*.csv")) if p.is_file()]
        archive = base / "archive"
        done = len(list(archive.glob("*"))) if archive.is_dir() else None
    except OSError as exc:
        # The folder is there and we could not read it, so whether anything is
        # waiting in it is UNKNOWN. "watch" would be a health level asserted
        # from a check that did not complete.
        return {"state": "unknown", "how": "probed", "metric": None, "at": None,
                "detail": f"The folder is there but could not be read: {exc.strerror}. "
                          f"Whether files are waiting cannot be established."}

    newest = max((p.stat().st_mtime for p in files), default=None)
    return {
        # Files sitting in the drop are not an error — the worker polls every
        # 15s, so a file here is either seconds old or is not being picked up.
        # This cannot tell those apart, and says the count rather than a verdict.
        "state": "ok",
        "how": "probed",
        "metric": f"{len(files)} waiting",
        "detail": (f"{len(files)} file(s) waiting to be picked up"
                   + (f", newest {int((_time_now() - newest) / 60)} min old" if newest else "")
                   + (f" · {done} archived" if done is not None else "")),
        "at": None,
    }


def _time_now() -> float:
    import time as _t

    return _t.time()


async def _arch_observe_ingest() -> Dict:
    """The worker is another container. All we have is what it wrote down."""

    try:
        rows = await q(
            "SELECT max(at) AS at, count(*) AS n FROM ingest_events "
            "WHERE at > now() - interval '7 days'"
        )
        at, n = rows[0]["at"], _i(rows[0]["n"])
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "how": "not_checked", "metric": None, "at": None,
                "detail": "The pipeline's own log could not be read, so nothing here is known "
                          "about the worker — not that it is down."}

    if at is None:
        return {"state": "unknown", "how": "observed", "metric": None, "at": None,
                "detail": "Nothing in the last seven days. That is what an idle worker and a "
                          "stopped worker both look like from here."}

    import datetime as _dt

    age_h = (_dt.datetime.now(_dt.timezone.utc) - at).total_seconds() / 3600
    return {
        "state": "ok" if age_h < _ARCH_STALE_INGEST_H else "watch",
        "how": "observed",
        "metric": f"{int(age_h)} h ago" if age_h >= 1 else f"{int(age_h * 60)} min ago",
        "at": at,
        "detail": f"{n:,} pipeline steps in the last seven days. This is the trail it leaves, "
                  f"not the process — nothing here can see the container.",
    }


async def _arch_observe_model() -> Dict:
    """Never called on purpose. Read from what real questions already paid for."""

    try:
        r = (await q(
            """SELECT count(*) AS n, max(ts) AS at,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                      sum(cost_usd) AS cost,
                      count(DISTINCT model) AS models,
                      mode() WITHIN GROUP (ORDER BY model) AS top_model
                 FROM llm_calls WHERE ts > now() - interval '7 days'"""
        ))[0]
        n, at, p50, cost = _i(r["n"]), r["at"], _ms(r["p50"]), r["cost"]
        models, top_model = _i(r["models"]), r["top_model"]
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "how": "not_checked", "metric": None, "at": None,
                "detail": "No per-call record could be read, so how the provider is behaving "
                          "is unknown from here."}

    if not n:
        return {"state": "unknown", "how": "observed", "metric": None, "at": at,
                "detail": "No model call in the last seven days. This is deliberately not "
                          "probed — a health check here would spend money and add five "
                          "seconds to every page load."}
    return {
        "state": "ok",
        "how": "observed",
        "metric": f"{round(p50 / 1000, 1)} s per call" if p50 else None,
        "at": at,
        "detail": (f"{n:,} calls in seven days"
                   + (f" · ${float(cost):.2f}" if cost is not None else "")
                   + (f" · {top_model}" if top_model else "")
                   + (f" · {models} models" if models > 1 else "")
                   + ". Read from calls real questions already made — never probed."),
    }


async def _arch_observe_agent() -> Dict:
    """The agent is in this process. Its health is what its tools did."""

    try:
        r = (await q(
            """SELECT count(*) AS n,
                      count(*) FILTER (WHERE outcome = 'failed')  AS failed,
                      count(*) FILTER (WHERE outcome = 'refused') AS refused,
                      count(DISTINCT turn_id) AS turns
                 FROM tool_calls WHERE ts > now() - interval '7 days'"""
        ))[0]
        n, failed, refused, turns = (_i(r["n"]), _i(r["failed"]),
                                     _i(r["refused"]), _i(r["turns"]))
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "how": "not_checked", "metric": None, "at": None,
                "detail": "The tool log could not be read."}

    if not n:
        return {"state": "unknown", "how": "observed", "metric": None, "at": None,
                "detail": "No tool call in the last seven days, so there is nothing to judge it on."}
    per = round(n / turns, 1) if turns else None
    return {
        # A refusal is NOT a failure: the only deliberate refusal today is a
        # store-scope decline, which is the scoping working. Counting it here
        # would give a correct product a failure rate.
        "state": "ok" if not failed else "watch",
        "how": "observed",
        "metric": f"{per} tools / question" if per else f"{n:,} calls",
        "at": None,
        "detail": (f"{n:,} tool calls over {turns:,} questions · {failed:,} failed"
                   + (f" · {refused:,} refused on purpose (a branch asked about another branch)"
                      if refused else "")
                   + "."),
    }


async def _arch_observe_widget() -> Dict:
    """Who is actually using the embedded assistant, minus this console."""

    from app.cache import INTERNAL_CHAT_EMBED_ID

    try:
        r = (await q(
            """SELECT count(*) AS n, count(DISTINCT store_id) AS sites, max(ts) AS at
                 FROM chat_logs
                WHERE ts > now() - interval '7 days'
                  AND embed_id IS NOT NULL AND embed_id <> $1""",
            INTERNAL_CHAT_EMBED_ID,
        ))[0]
        n, sites, at = _i(r["n"]), _i(r["sites"]), r["at"]
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "how": "not_checked", "metric": None, "at": None,
                "detail": "The turn log could not be read."}
    if not n:
        return {"state": "unknown", "how": "observed", "metric": None, "at": at,
                "detail": "No branch asked anything in the last seven days, so there is nothing "
                          "here about whether the installations work."}
    return {"state": "ok", "how": "observed", "metric": f"{n:,} questions", "at": at,
            "detail": f"{n:,} questions from {sites:,} branch(es) in seven days, this console's "
                      f"own chat excluded."}


# ---- architecture: what we can see, and what we cannot ----------------------
#
# A list of signals with ticks against them is worth nothing on its own: the
# failure this system has already had is a capture layer that was wired in one
# of the two places it needed to be, so `tool_calls` stayed empty forever,
# nothing errored, and 804 tests passed. "We record a tool trace" was true of
# the code and false of the database.
#
# So every signal that claims to be in place names a table and is answered with
# a live COUNT. A claim with a zero behind it is downgraded to `partial` by the
# count itself, and says so — nobody has to remember to notice.

#: (id, signal, where it is read, and the SQL that proves the pipe is not dry).
#: `sql` is None for a signal whose evidence is not a table.
_OBS_SIGNALS = [
    {
        "id": "deps",
        "signal": "Whether each part is answering",
        "where": "This page, and Version & releases",
        "sql": None,
        "note": "Probed when you load the page. The model provider and the ingest worker are "
                "read rather than probed, and their rows say so.",
    },
    {
        "id": "timing",
        "signal": "How long each question took",
        "where": "Health & usage · Conversations",
        "sql": "SELECT count(*) AS n FROM chat_logs WHERE latency_ms IS NOT NULL",
        "note": "End to end, per turn, with the route it took beside it.",
    },
    {
        "id": "route",
        "signal": "Which of the three routes a question took",
        "where": "This page",
        "sql": "SELECT count(*) AS n FROM chat_logs WHERE path IS NOT NULL",
        "note": "Cache, fast path or full agent. Without it a median describes a mix rather "
                "than a question.",
    },
    {
        "id": "tools",
        "signal": "Which tools an answer used, and how each ended",
        "where": "Conversations · turn detail",
        "sql": "SELECT count(*) AS n FROM tool_calls",
        "note": "Three outcomes, not two: succeeded, refused, failed. A store-scope refusal is "
                "the scoping working, and counting it as a failure gives a correct product a "
                "failure rate.",
    },
    {
        "id": "spend",
        "signal": "Tokens and cost, per model call",
        "where": "Cost & KPIs",
        "sql": "SELECT count(*) AS n FROM llm_calls",
        "note": None,   # filled in below from cost_is_estimated
    },
    {
        "id": "audit",
        "signal": "Who signed in and what they changed",
        "where": "Security log · Activity feed",
        "sql": "SELECT (SELECT count(*) FROM app_events) + (SELECT count(*) FROM auth_events) AS n",
        "note": "Admin actions and authentication events, kept apart from the turn log.",
    },
    {
        "id": "verdict",
        "signal": "What a person thought of an answer",
        "where": "Answer quality",
        "sql": "SELECT count(*) AS n FROM chat_feedback",
        "note": "The only signal here a human leaves by hand. Everything else is a count of "
                "rows that might be wrong.",
    },
]

#: Things nothing in this system records. Each says what would have to exist,
#: because "no alerting" is a fact and "nobody would know until someone opened
#: this console" is the consequence somebody has to decide about.
_OBS_GAPS = [
    {
        "id": "accuracy",
        "signal": "Whether the answers are right",
        "note": "An eval set exists in the repository and no result from it is stored anywhere, "
                "on this deployment or any other. Every latency and cost number on this console "
                "describes how fast and how cheaply the assistant said something — none of them "
                "says whether it was true.",
    },
    {
        "id": "alerting",
        "signal": "Anyone being told when it breaks",
        "note": "Nothing pages, emails or posts. A failure is visible only to someone who opens "
                "this console and looks, which is why the Today page leads with what needs a "
                "person rather than with usage.",
    },
    {
        "id": "applogs",
        "signal": "Searching the application log",
        "note": "The turn log and the audit trail are in the database and have a retention "
                "policy. The application's own log is container output only: not searchable, not "
                "shipped anywhere, and gone on the next deploy.",
    },
    {
        "id": "widget_errors",
        "signal": "Errors inside the widget on a customer site",
        "note": "A script error in a branch's browser never reaches us. If the widget failed to "
                "open on one site, this console would show that site simply asking nothing — "
                "which is exactly what a quiet branch looks like.",
    },
]


@router.get("/architecture/observability")
async def architecture_observability() -> Dict:
    """Every signal this system records, each proved by a live count.

    A signal whose table is EMPTY comes back `partial`, not `in_place`,
    whatever the code does. That is the whole point: the capture layer for
    ``tool_calls`` once shipped wired into one of the two places it needed to
    be, and "we record a tool trace" stayed true of the code and false of the
    database for as long as nobody looked.
    """

    rows = []
    for spec in _OBS_SIGNALS:
        n = None
        if spec["sql"]:
            try:
                n = _i((await q(spec["sql"]))[0]["n"])
            except Exception:  # noqa: BLE001 — table absent on a fresh install
                n = None
        note = spec["note"]
        if spec["id"] == "spend":
            note = await _obs_spend_note()
        rows.append({
            "id": spec["id"],
            "signal": spec["signal"],
            "where": spec["where"],
            "note": note,
            "rows": n,
            # `n is None` -> we could not look, which is not the same as empty.
            "state": "in_place" if n is None and spec["sql"] is None
                     else "unknown" if n is None
                     else "in_place" if n > 0
                     else "partial",
        })

    for gap in _OBS_GAPS:
        rows.append({**gap, "where": None, "rows": None, "state": "none"})

    return {
        "signals": rows,
        "counts": {
            state: sum(1 for r in rows if r["state"] == state)
            for state in ("in_place", "partial", "unknown", "none")
        },
    }


async def _obs_spend_note() -> str:
    """Whether the money figure came from the provider or was worked out here.

    `cost_is_estimated` is a real column and the difference is not cosmetic: an
    estimate is our arithmetic over a token count and a price we typed in, and
    it drifts the moment the provider's price changes without us noticing.
    """

    try:
        r = (await q(
            "SELECT count(*) FILTER (WHERE cost_is_estimated) AS est, count(*) AS n "
            "FROM llm_calls"
        ))[0]
        est, n = _i(r["est"]), _i(r["n"])
    except Exception:  # noqa: BLE001
        return ("Each call records whether its cost came from the provider or was worked out "
                "here, but that could not be read just now.")
    if not n:
        return ("Each call records whether its cost came from the provider or was worked out "
                "from tokens and a price typed into the code.")
    if not est:
        return (f"All {n:,} calls carry a cost the provider itself reported — none is our own "
                f"arithmetic. Where a provider reports nothing, the estimate is used and the "
                f"row is marked as one.")
    return (f"{est:,} of {n:,} calls carry an ESTIMATE rather than a figure the provider "
            f"reported. An estimate is tokens times a price typed into the code, and it drifts "
            f"silently when that price changes.")


# ---- architecture: the three routes a question can take ---------------------
#
# One question does not have one path — it has three, and which one it takes is
# the single biggest thing about how the product feels. The console had no way
# to say so, and "average latency" hides it completely: a 5s median over a mix
# of 1ms cache hits and 15s agent runs describes no question anybody asked.
#
# The route is recorded per turn in `chat_logs.path`, so these are counts of
# what really happened rather than a description of what the code could do.

#: `chat_logs.path` values, in the order a question tries them. Each carries
#: what it is and what makes a question take it — both traceable to `_answer`
#: in app/api.py, which is the only place the branch is decided.
_QUESTION_ROUTES = [
    {
        "id": "cache",
        "name": "Answered from cache",
        "when": "The same question, from the same branch, since the last stock file landed.",
        "does": "No model call at all. The key is (data version, model, branch, question), so a "
                "stock file landing retires every cached answer at once.",
        "skips": "A follow-up never reads this cache: the key holds no conversation, so "
                 "\u201cwhich other shop has it?\u201d would be served somebody else's drug.",
    },
    {
        "id": "fast_path",
        "name": "Fast path",
        "when": "\u201cDo you have X\u201d and \u201cwho else has X\u201d \u2014 the two "
                "questions branches actually ask, matched by regex and resolved without a model.",
        "does": "One model call, and that call has no tools: it restates a block of facts the "
                "database already returned, so it cannot fetch or invent a number.",
        "skips": "An ambiguous product name falls through to the full agent rather than "
                 "guessing. In a pharmacy a wrong fast answer is worse than a slow right one.",
    },
    {
        "id": "agent",
        "name": "Full agent",
        "when": "Everything else \u2014 substitutes, prices, \u201cwhat is it for\u201d, and "
                "every follow-up.",
        "does": "The model picks among twelve read-only tools, runs them, then writes the "
                "answer. That is several provider calls in one turn.",
        "skips": "Nothing. This is the route that can answer anything, and it is the slow one.",
    },
]


@router.get("/architecture/question-path")
async def architecture_question_path() -> Dict:
    """The three routes, with how many real questions took each and how long.

    **A median per route, never one median over all of them.** Mixing a 1 ms
    cache hit with a 15 s agent run produces a number describing no question
    anybody asked, and it moves when the cache warms rather than when anything
    gets faster.

    ``turns`` is null, not 0, for a route no question has taken in the window:
    a route nothing exercised and a route measured at zero are different
    claims, and only one of them is possible here.
    """

    routes = {r["id"]: dict(r, turns=None, p50_ms=None, share=None) for r in _QUESTION_ROUTES}
    total = 0
    try:
        rows = await q(
            """SELECT path, count(*) AS n,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50
                 FROM chat_logs
                WHERE ts > now() - interval '30 days' AND path IS NOT NULL
                GROUP BY path"""
        )
    except Exception:  # noqa: BLE001
        rows = []

    seen_unknown = []
    for r in rows:
        n = _i(r["n"])
        total += n
        key = r["path"]
        if key not in routes:
            # A path the console does not know about is NOT folded into one it
            # does. It is named, so a new branch in `_answer` shows up here as
            # an unnamed route rather than silently inflating "full agent".
            seen_unknown.append({"id": key, "turns": n, "p50_ms": _ms(r["p50"])})
            continue
        routes[key]["turns"] = n
        routes[key]["p50_ms"] = _ms(r["p50"])

    ordered = [routes[r["id"]] for r in _QUESTION_ROUTES]
    if total:
        for r in ordered:
            if r["turns"] is not None:
                r["share"] = round(r["turns"] / total, 4)

    # The one model call is the floor. Nothing about the fast path or the cache
    # changes it — it is what a provider round trip costs on this stack, and it
    # is why "delete a round trip" beats "make the round trip faster".
    call_ms = None
    try:
        call_ms = _ms((await q(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50
                 FROM llm_calls WHERE ts > now() - interval '30 days'"""
        ))[0]["p50"])
    except Exception:  # noqa: BLE001
        call_ms = None

    return {
        "routes": ordered,
        "total": total or None,
        "unknown_paths": seen_unknown,
        "model_call_p50_ms": call_ms,
        "window_days": 30,
    }


@router.get("/architecture/health")
async def architecture_health() -> Dict:
    """Every part of this system, what it is doing, and how we know.

    `how` is the field that keeps this honest. `probed` means we asked it in
    the course of answering this request and timed the reply. `observed` means
    we read a record it left. `not_checked` means we have nothing — and the
    state is then `unknown`, which the UI must not paint the colour of `ok`.
    """

    import asyncio

    pg, rd, ingest, model, agent, widget = await asyncio.gather(
        _arch_probe_postgres(), _arch_probe_redis(), _arch_observe_ingest(),
        _arch_observe_model(), _arch_observe_agent(), _arch_observe_widget(),
    )

    parts = [
        {"id": "widget", "name": "Widget on your sites", "kind": "client", **widget},
        {"id": "api", "name": "This API", "kind": "service",
         "state": "ok", "how": "probed", "metric": None, "at": None,
         "detail": "You are talking to it — that is the whole of the check. Its per-process "
                   "counters reset on restart and are not health."},
        {"id": "agent", "name": "Agent", "kind": "service", **agent},
        {"id": "model", "name": "Model provider", "kind": "external", **model},
        {"id": "postgres", "name": "Catalog & stock database", "kind": "store", **pg},
        {"id": "redis", "name": "Answer cache", "kind": "store", **rd},
        {"id": "ingest", "name": "Ingest worker", "kind": "worker", **ingest},
        {"id": "drop", "name": "File drop", "kind": "edge", **_arch_probe_drop()},
    ]
    for p in parts:
        p.setdefault("at", None)

    return {
        "parts": parts,
        "counts": {
            state: sum(1 for p in parts if p["state"] == state)
            for state in ("ok", "watch", "down", "unknown")
        },
    }

@router.get("/analytics/branch-stock")
async def analytics_branch_stock(
    limit: int = 6,
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Branches ranked by how many impossible quantities they hold.

    ``/analytics/data-health`` counts the same bands across the whole estate.
    This splits them by branch, because "79 rows hold a negative quantity"
    tells an operator to go and look and nothing about WHERE, and the only
    action available is ringing one branch.

    **It ranks by ``negative`` because that is the only band with a shape on
    this data.** Measured before the console section was written: ``out`` is 2
    rows in 111,654, ``unknown`` is 0, and the 1-19 band holds 86% of every
    branch because the median quantity in this catalog is 6. A ranking by those
    is 53 near-ties presented as a league table. Negatives are 79 rows across
    32 of 53 branches, worst branch 13 — a real ordering, and the one an
    operator can act on.

    If a re-ingest ever brings real zeroes or real blanks, they are already
    counted per branch here and the tie-break already reads them; what would
    need revisiting is which number the UI puts first, not this query.

    **The four bands are four different answers and are never merged.**

    * ``out`` — ``stock_qty = 0``. Measured, and the shelf is empty.
    * ``low`` — 1–19, the same band ``GET /admin/inventory`` calls low. It
      excludes 0 and it excludes NULL: NULL is UNKNOWN, and unknown is not
      "nearly out".
    * ``unknown`` — ``stock_qty IS NULL``. The file arrived with a blank cell.
      The assistant has no number to quote, which is a different failure from
      quoting zero, and folding it into ``out`` would report a stocked branch
      as empty.
    * ``negative`` — below zero. Impossible rather than low, and the assistant
      reads it out as written.

    ``sellable`` is what is left: a positive, known quantity. ``coverage`` is
    ``sellable / rows`` and is **null when the branch has no rows at all**,
    never 0.0 — a branch with nothing recorded has no coverage to report, and a
    zero there would rank it alongside a branch that genuinely has nothing on
    the shelf.

    ``total`` is every branch with rows; ``affected`` is how many of them hold
    at least one impossible quantity. Both are counted BEFORE ``limit``, and a
    caller showing six needs ``affected`` rather than ``total`` to say what it
    is not showing: "6 of 53" would imply the other 47 are also wrong.

    Scoped exactly like ``/stores``: unscoped, this hands a branch-pinned admin
    every other branch's stock position. ``_site_clause`` and never a bare
    ``ILIKE '%x%'`` — a prefix-shaped store id would substring-match siblings.

    There is no branch NAME anywhere in this database: ``inventory`` carries
    ``site_code`` and there is no sites table. The code is returned alone rather
    than joined to an invented label.
    """

    limit = min(max(limit, 1), 200)

    where, args = "", []
    if scope:
        where = " WHERE " + _site_clause("site_code", "$1")
        args = [scope]

    try:
        rows = await q(
            """SELECT site_code,
                      count(*)                                            AS rows,
                      count(*) FILTER (WHERE stock_qty = 0)               AS out,
                      count(*) FILTER (WHERE stock_qty BETWEEN 1 AND 19)  AS low,
                      count(*) FILTER (WHERE stock_qty IS NULL)           AS unknown,
                      count(*) FILTER (WHERE stock_qty < 0)               AS negative,
                      count(*) FILTER (WHERE stock_qty >= 20)             AS sellable
                 FROM inventory"""
            + where
            + """ GROUP BY site_code
                  ORDER BY count(*) FILTER (WHERE stock_qty < 0) DESC,
                           (count(*) FILTER (WHERE stock_qty = 0)
                            + count(*) FILTER (WHERE stock_qty IS NULL)) DESC,
                           site_code""",
            *args,
        )
    except Exception:  # noqa: BLE001 — table absent on a fresh install
        return {"total": None, "affected": None, "rows": [], "shown": 0}

    out = []
    for r in rows[:limit]:
        n = _i(r["rows"])
        # `sellable` above is >= 20 only, because the four bands must partition
        # the rows. What "coverage" means to a reader is "has a real number I
        # can quote", which includes the 1-19 band.
        good = _i(r["sellable"]) + _i(r["low"])
        out.append(
            {
                "site_code": r["site_code"],
                "rows": n,
                "out": _i(r["out"]),
                "low": _i(r["low"]),
                "unknown": _i(r["unknown"]),
                "negative": _i(r["negative"]),
                # null, never 0.0 — see the docstring.
                "coverage": round(good / n, 4) if n else None,
            }
        )

    return {
        "total": len(rows),
        "affected": sum(1 for r in rows if _i(r["negative"]) > 0),
        "rows": out,
        "shown": len(out),
    }


# ---- charts: the same turn log, shaped for plotting ------------------------
#
# Four endpoints the Analytics page charts. They obey the three rules the block
# above states — store-scoped, bound parameters only, aggregated in Postgres —
# and add a fourth that only matters once a chart is drawn from the numbers:
#
# 4. **An axis says what was measured, and says nothing where nothing was.** A
#    bucket with no traffic is a zero-filled point, so a gap in the series is
#    visibly a gap and not a line drawn straight across it. A turn whose `path`
#    or `tools` was never recorded keeps NULL and is labelled "not recorded" —
#    it is not folded into `agent` or into an empty tool list. And the cost panel
#    reports `available: false` rather than charting a flat zero over history
#    that predates token capture. Every one of those is the same mistake in a
#    different costume: presenting an absence as a measurement.

_BUCKETS_TS = {
    # bucket -> (date_trunc unit, to_char format, generate_series step, max span)
    # The span caps how many points one request can generate: an operator who
    # asks for hourly over a year would otherwise ask Postgres for 8,760 rows
    # and the browser for a chart nobody can read.
    "day": ("day", "YYYY-MM-DD", "1 day", "1100 days"),
    "hour": ("hour", "YYYY-MM-DD HH24:00", "1 hour", "60 days"),
}


@router.get("/analytics/timeseries")
async def analytics_timeseries(
    bucket: str = "day",
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Traffic / latency / cost per day or hour, **zero-filled across the range**.

    The zero fill is the whole point. ``GROUP BY day`` returns only days that
    have rows, so a day the service was down comes back absent — and every chart
    library joins the point before it to the point after it, drawing a straight
    line through the outage. The reader sees steady traffic on the day there was
    none. Here the missing bucket is materialised with ``turns: 0``, and the
    metrics nobody measured stay NULL (``p50_ms``, ``tokens``, ``cost_usd``), so
    a gap reads as a gap and an unmeasured latency does not read as 0 ms.

    The range is the requested one when ``from``/``to`` are given — including
    buckets after the last turn, which is how "we stopped getting traffic on
    Tuesday" becomes visible — and the data's own span otherwise.

    ``tokens``/``cost_usd`` are NULL on any database that predates
    ``migrations/0006_turn_metrics.sql``; the query falls back rather than 500ing,
    because a missing cost column must not cost the operator their traffic chart.
    """

    if bucket not in _BUCKETS_TS:
        raise HTTPException(status_code=400, detail="`bucket` must be day or hour")
    unit, fmt, step, span = _BUCKETS_TS[bucket]

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)

    # Two extra binds for the zero-fill bounds. They repeat the values
    # `_log_filters` already validated and bound — re-bound rather than reused so
    # the placeholder numbering stays a straight 1..n and nothing depends on
    # which order the filters appended.
    #
    # Either spelling of the window feeds the axis. `start`/`end` mean exactly
    # what `from`/`to` mean (§4 amended) and the console sends the former, so
    # reading only the latter left the requested-range axis working for the old
    # callers and silently falling back to the data's own span for the new ones —
    # which is precisely the "traffic stopped on Tuesday" case the fill exists for.
    frm_v, _ = _parse_ts(frm, "from")
    to_v, to_day = _parse_ts(to, "to")
    if not frm_v and extra.start:
        frm_v = extra.start
    if not to_v and extra.end:
        to_v, to_day = extra.end, extra.end_is_day
    # The buckets below are NAIVE LOCAL timestamps (`ts AT TIME ZONE tz`), so the
    # axis bounds have to be in that same space or generate_series starts the
    # axis at a different midnight than the one the data was grouped into.
    p_from = _local_bound_expr(frm_v, params, extra.tz)
    p_to = _local_bound_expr(to_v, params, extra.tz)
    # A bare `to` date means the WHOLE day (the same trap `_log_filters` guards):
    # with hourly buckets, truncating '2026-08-13' would end the axis at 00:00
    # and drop 23 hours of it.
    to_expr = (
        f"({p_to} + interval '1 day' - interval '1 second')" if to_day else p_to
    )

    metrics = "sum(total_tokens) AS tokens, sum(cost_usd) AS cost"
    bucket_sql = _bucket_expr("ts", unit, extra.tz, params)

    def _sql(metric_cols: str) -> str:
        return f"""
        WITH agg AS (
            SELECT {bucket_sql} AS b,
                   count(*)                                  AS turns,
                   count(*) FILTER (WHERE cached)            AS cached,
                   count(*) FILTER (WHERE answer IS NULL
                                      OR btrim(answer) = '') AS refusals,
                   percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                   {metric_cols}
              FROM chat_logs {where}
             GROUP BY 1
        ),
        bounds AS (
            SELECT COALESCE(date_trunc('{unit}', {p_from}),
                            (SELECT min(b) FROM agg)) AS lo,
                   COALESCE(date_trunc('{unit}', {to_expr}),
                            (SELECT max(b) FROM agg)) AS hi
        ),
        series AS (
            SELECT generate_series(lo, LEAST(hi, lo + interval '{span}'),
                                   interval '{step}') AS b
              FROM bounds WHERE lo IS NOT NULL AND hi IS NOT NULL AND hi >= lo
        )
        SELECT to_char(s.b, '{fmt}')       AS t,
               COALESCE(a.turns, 0)        AS turns,
               COALESCE(a.cached, 0)       AS cached,
               COALESCE(a.refusals, 0)     AS refusals,
               a.p50, a.p95, a.tokens, a.cost
          FROM series s LEFT JOIN agg a ON a.b = s.b
         ORDER BY s.b"""

    try:
        rows = await q(_sql(metrics), *params)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — pre-0006 columns, or no chat_logs at all
        try:
            rows = await q(_sql("NULL::bigint AS tokens, NULL::numeric AS cost"), *params)
        except Exception:  # noqa: BLE001
            rows = []

    # ---- feedback per bucket, asked SEPARATELY -------------------------------
    #
    # Its own query, merged in Python, for the same reason the token metrics fall
    # back rather than raise: `chat_feedback.turn_id` is new, and on a database
    # that has not picked it up this join fails. Folded into the main SELECT it
    # would take the traffic chart down with it — an operator losing the answer to
    # "did anyone talk to us yesterday" because a ratings column is missing.
    #
    # Joined on the FK, never on question text (§5). Counted as RATINGS, not as
    # rated turns: two people disliking the same answer is two pieces of
    # feedback, and a per-turn boolean would report it as one.
    #
    # `up`/`down` are 0 when the join ran and found nothing — a measured zero,
    # because we can see the feedback table — and `null` for every bucket when
    # the join could not run at all. The UI renders `—` for the second.
    fb_by_bucket: Dict[str, Dict[str, int]] = {}
    fb_available = False
    try:
        fb_params: List = []
        fb_conds = _log_filters(fb_params, scope, frm, to, store, embed, lang,
                                q_text, tool, path, col="l.", extra=extra)
        fb_bucket = _bucket_expr("l.ts", unit, extra.tz, fb_params)
        fb_rows = await q(
            f"""SELECT to_char({fb_bucket}, '{fmt}') AS t,
                       count(*) FILTER (WHERE cf.verdict = 'up')   AS up,
                       count(*) FILTER (WHERE cf.verdict = 'down') AS down
                  FROM chat_logs l
                  JOIN chat_feedback cf ON cf.turn_id = l.id
                  {_where(fb_conds)}
                 GROUP BY 1""",
            *fb_params,
        )
        fb_available = True
        fb_by_bucket = {r["t"]: {"up": _i(r["up"]), "down": _i(r["down"])}
                        for r in fb_rows}
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — no turn_id column, or no chat_feedback
        pass

    def _fb(t: str, key: str) -> Optional[int]:
        if not fb_available:
            return None
        return fb_by_bucket.get(t, {}).get(key, 0)

    return {
        "bucket": bucket,
        # Echoed so the header can say WHICH midnight these buckets are cut at.
        # A day label without its zone is the defect this endpoint just fixed,
        # one layer up: the reader supplies the missing half from habit.
        "tz": extra.tz,
        # Stated in the payload so the UI never has to infer "all zeroes" from a
        # chart that is simply blind.
        "feedback_available": fb_available,
        "rows": [
            {
                "t": r["t"],
                "turns": _i(r["turns"]),
                "cached": _i(r["cached"]),
                "p50_ms": _ms(r["p50"]),
                "p95_ms": _ms(r["p95"]),
                "tokens": _int_or_none(r["tokens"]),
                "cost_usd": float(r["cost"]) if r["cost"] is not None else None,
                "refusals": _i(r["refusals"]),
                "up": _fb(r["t"], "up"),
                "down": _fb(r["t"], "down"),
            }
            for r in rows
        ],
    }


@router.get("/analytics/tools")
async def analytics_tools(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Which tools the agent actually called, from the ``tools`` JSONB array.

    ``calls`` counts array ELEMENTS and ``turns`` counts distinct turns, and they
    differ whenever one turn called a tool twice — a distinction that matters
    here, because "get_stock ran 400 times across 90 turns" is a retry loop and
    "400 across 400" is normal traffic.

    ``not_recorded`` is the count of turns whose ``tools`` is NULL: history from
    before the audit columns existed, plus every cache hit (no model ran, so no
    tool did either). It is reported separately instead of being counted as "no
    tools used", so the page can say *how much of the window it cannot speak
    for* rather than quietly implying the agent answered those bare.

    ``p50_ms`` is the median over the unnested rows, i.e. weighted by calls, and
    it is the latency of the whole TURN — the log has no per-tool timing.
    """

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)
    unnested = _where(conds + ["jsonb_typeof(tools) = 'array'"])

    try:
        head = (
            await q(
                f"""SELECT count(*) FILTER (WHERE jsonb_typeof(tools) = 'array'
                                              AND jsonb_array_length(tools) > 0)
                               AS with_tools,
                           count(*) FILTER (WHERE tools IS NULL) AS not_recorded
                      FROM chat_logs {where}""",
                *params,
            )
        )[0]
        rows = await q(
            f"""WITH t AS (
                    SELECT id, latency_ms,
                           jsonb_array_elements_text(tools) AS tool
                      FROM chat_logs {unnested}
                )
                SELECT tool, count(*) AS calls, count(DISTINCT id) AS turns,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50
                  FROM t GROUP BY 1 ORDER BY calls DESC, tool ASC""",
                *params,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — table not created yet
        return {"total_turns_with_tools": 0, "not_recorded": 0, "rows": []}

    return {
        "total_turns_with_tools": _i(head["with_tools"]),
        "not_recorded": _i(head["not_recorded"]),
        "rows": [
            {
                "tool": r["tool"],
                "calls": _i(r["calls"]),
                "turns": _i(r["turns"]),
                "p50_ms": _ms(r["p50"]),
            }
            for r in rows
        ],
    }


@router.get("/analytics/paths")
async def analytics_paths(
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> List[Dict]:
    """Traffic split by route: agent / fast_path / cache — and NULL.

    **A NULL ``path`` is returned as NULL**, exactly as ``/analytics/embeds``
    keeps a NULL ``embed_id``. Those turns are older than the audit columns; the
    honest label is "not recorded" and the UI renders it as one. Folding them
    into ``agent`` — the plausible guess, since the agent was the only route
    then — would put a number on this chart that nobody can reproduce from the
    table, and the chart exists precisely to answer "how much traffic never
    reaches the model".
    """

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)
    try:
        rows = await q(
            f"""SELECT path, count(*) AS turns,
                       count(*) FILTER (WHERE cached) AS cached,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
                  FROM chat_logs {where}
                 GROUP BY 1 ORDER BY turns DESC, path ASC NULLS LAST""",
            *params,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — table not created yet
        return []
    return [
        {
            "path": r["path"],  # NULL stays NULL — "not recorded", never guessed
            "turns": _i(r["turns"]),
            "cached": _i(r["cached"]),
            "p50_ms": _ms(r["p50"]),
            "p95_ms": _ms(r["p95"]),
        }
        for r in rows
    ]


@router.get("/analytics/cost")
async def analytics_cost(
    group: str = "day",
    frm: str = Query("", alias="from"),
    to: str = "",
    store: str = "",
    embed: str = "",
    lang: str = "",
    q_text: str = Query("", alias="q"),
    tool: str = "",
    path: str = "",
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """Token + spend rollup per day or per model, with an honest ``available``.

    ``available`` is **computed** — "does any matching row carry a non-NULL token
    count" — and not inferred from the column existing or from the sums being
    non-zero. That is the difference between a panel that says *we have not
    measured this yet* and a chart of flat zeros that says *this cost nothing*.
    Every row logged before ``migrations/0006_turn_metrics.sql`` is in the first
    category, and so is every cache hit forever after: no model ran, so no token
    was spent and none was recorded.

    ``cost_usd`` is summed in Postgres as NUMERIC and only then converted, so the
    spend figure is not a float accumulation. A NULL sum (nothing priced) stays
    NULL.
    """

    if group not in ("day", "model"):
        raise HTTPException(status_code=400, detail="`group` must be day or model")

    params: List = []
    conds = _log_filters(params, scope, frm, to, store, embed, lang, q_text,
                         tool, path, extra=extra)
    where = _where(conds)

    # Fixed expressions chosen in this file, keyed by a validated literal — the
    # caller's string never reaches the SQL. The day key is cut in the caller's
    # zone for the same reason the traffic chart is (§A): a spend row labelled
    # "17 Aug" that actually runs 06:30 to 06:30 cannot be reconciled with an
    # invoice, and the discrepancy looks like a pricing bug.
    #
    # Built with an `if`, not by indexing a dict of both: `_bucket_expr` BINDS as
    # a side effect, so a dict literal would append the zone parameter even when
    # grouping by model and asyncpg would reject the call for passing an argument
    # the statement never references.
    if group == "day":
        key_expr = (
            f"to_char({_bucket_expr('ts', 'day', extra.tz, params)}, 'YYYY-MM-DD')"
        )
    else:
        key_expr = "model"

    try:
        rows = await q(
            f"""SELECT {key_expr} AS key, count(*) AS turns,
                       sum(input_tokens)  AS inp,
                       sum(output_tokens) AS outp,
                       sum(total_tokens)  AS tot,
                       sum(cost_usd)      AS cost,
                       count(*) FILTER (WHERE input_tokens  IS NOT NULL
                                          OR output_tokens IS NOT NULL
                                          OR total_tokens  IS NOT NULL) AS captured
                  FROM chat_logs {where}
                 GROUP BY 1 ORDER BY 1 NULLS LAST""",
            *params,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — pre-0006 database, or no chat_logs at all
        return {
            "available": False,
            "reason": "this database has no turn-metric columns yet"
                      " (run migrations/0006_turn_metrics.sql)",
            "tz": extra.tz,
            "rows": [],
        }

    if not any(_i(r["captured"]) for r in rows):
        return {
            "available": False,
            "reason": "no turn has token data yet",
            "tz": extra.tz,
            "rows": [],
        }

    # ---- turn-level cost is structurally NULL on this stack ------------------
    #
    # `chat_logs.cost_usd` is written from agno's aggregate `metrics.cost`, and
    # OpenRouter returns no price on the completion response — so agno leaves it
    # None and the column is NULL for every turn. `llm_calls.cost_usd` is a
    # DIFFERENT path: it reads the per-call `usage.cost` and falls back to
    # `activity._estimated_cost`, so the money is there and only there.
    #
    # Without this, the Cost tab's turn-level panels read `—` forever, look
    # exactly like a window with no traffic, and send somebody to check the
    # instrumentation that is working. `cost_available` says the token half is
    # real and the cost half is not, and `cost_hint` names where to go.
    priced_calls: Optional[int] = None
    try:
        cp: List = []
        cconds = _log_filters(cp, scope, frm, to, store, embed, lang, q_text,
                              tool, path, col="l.", extra=extra)
        priced_calls = _i((await q(
            f"""SELECT count(*) AS n
                  FROM llm_calls c JOIN chat_logs l ON l.id = c.turn_id
                  {_where(cconds + ['c.cost_usd IS NOT NULL'])}""",
            *cp,
        ))[0]["n"])
    except Exception:  # noqa: BLE001 — no llm_calls table: we cannot say
        priced_calls = None

    has_turn_cost = any(r["cost"] is not None for r in rows)
    return {
        "available": True,
        "group": group,
        # The zone the `day` keys were cut at (§F1). Present on the empty shapes
        # above too: the chip has to say which midnight even when there is
        # nothing to chart, or it silently reverts to UTC on a quiet window.
        "tz": extra.tz,
        "cost_available": has_turn_cost,
        "priced_calls": priced_calls,
        "cost_hint": None if has_turn_cost else (
            "Turn-level cost is not recorded on this stack: OpenRouter returns"
            " no price on the completion response, so `chat_logs.cost_usd` stays"
            " NULL. Per-call cost IS recorded"
            + (f" ({priced_calls} priced calls in this window)"
               if priced_calls else "")
            + " — read /admin/analytics/economics or /admin/analytics/llm-calls."
        ),
        "rows": [
            {
                "key": r["key"],
                "turns": _i(r["turns"]),
                "input_tokens": _int_or_none(r["inp"]),
                "output_tokens": _int_or_none(r["outp"]),
                "total_tokens": _int_or_none(r["tot"]),
                "cost_usd": float(r["cost"]) if r["cost"] is not None else None,
            }
            for r in rows
        ],
    }


# ---- instrumented analytics (tool_calls / llm_calls) ------------------------
#
# Everything below reads the per-call tables described in
# `docs/ANALYTICS_CONTRACT.md` (§1): `tool_calls` — one row per tool invocation
# with a THREE-state outcome — and `llm_calls` — one row per model call, which is
# where token counts actually belong. `chat_logs` keeps the turn.
#
# Four rules, all of them the contract's:
#
# 1. **One params object.** `AnalyticsFilters` declares every shared filter as a
#    real FastAPI parameter exactly once, and `chat_conds()` turns it into
#    predicates. Declaring a filter per-endpoint is how a filter goes missing:
#    FastAPI drops an undeclared query param silently, so the endpoint answers
#    200 with UNFILTERED data while the UI draws a filter chip over it. Adding a
#    filter means adding it here, and every endpoint gets it.
#
# 2. **Store scope is not a filter.** `scope` comes from the caller's own users
#    row (`caller_store_scope`) and is ANDed with `store`, never replaced by it —
#    so `?store=<sibling>` from a pinned caller narrows to nothing rather than
#    crossing the boundary. Same rule, same `_site_clause`, as `/admin/inventory`.
#
# 3. **Unknown is `null`, never 0**, and any rate ships with its denominator as
#    `{"rate": …, "n": …}`. `not_recorded` counts the turns that have no rows in
#    the table being aggregated — the 122 pre-instrumentation turns and every
#    cache hit — so a panel can say how much of the window it cannot speak for
#    instead of implying those turns used no tools and cost nothing.
#
# 4. **Sections are isolated.** Each panel is computed through `_section()`,
#    which returns that panel's EMPTY SHAPE — same keys, same types — if it
#    raises. A missing `tool_calls` table costs you the tool panel, not the page.


class AnalyticsFilters:
    """The shared filter object for every `/admin/analytics/*` endpoint (§4).

    Built by the `analytics_filters` dependency below, which is where the FastAPI
    parameter declarations live. Holding them in one place is the whole point: an
    endpoint cannot accidentally support a subset.

    It does **not** own a second copy of the WHERE clause. `chat_conds` delegates
    to `_log_filters` + `SharedFilters` — the same builder the older endpoints
    use — so `?embed=none` or a comma-separated `store` cannot mean one thing on
    `/summary` and another on `/tool-outcomes`. Two builders over one table is
    exactly how a page ends up with panels that disagree about what it is showing.
    """

    __slots__ = ("scope", "store", "lang", "path", "embed", "extra")

    def __init__(self, scope, store, lang, path, embed, extra: SharedFilters):
        self.scope = scope
        self.store = store
        self.lang = lang
        self.path = path
        self.embed = embed
        self.extra = extra

    # Read-through to the §4 filters that live on `extra`, so callers can say
    # `f.model` without caring which object holds it.
    @property
    def model(self) -> List[str]:
        return _csv(self.extra.model)

    @property
    def actor(self) -> List[str]:
        return _csv(self.extra.actor)

    @property
    def start(self) -> str:
        return self.extra.start

    @property
    def end(self) -> str:
        return self.extra.end

    @property
    def tz(self) -> str:
        return self.extra.tz

    def shifted(self, start: str, end: str) -> "AnalyticsFilters":
        """The same filters over a different window — the previous-period half."""

        return AnalyticsFilters(
            scope=self.scope, store=self.store, lang=self.lang, path=self.path,
            embed=self.embed, extra=self.extra.shifted(start, end),
        )

    def prev_window(self) -> Optional[Tuple[str, str]]:
        return _prev_window(self.extra.start, self.extra.end, self.extra.tz)

    def prev_period(self) -> Optional[Dict]:
        w = self.prev_window()
        return {"start": w[0], "end": w[1]} if w else None

    def chat_conds(
        self,
        params: List,
        col: str = "",
        *,
        skip: tuple = (),
    ) -> List[str]:
        """Predicates over `chat_logs`, appending binds to ``params``.

        ``col`` is an optional alias prefix (``"l."``). ``skip`` drops named
        filters an endpoint applies to a different column instead — `llm-usage`
        filters `model` on `llm_calls.model`, not on the turn's headline model.

        The **enforced store scope is not skippable**: it is passed to
        `_log_filters` unconditionally and there is no name for it in ``skip``.
        A boundary a caller can turn off with a keyword is not a boundary.

        Nothing a caller sends is interpolated: the only f-string content is
        `$n` placeholder numbers and column names chosen in this file.
        """

        def keep(name: str, value):
            return value if name not in skip else ("" if isinstance(value, str) else None)

        extra = SharedFilters(
            start=self.extra.start,
            end=self.extra.end,
            # Load-bearing: drop this and a bare `end` date silently reverts to a
            # plain exclusive bound on the NEW endpoints only, so the same date
            # would mean two different things depending on which panel asked.
            end_is_day=self.extra.end_is_day,
            model=keep("model", self.extra.model),
            actor=keep("actor", self.extra.actor),
            cached=keep("cached", self.extra.cached),
            rated=keep("rated", self.extra.rated),
            # Equally load-bearing: drop this and the window silently reverts to
            # UTC while the buckets drawn against it are cut in the caller's zone.
            tz=self.extra.tz,
        )
        return _log_filters(
            params,
            scope=self.scope,
            store=keep("store", self.store),
            embed=keep("embed", self.embed),
            lang=keep("lang", self.lang),
            path=keep("path", self.path),
            col=col,
            extra=extra,
        )


async def analytics_filters(
    store: str = Query("", description="comma-separated store ids (narrows within scope)"),
    lang: str = Query("", description="comma-separated"),
    path: str = Query("", description="comma-separated: fast_path|agent|cache|none"),
    embed: str = Query("", description="comma-separated embed ids; `none` = unattributed"),
    extra: SharedFilters = Depends(shared_filters),
    scope: Optional[str] = Depends(caller_store_scope),
) -> AnalyticsFilters:
    """Declare — once — every shared analytics query parameter.

    This function is the reason the filters cannot silently go missing. An
    endpoint that takes `f: AnalyticsFilters = Depends(analytics_filters)` has
    all ten declared by construction (four here, six on `shared_filters`); there
    is no way to support nine.
    """

    return AnalyticsFilters(
        scope=scope, store=store, lang=lang, path=path, embed=embed, extra=extra,
    )


def _rate(numerator: Optional[int], denominator: Optional[int]) -> Dict:
    """`{"rate": …, "n": …}` — a percentage never travels without its sample.

    `rate` is **null**, not 0.0, when the denominator is 0: "we have measured
    nothing" and "we measured zero" are different answers, and only one of them
    should render as a number (§3).
    """

    n = _i(denominator)
    if not n:
        return {"rate": None, "n": n}
    return {"rate": round(_i(numerator) / n, 4), "n": n}


async def _section(compute, empty):
    """Run one panel; on failure hand back its EMPTY SHAPE instead of a 500 (§6).

    The empty value must be shaped like the real one — same keys, same types —
    because the frontend must never have to branch on a missing key. An
    `HTTPException` is re-raised: a 401/403/400 is an answer about the REQUEST,
    and swallowing it into an empty panel would show a scoped caller a blank
    dashboard instead of telling them why.
    """

    try:
        return await compute()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — missing table/column, or a bad query
        return empty


async def _turn_count(f: AnalyticsFilters) -> Optional[int]:
    """How many turns the filter selects, or None if `chat_logs` cannot be read."""

    params: List = []
    where = _where(f.chat_conds(params))
    try:
        rows = await q(f"SELECT count(*) AS n FROM chat_logs {where}", *params)
        return _i(rows[0]["n"])
    except Exception:  # noqa: BLE001
        return None


async def _not_recorded(f: AnalyticsFilters, table: str) -> Optional[int]:
    """Filtered turns with NO row in ``table`` — the honest "we cannot say" count.

    ``table`` is a literal chosen at the call site in this file, never caller
    input. The count is `null` when it cannot be computed, so the UI renders `—`
    rather than claiming zero unrecorded turns on a database that has no
    instrumentation tables at all.
    """

    if table not in ("tool_calls", "llm_calls"):  # defence in depth, not input
        raise ValueError("unknown table")
    params: List = []
    where = _where(f.chat_conds(params, "l."))
    try:
        rows = await q(
            f"""SELECT count(*) AS n FROM chat_logs l {where}
                {'AND' if where else 'WHERE'} NOT EXISTS (
                    SELECT 1 FROM {table} c WHERE c.turn_id = l.id)""",
            *params,
        )
        return _i(rows[0]["n"])
    except Exception:  # noqa: BLE001 — table absent: we genuinely cannot say
        return None


# ---- tool outcomes ---------------------------------------------------------

_TOOL_OUTCOMES_EMPTY: Dict = {"bars": [], "totals": {"succeeded": 0, "refused": 0,
                                                     "failed": 0, "calls": 0},
                              "success_rate": {"rate": None, "n": 0},
                              "not_recorded": None, "available": False}


@router.get("/analytics/tool-outcomes")
async def analytics_tool_outcomes(
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """Per tool: succeeded / refused / failed, and mean duration.

    **`refused` is never folded into `failed`.** A tool that deliberately
    declines and redirects did its job; a tool that crashed did not. Collapsing
    them is what produced a 56% "failure rate" on a working tool in the product
    this instrumentation came from, and every number built on that reading lies
    in the same direction. The three states come from the table's own CHECK
    constraint, classified at the raise/return site — never string-matched here.

    `avg_ms` is `null`, not 0, when no call in the group recorded a duration.

    `not_recorded` is the count of filtered turns with no `tool_calls` row at
    all: pre-instrumentation history and every cache hit. It is reported beside
    the bars, never inside them.
    """

    async def compute() -> Dict:
        params: List = []
        conds = f.chat_conds(params, "l.")
        where = _where(conds)
        rows = await q(
            f"""SELECT c.name,
                       count(*) FILTER (WHERE c.outcome = 'succeeded') AS succeeded,
                       count(*) FILTER (WHERE c.outcome = 'refused')   AS refused,
                       count(*) FILTER (WHERE c.outcome = 'failed')    AS failed,
                       count(*)                                        AS calls,
                       avg(c.duration_ms)                              AS avg_ms,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY c.duration_ms) AS p50
                  FROM tool_calls c JOIN chat_logs l ON l.id = c.turn_id
                  {where}
                 GROUP BY 1 ORDER BY calls DESC, name ASC""",
            *params,
        )
        bars = [
            {
                "name": r["name"],
                "succeeded": _i(r["succeeded"]),
                "refused": _i(r["refused"]),
                "failed": _i(r["failed"]),
                "calls": _i(r["calls"]),
                "avg_ms": _ms(r["avg_ms"]),
                "p50_ms": _ms(r["p50"]),
                # Denominator travels with the rate, always (§3).
                "success_rate": _rate(_i(r["succeeded"]), _i(r["calls"])),
            }
            for r in rows
        ]
        totals = {
            "succeeded": sum(b["succeeded"] for b in bars),
            "refused": sum(b["refused"] for b in bars),
            "failed": sum(b["failed"] for b in bars),
            "calls": sum(b["calls"] for b in bars),
        }
        return {
            "bars": bars,
            "totals": totals,
            "success_rate": _rate(totals["succeeded"], totals["calls"]),
            "not_recorded": await _not_recorded(f, "tool_calls"),
            "available": bool(bars),
        }

    # Deep-copied: the empty shape is a module constant with nested dicts, and
    # handing the same nested object to two requests is one careless mutation
    # away from a panel that "remembers" another caller's numbers.
    return await _section(compute, json.loads(json.dumps(_TOOL_OUTCOMES_EMPTY)))


# ---- LLM usage -------------------------------------------------------------

_LLM_USAGE_EMPTY: Dict = {"rows": [], "totals": {"calls": 0, "prompt_tokens": None,
                                                 "completion_tokens": None,
                                                 "cache_read_tokens": None,
                                                 "cache_creation_tokens": None,
                                                 "cost_usd": None,
                                                 "cost_is_estimated": False},
                          "not_recorded": None, "available": False}


@router.get("/analytics/llm-usage")
async def analytics_llm_usage(
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """Per model: calls, the four token counts, spend, and p50 time-to-first-token.

    **`cost_usd` is `null` when no price is configured — never `0.0`.** A zero
    reads as "this model is free" and nobody notices for months; a null renders
    as `—` and somebody asks. `priced_calls` is the denominator: it says how many
    of the calls in the row actually carried a cost, so a partial sum cannot pass
    itself off as the total spend.

    `cost_is_estimated` is true when ANY priced call in the group was estimated
    rather than reported by the provider. Derived is flagged as derived; it is
    never presented as measured.

    The `model` filter applies to `llm_calls.model` here, not to the turn's
    headline `chat_logs.model` — this endpoint's whole subject is the per-call
    model, and one turn can use several.
    """

    async def compute() -> Dict:
        params: List = []
        where = _where(_llm_call_conds(f, params))

        rows = await q(
            f"""SELECT c.model,
                       count(*)                       AS calls,
                       sum(c.prompt_tokens)           AS prompt_tokens,
                       sum(c.completion_tokens)       AS completion_tokens,
                       sum(c.reasoning_tokens)        AS reasoning_tokens,
                       sum(c.cache_read_tokens)       AS cache_read_tokens,
                       sum(c.cache_creation_tokens)   AS cache_creation_tokens,
                       sum(c.cost_usd)                AS cost_usd,
                       count(*) FILTER (WHERE c.cost_usd IS NOT NULL) AS priced_calls,
                       bool_or(c.cost_is_estimated) FILTER (WHERE c.cost_usd IS NOT NULL)
                                                      AS cost_is_estimated,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY c.ttft_ms)     AS p50_ttft,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY c.duration_ms) AS p50_dur
                  FROM llm_calls c JOIN chat_logs l ON l.id = c.turn_id
                  {where}
                 GROUP BY 1 ORDER BY calls DESC, model ASC NULLS LAST""",
            *params,
        )

        out = []
        for r in rows:
            priced = _i(r["priced_calls"])
            out.append({
                "model": r["model"],  # NULL stays NULL — "not recorded"
                "calls": _i(r["calls"]),
                "prompt_tokens": _int_or_none(r["prompt_tokens"]),
                "completion_tokens": _int_or_none(r["completion_tokens"]),
                "reasoning_tokens": _int_or_none(r["reasoning_tokens"]),
                "cache_read_tokens": _int_or_none(r["cache_read_tokens"]),
                "cache_creation_tokens": _int_or_none(r["cache_creation_tokens"]),
                # NULL sum stays NULL. sum() over all-NULL is NULL in Postgres,
                # which is exactly the answer we want to keep.
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                "priced_calls": priced,
                "cost_coverage": _rate(priced, _i(r["calls"])),
                "cost_is_estimated": bool(r["cost_is_estimated"]),
                "p50_ttft_ms": _ms(r["p50_ttft"]),
                "p50_duration_ms": _ms(r["p50_dur"]),
            })

        def _sum(field: str) -> Optional[int]:
            vals = [x[field] for x in out if x[field] is not None]
            return sum(vals) if vals else None

        costs = [x["cost_usd"] for x in out if x["cost_usd"] is not None]
        totals = {
            "calls": sum(x["calls"] for x in out),
            "prompt_tokens": _sum("prompt_tokens"),
            "completion_tokens": _sum("completion_tokens"),
            "cache_read_tokens": _sum("cache_read_tokens"),
            "cache_creation_tokens": _sum("cache_creation_tokens"),
            "cost_usd": round(sum(costs), 6) if costs else None,
            "cost_is_estimated": any(x["cost_is_estimated"] for x in out),
        }
        return {
            "rows": out,
            "totals": totals,
            "not_recorded": await _not_recorded(f, "llm_calls"),
            "available": bool(out),
        }

    return await _section(compute, json.loads(json.dumps(_LLM_USAGE_EMPTY)))


# ---- one turn, end to end --------------------------------------------------

_TRACE_TURN_COLS = (
    "id, ts, question, answer, lang, store_id, embed_id, session_id, model, "
    "tools, cached, path, latency_ms"
)


@router.get("/analytics/trace/{turn_id}")
async def analytics_trace(
    turn_id: int,
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """One turn with every tool call and LLM call interleaved in `seq` order.

    Store scope is enforced on the TURN, before anything else is read: a pinned
    caller asking for a sibling branch's turn id gets a 404, and gets it whether
    or not that turn exists — the alternative distinguishes "not yours" from
    "no such turn", which is itself a leak.

    `calls` is one flat list so the UI can render a single timeline. Every entry
    carries `kind` (`tool` | `llm`) and `seq`; the two tables number their own
    sequences independently, so ties are ordered tool-before-llm — the tool
    result is what the next model call consumes.

    A turn with no calls returns `calls: []` and `instrumented: false`, which is
    the truthful shape for the 122 pre-instrumentation turns: the turn happened,
    and nothing was written down about how.

    The shared filters apply here too (the console carries them across a
    drill-down), so a turn outside the current window is a 404 rather than a row
    that contradicts the page it was opened from. `rated` is skipped: it is a
    property of the answer, not a way to address a turn you already have the id
    of.
    """

    params: List = [turn_id]
    conds = ["id = $1"] + f.chat_conds(params, skip=("rated",))
    try:
        rows = await q(
            f"SELECT {_TRACE_TURN_COLS} FROM chat_logs {_where(conds)}", *params
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — no chat_logs at all
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="turn not found")

    turn = _log_row(rows[0])
    turn["latency_ms"] = _int_or_none(turn.get("latency_ms"))

    async def tools_section() -> List[Dict]:
        return [
            {
                "kind": "tool",
                "seq": _i(r["seq"]),
                "name": r["name"],
                "outcome": r["outcome"],
                "error_message": r["error_message"],
                "attempt": _i(r["attempt"]),
                "duration_ms": _int_or_none(r["duration_ms"]),
                "arguments": _json_obj(r["arguments"]),
                "ts": r["ts"],
            }
            for r in await q(
                """SELECT seq, name, outcome, error_message, attempt, duration_ms,
                          arguments, ts
                     FROM tool_calls WHERE turn_id = $1 ORDER BY seq""",
                turn_id,
            )
        ]

    async def llm_section() -> List[Dict]:
        return [
            {
                "kind": "llm",
                "seq": _i(r["seq"]),
                "model": r["model"],
                "prompt_tokens": _int_or_none(r["prompt_tokens"]),
                "completion_tokens": _int_or_none(r["completion_tokens"]),
                "reasoning_tokens": _int_or_none(r["reasoning_tokens"]),
                "cache_read_tokens": _int_or_none(r["cache_read_tokens"]),
                "cache_creation_tokens": _int_or_none(r["cache_creation_tokens"]),
                "ttft_ms": _int_or_none(r["ttft_ms"]),
                "duration_ms": _int_or_none(r["duration_ms"]),
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                "cost_is_estimated": bool(r["cost_is_estimated"]),
                "finish_reason": r["finish_reason"],
                "ts": r["ts"],
            }
            for r in await q(
                """SELECT seq, model, prompt_tokens, completion_tokens,
                          reasoning_tokens, cache_read_tokens, cache_creation_tokens,
                          ttft_ms, duration_ms, cost_usd, cost_is_estimated,
                          finish_reason, ts
                     FROM llm_calls WHERE turn_id = $1 ORDER BY seq""",
                turn_id,
            )
        ]

    tool_calls = await _section(tools_section, [])
    llm_calls = await _section(llm_section, [])
    calls = sorted(tool_calls + llm_calls,
                   key=lambda c: (c["seq"], 0 if c["kind"] == "tool" else 1))

    return {
        "turn": turn,
        "calls": calls,
        "tool_calls": len(tool_calls),
        "llm_calls": len(llm_calls),
        "instrumented": bool(calls),
        "store_scope": f.scope,
    }


# ---- diagnosis queue -------------------------------------------------------

_DIAGNOSIS_EMPTY: Dict = {"rows": [], "counts": {"failed_tool": 0,
                                                 "negative_feedback": 0,
                                                 "gave_up": 0, "both": 0},
                          "turns": None, "problem_rate": {"rate": None, "n": 0},
                          "available": False}


@router.get("/analytics/diagnosis")
async def analytics_diagnosis(
    limit: int = 100,
    issue: str = "",
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """Turns that went wrong: failed tools ∪ negative feedback ∪ gave-up.

    Three independent signals, deliberately not merged into one "bad" flag,
    because they fail in different places and a row carrying two of them is the
    highest-value item in the queue — `issue_type: 'both'`. `'both'` means *more
    than one signal*, and the row also carries the individual booleans so the UI
    never has to reverse-engineer which two.

    A **refused** tool call is not in here. Refusal is correct behaviour (§2);
    listing it as a defect is the exact mistake the three-state outcome exists to
    prevent.

    `gave_up` is only true where somebody evaluated the answer text; NULL means
    not evaluated and is not counted as "fine".

    `issue` optionally narrows to one bucket. It is validated against a fixed
    set, never reaches SQL as text, and is applied **inside** the query, before
    the LIMIT — filtering the page after slicing it would return "up to 100 rows
    of which some are the ones you asked for", which looks like an empty queue
    whenever the newest 100 problems are the other kind.
    """

    if issue not in ("", "failed_tool", "negative_feedback", "gave_up", "both"):
        raise HTTPException(status_code=400, detail="unknown `issue`")
    limit = min(max(limit, 1), 500)
    # Fixed expressions keyed by a validated literal — the caller's string never
    # reaches the SQL. `signals` is how many of the three fired; "both" is >1.
    issue_sql = {
        "": "TRUE",
        "both": "t.signals > 1",
        "failed_tool": "t.signals = 1 AND t.failed_tool",
        "negative_feedback": "t.signals = 1 AND t.negative_feedback",
        "gave_up": "t.signals = 1 AND t.gave_up",
    }[issue]

    async def compute() -> Dict:
        params: List = []
        conds = f.chat_conds(params, "l.", skip=("rated",))
        where = _where(conds)
        params.append(limit)
        limit_bind = f"${len(params)}"
        rows = await q(
            f"""WITH base AS (
                    SELECT l.id, l.ts, l.question, l.answer, l.lang, l.store_id,
                           l.path, l.model, l.latency_ms,
                           COALESCE(l.gave_up, false) AS gave_up,
                           EXISTS (SELECT 1 FROM tool_calls c
                                    WHERE c.turn_id = l.id AND c.outcome = 'failed')
                               AS failed_tool,
                           EXISTS (SELECT 1 FROM chat_feedback cf
                                    WHERE cf.verdict = 'down'
                                      AND cf.question = l.question
                                      AND cf.answer   = l.answer)
                               AS negative_feedback
                      FROM chat_logs l {where}
                ), t AS (
                    SELECT base.*,
                           failed_tool::int + negative_feedback::int
                             + gave_up::int AS signals
                      FROM base
                     WHERE failed_tool OR negative_feedback OR gave_up
                )
                SELECT t.*, ft.name AS failed_tool_name,
                       ft.error_message AS failed_tool_error
                  FROM t
                  LEFT JOIN LATERAL (
                        SELECT name, error_message FROM tool_calls c
                         WHERE c.turn_id = t.id AND c.outcome = 'failed'
                         ORDER BY seq LIMIT 1) ft ON true
                 WHERE {issue_sql}
                 ORDER BY t.ts DESC
                 LIMIT {limit_bind}""",
            *params,
        )

        # Counts are asked over the WHOLE filtered window, not over the page: a
        # queue that says "12 failed_tool" because that is what fitted on page 1
        # is a number that shrinks when you scroll, and the KPI above a list must
        # not disagree with the list's own total.
        c_params: List = []
        c_where = _where(f.chat_conds(c_params, "l.", skip=("rated",)))
        crow = (
            await q(
                f"""WITH base AS (
                        SELECT COALESCE(l.gave_up, false) AS gave_up,
                               EXISTS (SELECT 1 FROM tool_calls c
                                        WHERE c.turn_id = l.id
                                          AND c.outcome = 'failed') AS failed_tool,
                               EXISTS (SELECT 1 FROM chat_feedback cf
                                        WHERE cf.verdict = 'down'
                                          AND cf.question = l.question
                                          AND cf.answer   = l.answer)
                                   AS negative_feedback
                          FROM chat_logs l {c_where}
                    ), t AS (
                        SELECT *, failed_tool::int + negative_feedback::int
                                  + gave_up::int AS signals
                          FROM base
                         WHERE failed_tool OR negative_feedback OR gave_up
                    )
                    SELECT count(*) FILTER (WHERE signals > 1) AS both,
                           count(*) FILTER (WHERE signals = 1 AND failed_tool)
                               AS failed_tool,
                           count(*) FILTER (WHERE signals = 1 AND negative_feedback)
                               AS negative_feedback,
                           count(*) FILTER (WHERE signals = 1 AND gave_up)
                               AS gave_up
                      FROM t""",
                *c_params,
            )
        )[0]
        counts = {k: _i(crow[k]) for k in
                  ("failed_tool", "negative_feedback", "gave_up", "both")}

        out = []
        for r in rows:
            flags = {
                "failed_tool": bool(r["failed_tool"]),
                "negative_feedback": bool(r["negative_feedback"]),
                "gave_up": bool(r["gave_up"]),
            }
            live = [k for k, v in flags.items() if v]
            issue_type = "both" if len(live) > 1 else live[0]
            out.append({
                "turn_id": r["id"],
                "ts": r["ts"],
                "question": r["question"],
                "answer": r["answer"],
                "lang": r["lang"],
                "store_id": r["store_id"],
                "path": r["path"],
                "model": r["model"],
                "latency_ms": _int_or_none(r["latency_ms"]),
                "issue_type": issue_type,
                "signals": live,
                "failed_tool_name": r["failed_tool_name"],
                "failed_tool_error": r["failed_tool_error"],
                **flags,
            })

        turns = await _turn_count(f)
        return {
            "rows": out,
            "counts": counts,
            "turns": turns,
            # Denominator attached: "18 problem turns" means nothing without
            # knowing whether the window held 20 turns or 20,000.
            "problem_rate": _rate(sum(counts.values()), turns),
            "available": True,
        }

    return await _section(compute, json.loads(json.dumps(_DIAGNOSIS_EMPTY)))


# ---- who is using the console ----------------------------------------------

_ACTORS_EMPTY: Dict = {"rows": [], "scope_limited": False, "available": False}


def _actor_row(by_actor: Dict[str, Dict], actor, role) -> Dict:
    """Fetch-or-create one actor row with EVERY key already present.

    Both halves of this endpoint (turns, console events) go through here, so a
    row built by one half has the same key set as a row built by the other. A key
    that exists only on some rows forces the frontend to branch on its absence,
    which §6 forbids — and in practice it renders as a column that is blank for
    half the table with no way to tell blank from unknown.
    """

    row = by_actor.setdefault(actor or "", {
        "actor": actor,          # NULL stays NULL — an unattributed turn
        "role": role,
        "turns": 0,
        "cached_turns": 0,
        "p50_ms": None,
        "last_turn": None,
        "console_events": None,  # null, not 0: `app_events` may not be readable
        "distinct_actions": None,
        "last_event": None,
    })
    row["role"] = row["role"] or role
    return row


@router.get("/analytics/actors")
async def analytics_actors(
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """Console activity per actor: admin events plus the turns they asked.

    Two sources, joined in Python on the email: `app_events` (what an admin DID
    — see `app/activity.py`) and `chat_logs.actor_email` (what they ASKED).
    Neither is a superset of the other; an operator who only reads the dashboard
    has events and no turns, and a pharmacist asking through the console has
    turns and few events.

    **`app_events` has no branch column**, so it cannot be store-scoped. For a
    pinned caller the actor list is therefore built from their own branch's turns
    only, and `console_events` is returned as `null` with `scope_limited: true`
    rather than as a global figure the caller is not entitled to. Null-and-say-so
    beats a number that quietly spans other branches.
    """

    async def compute() -> Dict:
        params: List = []
        where = _where(f.chat_conds(params, "l."))
        # Grouped by email alone, with the role picked by max(): grouping by
        # (email, role) would split one person into two rows the day their role
        # changed, and the page would read as two half-active admins.
        turn_rows = await q(
            f"""SELECT l.actor_email AS actor, max(l.actor_role) AS role,
                       count(*) AS turns, max(l.ts) AS last_turn,
                       count(*) FILTER (WHERE l.cached) AS cached_turns,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms) AS p50
                  FROM chat_logs l {where}
                 GROUP BY 1""",
            *params,
        )

        by_actor: Dict[str, Dict] = {}
        for r in turn_rows:
            row = _actor_row(by_actor, r["actor"], r["role"])
            row["turns"] = _i(r["turns"])
            row["cached_turns"] = _i(r["cached_turns"])
            row["p50_ms"] = _ms(r["p50"])
            row["last_turn"] = r["last_turn"]

        # `app_events` half — omitted entirely for a pinned caller (see docstring).
        if not f.scope:
            ev_params: List = []
            ev_conds: List[str] = []
            if f.start:
                ev_params.append(f.start)
                ev_conds.append(f"ts >= ${len(ev_params)}::text::timestamptz")
            if f.end:
                ev_params.append(f.end)
                ev_conds.append(f"ts < ${len(ev_params)}::text::timestamptz")
            if f.actor:
                ors = []
                for tok in f.actor:
                    if tok == _NONE_TOKEN:
                        ors.append("actor_email IS NULL")
                    else:
                        ev_params.append(tok)
                        ors.append(f"actor_email = ${len(ev_params)}")
                ev_conds.append("(" + " OR ".join(ors) + ")")
            ev_rows = await _section(
                lambda: q(
                    f"""SELECT actor_email AS actor, max(actor_role) AS role,
                               count(*) AS events, max(ts) AS last_event,
                               count(DISTINCT action) AS distinct_actions
                          FROM app_events {_where(ev_conds)}
                         GROUP BY 1""",
                    *ev_params,
                ),
                [],
            )
            for r in ev_rows:
                row = _actor_row(by_actor, r["actor"], r["role"])
                row["console_events"] = _i(r["events"])
                row["last_event"] = r["last_event"]
                row["distinct_actions"] = _i(r["distinct_actions"])

        rows = sorted(
            by_actor.values(),
            key=lambda x: (x["turns"], x["console_events"] or 0),
            reverse=True,
        )
        return {
            "rows": rows,
            "scope_limited": bool(f.scope),
            "available": bool(rows),
        }

    return await _section(compute, dict(_ACTORS_EMPTY))


# ---- intents ---------------------------------------------------------------

# Keyword buckets, verbatim from the contract (§5). Crude, cheap, debuggable —
# and unlike an embedding cluster, an operator can read this list and predict
# what a given question will be counted as. The Burmese terms are not optional
# decoration: roughly half this product's traffic is Burmese, and an
# English-only bucket list would report that half as "other" and then get read
# as "nobody asks about price".
_INTENT_BUCKETS: Dict[str, List[str]] = {
    "stock":      ["stock", "available", "ရှိ", "ရှိလား", "in stock"],
    "price":      ["price", "cost", "how much", "ဈေး", "စျေး"],
    "substitute": ["substitute", "alternative", "instead", "အစား"],
    "branch":     ["branch", "store", "which shop", "ဆိုင်"],
    "dosage":     ["dose", "dosage", "how many", "mg", "သောက်"],
}

_INTENTS_EMPTY: Dict = {"buckets": [], "matrix": {"intents": [], "tools": [],
                                                  "cells": []},
                        "turns": None, "unclassified": None,
                        "not_recorded": None, "available": False}


def _intent_case(col: str, params: List) -> str:
    """A CASE expression labelling each row with its FIRST matching bucket.

    First match wins and the order is the dict's — a question containing both
    "price" and "stock" is counted once, under `stock`, not twice. Double
    counting would make the shares sum past 100% and the chart unreadable.

    Every keyword is a BOUND parameter; the only thing interpolated is the `$n`
    number and the bucket name, which is a key of a constant in this file.
    """

    whens = []
    for bucket, words in _INTENT_BUCKETS.items():
        ors = []
        for word in words:
            params.append(word)
            ors.append(f"{col} ILIKE '%'||${len(params)}||'%'")
        whens.append(f"WHEN {' OR '.join(ors)} THEN '{bucket}'")
    return "CASE " + " ".join(whens) + " ELSE 'other' END"


@router.get("/analytics/intents")
async def analytics_intents(
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """What people come here for, plus the intent x tool matrix for the heatmap.

    Buckets are keyword matches over the question text (§5) — see
    `_INTENT_BUCKETS`. Anything matching none of them is `other`; that bucket is
    reported as a real row rather than hidden, because a large `other` is the
    signal that the bucket list has gone stale.

    The matrix counts TOOL CALLS per intent from `tool_calls`, so a turn that
    called two tools contributes to two cells. `not_recorded` counts the turns
    with no `tool_calls` row: they contribute to the buckets (we know what was
    asked) and to no cell (we do not know what ran). Folding them into a cell
    would draw a heatmap over turns nobody instrumented.
    """

    async def buckets_section() -> Dict:
        params: List = []
        conds = f.chat_conds(params, "l.")
        case = _intent_case("l.question", params)
        rows = await q(
            f"""SELECT {case} AS bucket, count(*) AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms) AS p50
                  FROM chat_logs l {_where(conds)}
                 GROUP BY 1 ORDER BY n DESC""",
            *params,
        )
        total = sum(_i(r["n"]) for r in rows)
        return {
            "buckets": [
                {
                    "bucket": r["bucket"],
                    "n": _i(r["n"]),
                    "share": _rate(_i(r["n"]), total),
                    "p50_ms": _ms(r["p50"]),
                }
                for r in rows
            ],
            "turns": total,
            "unclassified": next(
                (_i(r["n"]) for r in rows if r["bucket"] == "other"), 0
            ),
        }

    async def matrix_section() -> Dict:
        params: List = []
        conds = f.chat_conds(params, "l.")
        case = _intent_case("l.question", params)
        rows = await q(
            f"""SELECT {case} AS bucket, c.name AS tool, count(*) AS n
                  FROM tool_calls c JOIN chat_logs l ON l.id = c.turn_id
                  {_where(conds)}
                 GROUP BY 1, 2 ORDER BY n DESC""",
            *params,
        )
        intents, tools, cells = [], [], []
        for r in rows:
            if r["bucket"] not in intents:
                intents.append(r["bucket"])
            if r["tool"] not in tools:
                tools.append(r["tool"])
            cells.append({"intent": r["bucket"], "tool": r["tool"],
                          "n": _i(r["n"])})
        return {"intents": intents, "tools": sorted(tools), "cells": cells}

    head = await _section(buckets_section,
                          {"buckets": [], "turns": None, "unclassified": None})
    matrix = await _section(matrix_section,
                            {"intents": [], "tools": [], "cells": []})
    return {
        "buckets": head["buckets"],
        "matrix": matrix,
        "turns": head["turns"],
        "unclassified": head["unclassified"],
        "not_recorded": await _not_recorded(f, "tool_calls"),
        "available": bool(head["buckets"]),
    }


# ---- activity feed ---------------------------------------------------------
#
# One merged, newest-first stream over the three event tables this system keeps:
# `app_events` (what an admin DID — see app/activity.py), `auth_events` (who
# signed in, and who failed to), and optionally `ingest_events` (what the data
# pipeline did). They were only ever readable one table at a time, so
# reconstructing an incident — a config change, then a wave of failed logins,
# then a bad file — meant reading three pages and merging them by eye.
#
# super_admin only. `auth_events` carries the email of every account somebody
# tried to sign in as, `app_events` carries the target of every admin action;
# together they are the closest thing here to a security log, and a branch
# manager has no business in it. It takes no store scope because none of the
# three tables has a branch column — the honest answer for a pinned admin is
# "not your page", which the 403 gives, not a silently empty feed.

# Normalised row shape. Each source maps onto exactly these columns and types so
# they can be UNIONed; nothing here interpolates caller input.
_ACT_APP = """
    SELECT ts, 'app'::text AS source, actor_email AS actor, action::text AS action,
           target::text AS target, status::int AS status, ip::text AS ip,
           COALESCE(detail, '{}'::jsonb)
             || jsonb_build_object('actor_role', actor_role, 'method', method,
                                   'path', path, 'duration_ms', duration_ms) AS detail
      FROM app_events
"""
# auth_events has no status code and its `detail` is free text, so it is lifted
# into an object rather than cast — a JSONB column and a message column are
# different things and squashing one into the other loses which it was.
_ACT_AUTH = """
    SELECT ts, 'auth'::text AS source, COALESCE(actor_email, email) AS actor,
           event::text AS action, email::text AS target, NULL::int AS status,
           ip::text AS ip,
           jsonb_build_object('detail', detail, 'email', email,
                              'actor_email', actor_email) AS detail
      FROM auth_events
"""
# `at`, not `ts`; no actor at all, because the pipeline is not a person. NULL
# actor stays NULL for the same reason a NULL path does.
_ACT_INGEST = """
    SELECT at AS ts, 'ingest'::text AS source, NULL::text AS actor,
           step::text AS action, file::text AS target, NULL::int AS status,
           NULL::text AS ip,
           jsonb_build_object('status', status, 'kind', kind, 'run_id', run_id,
                              'detail', detail, 'data', data) AS detail
      FROM ingest_events
"""
_ACT_SOURCES = (("app", "app_events", _ACT_APP),
                ("auth", "auth_events", _ACT_AUTH),
                ("ingest", "ingest_events", _ACT_INGEST))


async def _existing_tables(names: List[str]) -> set:
    """Which of these tables exist right now, via ``to_regclass``.

    Asked explicitly rather than by running the UNION and catching the error.
    A blanket ``except`` around the feed would turn any SQL fault — a typo, a
    permissions change, a column renamed by a migration — into "no activity",
    and an empty audit feed is the single most dangerous wrong answer this
    endpoint can give: it looks exactly like a quiet system.
    """

    try:
        rows = await q(
            "SELECT n AS name, to_regclass('public.' || n) AS oid"
            " FROM unnest($1::text[]) AS n",
            list(names),
        )
    except Exception:  # noqa: BLE001 — no database at all
        return set()
    return {r["name"] for r in rows if r["oid"] is not None}


async def _activity_union(wanted: List[str]) -> Tuple[str, List[str], List[str]]:
    """``(union_sql, included, unavailable)`` for the named sources.

    Factored out of the feed so the summary / trends / explore / audit endpoints
    read the SAME normalised rows. A second hand-written UNION would drift, and
    a KPI that disagrees with the list underneath it is worse than no KPI.
    """

    present = await _existing_tables([t for _, t, _ in _ACT_SOURCES])
    parts, included, unavailable = [], [], []
    for name, table, sql in _ACT_SOURCES:
        if name not in wanted:
            continue
        if table in present:
            parts.append(sql)
            included.append(name)
        else:
            unavailable.append(name)
    return " UNION ALL ".join(parts), included, unavailable


@router.get("/activity", dependencies=[Depends(require_super_admin)])
async def activity(
    frm: str = Query("", alias="from"),
    to: str = "",
    actor: str = "",
    source: str = "",
    action: str = "",
    q_text: str = Query("", alias="q"),
    tz: str = Query(DEFAULT_TZ, description="IANA zone for the date bounds (§A)"),
    limit: int = 100,
    offset: int = 0,
    include_ingest: bool = False,
) -> Dict:
    """Merged app + auth (+ optional ingest) events, newest first, with a real total.

    ``include_ingest`` defaults to **false**: one file run writes a dozen step
    rows, so folding the pipeline in by default buries the handful of human
    actions this page exists to show. Asking for ``source=ingest`` turns it on
    implicitly — filtering to a source you cannot see would otherwise return an
    empty page for a table full of rows.

    ``total`` is the count before ``limit``/``offset``, like
    ``/analytics/questions``, so the pager can say "page 3 of 40" instead of
    guessing.

    ``sources.unavailable`` names any table that does not exist on this database
    (a fresh install has no ``app_events`` until the first admin action). The
    feed degrades to the tables it has and SAYS SO, because "no activity" and
    "we cannot see that half of it" must never look the same.
    """

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    tz_v = _validate_tz(tz)

    wanted = ["app", "auth"] + (["ingest"] if (include_ingest or source == "ingest") else [])
    if source and source not in [s for s, _, _ in _ACT_SOURCES]:
        raise HTTPException(status_code=400, detail="`source` must be app, auth or ingest")

    union, included, unavailable = await _activity_union(wanted)

    if not union:
        return {
            "total": 0,
            "rows": [],
            "sources": {"included": [], "unavailable": unavailable},
        }

    params: List = []
    conds: List[str] = []
    frm_v, _ = _parse_ts(frm, "from")
    if frm_v:
        conds.append("e.ts >= " + _ts_expr(frm_v, params, tz_v))
    to_v, to_day = _parse_ts(to, "to")
    if to_v:
        # Same bare-date rule as `_log_filters`: `to=2026-08-13` means that day,
        # and `tz` says whose 13th.
        conds.append("e.ts < " + _ts_expr(to_v, params, tz_v, plus_day=to_day)
                     if to_day
                     else "e.ts <= " + _ts_expr(to_v, params, tz_v))
    if actor:
        params.append(actor)
        conds.append(f"e.actor ILIKE '%'||${len(params)}||'%'")
    if source:
        params.append(source)
        conds.append(f"e.source = ${len(params)}")
    if action:
        params.append(action)
        conds.append(f"e.action ILIKE '%'||${len(params)}||'%'")
    if q_text:
        params.append(q_text)
        n = len(params)
        conds.append(
            f"(COALESCE(e.actor,'') ILIKE '%'||${n}||'%'"
            f" OR COALESCE(e.action,'') ILIKE '%'||${n}||'%'"
            f" OR COALESCE(e.target,'') ILIKE '%'||${n}||'%'"
            f" OR COALESCE(e.detail::text,'') ILIKE '%'||${n}||'%')"
        )
    where = _where(conds)

    total = _i(
        (await q(f"SELECT count(*) AS n FROM ({union}) e {where}", *params))[0]["n"]
    )
    page = list(params)
    page.append(limit)
    page.append(offset)
    rows = await q(
        f"""SELECT e.* FROM ({union}) e {where}
             ORDER BY e.ts DESC NULLS LAST
             LIMIT ${len(page)-1} OFFSET ${len(page)}""",
        *page,
    )

    return {
        "total": total,
        "rows": [
            {
                "ts": r["ts"],
                "source": r["source"],
                "actor": r["actor"],       # NULL for the pipeline — not a person
                "action": r["action"],
                "target": r["target"],
                "status": _int_or_none(r["status"]),
                "ip": r["ip"],
                "detail": _json_obj(r["detail"]),
            }
            for r in rows
        ],
        "sources": {"included": included, "unavailable": unavailable},
    }


# ---- console v2 · the activity console (addendum §C) ------------------------
#
# Four endpoints over the SAME normalised union the feed reads (`_activity_union`),
# so a KPI, a trend line and the row list underneath them cannot disagree about
# what happened. They are super_admin only for the reason the feed is: none of
# the three source tables has a branch column, so there is no honest way to scope
# them, and "not your page" is a better answer than a silently partial one.
#
# Everything in the block above applies here too, and two rules are added:
#
# * **Every KPI ships its movement** (§B). `_kpi` pairs a value with the same
#   measurement over the immediately preceding window of the same length, and
#   returns nulls — never `0` — when there is no prior window to compare with.
# * **The pivot builds SQL from a fixed map** (§C). `measure` and `by` are keys
#   into `_EXPLORE_MEASURES` / `_EXPLORE_DIMS`; an unknown one is a 400 and the
#   caller's string never reaches the query text.

_ACT_SOURCE_NAMES = tuple(s for s, _, _ in _ACT_SOURCES)

# rollup -> (date_trunc unit, to_char format, series step, max span)
# The span caps how many points one request can generate, exactly as
# `_BUCKETS_TS` does: hourly over a year is 8,760 rows nobody can read.
_ROLLUPS = {
    "hour":  ("hour",  "YYYY-MM-DD HH24:00", "1 hour",  "60 days"),
    "day":   ("day",   "YYYY-MM-DD",         "1 day",   "1100 days"),
    "week":  ("week",  "YYYY-MM-DD",         "1 week",  "5200 days"),
    "month": ("month", "YYYY-MM",            "1 month", "5200 days"),
}

# The TestClient's client host. Every audit row a pytest run writes carries it
# and no real request can (a real one carries an address), so it is the one
# reliable way to keep suite debris out of a traffic number. See
# `tests/conftest.py`, which documents the 144 rows that reached production.
_TESTCLIENT_IP = "testclient"


class ActivityFilters:
    """Every filter the activity console sends, declared exactly once.

    Same reasoning as :class:`AnalyticsFilters`: FastAPI drops an undeclared
    query parameter without a word and the endpoint then answers 200 with
    unfiltered rows under a chip that says otherwise. Three bugs of that exact
    shape have been found in this file already.
    """

    __slots__ = ("start", "end", "end_is_day", "tz", "actor", "source", "action",
                 "text", "ip", "include_ingest")

    def __init__(self, start="", end="", end_is_day=False, tz=DEFAULT_TZ,
                 actor="", source="", action="", text="", ip="",
                 include_ingest=False):
        self.start = start
        self.end = end
        self.end_is_day = end_is_day
        self.tz = tz
        self.actor = actor
        self.source = source
        self.action = action
        self.text = text
        self.ip = ip
        self.include_ingest = include_ingest

    # -- the sources this request wants ---------------------------------------
    def wanted(self) -> List[str]:
        """app + auth always; ingest only when asked for.

        One file run writes a dozen step rows, so folding the pipeline in by
        default buries the handful of human actions this console exists to show.
        Filtering to `source=ingest` turns it on implicitly — otherwise the
        answer to "show me the pipeline" is an empty page over a full table.
        """

        extra = ["ingest"] if (self.include_ingest or self.source == "ingest") else []
        return ["app", "auth"] + extra

    def shifted(self, start: str, end: str) -> "ActivityFilters":
        return ActivityFilters(start=start, end=end, end_is_day=False, tz=self.tz,
                               actor=self.actor, source=self.source,
                               action=self.action, text=self.text, ip=self.ip,
                               include_ingest=self.include_ingest)

    def prev_window(self) -> Optional[Tuple[str, str]]:
        return _prev_window(self.start, self.end, self.tz)

    def prev_period(self) -> Optional[Dict]:
        w = self.prev_window()
        return {"start": w[0], "end": w[1]} if w else None

    # -- predicates over the union, aliased `e` --------------------------------
    def conds(self, params: List) -> List[str]:
        conds: List[str] = []
        if self.start:
            conds.append("e.ts >= " + _ts_expr(self.start, params, self.tz))
        if self.end:
            conds.append("e.ts < " + _ts_expr(self.end, params, self.tz,
                                              plus_day=self.end_is_day))
        if self.actor:
            params.append(self.actor)
            conds.append(f"e.actor ILIKE '%'||${len(params)}||'%'")
        if self.source:
            params.append(self.source)
            conds.append(f"e.source = ${len(params)}")
        if self.action:
            params.append(self.action)
            conds.append(f"e.action ILIKE '%'||${len(params)}||'%'")
        if self.ip:
            params.append(self.ip)
            conds.append(f"e.ip = ${len(params)}")
        if self.text:
            params.append(self.text)
            n = len(params)
            conds.append(
                f"(COALESCE(e.actor,'') ILIKE '%'||${n}||'%'"
                f" OR COALESCE(e.action,'') ILIKE '%'||${n}||'%'"
                f" OR COALESCE(e.target,'') ILIKE '%'||${n}||'%'"
                f" OR COALESCE(e.detail::text,'') ILIKE '%'||${n}||'%')"
            )
        return conds

    # -- window-only predicates, for a table the union does not cover ---------
    def window_conds(self, params: List, col: str = "ts") -> List[str]:
        conds: List[str] = []
        if self.start:
            conds.append(f"{col} >= " + _ts_expr(self.start, params, self.tz))
        if self.end:
            conds.append(f"{col} < " + _ts_expr(self.end, params, self.tz,
                                                plus_day=self.end_is_day))
        return conds

    def honours_all(self) -> bool:
        """Whether a window-only block can claim to have applied every filter.

        `ingest_events` has no actor, no ip and no source of its own, so a block
        reading it cannot obey those chips. §5: a number sitting under a filter
        it silently ignores is the same lie as an undeclared parameter, just
        further from the wire — so the block says `filters_applied: false` and
        the UI marks it unfiltered.
        """

        return not (self.actor or self.action or self.text or self.ip
                    or self.source)


async def activity_filters(
    start: str = Query("", description="ISO8601, inclusive (contract §4)"),
    end: str = Query("", description="ISO8601; a bare date includes the whole day"),
    frm: str = Query("", alias="from", description="legacy spelling of `start`"),
    to: str = Query("", description="legacy spelling of `end`"),
    tz: str = Query(DEFAULT_TZ,
                    description="IANA zone for buckets AND date bounds (§A)"),
    actor: str = Query("", description="substring match on the actor email"),
    source: str = Query("", description="app|auth|ingest"),
    action: str = Query("", description="substring match on the action"),
    q_text: str = Query("", alias="q", description="free text over the row"),
    ip: str = Query("", description="exact client address"),
    include_ingest: bool = Query(False),
) -> ActivityFilters:
    """Declare — once — every activity console query parameter."""

    tz_v = _validate_tz(tz)
    if source and source not in _ACT_SOURCE_NAMES:
        raise HTTPException(status_code=400,
                            detail="`source` must be app, auth or ingest")

    start_v, _ = _parse_ts(start, "start")
    end_v, end_day = _parse_ts(end, "end")
    frm_v, _ = _parse_ts(frm, "from")
    to_v, to_day = _parse_ts(to, "to")

    # Both spellings mean the same thing (§4 amended); sending both with
    # DIFFERENT values is a caller bug and is answered with a 400 rather than by
    # quietly preferring one — a window narrower than the one on screen is
    # unfalsifiable from outside.
    if frm_v and start_v and frm_v != start_v:
        raise HTTPException(
            status_code=400,
            detail="`from` and `start` were both sent with different values")
    if to_v and end_v and to_v != end_v:
        raise HTTPException(
            status_code=400,
            detail="`to` and `end` were both sent with different values")
    if not start_v and frm_v:
        start_v = frm_v
    if not end_v and to_v:
        end_v, end_day = to_v, to_day

    return ActivityFilters(start=start_v, end=end_v, end_is_day=end_day, tz=tz_v,
                           actor=actor, source=source, action=action,
                           text=q_text, ip=ip, include_ingest=include_ingest)


def _window_payload(f: ActivityFilters) -> Dict:
    """What the header prints: the window, its zone, and the window it moved from.

    The zone is ALSO echoed at the top level of every one of these payloads, not
    only in here (§F1). That duplication is deliberate: the UI reads the chip
    from a top-level `tz`, and burying the only copy one level down makes an
    endpoint that forgot to declare the parameter indistinguishable from one that
    honoured it — which is the bug §F1 exists to make visible.
    """

    return {"start": f.start or None, "end": f.end or None, "tz": f.tz,
            "prev_period": f.prev_period()}


def _axis_bounds(f: ActivityFilters, params: List) -> Tuple[str, str]:
    """`(lo, hi)` as NAIVE LOCAL timestamps for the zero-fill, NULL when unpinned.

    ``hi`` is one second inside the window's exclusive end, so the LAST bucket
    drawn is the one the window actually reaches into. Using the exclusive
    instant itself adds an empty bucket at the right edge of every chart whose
    end lands on a boundary — which reads as "traffic stopped", the exact
    misreading zero-fill exists to prevent.
    """

    lo = _local_bound_expr(f.start, params, f.tz)
    hi_raw = _local_bound_expr(f.end, params, f.tz)
    day = " + interval '1 day'" if f.end_is_day else ""
    return lo, f"({hi_raw}{day} - interval '1 second')"


def _rollup(name: str) -> Tuple[str, str, str, str]:
    if name not in _ROLLUPS:
        raise HTTPException(
            status_code=400,
            detail="`rollup` must be one of: " + ", ".join(sorted(_ROLLUPS)))
    return _ROLLUPS[name]


# ---- activity: the KPI row -------------------------------------------------

_EMPTY_KPI: Dict = {"value": None, "prev": None, "delta": None,
                    "delta_pct": None, "prev_period": None}


def _empty_kpi(prev_period: Optional[Dict] = None) -> Dict:
    k = dict(_EMPTY_KPI)
    k["prev_period"] = prev_period
    return k


_STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")

# The auth events this console reports on, in the order the funnel reads.
_SIGNIN_EVENTS = ("login_ok", "login_fail", "login_locked", "login_blocked",
                  "sso_ok", "sso_fail")


async def _act_counts(f: ActivityFilters, union: str) -> Optional[Dict]:
    """One pass over the union: events, client split, status classes."""

    if not union:
        return None
    params: List = []
    where = _where(f.conds(params))
    params.append(_TESTCLIENT_IP)
    tc = f"${len(params)}"
    classes = " ".join(
        f"count(*) FILTER (WHERE e.status >= {int(c[0]) * 100}"
        f" AND e.status < {(int(c[0]) + 1) * 100}) AS s{c},"
        for c in _STATUS_CLASSES
    )
    rows = await q(
        f"""SELECT count(*) AS events,
                   count(*) FILTER (WHERE e.ip = {tc})            AS testclient,
                   count(*) FILTER (WHERE e.ip IS NOT NULL
                                      AND e.ip <> {tc})           AS browser,
                   count(*) FILTER (WHERE e.ip IS NULL)           AS no_client,
                   count(*) FILTER (WHERE e.status IS NOT NULL)   AS statused,
                   count(DISTINCT e.actor)                        AS distinct_actors,
                   {classes}
                   count(*) FILTER (WHERE e.status >= 400)        AS failed
              FROM ({union}) e {where}""",
        *params,
    )
    return dict(rows[0]) if rows else None


async def _act_status_codes(f: ActivityFilters, union: str) -> Dict[str, int]:
    """403s and their siblings, per EXACT code rather than per class.

    A class tells you something failed; the code tells you which conversation to
    have. 403 is an authorisation wall, 422 is a file the validator turned away,
    500 is ours. Rolled into `4xx` the first two are indistinguishable, and they
    lead to completely different places.
    """

    if not union:
        return {}
    params: List = []
    conds = f.conds(params)
    conds.append("e.status IS NOT NULL")
    rows = await q(
        f"SELECT e.status AS code, count(*) AS n FROM ({union}) e {_where(conds)}"
        f" GROUP BY 1 ORDER BY 1",
        *params,
    )
    return {str(_i(r["code"])): _i(r["n"]) for r in rows}


async def _act_signins(f: ActivityFilters) -> Optional[Dict]:
    """Sign-in outcomes, read straight from `auth_events`.

    Not from the union: the union normalises `auth_events.detail` (free text)
    into a JSONB object, and this block wants the outcome counts rather than the
    rows. Reading the table directly also means a database with no `app_events`
    still gets its sign-in numbers.
    """

    params: List = []
    conds = f.window_conds(params)
    if f.actor:
        params.append(f.actor)
        n = len(params)
        conds.append(f"(COALESCE(actor_email,'') ILIKE '%'||${n}||'%'"
                     f" OR COALESCE(email,'') ILIKE '%'||${n}||'%')")
    if f.ip:
        params.append(f.ip)
        conds.append(f"ip = ${len(params)}")
    filters = " ".join(
        f"count(*) FILTER (WHERE event = '{ev}') AS {ev}," for ev in _SIGNIN_EVENTS
    )
    rows = await q(
        f"""SELECT {filters} count(*) AS attempts
              FROM auth_events {_where(conds)}""",
        *params,
    )
    return dict(rows[0]) if rows else None


async def _act_set_aside(f: ActivityFilters) -> Optional[int]:
    """Pipeline attempts moved to `failed/` in the window."""

    params: List = []
    conds = f.window_conds(params, "at")
    conds.append("step = 'set_aside'")
    rows = await q(
        f"SELECT count(DISTINCT run_id) AS n FROM ingest_events {_where(conds)}",
        *params,
    )
    return _i(rows[0]["n"]) if rows else None


async def _act_arrived(f: ActivityFilters) -> Optional[int]:
    """Pipeline attempts that ARRIVED in the window — the set-aside denominator.

    "190 files set aside" is a number nobody can act on; "190 of 573" is. Counted
    the same way as the funnel and as `_act_set_aside` — distinct `run_id`, one
    attempt at one file — because a rate whose numerator and denominator count
    different things is worse than no rate at all.
    """

    params: List = []
    conds = f.window_conds(params, "at")
    conds.append("step = 'arrived'")
    rows = await q(
        f"SELECT count(DISTINCT run_id) AS n FROM ingest_events {_where(conds)}",
        *params,
    )
    return _i(rows[0]["n"]) if rows else None


# The empty shapes for these four endpoints are built INLINE, next to the
# section that can fail, rather than declared as module constants like
# `_TOOL_OUTCOMES_EMPTY` above. They have to be: an empty KPI still carries
# `prev_period`, which is a property of the REQUEST, and a constant would hand
# the UI a null previous window on a request that pinned one — the same absent-
# as-measurement mistake the constants exist to prevent, inverted.


@router.get("/activity/summary", dependencies=[Depends(require_super_admin)])
async def activity_summary(f: ActivityFilters = Depends(activity_filters)) -> Dict:
    """The feed's KPI row: volume, who it came from, what failed, who signed in.

    Every number carries its movement against the immediately preceding window
    of the same length (§B), **absolute and percentage both**. With no window
    pinned there is no prior period, and `delta`/`delta_pct` are `null` rather
    than `0` — the UI prints "no prior period", because `0%` reads as "no change"
    and is the wrong answer to a question that was never asked.

    `browser` vs `testclient` splits on the client address. It is not cosmetic:
    a pytest run writes audit rows with `ip = 'testclient'`, 144 of them once
    reached a production table and out-ranked the only real admin on the
    instance. A traffic number that cannot tell those apart is measuring itself.

    Sections are isolated (§6): a database with no `auth_events` loses the
    sign-in block and keeps the rest, each empty block keeping the shape of the
    real one so the frontend never branches on a missing key.
    """

    prev = f.prev_window()
    prev_period = f.prev_period()
    union, included, unavailable = await _activity_union(f.wanted())

    def kpi(cur, was):
        return _kpi(cur, was, prev_period)

    async def volume_section() -> Dict:
        cur = await _act_counts(f, union)
        was = await _act_counts(f.shifted(*prev), union) if prev else None
        if cur is None:
            raise RuntimeError("no readable activity source")

        def pair(key):
            return kpi(_i(cur[key]), _i(was[key]) if was else None)

        statused = _i(cur["statused"])
        return {
            "events": pair("events"),
            "distinct_actors": pair("distinct_actors"),
            "clients": {"browser": pair("browser"),
                        "testclient": pair("testclient"),
                        "no_client": pair("no_client"),
                        "n": _i(cur["events"])},
            "failures": {
                "failed": pair("failed"),
                # Only rows that HAVE a status can be a failure or not. auth and
                # ingest rows carry none, and counting them as successes would
                # dilute the rate with events that were never HTTP at all.
                "classes": [{"class": c, "n": _i(cur[f"s{c}"])}
                            for c in _STATUS_CLASSES],
                "by_status": await _act_status_codes(f, union),
                "rate": _rate(_i(cur["failed"]), statused),
            },
        }

    async def by_source_section() -> List[Dict]:
        if not union:
            return []
        params: List = []
        where = _where(f.conds(params))
        rows = await q(
            f"SELECT e.source, count(*) AS n FROM ({union}) e {where}"
            f" GROUP BY 1 ORDER BY n DESC",
            *params,
        )
        return [{"source": r["source"], "n": _i(r["n"])} for r in rows]

    async def signin_section() -> Dict:
        cur = await _act_signins(f)
        was = await _act_signins(f.shifted(*prev)) if prev else None
        if cur is None:
            raise RuntimeError("auth_events unreadable")
        out = {ev: kpi(_i(cur[ev]), _i(was[ev]) if was else None)
               for ev in _SIGNIN_EVENTS}
        succeeded = _i(cur["login_ok"]) + _i(cur["sso_ok"])
        out["attempts"] = kpi(_i(cur["attempts"]),
                              _i(was["attempts"]) if was else None)
        out["success_rate"] = _rate(succeeded, _i(cur["attempts"]))
        # `auth_events` has an email and an ip and nothing else this console
        # filters on: no `action` of its own (the event name is the outcome, not
        # an admin action), no `source`, and its `detail` is free text rather
        # than the searchable object the union builds. §5: say so rather than let
        # the number sit under a chip it ignores.
        out["filters_applied"] = not (f.action or f.source or f.text)
        return out

    async def set_aside_section() -> Dict:
        cur = await _act_set_aside(f)
        was = await _act_set_aside(f.shifted(*prev)) if prev else None
        out = kpi(cur, was)
        # A rate ships with its denominator (§3). "190 set aside" is a number
        # nobody can act on; "190 of 573 arrived" is.
        arrived = await _act_arrived(f)
        out["arrived"] = arrived
        out["of_arrived"] = _rate(cur, arrived)
        # Counted in pipeline ATTEMPTS, like the funnel: a file that failed on
        # Monday and loaded on Tuesday is two attempts, and collapsing it to one
        # hides the retry somebody opened this page to find.
        out["unit"] = "run"
        # `ingest_events` is the pipeline's own log: no actor, no ip, no source.
        out["filters_applied"] = f.honours_all()
        return out

    volume = await _section(volume_section,
                            {"events": _empty_kpi(prev_period),
                             "distinct_actors": _empty_kpi(prev_period),
                             "clients": {"browser": _empty_kpi(prev_period),
                                         "testclient": _empty_kpi(prev_period),
                                         "no_client": _empty_kpi(prev_period),
                                         "n": None},
                             "failures": {"failed": _empty_kpi(prev_period),
                                          "classes": [], "by_status": {},
                                          "rate": {"rate": None, "n": 0}}})
    empty_signins = {ev: _empty_kpi(prev_period) for ev in _SIGNIN_EVENTS}
    empty_signins.update({"attempts": _empty_kpi(prev_period),
                          "success_rate": {"rate": None, "n": 0},
                          "filters_applied": False})
    return {
        # §F1: the zone actually used, at the top level, on every response.
        "tz": f.tz,
        "window": _window_payload(f),
        "events": volume["events"],
        "distinct_actors": volume["distinct_actors"],
        "by_source": await _section(by_source_section, []),
        "clients": volume["clients"],
        "failures": volume["failures"],
        "signins": await _section(signin_section, empty_signins),
        "files_set_aside": await _section(
            set_aside_section, {**_empty_kpi(prev_period), "arrived": None,
                                "of_arrived": {"rate": None, "n": 0},
                                "unit": "run", "filters_applied": False}),
        "sources": {"included": included, "unavailable": unavailable},
        "available": volume["events"]["value"] is not None,
    }


# ---- activity: trends + movers ---------------------------------------------
#
# `measure` selects which quantity the comparison line and the heatmap are cut
# for. Same fixed-map treatment as `/explore`: a key into a map defined here,
# never a caller's string reaching the SQL. Every one of these is also a column
# on every `series` row, so switching measure is a cheap re-request rather than
# a different shape.
_TREND_MEASURES: Dict[str, str] = {
    "events": "count(*)",
    "failed": "count(*) FILTER (WHERE e.status >= 400)",
    "app":    "count(*) FILTER (WHERE e.source = 'app')",
    "auth":   "count(*) FILTER (WHERE e.source = 'auth')",
    "ingest": "count(*) FILTER (WHERE e.source = 'ingest')",
}

# The heatmap's COLUMNS: 24 hours, the conventional hour-of-day grid.
#
# Six-hour bands were tried and reverted. The panel exists to answer "when does
# the load happen", and bands answer "mornings" — which everybody already knows,
# so the panel earns nothing. Hourly answers "09:00", which is a number somebody
# can staff or warm a cache against. On a retail pharmacy 00-06 is near-empty
# too, so bands are really three useful buckets: a bar chart drawn as a heatmap.
#
# The argument for bands was that 24 is too many to read. That is true of 24
# ROWS and not of 24 COLUMNS, and it originally rode in on a reported card
# overflow that turned out not to exist — the grid scrolls, and the screenshot
# that showed it clipped was captured with `--hide-scrollbars`.
#
# Fixed 0..23 rather than derived from the data, for the same reason the series
# is zero-filled: an hour nothing landed in must still be a column, or the grid
# silently re-numbers itself and two days stop being comparable at the same x.
_HOURS = tuple(range(24))


@router.get("/activity/trends", dependencies=[Depends(require_super_admin)])
async def activity_trends(
    f: ActivityFilters = Depends(activity_filters),
    rollup: str = Query("day", description="hour|day|week|month"),
    measure: str = Query("events", description="events|failed|app|auth|ingest"),
    top: int = Query(8, ge=1, le=50, description="how many movers to rank"),
) -> Dict:
    """A zero-filled series over the window, plus the actions that MOVED.

    ``measure`` selects which column the comparison line is computed FOR, and is
    echoed back. Every column is always present on every row, so the caller can
    switch cheaply — but ``previous`` is one series, and it has to be the same
    quantity as the line it sits under. A `failed` line against an `events`
    ghost line compares two different things and looks entirely plausible doing
    it, which is why the measure is declared here rather than left to the
    frontend to pick after the fact.

    The series is zero-filled for the reason `/analytics/timeseries` is: a bucket
    with no rows comes back absent from `GROUP BY`, every chart library joins the
    point before it to the point after it, and the reader sees steady activity
    across the hours the service was down.

    Movers rank by absolute change against the previous window, not by volume: a
    login_fail count that went 2 -> 40 is the row worth surfacing even though
    some other action ran 4,000 times in both. Each carries `spark` (its own
    per-bucket counts) and BOTH numbers — `delta` and `delta_pct` — because a
    rise from 1 to 4 is +3, and calling it 300% is noise.

    An action seen only in the previous window is ranked too, with `n: 0`. Those
    are disappearances, which is the half of "what changed" a top-N over the
    current window alone can never show.
    """

    if measure not in _TREND_MEASURES:
        raise HTTPException(
            status_code=400,
            detail="`measure` must be one of: " + ", ".join(sorted(_TREND_MEASURES)))
    unit, fmt, step, span = _rollup(rollup)
    prev = f.prev_window()
    prev_period = f.prev_period()
    union, included, unavailable = await _activity_union(f.wanted())

    async def series_section() -> List[Dict]:
        if not union:
            raise RuntimeError("no readable activity source")
        params: List = []
        where = _where(f.conds(params))
        bucket = _bucket_expr("e.ts", unit, f.tz, params)
        lo, hi = _axis_bounds(f, params)
        rows = await q(
            f"""WITH agg AS (
                    SELECT {bucket} AS b, count(*) AS events,
                           count(*) FILTER (WHERE e.source = 'app')  AS app,
                           count(*) FILTER (WHERE e.source = 'auth') AS auth,
                           count(*) FILTER (WHERE e.source = 'ingest') AS ingest,
                           count(*) FILTER (WHERE e.status >= 400)   AS failed
                      FROM ({union}) e {where}
                     GROUP BY 1
                ),
                bounds AS (
                    SELECT COALESCE(date_trunc('{unit}', {lo}),
                                    (SELECT min(b) FROM agg)) AS lo,
                           COALESCE(date_trunc('{unit}', {hi}),
                                    (SELECT max(b) FROM agg)) AS hi
                ),
                axis AS (
                    SELECT generate_series(lo, LEAST(hi, lo + interval '{span}'),
                                           interval '{step}') AS b
                      FROM bounds
                     WHERE lo IS NOT NULL AND hi IS NOT NULL AND hi >= lo
                )
                SELECT to_char(x.b, '{fmt}')  AS t,
                       COALESCE(a.events, 0)  AS events,
                       COALESCE(a.app, 0)     AS app,
                       COALESCE(a.auth, 0)    AS auth,
                       COALESCE(a.ingest, 0)  AS ingest,
                       COALESCE(a.failed, 0)  AS failed
                  FROM axis x LEFT JOIN agg a ON a.b = x.b
                 ORDER BY x.b""",
            *params,
        )
        return [{"t": r["t"], "events": _i(r["events"]), "app": _i(r["app"]),
                 "auth": _i(r["auth"]), "ingest": _i(r["ingest"]),
                 "failed": _i(r["failed"])} for r in rows]

    async def movers_section() -> List[Dict]:
        if not union:
            raise RuntimeError("no readable activity source")

        async def by_action(win: ActivityFilters) -> Dict[str, int]:
            params: List = []
            where = _where(win.conds(params))
            rows = await q(
                f"SELECT COALESCE(e.action,'') AS k, count(*) AS n"
                f" FROM ({union}) e {where} GROUP BY 1",
                *params,
            )
            return {r["k"]: _i(r["n"]) for r in rows}

        cur = await by_action(f)
        was = await by_action(f.shifted(*prev)) if prev else {}

        keys = sorted(
            set(cur) | set(was),
            key=lambda k: (abs(cur.get(k, 0) - was.get(k, 0)) if prev
                           else cur.get(k, 0), cur.get(k, 0)),
            reverse=True,
        )[:top]
        if not keys:
            return []

        # Sparklines for the ranked keys only — one query, one bind for the whole
        # list, rather than N round trips.
        params: List = []
        conds = f.conds(params)
        params.append(list(keys))
        conds.append(f"COALESCE(e.action,'') = ANY(${len(params)}::text[])")
        bucket = _bucket_expr("e.ts", unit, f.tz, params)
        spark_rows = await q(
            f"""SELECT COALESCE(e.action,'') AS k, to_char({bucket}, '{fmt}') AS t,
                       count(*) AS n
                  FROM ({union}) e {_where(conds)}
                 GROUP BY 1, 2 ORDER BY 2""",
            *params,
        )
        spark: Dict[str, List[Dict]] = {}
        for r in spark_rows:
            spark.setdefault(r["k"], []).append({"t": r["t"], "n": _i(r["n"])})

        out = []
        for k in keys:
            n = cur.get(k, 0)
            # `prev` absent means no prior window at all, which is NOT the same
            # as a prior window in which this action did not occur. The first is
            # null; the second is a real 0 and a real delta.
            p = was.get(k, 0) if prev else None
            row = _kpi(n, p, prev_period)
            row["key"] = k or None      # NULL action stays NULL — never ""
            row["n"] = n
            row["spark"] = spark.get(k, [])
            out.append(row)
        return out

    async def previous_section() -> Dict:
        """The preceding window's own series, for the ghost line under the chart.

        Aligned by POSITION, not by label: the previous window's buckets carry
        different dates, so joining on the label would match nothing and draw an
        empty line. Position is what "the same point one window ago" means.

        Truncated or padded to the current series' length, because a window that
        crosses a DST boundary or a month end genuinely has a different number of
        buckets, and a ghost line one point longer than the chart it sits under
        renders as a spike at the edge.
        """

        if not prev or not union:
            return {"label": None, "measure": measure, "values": []}
        win = f.shifted(*prev)
        params: List = []
        where = _where(win.conds(params))
        bucket = _bucket_expr("e.ts", unit, win.tz, params)
        # Cut for the SELECTED measure, not always for `events`. A `failed` line
        # drawn against an `events` ghost compares two different quantities and
        # looks entirely plausible doing it.
        rows = await q(
            f"""SELECT {bucket} AS b, {_TREND_MEASURES[measure]} AS n
                  FROM ({union}) e {where}
                 GROUP BY 1 ORDER BY 1""",
            *params,
        )
        values = [_i(r["n"]) for r in rows]
        want = len(series)
        return {"label": "previous period", "measure": measure,
                "values": (values + [0] * want)[:want] if want else values}

    async def heatmap_section() -> Dict:
        """When it happens: hour-of-day (cols) x day (rows), in the caller's zone.

        Computed HERE rather than derived in the browser from an hourly rollup.
        "Hour of day" out of a serialised bucket label means re-parsing a
        timestamp whose zone handling is the exact thing this round fixed — the
        same defect one layer further from the data. `date_trunc('hour', ts AT
        TIME ZONE $tz)` shares its midnight with every other panel on the
        endpoint by construction, so the matrix and the series cannot disagree
        about which day a 01:00 event belongs to.

        An hour with no events inside the window is a measured `0`, not null:
        the query ran and looked. That is the opposite of the rule for a RATE,
        where an empty bucket has no denominator and must stay a gap — a count
        of zero is a fact, a rate over zero is not.
        """

        if not union:
            raise RuntimeError("no readable activity source")
        params: List = []
        where = _where(f.conds(params))
        params.append(f.tz)
        p_tz = f"${len(params)}"
        local = f"(e.ts AT TIME ZONE {p_tz}::text)"
        rows = await q(
            f"""SELECT to_char(date_trunc('day', {local}), 'YYYY-MM-DD') AS d,
                       to_char(date_trunc('day', {local}), 'DD Mon')     AS lbl,
                       extract(hour FROM date_trunc('hour', {local}))::int AS h,
                       {_TREND_MEASURES[measure]}                        AS n
                  FROM ({union}) e {where}
                 GROUP BY 1, 2, 3 ORDER BY 1, 3""",
            *params,
        )
        by_day: Dict[str, Dict[int, int]] = {}
        labels: Dict[str, str] = {}
        for r in rows:
            by_day.setdefault(r["d"], {})[_i(r["h"])] = _i(r["n"])
            labels[r["d"]] = r["lbl"]

        # Days come from the SERIES, so a day with no activity at all is still a
        # column of zeroes rather than a missing column — the same reason the
        # series itself is zero-filled. Falls back to the days that have rows
        # when the rollup is not daily and the series cannot supply them.
        days, seen = [], set()
        for t in ([r["t"][:10] for r in series] if unit == "day"
                  else sorted(by_day)):
            if t not in seen:
                seen.add(t)
                days.append(t)

        return {
            "measure": measure,
            "cols": [{"key": str(h), "label": f"{h:02d}"} for h in _HOURS],
            "rows": [
                {"key": d, "label": labels.get(d, d),
                 "cells": [{"value": by_day.get(d, {}).get(h, 0)}
                           for h in _HOURS]}
                for d in days
            ],
        }

    series = await _section(series_section, [])
    return {
        # §F1: the zone actually used, at the top level, on every response.
        "tz": f.tz,
        "window": _window_payload(f),
        "rollup": rollup,
        # Echoed so the caller can confirm which quantity `previous` and
        # `heatmap` were cut for, rather than assuming its request was honoured.
        "measure": measure,
        "series": series,
        # Drawn as a plain line under `series`, never filled: it is a reference,
        # not a second measurement of this window.
        "previous": await _section(previous_section,
                                   {"label": None, "measure": measure,
                                    "values": []}),
        "heatmap": await _section(heatmap_section,
                                  {"measure": measure, "cols": [], "rows": []}),
        "movers": await _section(movers_section, []),
        "sources": {"included": included, "unavailable": unavailable},
        "available": bool(series),
    }


# ---- activity: the pivot ----------------------------------------------------
#
# This endpoint takes a column name from a query parameter, which is how SQL
# injection happens. It therefore does NOT take a column name: it takes a KEY,
# looks the expression up in a map defined here, and 400s on anything it does not
# recognise. The caller's string never reaches the query text, not even quoted.

_EXPLORE_DIMS: Dict[str, str] = {
    "actor":        "e.actor",
    "action":       "e.action",
    "source":       "e.source",
    "target":       "e.target",
    "ip":           "e.ip",
    "status_class": "CASE WHEN e.status IS NULL THEN NULL"
                    " ELSE (e.status / 100)::text || 'xx' END",
}

# measure -> (per-row expression, whether a row without it still counts)
# `duration_ms` is guarded by a numeric regex rather than cast outright: the
# detail object is assembled from three different tables and one unparseable
# value would take the whole panel down with a 500 (§6).
# Display names for the picker, served alongside the keys. Kept beside the maps
# they label so a key added to one and not the other falls back to the key
# rather than vanishing from the menu.
_EXPLORE_LABELS: Dict[str, str] = {
    "events": "Events", "status": "HTTP status", "duration_ms": "Duration (ms)",
    "actor": "Actor", "action": "Action", "source": "Source",
    "target": "Target", "ip": "Client address", "status_class": "Status class",
}

_NUMERIC_RE = r"^-?[0-9]+(\.[0-9]+)?$"
_EXPLORE_MEASURES: Dict[str, str] = {
    "events":      "1::numeric",
    "status":      "e.status::numeric",
    "duration_ms": f"CASE WHEN e.detail->>'duration_ms' ~ '{_NUMERIC_RE}'"
                   " THEN (e.detail->>'duration_ms')::numeric END",
}

def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def _llm_call_conds(f: AnalyticsFilters, params: List) -> List[str]:
    """chat_logs predicates with `model` redirected onto ``llm_calls.model``.

    Every endpoint whose subject is the per-CALL model has to do this, and it is
    one helper rather than three copies because the copies drifted: `/economics`
    filtered `chat_logs.model` while `/llm-usage` and `/llm-calls` filtered
    `llm_calls.model`, so `?model=X` narrowed the table on one panel and emptied
    the panel beside it. One turn uses several models; the turn's headline model
    is not the one this family is about.
    """

    conds = f.chat_conds(params, "l.", skip=("model",))
    if f.model:
        ors = []
        for tok in f.model:
            if tok == _NONE_TOKEN:
                ors.append("c.model IS NULL")
            else:
                params.append(tok)
                ors.append(f"c.model = ${len(params)}")
        conds.append(_or_group(ors))
    return conds


@router.get("/activity/explore", dependencies=[Depends(require_super_admin)])
async def activity_explore(
    f: ActivityFilters = Depends(activity_filters),
    measure: str = Query("events", description="events|status|duration_ms"),
    by: str = Query("action", description="actor|action|source|target|ip|status_class"),
    sub: str = Query("", description="optional second dimension, for stacking"),
    rollup: str = Query("day", description="hour|day|week|month"),
    top: int = Query(10, ge=1, le=50),
    sub_top: int = Query(5, ge=1, le=20, description="parts per key when `sub` is set"),
) -> Dict:
    """measure x dimension x rollup x top-N: `{series[], table[]}`.

    Both `measure` and `by` are whitelisted against a fixed map and answered with
    a 400 when unknown. That is the point of the endpoint's design, not a
    formality: it is the only place in this file where a caller names something
    that looks like a column.

    **The table summarises the SERIES, not the raw rows**, and that is what makes
    `rollup` mean something. A row reads: for this key, across the buckets that
    had data, `n` buckets, `min`..`max` per bucket, `avg` per bucket, `sum` over
    all of them, and `share` of the grand total — "this actor did between 3 and
    40 things a day, 12 on average, 240 in all, 18% of everything". `rows` is the
    raw observation count beside it, so a large `sum` over one busy bucket cannot
    be mistaken for a steady one.

    `series` is flat — one `{t, key, value, rows}` per bucket per ranked key —
    rather than a nested object per bucket, so a key that is absent from a bucket
    is absent rather than zero, and the caller decides whether that is a gap or a
    zero for the chart it is drawing.

    Only the top-N keys appear. `truncated` says whether there were more, because
    a chart of "the top 10" that silently was the top 10 of exactly 10 is a
    different fact from one that was the top 10 of 400.
    """

    if measure not in _EXPLORE_MEASURES:
        raise HTTPException(
            status_code=400,
            detail="`measure` must be one of: " + ", ".join(sorted(_EXPLORE_MEASURES)))
    if by not in _EXPLORE_DIMS:
        raise HTTPException(
            status_code=400,
            detail="`by` must be one of: " + ", ".join(sorted(_EXPLORE_DIMS)))
    if sub and sub not in _EXPLORE_DIMS:
        raise HTTPException(
            status_code=400,
            detail="`sub` must be one of: " + ", ".join(sorted(_EXPLORE_DIMS)))
    if sub and sub == by:
        # Grouping a dimension by itself gives exactly one part per key and a
        # stacked chart identical to the unstacked one, which reads as a bug in
        # the chart rather than as a bad request. Say so instead.
        raise HTTPException(
            status_code=400, detail="`sub` must differ from `by`")
    unit, fmt, step, span = _rollup(rollup)

    m_expr = _EXPLORE_MEASURES[measure]
    d_expr = _EXPLORE_DIMS[by]
    s_expr = _EXPLORE_DIMS[sub] if sub else ""
    union, included, unavailable = await _activity_union(f.wanted())

    async def compute() -> Dict:
        if not union:
            raise RuntimeError("no readable activity source")

        # -- rank the keys ----------------------------------------------------
        params: List = []
        where = _where(f.conds(params))
        ranked = await q(
            f"""SELECT {d_expr} AS k, sum({m_expr}) AS total, count(*) AS rows_n,
                       count({m_expr}) AS measured
                  FROM ({union}) e {where}
                 GROUP BY 1
                 ORDER BY total DESC NULLS LAST, rows_n DESC""",
            *params,
        )
        keys = [r["k"] for r in ranked[:top]]
        grand = sum(_f(r["total"]) or 0.0 for r in ranked)

        # -- per (key, bucket) -------------------------------------------------
        params = []
        conds = f.conds(params)
        # NULL is a real key here — an event with no actor, a row with no status.
        # `= ANY(array)` can never match it however the array is spelled, so it is
        # lifted out exactly as `_list_clause` lifts the `none` sentinel. Without
        # this the "not recorded" band vanishes from the pivot and only the pivot,
        # and the shares stop summing to the total.
        literal = [k for k in keys if k is not None]
        ors: List[str] = []
        if len(literal) < len(keys):
            ors.append(f"({d_expr}) IS NULL")
        if literal:
            params.append(literal)
            ors.append(f"({d_expr}) = ANY(${len(params)}::text[])")
        if not ors:
            return {"series": [], "table": [], "truncated": False}
        conds.append(_or_group(ors))
        bucket = _bucket_expr("e.ts", unit, f.tz, params)

        # -- the optional second dimension ------------------------------------
        #
        # Top-N applies to `by` FIRST and then to `sub` within each key, so a
        # long tail on the second dimension cannot multiply the payload by its
        # cardinality. What is cut is reported (`sub_truncated`) rather than
        # silently dropped: a bucket that had forty sources and shows five is a
        # different fact from one that had five.
        sub_sel = f", {s_expr} AS s" if sub else ""
        # Grouped by the EXPRESSION, not by an ordinal. The sub column is the
        # fifth in this SELECT and `GROUP BY 1, 2, 3` grouped by `sum(...)` — an
        # aggregate — which Postgres rejects outright. Section isolation then
        # turned that into an empty table rather than an error, so the pivot
        # simply showed nothing whenever `sub` was set. Naming the expression
        # cannot drift with the column order.
        sub_grp = f", {s_expr}" if sub else ""
        cells = await q(
            f"""SELECT {d_expr} AS k, to_char({bucket}, '{fmt}') AS t,
                       sum({m_expr}) AS v, count(*) AS rows_n{sub_sel}
                  FROM ({union}) e {_where(conds)}
                 GROUP BY 1, 2{sub_grp} ORDER BY 2, 1""",
            *params,
        )

        kept_sub: Dict[Any, set] = {}
        sub_of: Dict[Any, int] = {}
        sub_truncated = False
        if sub:
            # Rank the parts by total within each key, keep the top `sub_top`.
            totals: Dict[Any, Dict[Any, float]] = {}
            for r in cells:
                totals.setdefault(r["k"], {})
                totals[r["k"]][r["s"]] = (totals[r["k"]].get(r["s"], 0.0)
                                          + (_f(r["v"]) or 0.0))
            for k, parts in totals.items():
                ranked_parts = sorted(parts, key=lambda s: parts[s], reverse=True)
                kept_sub[k] = set(ranked_parts[:sub_top])
                sub_of[k] = len(ranked_parts)
                if len(ranked_parts) > sub_top:
                    sub_truncated = True

        series = []
        for r in cells:
            if sub and r["s"] not in kept_sub.get(r["k"], set()):
                continue
            row = {"t": r["t"], "key": r["k"], "value": _f(r["v"]),
                   "rows": _i(r["rows_n"])}
            if sub:
                row["sub"] = r["s"]      # NULL stays NULL — "not recorded"
            series.append(row)

        # The table summarises BUCKETS, so with `sub` set the cells have to be
        # folded back to one value per (key, bucket) first. Reading them raw
        # would count a bucket once per part — `n` would report parts as
        # buckets, and min/max would describe the parts rather than the days.
        # It is also computed from ALL cells, not from the top-N-filtered
        # `series`: the table's `sum` is the key's real total, and dropping the
        # tail here would rebase every `share` on the page.
        folded: Dict[Any, Dict[str, float]] = {}
        rows_by_key: Dict[Any, int] = {}
        measured: Dict[Any, set] = {}
        for r in cells:
            v = _f(r["v"])
            rows_by_key[r["k"]] = rows_by_key.get(r["k"], 0) + _i(r["rows_n"])
            if v is not None:
                bucket_totals = folded.setdefault(r["k"], {})
                bucket_totals[r["t"]] = bucket_totals.get(r["t"], 0.0) + v
                measured.setdefault(r["k"], set()).add(r["t"])
        per_key: Dict[Any, List[float]] = {
            k: list(v.values()) for k, v in folded.items()
        }

        table = []
        for k in keys:
            vals = per_key.get(k, [])
            total = sum(vals) if vals else None
            row = {
                "key": k,                       # NULL stays NULL — "not recorded"
                "n": len(vals),                 # buckets that had a measurement
                "rows": rows_by_key.get(k, 0),
                "min": round(min(vals), 6) if vals else None,
                "max": round(max(vals), 6) if vals else None,
                "avg": round(total / len(vals), 6) if vals else None,
                "sum": round(total, 6) if total is not None else None,
                # A share needs its denominator (§3). `n` here is the grand total
                # the share is OF, not a sample count.
                "share": ({"rate": round(total / grand, 4), "n": round(grand, 6)}
                          if (total is not None and grand) else
                          {"rate": None, "n": round(grand, 6) if grand else 0}),
            }
            if sub:
                # Per ROW, so the marker sits on the key that actually lost its
                # tail and can read "top 5 of 12" rather than a page-level
                # "something was cut". The response-level flag stays, for the
                # chart, which has no row to hang it on.
                row["sub_of"] = sub_of.get(k, 0)
                row["sub_truncated"] = sub_of.get(k, 0) > sub_top
            table.append(row)

        return {"series": series, "table": table,
                "truncated": len(ranked) > len(keys),
                "sub_truncated": sub_truncated}

    body = await _section(compute, {"series": [], "table": [], "truncated": False,
                                    "sub_truncated": False})
    return {
        # §F1: the zone actually used, at the top level, on every response.
        # `measure`, `by` and `rollup` are echoed for the same reason — each
        # changes what the numbers MEAN, and a table whose header says "avg
        # duration" over event counts is the same class of lie as a UTC bucket
        # under a Yangon chip.
        "tz": f.tz,
        "window": _window_payload(f),
        "measure": measure,
        "by": by,
        # Echoed as null when unset, so "this backend does not implement
        # subgroup" and "I asked for none" are distinguishable from the payload.
        "sub": sub or None,
        "rollup": rollup,
        "series": body["series"],
        "table": body["table"],
        "truncated": body["truncated"],
        "sub_truncated": body["sub_truncated"],
        "sub_top": sub_top if sub else None,
        # Served from the SAME maps the validator reads, so the menu and the
        # whitelist cannot disagree. A picker offering an option that 400s on
        # click is a worse failure than a shorter picker.
        "options": {
            "measures": [{"key": k, "label": _EXPLORE_LABELS.get(k, k)}
                         for k in _EXPLORE_MEASURES],
            "dimensions": [{"key": k, "label": _EXPLORE_LABELS.get(k, k)}
                           for k in _EXPLORE_DIMS],
            "rollups": [{"key": k, "label": k} for k in _ROLLUPS],
        },
        "sources": {"included": included, "unavailable": unavailable},
        "available": bool(body["table"]),
    }


# ---- activity: the audit view ----------------------------------------------

@router.get("/activity/audit", dependencies=[Depends(require_super_admin)])
async def activity_audit(
    f: ActivityFilters = Depends(activity_filters),
    rollup: str = Query("day", description="hour|day|week|month"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict:
    """Sign-in outcomes over time, 403s by action, and the events behind them.

    Three questions an incident actually asks, on one page: *when did the failed
    logins start*, *what was somebody repeatedly refused*, and *show me the rows*.
    They were answerable only one table at a time before this.

    The sign-in series is zero-filled, so an hour with no attempts is a visible
    gap rather than a line drawn through it. A `403` count is per action and
    carries `actors` — the number of DISTINCT accounts refused — because one
    account hitting a wall forty times and forty accounts hitting it once are
    different incidents and the same number.
    """

    unit, fmt, step, span = _rollup(rollup)
    union, included, unavailable = await _activity_union(f.wanted())

    async def signin_series() -> List[Dict]:
        params: List = []
        conds = f.window_conds(params)
        if f.actor:
            params.append(f.actor)
            n = len(params)
            conds.append(f"(COALESCE(actor_email,'') ILIKE '%'||${n}||'%'"
                         f" OR COALESCE(email,'') ILIKE '%'||${n}||'%')")
        if f.ip:
            params.append(f.ip)
            conds.append(f"ip = ${len(params)}")
        bucket = _bucket_expr("ts", unit, f.tz, params)
        lo, hi = _axis_bounds(f, params)
        filters = " ".join(
            f"count(*) FILTER (WHERE event = '{ev}') AS {ev}," for ev in _SIGNIN_EVENTS
        )
        # Zero, not NULL, on a bucket with no attempts: the join ran and looked,
        # so "nobody tried to sign in that hour" is a measurement.
        zeroed = ", ".join(f"COALESCE(a.{ev}, 0) AS {ev}" for ev in _SIGNIN_EVENTS)
        rows = await q(
            f"""WITH agg AS (
                    SELECT {bucket} AS b, {filters} count(*) AS attempts
                      FROM auth_events {_where(conds)}
                     GROUP BY 1
                ),
                bounds AS (
                    SELECT COALESCE(date_trunc('{unit}', {lo}),
                                    (SELECT min(b) FROM agg)) AS lo,
                           COALESCE(date_trunc('{unit}', {hi}),
                                    (SELECT max(b) FROM agg)) AS hi
                ),
                axis AS (
                    SELECT generate_series(lo, LEAST(hi, lo + interval '{span}'),
                                           interval '{step}') AS b
                      FROM bounds
                     WHERE lo IS NOT NULL AND hi IS NOT NULL AND hi >= lo
                )
                SELECT to_char(x.b, '{fmt}') AS t,
                       COALESCE(a.attempts, 0) AS attempts,
                       {zeroed}
                  FROM axis x LEFT JOIN agg a ON a.b = x.b
                 ORDER BY x.b""",
            *params,
        )
        return [{"t": r["t"], "attempts": _i(r["attempts"]),
                 **{ev: _i(r[ev]) for ev in _SIGNIN_EVENTS}} for r in rows]

    async def forbidden_section() -> List[Dict]:
        if not union:
            raise RuntimeError("no readable activity source")
        params: List = []
        conds = f.conds(params)
        conds.append("e.status = 403")
        rows = await q(
            f"""SELECT e.action, count(*) AS n,
                       count(DISTINCT e.actor) AS actors,
                       max(e.ts) AS last_ts
                  FROM ({union}) e {_where(conds)}
                 GROUP BY 1 ORDER BY n DESC, e.action ASC""",
            *params,
        )
        return [{"action": r["action"], "n": _i(r["n"]),
                 "actors": _i(r["actors"]), "last_ts": r["last_ts"]}
                for r in rows]

    async def events_section() -> Dict:
        if not union:
            raise RuntimeError("no readable activity source")
        params: List = []
        where = _where(f.conds(params))
        total = _i((await q(
            f"SELECT count(*) AS n FROM ({union}) e {where}", *params))[0]["n"])
        page = list(params)
        page.append(limit)
        page.append(offset)
        rows = await q(
            f"""SELECT e.* FROM ({union}) e {where}
                 ORDER BY e.ts DESC NULLS LAST
                 LIMIT ${len(page)-1} OFFSET ${len(page)}""",
            *page,
        )
        return {
            "total": total,
            "rows": [{"ts": r["ts"], "source": r["source"], "actor": r["actor"],
                      "action": r["action"], "target": r["target"],
                      "status": _int_or_none(r["status"]), "ip": r["ip"],
                      "detail": _json_obj(r["detail"])} for r in rows],
        }

    events = await _section(events_section, {"total": 0, "rows": []})
    signins = await _section(signin_series, [])
    return {
        # §F1: the zone actually used, at the top level, on every response. The
        # Audit tab renders its own chip from THIS echo, not from Trends' — a
        # page where one panel confirms the zone for another is a page where the
        # unconfirmed panel is invisible.
        "tz": f.tz,
        "window": _window_payload(f),
        "rollup": rollup,
        "signins": signins,
        "forbidden": await _section(forbidden_section, []),
        "events": events["rows"],
        "total": events["total"],
        "sources": {"included": included, "unavailable": unavailable},
        "available": bool(events["rows"]) or bool(signins),
    }


# ---- analytics: one row per model call (addendum §C/§D) ---------------------

# Same keys, same types as the real payload (§6) — the frontend must never have
# to branch on a missing key, and a panel that loses `totals` on a bad database
# is a panel that renders `undefined.cost_usd`.
_LLM_CALLS_EMPTY: Dict = {
    "rows": [], "total": 0,
    "totals": {"calls": 0, "prompt_tokens": None, "completion_tokens": None,
               "reasoning_tokens": None, "cache_read_tokens": None,
               "cache_creation_tokens": None, "cost_usd": None,
               "priced_calls": 0, "cost_coverage": {"rate": None, "n": 0}},
    "tz": DEFAULT_TZ, "not_recorded": None, "available": False,
}


@router.get("/analytics/llm-calls")
async def analytics_llm_calls(
    f: AnalyticsFilters = Depends(analytics_filters),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("ts", description="ts|cost|tokens|ttft"),
) -> Dict:
    """Every LLM call in the window, one row each — the grain the lever lives at.

    At TURN grain the cache split is one number and invisible. Turn #20772 holds
    `seq 2` at $0.0026 with 5,597 cached tokens and `seq 4` at $0.0388 with none
    — **15x apart inside one turn** (§D). Averaged together that is a turn that
    cost $0.02 and nothing to act on.

    `cost_usd` is `null` where no price is configured — never `0.0`, which reads
    as "free" and goes unnoticed for months — and `cost_is_estimated` says
    whether a present cost was reported by the provider or derived here.

    The `model` filter applies to `llm_calls.model`, as it does on `/llm-usage`:
    this endpoint's subject is the per-call model, and one turn uses several.
    """

    # `turn` is the ordering to reach for when the point is the INTRA-TURN
    # spread. `ts` does not guarantee it: two calls of one turn are adjacent
    # only if no other turn's call happens to fall between their timestamps, and
    # under any concurrency at all one will. The turn_id tiebreaker cannot help,
    # because it applies only when the timestamps are exactly equal.
    #
    # So `turn` sorts by turn first and by `seq` ASCENDING within it: newest
    # turns at the top, and inside each one the calls read in the order they
    # actually ran. That is what makes "seq 2 cost $0.0026 with 5,597 cached and
    # seq 4 cost $0.0388 with none" legible as two rows of one turn (§D) rather
    # than as two unrelated rows that happen to be near each other.
    orders = {
        "ts": "c.ts DESC, c.turn_id DESC, c.seq DESC",
        "turn": "c.turn_id DESC, c.seq ASC",
        "cost": "c.cost_usd DESC NULLS LAST, c.ts DESC",
        "tokens": "(COALESCE(c.prompt_tokens,0) + COALESCE(c.completion_tokens,0))"
                  " DESC, c.ts DESC",
        "ttft": "c.ttft_ms DESC NULLS LAST, c.ts DESC",
    }
    if order not in orders:
        raise HTTPException(status_code=400,
                            detail="`order` must be one of: " + ", ".join(sorted(orders)))

    async def compute() -> Dict:
        params: List = []
        where = _where(_llm_call_conds(f, params))
        joined = f"FROM llm_calls c JOIN chat_logs l ON l.id = c.turn_id {where}"

        head = (await q(
            f"""SELECT count(*) AS n,
                       sum(c.prompt_tokens)         AS prompt_tokens,
                       sum(c.completion_tokens)     AS completion_tokens,
                       sum(c.reasoning_tokens)      AS reasoning_tokens,
                       sum(c.cache_read_tokens)     AS cache_read_tokens,
                       sum(c.cache_creation_tokens) AS cache_creation_tokens,
                       sum(c.cost_usd)              AS cost_usd,
                       count(*) FILTER (WHERE c.cost_usd IS NOT NULL) AS priced
                  {joined}""",
            *params,
        ))[0]

        page = list(params)
        page.append(limit)
        page.append(offset)
        rows = await q(
            f"""SELECT c.turn_id, c.seq, c.model, c.prompt_tokens,
                       c.completion_tokens, c.reasoning_tokens,
                       c.cache_read_tokens, c.cache_creation_tokens,
                       c.ttft_ms, c.duration_ms, c.cost_usd, c.cost_is_estimated,
                       c.finish_reason, c.ts,
                       l.store_id, l.lang, l.path, l.cached AS turn_cached
                  {joined}
                 ORDER BY {orders[order]}
                 LIMIT ${len(page)-1} OFFSET ${len(page)}""",
            *page,
        )

        total = _i(head["n"])
        return {
            "rows": [
                {
                    "turn_id": _int_or_none(r["turn_id"]),
                    "seq": _int_or_none(r["seq"]),
                    "model": r["model"],
                    "prompt_tokens": _int_or_none(r["prompt_tokens"]),
                    "completion_tokens": _int_or_none(r["completion_tokens"]),
                    "reasoning_tokens": _int_or_none(r["reasoning_tokens"]),
                    "cache_read_tokens": _int_or_none(r["cache_read_tokens"]),
                    "cache_creation_tokens": _int_or_none(r["cache_creation_tokens"]),
                    "ttft_ms": _int_or_none(r["ttft_ms"]),
                    "duration_ms": _int_or_none(r["duration_ms"]),
                    # NULL stays NULL. An unpriced call is not a free call.
                    "cost_usd": _f(r["cost_usd"]),
                    "cost_is_estimated": bool(r["cost_is_estimated"]),
                    "finish_reason": r["finish_reason"],
                    "ts": r["ts"],
                    "store_id": r["store_id"],
                    "lang": r["lang"],
                    "path": r["path"],
                    "turn_cached": r["turn_cached"],
                }
                for r in rows
            ],
            "total": total,
            "totals": {
                "calls": total,
                "prompt_tokens": _int_or_none(head["prompt_tokens"]),
                "completion_tokens": _int_or_none(head["completion_tokens"]),
                "reasoning_tokens": _int_or_none(head["reasoning_tokens"]),
                "cache_read_tokens": _int_or_none(head["cache_read_tokens"]),
                "cache_creation_tokens": _int_or_none(head["cache_creation_tokens"]),
                "cost_usd": _f(head["cost_usd"]),
                # The denominator for that cost: how many of the calls were
                # priced at all. A partial sum must not pass as the total spend.
                "priced_calls": _i(head["priced"]),
                "cost_coverage": _rate(_i(head["priced"]), total),
            },
            "tz": f.tz,
            "not_recorded": await _not_recorded(f, "llm_calls"),
            "available": total > 0,
        }

    return await _section(compute, json.loads(json.dumps(_LLM_CALLS_EMPTY)))


# ---- analytics: economics ---------------------------------------------------

_ECONOMICS_EMPTY: Dict = {
    "cost_usd": None, "priced_calls": 0, "unpriced_calls": 0,
    "estimated_calls": 0, "calls": 0, "turns": 0,
    "cost_is_estimated": False,
    "blended_per_1m_usd": {"value": None, "n": 0,
                           "denominator": "tokens on priced calls"},
    "cost_per_turn_usd": {"value": None, "n": 0,
                          "denominator": "turns with a recorded LLM call"},
    "cost_per_call_usd": {"value": None, "n": 0,
                          "denominator": "priced LLM calls"},
    "cache_read_share": {"rate": None, "n": 0},
    "completion_share": {"rate": None, "n": 0},
    "prompt_completion_ratio": {"value": None, "n": 0,
                                "denominator": "completion tokens"},
    "tokens": {"prompt": None, "completion": None, "reasoning": None,
               "cache_read": None, "cache_creation": None, "total": None},
    "tz": DEFAULT_TZ, "not_recorded": None, "available": False,
}


@router.get("/analytics/economics")
async def analytics_economics(
    f: AnalyticsFilters = Depends(analytics_filters),
) -> Dict:
    """Blended price, cost per turn, cache-read share, prompt:completion ratio.

    **Every one of them ships its denominator.** "$2.14 per million" over four
    priced calls and over forty thousand are the same number and different facts,
    and the panel that omits the second half is the one somebody plans a budget
    on.

    The ratios are the levers §D identified, and they are here because they point
    the opposite way to intuition:

    * completion is ~3.7% of tokens — **shortening answers saves nothing**, and a
      UI that implies otherwise sends people to optimise the wrong thing;
    * cache read is ~26.5% of prompt tokens and is the largest lever available.

    `cost_usd` sums only the calls that HAVE a price and reports how many did
    (`priced_calls`) and how many did not (`unpriced_calls`). It is `null`, never
    `0.0`, when nothing in the window was priced: a zero reads as "this window
    was free", which is the one thing it certainly does not mean.
    """

    async def compute() -> Dict:
        params: List = []
        # `model` filters the CALL, as it does on /llm-usage and /llm-calls. It
        # used to filter the turn's headline model here, so `?model=X` emptied
        # this panel while narrowing the table beside it — the same chip meaning
        # two different things on one page.
        where = _where(_llm_call_conds(f, params))
        head = (await q(
            f"""SELECT count(*) AS calls,
                       count(*) FILTER (WHERE c.cost_usd IS NOT NULL) AS priced,
                       count(*) FILTER (WHERE c.cost_usd IS NULL)     AS unpriced,
                       count(*) FILTER (WHERE c.cost_usd IS NOT NULL
                                          AND c.cost_is_estimated)    AS estimated,
                       sum(c.cost_usd)                                AS cost,
                       sum(c.prompt_tokens)                           AS prompt,
                       sum(c.completion_tokens)                       AS completion,
                       sum(c.reasoning_tokens)                        AS reasoning,
                       sum(c.cache_read_tokens)                       AS cache_read,
                       sum(c.cache_creation_tokens)                   AS cache_creation,
                       -- tokens on PRICED calls only: the blended rate's own
                       -- denominator. Dividing a partial cost by every token in
                       -- the window understates the price by however much of it
                       -- was never priced.
                       sum(COALESCE(c.prompt_tokens,0) + COALESCE(c.completion_tokens,0))
                           FILTER (WHERE c.cost_usd IS NOT NULL)      AS priced_tokens,
                       count(DISTINCT c.turn_id)                      AS turns
                  FROM llm_calls c JOIN chat_logs l ON l.id = c.turn_id
                  {where}""",
            *params,
        ))[0]

        calls = _i(head["calls"])
        priced = _i(head["priced"])
        cost = _f(head["cost"])
        priced_tokens = _i(head["priced_tokens"])
        turns = _i(head["turns"])
        prompt = _int_or_none(head["prompt"])
        completion = _int_or_none(head["completion"])
        cache_read = _int_or_none(head["cache_read"])
        total_tokens = (prompt or 0) + (completion or 0)

        def per(value: Optional[float], n: int, denom: str) -> Dict:
            return {"value": round(value / n, 6) if (value is not None and n)
                    else None,
                    "n": n, "denominator": denom}

        return {
            "cost_usd": round(cost, 6) if cost is not None else None,
            "priced_calls": priced,
            "unpriced_calls": _i(head["unpriced"]),
            "estimated_calls": _i(head["estimated"]),
            # True when ANY priced call in the window was derived here rather
            # than reported by the provider. Derived is flagged as derived; it
            # is never presented as measured. `estimated_calls` beside it says
            # how much of the total that is, so "estimated" cannot mean one
            # rounding on a $500 bill and the whole bill in the same badge.
            "cost_is_estimated": _i(head["estimated"]) > 0,
            "calls": calls,
            "turns": turns,
            "blended_per_1m_usd": per(
                cost * 1_000_000 if cost is not None else None, priced_tokens,
                "tokens on priced calls"),
            "cost_per_turn_usd": per(cost, turns,
                                     "turns with a recorded LLM call"),
            "cost_per_call_usd": per(cost, priced, "priced LLM calls"),
            # Cache read is measured against PROMPT tokens, which is the only
            # thing it can be read from. Against total tokens it would look
            # smaller than it is and the largest lever would read as a minor one.
            "cache_read_share": _rate(cache_read, prompt),
            "completion_share": _rate(completion, total_tokens),
            "prompt_completion_ratio": {
                "value": round((prompt or 0) / completion, 2) if completion else None,
                "n": completion or 0,
                "denominator": "completion tokens",
            },
            "tokens": {
                "prompt": prompt,
                "completion": completion,
                "reasoning": _int_or_none(head["reasoning"]),
                "cache_read": cache_read,
                "cache_creation": _int_or_none(head["cache_creation"]),
                "total": total_tokens if (prompt is not None
                                          or completion is not None) else None,
            },
            "tz": f.tz,
            "not_recorded": await _not_recorded(f, "llm_calls"),
            "available": calls > 0,
        }

    return await _section(compute, json.loads(json.dumps(_ECONOMICS_EMPTY)))


# ---- credentials (tenants) -------------------------------------------------


class Credential(BaseModel):
    embed_id: str
    public_key: str


@router.get("/credentials")
async def list_creds() -> List[Dict]:
    creds = await cache.list_credentials()
    return [{"embed_id": k, "public_key": v} for k, v in creds.items()]


@router.post("/credentials")
async def add_cred(c: Credential) -> Dict:
    await cache.register_credential(c.embed_id, c.public_key)
    return {"status": "ok", "embed_id": c.embed_id}


@router.delete("/credentials/{embed_id}")
async def del_cred(embed_id: str) -> Dict:
    n = await cache.remove_credential(embed_id)
    return {"status": "ok", "removed": n}


# ---- drug aliases (fast-path memory) ---------------------------------------
#
# resolver.py resolves a free-text mention in three layers: exact code -> alias
# -> trigram. The alias layer was dead: the table existed, the read existed, and
# NOTHING wrote to it, so every lookup missed and every mention fell through to
# the trigram scan. This is the missing write path.


def _norm_alias(alias: str) -> str:
    """Normalise a mention to its stored key.

    Must match ``resolver._alias_lookup``, which queries ``WHERE alias = lower($1)``
    against a mention that ``resolve()`` has only ``.strip()``ed. So: strip, then
    lowercase — and nothing else. Collapsing internal whitespace here would store
    a key the resolver can never look up.
    """

    return (alias or "").strip().lower()


class Alias(BaseModel):
    alias: str
    article_code: str
    source: str = "admin"


@router.get("/aliases")
async def list_aliases(
    search: str = "", article_code: str = "", limit: int = 100, offset: int = 0
) -> List[Dict]:
    """Learned aliases, newest first. Filter by alias substring and/or article."""

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    conds, params = [], []
    if search:
        params.append(search)
        conds.append(f"a.alias ILIKE '%'||${len(params)}||'%'")
    if article_code:
        params.append(article_code)
        conds.append(f"a.article_code = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    try:
        return await q(
            f"""SELECT a.alias, a.article_code, a.source, a.created_at,
                       c.brand_name, c.generic_name
                  FROM drug_alias a LEFT JOIN catalog c USING (article_code)
                  {where}
                 ORDER BY a.created_at DESC
                 LIMIT ${len(params)-1} OFFSET ${len(params)}""",
            *params,
        )
    except Exception:  # noqa: BLE001 — table not created yet
        return []


@router.post("/aliases")
async def add_alias(a: Alias) -> Dict:
    """Teach the resolver that ``alias`` means ``article_code`` (upsert).

    The article must exist: drug_alias FKs to catalog, so an unknown code would
    surface as a 500 from asyncpg. Check first and answer 400.
    """

    alias = _norm_alias(a.alias)
    code = (a.article_code or "").strip()
    if len(alias) < 2:
        raise HTTPException(status_code=400, detail="alias must be at least 2 characters")
    if not code:
        raise HTTPException(status_code=400, detail="article_code is required")
    if not await q("SELECT 1 FROM catalog WHERE article_code=$1", code):
        raise HTTPException(status_code=404, detail="article not found")

    rows = await q(
        """INSERT INTO drug_alias (alias, article_code, source)
           VALUES ($1,$2,$3)
           ON CONFLICT (alias) DO UPDATE
               SET article_code = EXCLUDED.article_code,
                   source       = EXCLUDED.source,
                   created_at   = now()
           RETURNING alias, article_code, source""",
        alias, code, (a.source or "admin").strip() or "admin",
    )
    # An alias changes what a question RESOLVES to, so answers cached against the
    # old resolution are now wrong for the same words. Same rule as
    # /admin/graph/rebuild: a writer that changes answers bumps the version.
    version = await cache.bump_data_version()
    return {"status": "ok", **(rows[0] if rows else {}), "data_version": version}


@router.delete("/aliases/{alias}")
async def del_alias(alias: str) -> Dict:
    """Forget one learned alias."""

    rows = await q(
        "DELETE FROM drug_alias WHERE alias=$1 RETURNING alias", _norm_alias(alias)
    )
    version = await cache.bump_data_version()
    return {"status": "ok", "removed": len(rows), "data_version": version}


# ---- agent config ----------------------------------------------------------


class ConfigUpdate(BaseModel):
    system_prompt: Optional[str] = None


@router.get("/config")
async def get_config() -> Dict:
    s = get_settings()
    overrides = await cache.get_config_overrides()
    from app.agent import BILINGUAL_SYSTEM_PROMPT

    return {
        "model": s.openrouter_model,
        "embedding_model": s.embedding_model,
        "rate_limit_per_min": s.rate_limit_per_min,
        "cache_ttl_seconds": s.cache_ttl_seconds,
        "session_ttl_seconds": s.session_ttl_seconds,
        "system_prompt": overrides.get("system_prompt", BILINGUAL_SYSTEM_PROMPT),
        "prompt_overridden": "system_prompt" in overrides,
    }


@router.put("/config")
async def put_config(c: ConfigUpdate) -> Dict:
    if c.system_prompt is not None:
        await cache.set_config_override("system_prompt", c.system_prompt)
    return {"status": "ok", "note": "applied on next agent rebuild/restart"}


# ---- authentication config (Keycloak SSO + LDAP) ---------------------------
#
# **super_admin only, read AND write.** Unlike the users surface above, the read
# is not the safe half here: the GET returns the directory host, the service-
# account DN, the base DN and the whole OIDC client identity — a map of the
# customer's internal auth infrastructure. (Secrets themselves are masked by
# `auth.get_auth_config`, which is a separate defence and stays.)
#
# The write is worse. `PUT /auth-config` can repoint `oidc_discovery_url` and
# `oidc_client_id` at an attacker-controlled realm; every subsequent SSO login
# is then brokered by that realm, and because `_merge_external` matches on the
# email the IdP asserts, whoever controls the realm can assert an existing
# admin's address. It can also flip `ldap_validate_cert` off, turning the
# directory bind into a MITM-able plaintext-equivalent. Both were reachable by
# any `admin` when these routes inherited only `require_admin`.


@router.get("/auth-config", dependencies=[Depends(require_super_admin)])
async def get_auth_config() -> Dict:
    """Effective LDAP/OIDC config for the admin page. Secrets are masked.

    A secret value is never returned; the client only learns whether one is set
    (``ldap_bind_password_set`` / ``oidc_client_secret_set``). **super_admin
    only** — the non-secret half is still an infrastructure map (see above).
    """

    from app import auth as authmod

    return await authmod.get_auth_config()


@router.put("/auth-config", dependencies=[Depends(require_super_admin)])
async def put_auth_config(updates: Dict = Body(...)) -> Dict:
    """Persist a partial LDAP/OIDC update. Takes effect on the next login.

    An empty secret field is treated as "keep the current value", so the masked
    password box can be saved without wiping the stored secret. **super_admin
    only** — repointing OIDC at a hostile realm is an account takeover (above).
    """

    from app import auth as authmod

    try:
        await authmod.set_auth_config(updates)
    except authmod.AuthError as exc:
        # An out-of-range enum (`signin_mode`, `oidc_provider_type`) is an
        # operator typo, not a server fault: 400 with the allowed values, never
        # a 500 — and nothing is written, so the stored policy is unchanged.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "note": "applied on next login; no restart needed"}


# ---- authentication config: test the connection ----------------------------
#
# The failure mode these exist to kill: an operator saves a bind DN with a typo,
# nothing complains (the config is only read at the *next login*), and the first
# person to discover it is a pharmacist who cannot sign in. There was no way to
# ask "is this config actually reachable" short of logging out and trying.
#
# Three rules hold for both probes.
#
# 1. **Never a user password.** The LDAP probe does the SERVICE-ACCOUNT bind and
#    stops. Verifying a real user's password would mean asking an admin to type
#    someone's credentials into an admin form, which is exactly the habit an
#    auth page should not teach. The service bind is also the step that actually
#    fails in practice.
# 2. **Never echo a secret.** `detail` is composed from classified failure
#    modes, not from raw driver text, and is scrubbed of the bind password and
#    the client secret before it is returned (`_redact`) in case a library ever
#    embeds one in an exception message.
# 3. **Bounded and idempotent.** Both are read-only, both carry an explicit
#    timeout, and neither retries — the button is safe to click repeatedly and
#    cannot become a retry storm against a customer's directory. ldap3's API is
#    synchronous, so it runs via `asyncio.to_thread` and never blocks the event
#    loop (the same rule the pandas parsers follow).

_PROBE_TIMEOUT_SECONDS = 8


def _redact(text: str, *secrets: Optional[str]) -> str:
    """Blank any known secret that leaked into a driver's error string."""

    out = text or ""
    for s in secrets:
        if s and len(s) >= 3:
            out = out.replace(s, "***")
    return out


def _probe_ldap(cfg) -> tuple[bool, str]:
    """Service-account bind + a BASE-scope read of the base DN. Blocking.

    Returns ``(ok, detail)``. Every failure class gets its own message, because
    "LDAP test failed" tells an operator nothing they can act on — the four they
    actually hit are a wrong host/port, a TLS mismatch, a wrong bind DN or
    password, and a base DN that does not exist. Those need four different
    fixes, so they must not collapse into one string.

    The base-DN read matters as much as the bind: a bind can succeed against a
    perfectly healthy directory while `ldap_base_dn` points at nothing, and then
    every login fails with "invalid credentials" because the user search returns
    no entries. That is the single most misleading symptom in this stack.
    """

    import ldap3
    from ldap3.core.exceptions import LDAPException

    from app import auth as authmod

    host = (cfg.ldap_host or "").strip()
    if not host:
        return False, "No LDAP host is configured. Set the host, then save."
    if not (cfg.ldap_bind_dn or "").strip():
        return False, (
            "No service-account bind DN is configured. This app searches for the "
            "user with a service account before it binds as them, so a bind DN is "
            "required."
        )
    if not (cfg.ldap_bind_password or ""):
        return False, (
            "No service-account password is stored. Type it into the password "
            "box and save before testing (a blank password would be an "
            "unauthenticated bind, which this app refuses)."
        )

    where = f"{host}:{cfg.ldap_port}"
    conn = None
    try:
        server = authmod._ldap_server(cfg)
        conn = ldap3.Connection(
            server,
            cfg.ldap_bind_dn,
            cfg.ldap_bind_password,
            authentication=ldap3.SIMPLE,
            auto_bind=False,
            raise_exceptions=False,
            receive_timeout=_PROBE_TIMEOUT_SECONDS,
        )
        try:
            conn.open()
        except LDAPException as exc:
            msg = _redact(str(exc), cfg.ldap_bind_password).lower()
            if any(k in msg for k in ("certificate", "ssl", "tls", "handshake")):
                return False, (
                    f"TLS failed against {where}: {_redact(str(exc), cfg.ldap_bind_password)}. "
                    "Check whether the server really speaks LDAPS on this port, and "
                    "whether its certificate chains to a CA you trust "
                    "(ldap_ca_cert_file)."
                )
            return False, (
                f"Cannot reach {where}: {_redact(str(exc), cfg.ldap_bind_password)}. "
                "Check the host, the port, and that this container can route to it."
            )

        if cfg.ldap_start_tls and not cfg.ldap_use_ssl:
            try:
                conn.start_tls()
            except LDAPException as exc:
                return False, (
                    f"StartTLS failed against {where}: "
                    f"{_redact(str(exc), cfg.ldap_bind_password)}. The server may not "
                    "offer StartTLS on this port, or its certificate is not trusted."
                )

        if not conn.bind() or not conn.bound:
            desc = (conn.result or {}).get("description") or "bind failed"
            detail = (conn.result or {}).get("message") or ""
            if desc == "invalidCredentials":
                return False, (
                    "Reached the directory, but the service account was rejected: "
                    "the bind DN or its password is wrong. The DN must be a full "
                    f"distinguished name (e.g. cn=admin,{cfg.ldap_base_dn or 'dc=example,dc=com'}), "
                    "not a bare username."
                )
            return False, (
                f"Reached {where}, but the bind was refused ({desc}). "
                f"{_redact(detail, cfg.ldap_bind_password)}".strip()
            )

        base = (cfg.ldap_base_dn or "").strip()
        if not base:
            return True, (
                "Service-account bind succeeded, but no base DN is set, so user "
                "searches have nowhere to look. Set ldap_base_dn."
            )
        conn.search(base, "(objectClass=*)", search_scope=ldap3.BASE, attributes=[])
        desc = (conn.result or {}).get("description")
        if desc == "noSuchObject":
            return False, (
                f"Service-account bind succeeded, but the base DN `{base}` does not "
                "exist on this server. Every user search would return nothing, which "
                "shows up at the login screen as 'invalid credentials'."
            )
        if desc not in ("success", None):
            return False, (
                f"Service-account bind succeeded, but reading the base DN `{base}` "
                f"was refused ({desc}). The service account may lack read rights there."
            )
        return True, (
            f"Bound as the service account and read `{base}` on {where}. "
            "LDAP configuration looks usable."
        )
    except LDAPException as exc:
        return False, (
            f"LDAP error talking to {where}: "
            f"{_redact(str(exc), cfg.ldap_bind_password)}"
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never 500 the page
        return False, (
            f"Unexpected error talking to {where}: "
            f"{_redact(str(exc), cfg.ldap_bind_password)}"
        )
    finally:
        try:
            if conn is not None and conn.bound:
                conn.unbind()
        except Exception:  # noqa: BLE001 — teardown must not mask the result
            pass


@router.post("/auth-config/test-ldap", dependencies=[Depends(require_super_admin)])
async def test_ldap_connection() -> Dict:
    """Probe the *effective* LDAP config with a service-account bind only.

    Answers the question the /auth page could not: is what I just saved actually
    reachable and usable? Uses `auth.effective_auth()` — env overlaid with the
    Redis override — so it tests the config the next login will really use, not
    whatever is in `.env`. Never binds as a real user, never returns a secret,
    and is bounded by an explicit timeout (see the block comment above).

    Response: ``{"ok": bool, "detail": str, "ms": int}``.
    """

    from app import auth as authmod

    cfg = await authmod.effective_auth()
    started = time.monotonic()
    try:
        ok, detail = await asyncio.to_thread(_probe_ldap, cfg)
    except Exception as exc:  # noqa: BLE001 — never 500 a diagnostics button
        ok, detail = False, f"Probe failed to run: {exc}"
    return {"ok": ok, "detail": detail, "ms": int((time.monotonic() - started) * 1000)}


# The endpoints a login actually needs. `jwks_uri` is reported but not required:
# this app redeems the code over TLS against `token_endpoint` with the client
# secret and reads the profile from `userinfo`, so it never verifies the
# id_token signature. That reasoning only holds while the client is
# CONFIDENTIAL — make it public and JWKS becomes mandatory (see CLAUDE.md).
_OIDC_REQUIRED = ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
_OIDC_REPORTED = _OIDC_REQUIRED + ("jwks_uri",)


@router.post("/auth-config/test-oidc", dependencies=[Depends(require_super_admin)])
async def test_oidc_connection() -> Dict:
    """Fetch the *effective* OIDC discovery document and report what it offers.

    Deliberately does NOT need a client secret to be useful: the overwhelmingly
    common misconfiguration is a discovery URL that is unreachable, points at the
    wrong realm, or is missing a trailing path segment — all visible before any
    credential is exercised. So an operator can verify the realm first and add
    the client credentials second, rather than debugging both at once.

    This bypasses `auth._oidc_metadata` on purpose. That helper caches the
    document (a test button must read live, or it reports a stale success after
    the realm was fixed) and raises on the first missing endpoint (a probe should
    report *which* are missing, not stop at one).

    Response: ``{"ok": bool, "detail": str, "ms": int, "issuer": str|null,
    "endpoints": {name: url|null}|null}``.
    """

    import httpx

    from app import auth as authmod

    cfg = await authmod.effective_auth()
    started = time.monotonic()

    def done(ok: bool, detail: str, issuer=None, endpoints=None) -> Dict:
        return {
            "ok": ok,
            "detail": detail,
            "ms": int((time.monotonic() - started) * 1000),
            "issuer": issuer,
            "endpoints": endpoints,
        }

    url = (cfg.oidc_discovery_url or "").strip()
    if not url:
        return done(False, "No discovery URL is configured. It normally ends in "
                           "/realms/<realm>/.well-known/openid-configuration.")

    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as c:
            r = await c.get(url)
    except httpx.HTTPError as exc:
        return done(False, f"Could not reach the discovery URL: "
                           f"{_redact(str(exc), cfg.oidc_client_secret)}")

    if r.status_code == 404:
        return done(False, f"The provider answered 404 for {url}. The realm name in "
                           "the URL is probably wrong, or the path is missing the "
                           "/.well-known/openid-configuration suffix.")
    if r.status_code >= 400:
        return done(False, f"The provider answered HTTP {r.status_code} for the "
                           "discovery URL.")
    try:
        meta = r.json()
        if not isinstance(meta, dict):
            raise ValueError("not a JSON object")
    except Exception:  # noqa: BLE001
        return done(False, "The discovery URL answered 200 but not with a JSON "
                           "object. It is probably an HTML page (a login screen or "
                           "a proxy error), not a discovery document.")

    endpoints = {k: (meta.get(k) or None) for k in _OIDC_REPORTED}
    issuer = meta.get("issuer") or None
    missing = [k for k in _OIDC_REQUIRED if not endpoints.get(k)]
    if missing:
        return done(False, "Discovery document fetched, but it is missing: "
                           + ", ".join(missing) + ". Sign-in cannot complete without "
                           "them.", issuer, endpoints)

    notes = [f"Discovery document fetched from issuer `{issuer or 'unknown'}`."]
    if not endpoints.get("jwks_uri"):
        notes.append("No jwks_uri is published — fine for the confidential client "
                     "this app uses, but a public client could not verify tokens.")
    if not (cfg.oidc_client_id or "").strip():
        notes.append("No client ID is set yet, so sign-in is not configured.")
    if not (cfg.oidc_redirect_uri or "").strip():
        notes.append("No redirect URI is set; it must match the one registered on "
                     "the client and end in /auth/sso/callback.")
    return done(True, " ".join(notes), issuer, endpoints)


# ---- security log ----------------------------------------------------------


@router.get("/security-log", dependencies=[Depends(require_super_admin)])
async def security_log(
    limit: int = 50,
    offset: int = 0,
    event: str = "",
    email: str = "",
    frm: str = Query("", alias="from"),
    to: str = "",
    tz: str = Query(DEFAULT_TZ, description="IANA zone for the date bounds (§A)"),
) -> Dict:
    """Authentication events (`auth_events`), newest first. **super_admin only.**

    The audit trail behind the login screen: who signed in, who failed, who got
    locked out, and from where. It is super_admin-only rather than admin-only
    because it is a list of email addresses and source IPs correlated with failed
    passwords — the raw material for targeting the people in it.

    **Degrades instead of 500ing when `auth_events` does not exist.** The table
    is created by the login-audit change, which ships independently of this
    endpoint and may land after it. A missing table is a deployment state, not an
    error, so this answers with an empty result and a `detail` explaining why —
    the two changes stay independently deployable in either order, and the page
    shows "no events yet" rather than "backend offline".

    Response: ``{"total": int, "rows": [...], "detail": str}``.
    """

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)

    # Checked explicitly rather than by catching the query's exception: a blanket
    # `except` here would silently turn a real SQL or connection fault into
    # "no security events", which is the most dangerous empty state in the app.
    exists = await q("SELECT to_regclass('public.auth_events') AS t")
    if not exists or exists[0]["t"] is None:
        return {
            "total": 0,
            "rows": [],
            "detail": "The auth_events table does not exist yet, so no login "
                      "history has been recorded. It is created by the "
                      "login-audit change; deploy that to start collecting.",
        }

    conds, params = [], []
    if event:
        params.append(event)
        conds.append(f"event = ${len(params)}")
    if email:
        params.append(email)
        conds.append(f"(email ILIKE '%'||${len(params)}||'%'"
                     f" OR actor_email ILIKE '%'||${len(params)}||'%')")
    tz_v = _validate_tz(tz)
    frm_v, _ = _parse_ts(frm, "from")
    if frm_v:
        conds.append("ts >= " + _ts_expr(frm_v, params, tz_v))
    to_v, to_day = _parse_ts(to, "to")
    if to_v:
        # A bare date means the WHOLE day; see _log_filters for the same trap.
        # `tz` decides whose day, so this page's window matches the console's.
        conds.append(
            "ts < " + _ts_expr(to_v, params, tz_v, plus_day=True)
            if to_day
            else "ts <= " + _ts_expr(to_v, params, tz_v)
        )
    where = _where(conds)

    total = (await q(f"SELECT count(*) AS n FROM auth_events {where}", *params))[0]["n"]
    params.append(limit)
    params.append(offset)
    rows = await q(
        f"""SELECT id, ts, event, email, actor_email, ip, detail
              FROM auth_events {where} ORDER BY ts DESC, id DESC
             LIMIT ${len(params)-1} OFFSET ${len(params)}""",
        *params,
    )
    return {"total": _i(total), "rows": rows, "detail": ""}


# ---- authentication overview + security posture ----------------------------
#
# Both endpoints back the redesigned Authentication page and both are
# **super_admin only**, for the same reason `/auth-config` and `/security-log`
# are: together they are a map of how this deployment authenticates and where it
# is weak. `/auth-overview` names the directory host and the SSO issuer;
# `/security-config` says whether the signing secret is still the shipped
# placeholder. That is a target list, not a dashboard for any admin.
#
# ⚠️ **Neither may return a secret, or any part of one.** `/security-config`
# reports the signing key as three facts — is one set, how long is it, is it the
# published default — and nothing else. No prefix, no fingerprint: a prefix of a
# 32-byte key is a real head start, and a "harmless" first four characters is
# how key material ends up in a screenshot. Same rule as `auth.get_auth_config`,
# which masks the bind password and the client secret to a bare boolean.


@router.get("/auth-overview", dependencies=[Depends(require_super_admin)])
async def auth_overview() -> Dict:
    """Status of every sign-in method, for the Methods tab. **super_admin only.**

    The distinction this exists to draw is **enabled vs configured**. A method
    that is switched on but missing the fields a login needs does not announce
    itself anywhere: `/auth/config` puts the SSO button on the login screen from
    `oidc_enabled` alone, and the failure lands on whoever clicks it, as
    "oidc: discovery URL is not set" — at the login screen, where nobody with
    the access to fix it is looking. `configured` is the minimum set of fields
    to *attempt* a login (OIDC: discovery URL + client id + secret; LDAP: host +
    base DN), so the card can say "On — but not configured" before a user does.

    **Never touches the network.** `configured` is a field check, not a probe:
    this endpoint is loaded on every visit to the page, and a dead IdP would
    otherwise hang it for the discovery timeout. The buttons that do reach out
    are `POST /auth-config/test-oidc` / `test-ldap`, which an operator invokes
    deliberately. `issuer` is therefore the discovery URL **as configured**, not
    the `issuer` claim from a fetched document.

    `local.enabled` is hard `true`: local password is the break-glass path — the
    way back in when the directory is down or the realm was misconfigured — and
    there is deliberately no switch for it anywhere in this codebase. Reporting
    it as a settable flag would imply one exists.

    Secrets are never returned; only whether one is set feeds `configured`.
    """

    from app import auth as authmod

    cfg = await authmod.effective_auth()

    rows = await q(
        """SELECT count(*)                                        AS users,
                  count(*) FILTER (WHERE NOT approved)            AS pending,
                  count(*) FILTER (WHERE role = 'super_admin')    AS super_admins
             FROM users"""
    )
    counts = rows[0] if rows else {}

    oidc_configured = all([
        (cfg.oidc_discovery_url or "").strip(),
        (cfg.oidc_client_id or "").strip(),
        (cfg.oidc_client_secret or "").strip(),
    ])
    ldap_configured = all([
        (cfg.ldap_host or "").strip(),
        (cfg.ldap_base_dn or "").strip(),
    ])
    # use_ssl wins: `_ldap_connect` skips StartTLS when the connection is
    # already LDAPS, so reporting "starttls" for a host with both set would
    # describe a handshake that never happens.
    encryption = "ldaps" if cfg.ldap_use_ssl else ("starttls" if cfg.ldap_start_tls else "none")

    return {
        "local": {
            "enabled": True,
            "users": _i(counts.get("users")),
            "pending": _i(counts.get("pending")),
            "super_admins": _i(counts.get("super_admins")),
        },
        "oidc": {
            "enabled": bool(cfg.oidc_enabled),
            "configured": bool(oidc_configured),
            "provider_name": cfg.oidc_provider_name or "",
            "provider_type": cfg.oidc_provider_type,
            "auto_create": bool(cfg.oidc_auto_create),
            "issuer": (cfg.oidc_discovery_url or "").strip() or None,
        },
        "ldap": {
            "enabled": bool(cfg.ldap_enabled),
            "configured": bool(ldap_configured),
            "auto_create": bool(cfg.ldap_auto_create),
            "host": (cfg.ldap_host or "").strip() or None,
            "encryption": encryption,
        },
        # Which sign-in methods this deployment offers. Here as well as in
        # `/auth-config` so the settings page renders current state in one call.
        "signin_mode": cfg.signin_mode,
        # Duplicated from `local.pending` for the nav badge: the count is
        # already in hand, so the second read costs nothing, and a badge that
        # has to reach two levels down into a payload gets it wrong once.
        "pending": _i(counts.get("pending")),
        # Still no *self*-signup: nothing here creates an account for a caller
        # who cannot already authenticate against a configured directory. JIT
        # provisioning (`oidc.auto_create` / `ldap.auto_create`) is reported
        # separately above, and what it creates is a PENDING, unprivileged row.
        "self_signup": False,
    }


# The process-level half of the security config, and the env var that owns each.
# These are read-only here on purpose:
#
# * `AUTH_TOKEN_TTL_HOURS` is baked into every token already minted — lowering
#   it from a form would not shorten a single live session, so a UI control
#   would report a protection it did not apply.
# * `SECRET_KEY` rotating from a web form invalidates every issued JWT, embed
#   session and preview link at once, including the caller's own, and the value
#   must MATCH the Laravel `CITYAGENT_SECRET_KEY` — it is a deploy-time secret,
#   not a setting.
# * `COOKIE_SECURE` / `APP_ENV` describe the deployment (is there TLS in front
#   of this process); a form cannot make that true.
_READONLY_SECURITY_ENV = {
    "token_ttl_hours": "AUTH_TOKEN_TTL_HOURS",
    "auth_token_ttl_hours": "AUTH_TOKEN_TTL_HOURS",
    "secret": "SECRET_KEY",
    "secret_key": "SECRET_KEY",
    "cookie_secure": "COOKIE_SECURE",
    "app_env": "APP_ENV",
}

# The editable three, page-facing name -> settings/override name.
_EDITABLE_LOCKOUT = {
    "max_fail": "login_max_fail",
    "lock_minutes": "login_lock_minutes",
    "ip_max_fail": "login_ip_max_fail",
}

# Mirrors the boot warning in `auth.boot_security_checks`: over a day, an admin
# session outlives any plausible shift and cannot be revoked before expiry
# except by disabling the account.
_RECOMMENDED_TTL_HOURS = 24


async def _security_config_body() -> Dict:
    """The `/security-config` response. Shared by GET and PUT so they cannot drift."""

    from app import auth as authmod

    s = get_settings()
    sec = await authmod.effective_security()
    cfg = await authmod.effective_auth()

    secret = s.secret_key or ""
    ttl = int(s.auth_token_ttl_hours)

    return {
        "lockout": {
            "max_fail": int(sec.login_max_fail),
            "lock_minutes": int(sec.login_lock_minutes),
            "ip_max_fail": int(sec.login_ip_max_fail),
        },
        "session": {
            "token_ttl_hours": ttl,
            "token_ttl_default": Settings.model_fields["auth_token_ttl_hours"].default,
            "exceeds_recommended": ttl > _RECOMMENDED_TTL_HOURS,
        },
        # is_set / length / is_default and NOTHING else — see the block comment.
        "secret": {
            "is_set": bool(secret),
            "length": len(secret),
            "is_default": secret == DEFAULT_SECRET_KEY,
        },
        "cookies": {
            "cookie_secure": bool(s.cookie_secure),
            "oidc_enabled": bool(cfg.oidc_enabled),
            "warn": bool(cfg.oidc_enabled and not s.cookie_secure),
        },
        "app_env": s.app_env,
        "events_24h": await _auth_events_24h(),
    }


async def _auth_events_24h() -> Optional[int]:
    """Auth events in the last 24h, or ``None`` when the table does not exist.

    `None`, never 0. "No logins recorded" and "the audit table was never
    deployed" are different facts and the second one is the one an operator has
    to act on; collapsing them into a reassuring zero is the same class of bug
    as summing UNKNOWN stock as 0. Existence is checked with `to_regclass` for
    the reason `/security-log` gives: a blanket `except` here would turn a real
    SQL or connection fault into "no security events".
    """

    exists = await q("SELECT to_regclass('public.auth_events') AS t")
    if not exists or exists[0]["t"] is None:
        return None
    rows = await q("SELECT count(*) AS n FROM auth_events WHERE ts > now() - interval '24 hours'")
    return _i(rows[0]["n"]) if rows else 0


@router.get("/security-config", dependencies=[Depends(require_super_admin)])
async def get_security_config() -> Dict:
    """Login-throttle settings + the process-level security posture around them.

    Three of these numbers are editable (see PUT); the rest are reported so the
    page can show *why* a posture is weak next to the part it can fix — a 7-day
    token TTL and a placeholder signing key are the two findings this deployment
    actually has, and neither is visible anywhere else in the console today.

    **super_admin only**, and no secret material in the body.
    """

    return await _security_config_body()


@router.put("/security-config", dependencies=[Depends(require_super_admin)])
async def put_security_config(updates: Dict = Body(...)) -> Dict:
    """Update the three lockout numbers. Everything else is env-owned.

    Accepts either the flat form (`{"max_fail": 5}`) or the nested one the GET
    returns (`{"lockout": {...}}`), so the page can round-trip its own body.

    **Read-only fields are rejected with a 400 naming the env var that owns
    them** — but only when the value would actually CHANGE. Re-sending the
    current `token_ttl_hours` is a no-op, which is what lets a page GET the
    whole document, edit one lockout number and PUT it back. This mirrors
    `cache.set_ingest_config`, which took the same line on the locked
    `catalog_mode` for the same reason. A setting that reports success and does
    nothing is the failure mode both are avoiding: silently ignoring a
    `token_ttl_hours` in the body would tell an operator the session length had
    been shortened when nothing had happened.

    Range violations are also 400s (`max_fail` 1–100, `lock_minutes` 1–1440,
    `ip_max_fail` >= `max_fail`). Applies immediately — the login path reads the
    same effective layer, so no restart (see `auth.apply_security_overrides`).
    """

    from app import auth as authmod

    body = dict(updates or {})
    flat: Dict[str, Any] = {}
    for key, value in body.items():
        if key == "lockout" and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value

    current = await _security_config_body()
    wanted: Dict[str, Any] = {}

    for key, value in flat.items():
        if key in _EDITABLE_LOCKOUT:
            wanted[_EDITABLE_LOCKOUT[key]] = value
            continue
        if key in _READONLY_SECURITY_ENV:
            env = _READONLY_SECURITY_ENV[key]
            if _unchanged(key, value, current):
                continue                     # echoing the current value is a no-op
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{key} is set by the {env} environment variable and cannot be "
                    f"changed from the console. Edit {env} in the deployment's "
                    "environment and restart the API."
                ),
            )
        # An unknown key is a typo or a field from a newer page; either way,
        # accepting it silently would look like it had been applied.
        if key in ("session", "secret", "cookies", "app_env", "events_24h"):
            raise HTTPException(
                status_code=400,
                detail=f"{key} is read-only; only the lockout settings are editable here.",
            )
        raise HTTPException(status_code=400, detail=f"unknown setting {key!r}")

    if not wanted:
        raise HTTPException(
            status_code=400,
            detail="nothing to update; send max_fail, lock_minutes or ip_max_fail.",
        )

    try:
        await authmod.set_security_config(wanted)
    except authmod.SecurityConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _security_config_body()


def _unchanged(key: str, value: Any, current: Dict) -> bool:
    """Is *value* what `/security-config` already reports for this read-only key?

    The nested-section keys are compared field by field, so a page can PUT back
    the whole `secret` or `session` object it was given.
    """

    if key in ("token_ttl_hours", "auth_token_ttl_hours"):
        try:
            return int(value) == int(current["session"]["token_ttl_hours"])
        except (TypeError, ValueError):
            return False
    if key == "cookie_secure":
        return bool(value) == bool(current["cookies"]["cookie_secure"])
    if key == "app_env":
        return str(value) == str(current["app_env"])
    if key in ("secret", "secret_key"):
        # There is nothing here a caller could legitimately be "re-sending":
        # the GET never returns key material, so any value is an attempt to set
        # one. An identical echo of the reported metadata is the only no-op.
        return isinstance(value, dict) and value == current["secret"]
    return False


# ---- evaluation ------------------------------------------------------------


@router.post("/eval/run")
async def run_eval() -> Dict:
    """Run the eval set live through the agent. Slow (LLM calls)."""

    from pathlib import Path

    from app.agent import build_agent

    cases = json.loads(
        (Path(__file__).parent.parent / "evals" / "eval_set.json").read_text("utf-8")
    )["cases"]
    agent = build_agent()
    results, passed = [], 0
    for c in cases:
        out = await agent.arun(c["question"])
        ans = getattr(out, "content", str(out))
        na = ans.lower().replace(",", "").replace(" ", "")
        ok = all(n.lower().replace(",", "").replace(" ", "") in na
                 for n in c.get("expect_contains", []))
        if c.get("expect_script") == "my":
            ok = ok and any("က" <= ch <= "႟" for ch in ans)
        passed += ok
        results.append({"id": c["id"], "question": c["question"],
                        "pass": ok, "answer": ans[:300]})
    return {"score": f"{passed}/{len(cases)}", "passed": passed,
            "total": len(cases), "results": results}


# ---- knowledge graph (GraphRAG) -------------------------------------------


@router.get("/graph")
async def graph_status() -> Dict:
    """Edge counts per relation in the drug knowledge graph."""

    out = {}
    for rel in ("has_generic", "contains", "in_category", "treats"):
        try:
            out[rel] = (await q("SELECT count(*) n FROM drug_edges WHERE rel=$1", rel))[0]["n"]
        except Exception:
            out[rel] = 0
    return out


@router.post("/graph/rebuild")
async def graph_rebuild() -> Dict:
    """Rebuild structured edges (generic/ingredient/category) from the catalog."""

    from app.cache import bump_data_version
    from app.graph import build_edges

    res = await build_edges()
    # Substitute/related answers are derived from drug_edges, so a rebuild can
    # change them. Without the bump, cached answers outlive the graph they came
    # from for up to CACHE_TTL_SECONDS.
    res["data_version"] = await bump_data_version()
    return res


@router.post("/graph/treats")
async def graph_treats(limit: int = 200, background: bool = True) -> Dict:
    """Stage 2: LLM-extract treats-edges from indication text (bounded by limit).

    Runs in the background by default so it never blocks the worker / UI — poll
    GET /admin/graph to watch the treats count grow. Set background=false to wait.
    """

    from app.graph import build_treats_edges

    if background:
        import asyncio

        asyncio.create_task(build_treats_edges(limit=limit))
        return {"status": "started", "limit": limit, "note": "poll /admin/graph for progress"}
    return await build_treats_edges(limit=limit)


@router.get("/graph/overview")
async def graph_overview(limit: int = 80) -> Dict:
    """Galaxy view — a bounded subgraph of the richest (treats-bearing) drugs plus
    their ingredient/condition hubs and same-generic drug links. Nodes + links for
    a d3 force layout."""

    limit = min(max(limit, 10), 200)

    # `treats` is the richest seed — a drug with conditions attached pulls in
    # ingredients and siblings around it — but it is STAGE 2, extracted from
    # indication text by the model, and on a deployment where that has never run
    # there are zero of them. Seeding only from `treats` then returned an empty
    # graph forever while the page's own counters showed 16,021 structural
    # edges sitting there: the reader was told "no graph data is available yet"
    # by a page simultaneously reporting three non-zero totals.
    #
    # So: fall back to the structural graph, which every deployment has from
    # ingest alone. `seeded_from` is returned so the UI can say which layer it
    # is drawing rather than leaving the difference invisible.
    sel = await q(
        "SELECT DISTINCT src FROM drug_edges WHERE rel='treats' ORDER BY src LIMIT $1", limit
    )
    codes = [r["src"] for r in sel]
    seeded_from = "treats"
    rels = ("contains", "treats")

    if not codes:
        sel = await q(
            """SELECT src, count(*) AS n FROM drug_edges
                WHERE rel IN ('contains','has_generic','in_category')
                GROUP BY src ORDER BY n DESC, src LIMIT $1""",
            limit,
        )
        codes = [r["src"] for r in sel]
        seeded_from = "structure"
        # Category is only worth drawing in this mode: with `treats` present it
        # is the weaker of the two groupings and just crowds the layout.
        rels = ("contains", "in_category")

    if not codes:
        # Genuinely nothing — no edges at all, not merely no `treats`.
        return {"nodes": [], "links": [], "seeded_from": "none"}

    attr_edges = await q(
        """SELECT e.src, e.rel, e.dst FROM drug_edges e
            WHERE e.src = ANY($1) AND e.rel = ANY($2)""",
        codes,
        list(rels),
    )
    gen_edges = await q(
        """SELECT a.src AS d1, b.src AS d2
             FROM drug_edges a JOIN drug_edges b
               ON a.dst = b.dst AND a.rel='has_generic' AND b.rel='has_generic' AND a.src < b.src
            WHERE a.src = ANY($1) AND b.src = ANY($1)""",
        codes,
    )
    brands = {r["article_code"]: r["brand_name"] for r in await q(
        "SELECT article_code, brand_name FROM catalog WHERE article_code = ANY($1)", codes)}

    nodes, seen = [], set()
    def add(nid, ntype, label):
        if nid not in seen:
            seen.add(nid); nodes.append({"id": nid, "type": ntype, "label": label})
    for c in codes:
        add(c, "drug", brands.get(c, c))
    links = []
    NODE_TYPE = {"contains": "ing", "treats": "cond", "in_category": "cat"}
    for e in attr_edges:
        ntype = NODE_TYPE[e["rel"]]
        add(e["dst"], ntype, e["dst"])
        links.append({"source": e["src"], "target": e["dst"], "rel": e["rel"]})
    for e in gen_edges:
        links.append({"source": e["d1"], "target": e["d2"], "rel": "generic"})
    return {"nodes": nodes, "links": links, "seeded_from": seeded_from}


@router.get("/graph/node")
async def graph_node(code: str) -> Dict:
    """Detail-panel data for one article — grouped graph links + live total stock."""

    cat = await q("SELECT article_code, brand_name, generic_name FROM catalog WHERE article_code=$1", code)
    contains = [r["dst"] for r in await q("SELECT dst FROM drug_edges WHERE src=$1 AND rel='contains'", code)]
    treats = [r["dst"] for r in await q("SELECT dst FROM drug_edges WHERE src=$1 AND rel='treats'", code)]
    same_generic = await q(
        """SELECT DISTINCT e2.src AS article_code, c.brand_name
             FROM drug_edges e1 JOIN drug_edges e2
               ON e1.dst = e2.dst AND e1.rel='has_generic' AND e2.rel='has_generic' AND e2.src <> $1
             JOIN catalog c ON c.article_code = e2.src
            WHERE e1.src = $1 ORDER BY c.brand_name LIMIT 20""",
        code,
    )
    stock = await q("SELECT total_stock, site_count FROM mv_article_summary WHERE article_code=$1", code)
    return {
        "article_code": code,
        "brand_name": cat[0]["brand_name"] if cat else code,
        "generic_name": cat[0]["generic_name"] if cat else None,
        # A summary row can exist with a NULL total (every branch blank), so the
        # int() must be guarded on the VALUE, not just on the row: int(None) is a
        # TypeError -> 500, and `or 0` would turn unknown into zero.
        "total_stock": _int_or_none(stock[0]["total_stock"]) if stock else None,
        "site_count": _int_or_none(stock[0]["site_count"]) if stock else None,
        "contains": contains,
        "treats": treats,
        "same_generic": same_generic,
    }


@router.get("/graph/by-attribute")
async def graph_by_attribute(rel: str, value: str, limit: int = 16) -> Dict:
    """Articles linked to an attribute node (ingredient/condition/generic) — lets
    the graph explorer expand a non-article node."""

    limit = min(max(limit, 1), 40)
    rows = await q(
        """SELECT e.src AS article_code, c.brand_name
             FROM drug_edges e JOIN catalog c ON c.article_code = e.src
            WHERE e.rel = $1 AND e.dst = $2
            ORDER BY c.brand_name LIMIT $3""",
        rel, value, limit,
    )
    return {"center": {"value": value, "rel": rel}, "articles": rows}


@router.get("/graph/neighbors")
async def graph_neighbors(code: str, limit: int = 14) -> Dict:
    """Subgraph around an article — its attribute nodes + articles sharing them.
    Powers the admin graph visualization."""

    limit = min(max(limit, 1), 40)
    center = await q("SELECT article_code, brand_name FROM catalog WHERE article_code=$1", code)
    attrs = await q(
        "SELECT rel, dst FROM drug_edges WHERE src=$1 AND rel IN ('has_generic','contains','treats') ORDER BY rel",
        code,
    )
    related = await q(
        """SELECT DISTINCT e2.src AS article_code, c.brand_name, e1.rel, e1.dst AS via
             FROM drug_edges e1
             JOIN drug_edges e2 ON e2.dst = e1.dst AND e2.rel = e1.rel AND e2.src <> e1.src
             JOIN catalog c ON c.article_code = e2.src
            WHERE e1.src = $1 AND e1.rel IN ('has_generic','contains','treats')
            ORDER BY e1.rel, c.brand_name LIMIT $2""",
        code, limit,
    )
    return {
        "center": center[0] if center else {"article_code": code, "brand_name": code},
        "attributes": attrs,
        "related": related,
    }


# ---- manual upload + SFTP status -------------------------------------------


@router.post("/upload")
async def upload(
    file: UploadFile = File(...), allow_shrink: bool = False
) -> Dict:
    """Validate an article/balance xlsx/csv, then REPLACE the matching table.

    Rejected files return 422 with the reasons and never reach the drop folder.
    ``allow_shrink=true`` overrides the guard on a file that would delete more
    than half the existing rows.
    """

    from pathlib import Path

    from app.watcher import scan_once

    import asyncio
    import tempfile

    from app.validation import check_shrink, validate_file

    name = Path(file.filename or "upload.xlsx").name
    if not name.lower().endswith((".xlsx", ".csv")):
        raise HTTPException(status_code=400, detail="only .xlsx or .csv files accepted")

    payload = await file.read()

    # Validate in a temp dir FIRST. The load replaces the table outright, so a
    # rejected file must never reach the drop folder — if it did, the watcher
    # would pick it up on its next poll and the API's rejection would mean
    # nothing. Staging here keeps "rejected" and "not ingested" the same thing.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / name
        staged.write_bytes(payload)
        report = await check_shrink(
            await asyncio.to_thread(validate_file, str(staged)),
            allow_shrink=allow_shrink,
        )
        if not report.ok:
            raise HTTPException(status_code=422, detail=report.as_dict())

    incoming = Path(get_settings().incoming_dir)
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / name
    dest.write_bytes(payload)
    summary = await scan_once(stable_only=False, allow_shrink=allow_shrink)
    return {
        "status": "uploaded",
        "file": name,
        "validation": report.as_dict(),
        **summary,
    }


@router.post("/validate", dependencies=[Depends(require_super_admin)])
async def validate_upload(file: UploadFile = File(...)) -> Dict:
    """Dry run: check a file and report, without loading anything.

    Lets an operator confirm a monthly export is well-formed before it replaces
    live data — the useful order of operations when the load is destructive.
    """

    import asyncio
    import tempfile

    from app.validation import check_shrink, validate_file

    name = Path(file.filename or "upload.xlsx").name
    payload = await file.read()
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / name
        staged.write_bytes(payload)
        report = await check_shrink(await asyncio.to_thread(validate_file, str(staged)))
    return report.as_dict()


# ---- ingest config + manual stale purge ------------------------------------
#
# These change ingest behaviour for the whole tenant (both the api and the
# worker container read the Redis config), and the purge is the ONLY delete path
# the operator can trigger from the UI, so every endpoint here is super_admin
# only — the router-level require_admin is not enough.


# ---- CORS allowed origins (runtime-managed) --------------------------------


class CorsOrigin(BaseModel):
    origin: str


@router.get("/cors-origins", dependencies=[Depends(require_super_admin)])
async def cors_origins_list() -> Dict:
    """The CORS allowlist: env origins (read-only) + runtime origins (editable).

    A browser widget on a customer site can only call the embed API if that
    site's origin is here. ``env`` comes from ``ALLOWED_ORIGINS`` and needs a
    restart to change; ``runtime`` is what this page adds and takes effect within
    seconds, no restart.
    """

    from app.api import cors_origins as _env_origins

    return {
        "env": sorted(o.lower() for o in _env_origins()),
        "runtime": sorted(await cache.get_cors_origins()),
    }


@router.post("/cors-origins", dependencies=[Depends(require_super_admin)])
async def cors_origins_add(c: CorsOrigin) -> Dict:
    """Allow a browser origin to call the embed API. Live within seconds."""

    try:
        added = await cache.add_cors_origin(c.origin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "origin": added}


@router.delete("/cors-origins", dependencies=[Depends(require_super_admin)])
async def cors_origins_remove(origin: str) -> Dict:
    """Remove a runtime origin. Env origins cannot be removed here."""

    removed = await cache.remove_cors_origin(origin)
    return {"status": "ok", "removed": removed}


# ---- answer style (crisp / standard / detailed) ----------------------------


class AnswerStyleUpdate(BaseModel):
    style: str


@router.get("/releases")
async def releases() -> Dict:
    """Full release history + the running build, for the console's Version page.

    Admin-only (the router already requires a bearer token) while `/version`
    is public: a version string tells an attacker nothing, but the changelog
    names which fixes are absent from an older deployment.

    Read from CHANGELOG.md on every call rather than cached — the file is a few
    KB, it changes only on deploy, and a stale release note is exactly the thing
    this page exists to prevent.
    """

    from app import release_notes
    from app.version import version_info

    entries = release_notes.load()
    return {
        "current": version_info(),
        "releases": entries,
        # So the UI can flag the case where the running build is not the one the
        # notes describe — a rebuild that skipped the changelog, or a changelog
        # edited without a rebuild. Both have to be visible or the page lies.
        "notes_match_build": bool(entries) and entries[0]["version"] == version_info()["version"],
        "known_sections": list(release_notes.KNOWN_SECTIONS),
    }


@router.get("/answer-style", dependencies=[Depends(require_super_admin)])
async def answer_style_get() -> Dict:
    """The configured answer length + the available options."""

    return {"style": await cache.get_answer_style(), "options": list(cache.ANSWER_STYLES)}


@router.post("/answer-style", dependencies=[Depends(require_super_admin)])
async def answer_style_set(u: AnswerStyleUpdate) -> Dict:
    """Set the answer length. Bumps data_version so cached answers in the old
    style are dropped — the same words should not return a long answer after a
    switch to crisp."""

    try:
        style = await cache.set_answer_style(u.style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await cache.bump_data_version()
    return {"style": style, "options": list(cache.ANSWER_STYLES)}


# ---- per-outlet embed snippets (static, pre-signed, download-and-go) --------
#
# Each outlet gets a snippet with its ``store_id`` baked in and HMAC-signed
# server-side, so the widget is locked to that one store. The customer's dev
# just drops the <script> tag on their site — no login, no signing on their end.
# The signature is a bearer token for that ONE store's stock (availability, no
# prices cross-store); that is the intended scope for an outlet embed.


class OutletSnippetRequest(BaseModel):
    store_id: str
    embed_id: str
    public_key: str
    base_url: str
    title: Optional[str] = None
    accent: Optional[str] = "#2F3293"
    stream: bool = True


def _outlet_user(store_id: str) -> Dict[str, str]:
    """The signed identity baked into an outlet snippet. Stable per store."""

    return {"id": f"outlet:{store_id}", "store_id": store_id}


def _snippet_html(req: "OutletSnippetRequest", signature: str, *, auto_open: bool = False) -> str:
    """The <script> tag a customer dev pastes onto their site.

    ``auto_open`` is a PREVIEW-ONLY escape hatch and must stay default-off: the
    snippet customers copy, download and already have in live HTML is defined by
    the ``False`` branch, and it must keep producing the same bytes it always
    has. It is a keyword argument rather than a field on
    ``OutletSnippetRequest`` for the same reason — the request model is what the
    admin UI posts, and nothing a customer can send should be able to make their
    own embed open itself on every page load.
    """

    import html as _html

    base = req.base_url.rstrip("/")
    user_json = json.dumps(_outlet_user(req.store_id), separators=(",", ":"), ensure_ascii=False)
    title = req.title or f"Pharmacy · {req.store_id}"
    attrs = [
        f'src="{_html.escape(base)}/api/embed/widget.js"',
        f'data-embed-id="{_html.escape(req.embed_id)}"',
        f'data-public-key="{_html.escape(req.public_key)}"',
        f"data-user='{_html.escape(user_json)}'",
        f'data-user-sig="{signature}"',
        f'data-title="{_html.escape(title)}"',
        f'data-accent="{_html.escape(req.accent or "#2F3293")}"',
        f'data-stream="{"true" if req.stream else "false"}"',
    ]
    if auto_open:
        # widget.js: data-open="true" starts the panel OPEN; absent (the
        # customer snippet) keeps today's closed-launcher behaviour.
        attrs.append('data-open="true"')
    attrs.append("async")
    return "<script " + "\n        ".join(attrs) + "></script>"


def _demo_page(req: "OutletSnippetRequest", snippet: str, *, auto_open: bool = False) -> str:
    """A complete standalone page that already works — open it, ask a question.

    ``auto_open`` defaults OFF, and the default is the safety property: this
    function's output reaches CUSTOMERS in two places — ``demo_html`` in the
    ``/embed/snippet`` response and ``index.html`` in the outlet ZIP — and a
    widget that opens itself on every page load of a pharmacy's own site is a
    product defect. Defaulting on would make that the outcome of forgetting an
    argument. Only ``GET /embed/preview`` asks for it, explicitly, because that
    page exists to SHOW the widget: rendered inside the console, a closed 58px
    launcher in the far bottom-right is often below the fold, so the one thing
    the reader came to try is the one thing they cannot see, and the panel reads
    as a page that failed to load.

    Note that when ``auto_open`` is set the ``snippet`` argument is REBUILT and
    the passed value discarded — pass a customised snippet with ``auto_open`` and
    the customisation is lost.

    Everything here is inline: the page must render standalone and offline, off
    a saved file, with no CSS, font, image or script fetched from anywhere.
    """

    import html as _html

    if auto_open:
        # Preview-only. Rebuilt rather than string-patched so the tag comes from
        # the one function that knows the attribute list; the signature is
        # deterministic per store, so this is the same HMAC lock the caller
        # baked in — the store scope is unchanged.
        from app.security import sign_user

        snippet = _snippet_html(req, sign_user(_outlet_user(req.store_id)), auto_open=True)

    store = _html.escape(req.store_id)
    title = _html.escape(req.title or ("Pharmacy · " + req.store_id))
    accent = _html.escape(req.accent or "#2F3293")

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{title}</title>\n"
        # EVERY content rule is scoped under `.demo`. The widget injects its
        # launcher and panel into this same document.body, so a bare `li`,
        # `ul` or `p` selector here repaints the ASSISTANT'S OWN ANSWERS. That
        # is not hypothetical: an unscoped
        #     ul{list-style:none;display:flex} li{...;font-family:ui-monospace}
        # turned a five-item answer into a row of grey monospace pills inside
        # the chat panel. The markdown was correct the whole time; this page
        # was painting over it. Do not reintroduce a bare element selector.
        "<style>\n"
        "*{box-sizing:border-box}\n"
        "body{font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;"
        "color:#18181b;background:#f6f7f9}\n"
        f".demo header{{background:#fff;border-bottom:1px solid #e5e7eb;border-top:3px solid {accent}}}\n"
        ".demo .bar{max-width:640px;margin:0 auto;padding:10px 20px;display:flex;"
        "align-items:baseline;gap:10px;flex-wrap:wrap}\n"
        ".demo .bar b{font-size:15px;letter-spacing:.2px}\n"
        ".demo .bar span{font-size:12px;color:#71717a}\n"
        ".demo main{max-width:640px;margin:0 auto;padding:20px}\n"
        ".demo .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px}\n"
        ".demo h1{font-size:19px;line-height:1.3;margin:0 0 8px}\n"
        ".demo p{margin:0 0 10px;color:#3f3f46}\n"
        ".demo ul{margin:0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:8px}\n"
        ".demo li{background:#f4f4f5;border:1px solid #e5e7eb;border-radius:999px;"
        "padding:4px 11px;font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}\n"
        ".demo .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#71717a;"
        "margin:14px 0 7px}\n"
        f".demo .note{{margin:14px 0 0;font-size:13px;color:#52525b;border-left:2px solid {accent};"
        "padding-left:10px}\n"
        ".demo footer{max-width:640px;margin:0 auto;padding:0 20px 24px;font-size:12px;color:#a1a1aa}\n"
        "</style>\n"
        "</head>\n<body>\n"
        # The wrapper the CSS above is scoped to. The widget's own nodes are
        # siblings of this div, so nothing in here can reach them.
        "<div class=\"demo\">\n"
        f"<header><div class=\"bar\"><b>Store {store}</b>"
        "<span>Pharmacy · example customer site</span></div></header>\n"
        "<main><div class=\"card\">\n"
        # The product's own name, not a second one invented here. `title` is
        # already resolved above (req.title, or the per-store default), so a
        # rename reaches this heading the same way it reaches the panel.
        f"<h1>{title} — store {store}</h1>\n"
        "<p>Ask what is on the shelf here, or where else to find it. The assistant "
        "is locked to this store and answers for no other branch.</p>\n"
        "<div class=\"lbl\">Try asking</div>\n"
        "<ul><li>do you have ROYAL-D 25G</li>"
        "<li>which other stores have ROYAL-D 25G</li></ul>\n"
        "<p class=\"note\">The chat panel below is the embedded widget — on a live "
        "site it sits in the corner behind a launcher button.</p>\n"
        "</div></main>\n"
        "<footer>This page is a demonstration of the embed. Everything above the "
        "widget is the customer&rsquo;s own site.</footer>\n"
        "</div>\n"
        "<!-- CityAgent pharmacy embed — paste this <script> on your own site -->\n"
        f"{snippet}\n"
        "</body>\n</html>\n"
    )


def _outlet_readme(req: "OutletSnippetRequest") -> str:
    return (
        f"Outlet embed — store {req.store_id}\n"
        "=====================================\n\n"
        "1. Copy the <script> tag from snippet.txt onto any page of your site\n"
        "   (or open index.html to see it working first).\n"
        f"2. Ask CityAgent's admin to add your site's origin to CORS (e.g.\n"
        "   https://your-domain.com) — otherwise the browser blocks the widget.\n"
        f"3. Backend: {req.base_url.rstrip('/')}\n\n"
        "The widget only ever answers for this one store. No login, no keys to keep.\n"
    )


async def _embeddable_codes() -> set:
    """The store codes a snippet or preview link may be minted for.

    ``stores.active_codes()`` — which is built as *everything the data knows
    about, minus what is explicitly disabled*, so an unregistered branch stays
    embeddable and only a deliberate disable removes one. Both callers hand out a
    PRE-SIGNED, store-locked artefact that outlives this request — a snippet
    pasted onto a customer's site, or a link mailed to their developer — so a
    disabled branch has to be refused at minting time; there is no later gate.

    An EMPTY result is honoured, not overridden: `active_codes` unions the
    registry with `inventory`, so it can only come back empty when every branch
    is explicitly disabled (or there is no data at all), and that is an operator
    decision rather than a broken registry.

    ⚠️ **This is the one path in this section that FAILS CLOSED.** Every other
    registry read here falls back to the raw `inventory` list when the registry
    cannot answer, because the cost of being wrong is a list that shows a branch
    it need not have — visible, and corrected on the next request. Minting is not
    that. A snippet is pasted onto a customer's site and a preview link is mailed
    to their developer; both outlive this request by months and **there is no
    later gate** — nothing re-checks the branch's status when the widget loads.
    Falling back to `inventory` here would mean a DB blip is enough to mint a
    permanent, working embed for a branch somebody deliberately hid, and nobody
    would ever learn it happened.

    So: 503 rather than a guess. Disabling a branch is rare and deliberate;
    refusing to mint for the seconds an outage lasts costs an operator one retry,
    and it is the one place where guessing wrong does not self-correct.
    """

    try:
        from app import stores as stores_mod

        return await stores_mod.active_codes()
    except Exception as exc:  # noqa: BLE001 — module absent, table absent, DB blip
        raise HTTPException(
            status_code=503,
            detail="cannot confirm which branches are visible; refusing to mint "
                   "an embed. Try again shortly.",
        ) from exc


async def _validate_outlet_request(req: "OutletSnippetRequest") -> None:
    if not await cache.is_valid_credential(req.embed_id, req.public_key):
        raise HTTPException(status_code=400, detail="embed_id / public_key are not a registered credential")
    if not re.match(r"^https?://[^/\s]+", req.base_url):
        raise HTTPException(status_code=400, detail="base_url must be a full http(s) origin")
    # A disabled branch is refused with the same 404 as a code that was never
    # seen: to the customer-facing surface the two are the same fact — this
    # branch does not exist — and a distinguishable response would tell anyone
    # holding a credential which branches have been taken offline.
    if req.store_id not in await _embeddable_codes():
        raise HTTPException(status_code=404, detail=f"unknown store_id {req.store_id!r}")


# ---- shareable preview links ------------------------------------------------
#
# `_demo_page` above already produces a page that works, but it only ever
# existed as a STRING in the `/embed/snippet` response — the only way to try an
# embed was to save a file. There is no URL to open or to send to the outlet's
# developer.
#
# A URL cannot carry a Bearer header: `app.include_router(admin_router,
# dependencies=[Depends(require_admin)])` guards EVERY /admin/* route, and a
# browser navigation (or an <iframe src>) sends no Authorization. So the page is
# served from a route registered on the app itself (`GET /embed/preview`, in
# app/api.py) and the ONLY thing authorising it is the signed token below. The
# minting half stays here, behind super_admin, so a preview link can only ever
# be created by someone who could already generate the snippet.

PREVIEW_PURPOSE = "embed_preview"
PREVIEW_TTL_SECONDS = 1800


def _mint_preview_token(req: "OutletSnippetRequest", ttl_seconds: int = PREVIEW_TTL_SECONDS) -> str:
    """Sign the outlet's demo-page parameters into a short-lived HS256 token.

    ``purpose`` is not decoration. The same ``secret_key`` signs the widget's
    chat session tokens (``app.security.create_session_token``), which are handed
    to every browser that loads an embed. Without an explicit purpose claim,
    *any* of those would decode cleanly here and render a preview page for
    whatever store it named. The claim — checked on the way back in — is what
    keeps the two token families apart.

    ``base_url`` is deliberately NOT a claim: the page is served from the host
    the link points at, so the serving request already knows its own origin, and
    a self-declared origin in a token is one more thing to have to trust.
    """

    import time as _time

    secret = get_settings().secret_key
    if not secret:
        raise HTTPException(status_code=500, detail="secret_key required to mint preview links")
    now = int(_time.time())
    return jwt.encode(
        {
            "purpose": PREVIEW_PURPOSE,
            "store_id": req.store_id,
            "embed_id": req.embed_id,
            "public_key": req.public_key,
            "title": req.title,
            "accent": req.accent,
            "stream": req.stream,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm="HS256",
    )


def _decode_preview_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify a preview token; return its claims, or ``None`` for anything else.

    One return value for absent / malformed / forged / expired / wrong-purpose,
    on purpose: the caller turns ``None`` into a flat 404, so probing the route
    cannot tell "this link has expired" from "this link never existed". A
    distinguishable response would let anyone enumerate which store codes have
    live previews.
    """

    secret = get_settings().secret_key
    if not token or not secret:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "purpose", "store_id"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("purpose") != PREVIEW_PURPOSE:
        return None          # a chat session token is signed with the SAME key
    if not claims.get("store_id"):
        return None
    return claims


@router.get("/embed/outlets", dependencies=[Depends(require_super_admin)])
async def embed_outlets() -> List[Dict]:
    """Every store that can be embedded, with SKU/unit counts for the picker.

    "Can be embedded" is a REGISTRY question, not a stock one. This used to
    ``GROUP BY site_code FROM inventory``, which both hid an active branch whose
    stock had not landed yet and offered a disabled branch — the one a customer
    must never be pointed at. A snippet is pasted onto a customer's site and
    left there for years, so this list is where a disabled branch has to stop.

    An active branch with no stock rows is still offered, with zeroes. That is
    the point of the registry: the branch exists, today's file just did not
    mention it, and refusing to embed it would let a broken export take a live
    branch off the air.

    ⚠️ The filter is ``status != 'disabled'``, not ``status == 'active'``. Same
    rule as ``stores.not_disabled_clause``: a branch is offered unless somebody
    deliberately hid it, so a row the registry has no opinion about is offered
    rather than silently dropped. See the note at the top of the stores section.

    Registry raised -> the pre-registry list, for the reason recorded there: an
    empty picker reads as "this product has no branches".
    """

    rows = await _registry_rows()
    if rows is None:
        return await q(
            """
            SELECT site_code,
                   COUNT(*)        AS skus,
                   SUM(stock_qty)  AS units
              FROM inventory
             GROUP BY site_code
             ORDER BY site_code
            """
        )

    out = [
        {
            "site_code": r["site_code"],
            "site_name": r.get("site_name"),
            "skus": int(r.get("skus") or 0),
            "units": int(r.get("units") or 0),
        }
        for r in rows
        if r.get("status") != "disabled"
    ]
    out.sort(key=lambda r: r["site_code"])
    return out


@router.post("/embed/snippet", dependencies=[Depends(require_super_admin)])
async def embed_snippet(req: OutletSnippetRequest) -> Dict:
    """Generate a pre-signed, store-locked embed snippet for one outlet."""

    from app.security import sign_user

    await _validate_outlet_request(req)
    user = _outlet_user(req.store_id)
    signature = sign_user(user)          # server-side secret; never leaves the box
    snippet = _snippet_html(req, signature)
    return {
        "store_id": req.store_id,
        "user": user,
        "signature": signature,
        "snippet": snippet,
        # Customer-facing HTML: the <script> in it must match snippet exactly.
        "demo_html": _demo_page(req, snippet, auto_open=False),
    }


_DEV_EMBED_CREDENTIAL = ("web", "web")


async def _default_embed_credential() -> Tuple[str, str]:
    """The credential a snippet minted from the branch panel is signed for.

    `/embed/snippet` takes the credential from the request because the Embed page
    has a picker and an operator standing at it. The branch detail panel has
    neither: it shows one branch's code as a fact about that branch, so the
    choice has to be made here, once, rather than as a third copy of the
    heuristic already spelled out in `GuidePanel.svelte` and `WidgetPanel.svelte`.

    ``web``/``web`` is the dev-only pair; a snippet signed with it is rejected in
    any deployment that has registered real credentials, so a real one wins
    whenever one exists. Ties are broken by sorting so the same branch does not
    hand out a snippet for a different tenant on the next page load — a customer
    pasting two different snippets for one branch would have no way to tell which
    of them was the one that works.

    No credential at all is a 400 naming what to do about it, not a snippet with
    a placeholder in it: `is_valid_credential` is dev-open (it accepts anything
    while the credential hash is empty — see the note in CLAUDE.md), so a
    placeholder would validate here and then 401 on the customer's site.
    """

    creds = await cache.list_credentials() or {}
    real = sorted(
        (eid, key) for eid, key in creds.items()
        if (eid, key) != _DEV_EMBED_CREDENTIAL
    )
    if real:
        return real[0]
    if _DEV_EMBED_CREDENTIAL[0] in creds:
        return _DEV_EMBED_CREDENTIAL
    raise HTTPException(
        status_code=400,
        detail="no embed credential is registered; add one on the Tenants page "
               "before handing a branch its website code",
    )


@router.get("/stores/{site_code}/embed", dependencies=[Depends(require_super_admin)])
async def store_embed(
    request: Request,
    site_code: str = Depends(_site_code_path),
    scope: Optional[str] = Depends(caller_store_scope),
) -> Dict:
    """The branch detail panel's "Website code" block, for one branch.

    **This is not a second signing path.** It resolves the two things the panel
    cannot supply — which credential, and this deployment's own origin — and then
    calls :func:`embed_snippet` itself. Validation, the disabled-branch refusal
    and `sign_user` are reached through that one function, so a snippet from here
    is byte-identical to the one the Embed page produces for the same branch, and
    a change to the refusal rules cannot apply to one caller and not the other.

    Consequently **a disabled branch is refused with 404 — the same 404 an
    unknown code gets** (`_validate_outlet_request`), and for the reason recorded
    there: a distinguishable response would tell any console user which branches
    have been taken offline. The mockup's disabled Copy button is the courtesy;
    this is the enforcement. And it has to be enforced at minting, because a
    snippet is pasted onto a customer's site and nothing re-checks the branch's
    status when the widget later loads.

    ``base_url`` is the serving request's own origin rather than anything the
    caller sent — uvicorn runs with `--proxy-headers`, so behind TLS this is the
    https origin the browser used, which is the whole reason that flag is
    load-bearing (see CLAUDE.md).

    GET, unlike its POST sibling, because the panel shows the code on open and
    nothing here is a nonce: `sign_user` signs a stable per-store identity, so
    two calls for one branch return the same bytes.

    The response is `embed_snippet`'s, plus the three fields the panel would
    otherwise have to fetch a credential list to learn (`embed_id`,
    `public_key`, `base_url`) — enough to POST `/embed/preview-link` for the
    Preview button without a second round trip. `public_key` is public by
    construction: it is already inside the snippet body.
    """

    if not await _scope_permits(scope, site_code):
        raise HTTPException(status_code=404, detail=f"unknown site_code {site_code!r}")

    embed_id, public_key = await _default_embed_credential()
    # No `title` and no `accent`: both default inside `_snippet_html` /
    # `OutletSnippetRequest`, and the Embed page sends neither either. Passing a
    # product name from here would be the branding trap in CLAUDE.md — a title
    # baked into a customer's HTML that survives a rename.
    req = OutletSnippetRequest(
        store_id=site_code,
        embed_id=embed_id,
        public_key=public_key,
        base_url=str(request.base_url),
    )
    out = await embed_snippet(req)
    out["embed_id"] = embed_id
    out["public_key"] = public_key
    out["base_url"] = req.base_url
    return out


@router.post("/embed/preview-link", dependencies=[Depends(require_super_admin)])
async def embed_preview_link(req: OutletSnippetRequest) -> Dict:
    """Mint a shareable, short-lived URL that renders the outlet's demo page.

    Same validation as ``/embed/snippet`` — an unregistered credential or an
    unknown store is refused identically, so a preview link can never exist for
    a store the snippet generator itself would refuse.

    The link expires in 30 minutes and is NOT renewable from the link itself:
    extending it means another authenticated admin call. It is a "try it now /
    send it to the dev" affordance, not a way to publish the widget.
    """

    await _validate_outlet_request(req)
    token = _mint_preview_token(req)
    return {
        "url": f"{req.base_url.rstrip('/')}/embed/preview?t={token}",
        "expires_in": PREVIEW_TTL_SECONDS,
        "store_id": req.store_id,
    }


@router.post("/embed/snippets.zip", dependencies=[Depends(require_super_admin)])
async def embed_snippets_zip(req: OutletSnippetRequest = Body(...)) -> Any:
    """One ZIP with a folder per store (demo page + snippet + README).

    ``store_id`` in the body is ignored — every store is generated. Hand each
    outlet's dev their own folder.
    """

    import io
    import zipfile

    from fastapi import Response

    from app.security import sign_user

    if not await cache.is_valid_credential(req.embed_id, req.public_key):
        raise HTTPException(status_code=400, detail="embed_id / public_key are not a registered credential")
    if not re.match(r"^https?://[^/\s]+", req.base_url):
        raise HTTPException(status_code=400, detail="base_url must be a full http(s) origin")

    # Same gate as `_validate_outlet_request`. This route builds a snippet for
    # EVERY store without going through that function, so leaving it on
    # `SELECT DISTINCT site_code FROM inventory` would have packed a working,
    # pre-signed embed for a disabled branch into the very ZIP an operator hands
    # to outlet developers — the one artefact nobody re-checks afterwards.
    codes = sorted(await _embeddable_codes())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "README.txt",
            f"CityAgent pharmacy — per-outlet embeds ({len(codes)} stores)\n"
            f"Backend: {req.base_url.rstrip('/')}\n\n"
            "One folder per store. Give each outlet's developer their own folder.\n"
            "Each folder has: index.html (working demo), snippet.txt (the tag to\n"
            "paste), README.txt (steps). Remember to add each site's origin to CORS.\n",
        )
        for code in codes:
            one = req.model_copy(update={"store_id": code})
            sig = sign_user(_outlet_user(code))
            snip = _snippet_html(one, sig)
            folder = f"outlet-{code}"
            # Ships next to snippet.txt in the customer's folder — same tag.
            zf.writestr(f"{folder}/index.html", _demo_page(one, snip, auto_open=False))
            zf.writestr(f"{folder}/snippet.txt", snip + "\n")
            zf.writestr(f"{folder}/README.txt", _outlet_readme(one))
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="outlet-embeds.zip"'},
    )


# ---- embed preflight (zero-LLM, real probes) --------------------------------
#
# The Embed test page shows a checklist before an outlet goes live. A checklist
# that GUESSES is worse than no checklist: a green tick next to "this store
# cannot read another branch's stock" is a claim about the leak class CLAUDE.md
# records shipping twice, so it has to be MEASURED, not asserted.
#
# Every field below is computed from a real query or a real tool call. Nothing
# here calls an LLM and nothing leaves the box. Where a check cannot be run —
# a store with no inventory, no sibling branch to probe against — the answer is
# ``None`` (UNKNOWN, rendered grey), never ``True``.


class EmbedPreflightRequest(BaseModel):
    """The outlet an operator is about to hand a snippet to."""

    store_id: str
    embed_id: str
    public_key: str


# How many articles the scope probe reads. Enough that a leak shows up as a
# handful of rows rather than one, cheap enough to stay well inside a page load.
_PREFLIGHT_PROBE_ARTICLES = 10


async def _preflight_sites_matching(token: str) -> List[str]:
    """Real ``site_code`` values that ``token`` resolves to, matched ANCHORED.

    Goes through ``tools._site_clause`` — the same predicate every scoped tool
    query uses — rather than a Python re-implementation, so this can never drift
    from what the agent actually sees. A token resolving to more than one branch
    is not a scoped store and is reported as such.
    """

    rows = await q(
        "SELECT DISTINCT site_code FROM inventory WHERE "
        + _site_clause("site_code", "$1")
        + " ORDER BY site_code",
        token,
    )
    return [r["site_code"] for r in rows]


async def _preflight_unmatched(sites: List[str], token: str) -> List[str]:
    """Of ``sites``, the ones that do NOT belong to ``token``.

    The membership test is delegated to Postgres with ``_site_clause`` against an
    unnested array, for the same reason as above: the endpoint must judge rows by
    the identical matcher the query used to fetch them, or it grades its own
    homework with a different pen.
    """

    if not sites:
        return []
    rows = await q(
        "SELECT s AS site_code FROM unnest($1::text[]) AS s "
        "WHERE NOT " + _site_clause("s", "$2"),
        sites,
        token,
    )
    return [r["site_code"] for r in rows]


@router.post("/embed/preflight", dependencies=[Depends(require_super_admin)])
async def embed_preflight(req: EmbedPreflightRequest) -> Dict:
    """Verify — by running them — the three things an outlet embed depends on.

    Returns ``credential`` / ``scope`` / ``cors``. Each ``ok`` is one of
    ``True`` (verified), ``False`` (verified broken) or ``None`` (could not be
    checked; the reason is in ``detail``). The UI renders ``None`` grey — it must
    never be shown as a pass.

    The ``scope`` check is the load-bearing one. It enters the real
    ``_STORE_SCOPE`` contextvar via ``tools.set_store_scope`` (never by passing a
    site argument — that is not the path the app uses), calls the real tools, and
    asserts three separate facts:

    1. every row the agent could read belongs to this store, matched anchored;
    2. exactly one branch is visible under scope;
    3. a REAL sibling branch's article, chosen from live inventory, stays
       invisible. Without (3) an empty result set would look like a pass.
    """

    from app import tools

    out: Dict[str, Any] = {}

    # ---- 1. credential ------------------------------------------------------
    # The same fail-closed check the snippet/preview endpoints make (and the same
    # one /api/embed/session/create makes), so a green tick here means the widget
    # will actually be let in.
    cred_ok = await cache.is_valid_credential(req.embed_id, req.public_key)
    out["credential"] = {
        "ok": bool(cred_ok),
        "detail": (
            f"{req.embed_id} accepted"
            if cred_ok
            else f"{req.embed_id} / public key are not a registered credential"
        ),
    }

    # ---- 2. store scope -----------------------------------------------------
    out["scope"] = await _preflight_scope(req.store_id, tools)

    # ---- 3. CORS ------------------------------------------------------------
    # Read exactly as GET /admin/cors-origins does: env origins plus the runtime
    # Redis set. No comparison against a customer domain happens here — the UI
    # owns that, because only the UI knows which domain the operator typed.
    from app.api import cors_origins as _env_origins

    origins = sorted({o.lower() for o in _env_origins()} | set(await cache.get_cors_origins()))
    out["cors"] = {
        "wildcard": "*" in origins,
        "count": len(origins),
        "origins": origins,
    }
    return out


async def _preflight_scope(store_id: str, tools: Any) -> Dict[str, Any]:
    """Run the store-scope probe. Split out so the endpoint reads as three checks.

    ``tools`` is passed in rather than imported here so both halves use the one
    module object — the contextvar lives on it, and a second import path would
    be a different variable.
    """

    unknown = {"rows_checked": 0, "sites_visible": 0, "sibling_leaked": None}

    matched = await _preflight_sites_matching(store_id)
    if not matched:
        return {
            "ok": None,
            "detail": f"{store_id!r} matches no branch with inventory rows — nothing to check",
            **unknown,
        }
    if len(matched) > 1:
        return {
            "ok": False,
            "detail": (
                f"{store_id!r} resolves to {len(matched)} branches "
                f"({', '.join(matched)}) — an embed token must name exactly one"
            ),
            "rows_checked": 0,
            "sites_visible": len(matched),
            "sibling_leaked": None,
        }
    mine = matched[0]

    # Articles this branch stocks, used as the probe set. Chosen before the scope
    # is entered: picking them under scope would only prove the scope agrees with
    # itself.
    probe_rows = await q(
        "SELECT DISTINCT article_code FROM inventory WHERE "
        + _site_clause("site_code", "$1")
        + " ORDER BY article_code LIMIT $2",
        store_id,
        _PREFLIGHT_PROBE_ARTICLES,
    )
    probes = [r["article_code"] for r in probe_rows]
    if not probes:
        return {
            "ok": None,
            "detail": f"{mine} has no inventory rows — nothing to read, so nothing to prove",
            **unknown,
        }

    # A real sibling branch and an article it stocks that THIS branch does not.
    # Scoped correctly the tools return nothing for it; leaking, they return the
    # sibling's row. An article stocked at both branches would prove nothing.
    sib_rows = await q(
        "SELECT i.site_code, i.article_code FROM inventory i "
        " WHERE NOT " + _site_clause("i.site_code", "$1") +
        "   AND NOT EXISTS ("
        "         SELECT 1 FROM inventory m"
        "          WHERE m.article_code = i.article_code"
        "            AND " + _site_clause("m.site_code", "$1") + ")"
        " ORDER BY i.site_code, i.article_code LIMIT 1",
        store_id,
    )
    sibling_site = sib_rows[0]["site_code"] if sib_rows else None
    sibling_article = sib_rows[0]["article_code"] if sib_rows else None

    token = tools.set_store_scope(store_id)
    try:
        seen_sites: List[str] = []
        rows_checked = 0
        for code in probes:
            for row in await tools.get_stock(code):
                rows_checked += 1
                seen_sites.append(row["site_code"])
        site_rows = await tools.list_sites("")
        seen_sites.extend(r["site_code"] for r in site_rows)
        sibling_rows = (
            await tools.get_stock(sibling_article) if sibling_article else []
        )
    finally:
        tools.reset_store_scope(token)

    distinct = sorted(set(seen_sites))
    stray = await _preflight_unmatched(distinct, store_id)
    sites_visible = len(distinct)
    sibling_leaked = None if sibling_article is None else bool(sibling_rows)

    if stray:
        return {
            "ok": False,
            "detail": (
                f"{rows_checked} rows over {len(probes)} articles under scope {store_id!r}, "
                f"but {len(stray)} branch(es) outside it were readable: {', '.join(stray)}"
            ),
            "rows_checked": rows_checked,
            "sites_visible": sites_visible,
            "sibling_leaked": sibling_leaked,
        }
    if sites_visible != 1:
        return {
            "ok": False,
            "detail": (
                f"{sites_visible} branches visible under scope {store_id!r} "
                f"({', '.join(distinct) or 'none'}) — a scoped session must see exactly one"
            ),
            "rows_checked": rows_checked,
            "sites_visible": sites_visible,
            "sibling_leaked": sibling_leaked,
        }
    if sibling_leaked:
        return {
            "ok": False,
            "detail": (
                f"{rows_checked} rows, all {mine}; but sibling {sibling_site} "
                f"leaked its stock of {sibling_article}"
            ),
            "rows_checked": rows_checked,
            "sites_visible": sites_visible,
            "sibling_leaked": True,
        }
    if sibling_article is None:
        return {
            "ok": None,
            "detail": (
                f"{rows_checked} rows, all {mine} — but no sibling branch stocks an article "
                f"{mine} does not, so a cross-branch leak could not be probed"
            ),
            "rows_checked": rows_checked,
            "sites_visible": sites_visible,
            "sibling_leaked": None,
        }
    return {
        "ok": True,
        "detail": (
            f"{rows_checked} rows over {len(probes)} articles, all {mine}; "
            f"sibling {sibling_site} not visible"
        ),
        "rows_checked": rows_checked,
        "sites_visible": sites_visible,
        "sibling_leaked": False,
    }


class IngestConfigUpdate(BaseModel):
    poll_seconds: Optional[int] = None
    catalog_mode: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/ingest/config", dependencies=[Depends(require_super_admin)])
async def ingest_config_get() -> Dict:
    """The effective ingest config (poll cadence + catalog mode) from Redis."""

    return await cache.get_ingest_config()


@router.post("/ingest/config", dependencies=[Depends(require_super_admin)])
async def ingest_config_set(c: IngestConfigUpdate) -> Dict:
    """Persist a partial ingest-config update. Clamped/validated on write.

    ``poll_seconds`` is clamped to 5..3600. ``enabled`` turns automatic loading
    on and off. ``catalog_mode`` is now REFUSED with a 400 — it was accepted and
    stored while the loader ignored it, so the console could report a change to
    delete behaviour that never happened (see ``cache.set_ingest_config``).

    Takes effect on the worker's next loop/scan — no restart.
    """

    try:
        return await cache.set_ingest_config(
            poll_seconds=c.poll_seconds,
            catalog_mode=c.catalog_mode,
            enabled=c.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _stale_cutoff(days: int):
    """now() - ``days``, as an aware datetime. Refuses days < 1."""

    from datetime import datetime, timedelta, timezone

    if days < 1:
        raise HTTPException(status_code=400, detail="days must be at least 1")
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/ingest/stale", dependencies=[Depends(require_super_admin)])
async def ingest_stale(days: int = 30) -> Dict:
    """PREVIEW ONLY — how many catalog rows a purge of ``days`` would remove.

    Counts rows whose ``last_seen`` is older than the cutoff OR is NULL (never
    seen since the column was added — unknown age). ``legacy_count`` breaks out
    the NULL rows so the operator knows the first purge may match legacy data.
    Deletes nothing.
    """

    cutoff = _stale_cutoff(days)
    rows = await q(
        """SELECT count(*) AS n,
                  count(*) FILTER (WHERE last_seen IS NULL) AS legacy
             FROM catalog
            WHERE last_seen < $1 OR last_seen IS NULL""",
        cutoff,
    )
    return {
        "count": int(rows[0]["n"]),
        "legacy_count": int(rows[0]["legacy"]),
        "cutoff": cutoff.isoformat(),
        "days": days,
    }


class PurgeStale(BaseModel):
    days: int


@router.post("/ingest/purge-stale", dependencies=[Depends(require_super_admin)])
async def ingest_purge_stale(body: PurgeStale) -> Dict:
    """Delete catalog rows older than ``days`` (or never seen). Busts the cache.

    The destructive twin of GET /ingest/stale — same predicate. Deletions change
    answers, so the data version is bumped afterwards. Refuses days < 1.
    """

    cutoff = _stale_cutoff(body.days)
    rows = await q(
        "DELETE FROM catalog WHERE last_seen < $1 OR last_seen IS NULL "
        "RETURNING article_code",
        cutoff,
    )
    version = await cache.bump_data_version()
    return {"deleted": len(rows), "cutoff": cutoff.isoformat(), "data_version": version}


@router.get("/sftp")
async def sftp_status() -> Dict:
    """SFTP connection info + pending / archived / failed file listings."""

    from pathlib import Path

    s = get_settings()
    base = Path(s.incoming_dir)

    def listing(p: Path):
        if not p.is_dir():
            return []
        out = []
        files = list(p.glob("*.xlsx")) + list(p.glob("*.csv"))
        for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out[:50]

    return {
        # The page's connection card reads the richer GET /sftp/connection; this
        # block is legacy. Report the real configured values rather than a
        # "<server>" placeholder so anything still reading it is not misled.
        "connection": {
            "host": (s.sftp_public_host or "").strip(),
            "port": s.sftp_public_port,
            "user": s.sftp_username,
            "path": "upload/",
        },
        "incoming_dir": str(base),
        "pending": listing(base),
        "archived": listing(base / "archive"),
        "failed": listing(base / "failed"),
        # Effective cadence (Redis override, else the settings default) so the
        # page never claims a poll interval the worker isn't actually using.
        "poll_seconds": await cache.get_poll_seconds(),
    }


# ---- SFTP files: one history, downloadable, retryable -----------------------
#
# The three folders ARE the status. A file in archive/ loaded, one in failed/ was
# refused, one still in the drop folder is on its way. The old endpoint returned
# them as three separate arrays, which is why the page could never show one
# sorted history.

_FOLDER_STATE = {"incoming": "wait", "archive": "ok", "failed": "bad"}


def _sftp_dirs() -> Dict[str, Path]:
    base = Path(get_settings().incoming_dir)
    return {"incoming": base, "archive": base / "archive", "failed": base / "failed"}


def _original_name(stamped: str) -> str:
    """Strip the archive timestamp: ``1782107161_balance.xlsx`` -> ``balance.xlsx``.

    The prefix is ours (``watcher._stamp``), not the partner's. Showing it makes
    every filename look mangled, and makes two uploads of the same export look
    like two unrelated files.
    """

    head, sep, tail = stamped.partition("_")
    return tail if sep and head.isdigit() and tail else stamped


def _resolve_sftp_file(name: str) -> Path:
    """Locate one file inside the drop folders, or 404.

    SECURITY. ``name`` arrives in a URL and names a file a *partner* uploaded
    over SFTP, so it is doubly untrusted. Two independent defences, because
    either alone has a bypass:

    1. ``Path(name).name`` discards every directory part, so ``../../etc/passwd``
       becomes ``passwd``. This alone would still be fooled by a symlink sitting
       in the drop folder and pointing anywhere on the container.
    2. The resolved path's parent must BE one of the three real directories,
       compared after ``resolve()`` on both sides. A symlink resolves to its
       target, whose parent is elsewhere, so it fails here.

    Anything else is a 404 — the same answer as "no such file", so probing
    cannot map the filesystem.
    """

    leaf = Path(name).name
    if not leaf or leaf in {".", ".."}:
        raise HTTPException(status_code=404, detail="no such file")

    for key, folder in _sftp_dirs().items():
        candidate = folder / leaf
        try:
            if not candidate.is_file():
                continue
            real = candidate.resolve(strict=True)
            if real.parent != folder.resolve(strict=True):
                continue  # a symlink out of the folder — treat as absent
        except OSError:
            continue
        return real

    raise HTTPException(status_code=404, detail="no such file")


def _mark_live(files: List[Dict]) -> None:
    """Flag the file each kind's data actually came from. Mutates in place.

    Product list and stock levels replace their own table outright, so exactly
    one file per kind is live — everything else is a superseded copy kept for
    download and retry. Without this the console lists four uploads under two
    names, all reading "Loaded", and nothing on the page says which pair the
    agent is answering from.

    Live is the newest **successfully loaded** file of a kind, NOT the newest
    file of a kind. Those differ exactly when they matter: on 2026-08-13 a stock
    file loaded at 10:52 and a second was refused at 10:54, so the newest upload
    was the rejected one while the live data was still the earlier load. Only
    ``state == "ok"`` (the archive folder) can be live; a rejected file in
    failed/ never replaced anything, and a file still waiting in the drop folder
    has not replaced anything yet.

    ``files`` must already be sorted newest-first.
    """

    seen: set = set()
    for r in files:
        kind = r.get("kind")
        if r["state"] != "ok" or not kind or kind in seen:
            continue
        seen.add(kind)
        r["live"] = True


@router.get("/sftp/files", dependencies=[Depends(require_super_admin)])
async def sftp_files() -> Dict:
    """Every file we hold, newest first, with what happened to it.

    The folder gives the status; ``ingest_events`` gives the story. A file with
    no history (it predates the table, or the recorder was down) still lists —
    it just has nothing to show in the drawer.
    """

    from app import ingest_events
    from app.ingest import detect_kind

    # Two summaries, used for two different things. by_stamp is keyed by the
    # STORED name, so each archived copy gets its OWN run; by_name is keyed by
    # the partner's filename and is only correct for a file still sitting in the
    # drop folder, which has not been stamped yet.
    by_stamp = await ingest_events.latest_by_stamped()
    by_name = await ingest_events.latest()
    out: List[Dict] = []

    for key, folder in _sftp_dirs().items():
        if not folder.is_dir():
            continue
        for f in list(folder.glob("*.xlsx")) + list(folder.glob("*.csv")):
            if not f.is_file():
                continue
            st = f.stat()
            original = _original_name(f.name)
            # An archived copy with no stamped run shows NOTHING rather than
            # borrowing the newest run for its name — that borrowing is what
            # made five copies of one export all claim the same row count.
            last = by_stamp.get(f.name) or (by_name.get(original) if key == "incoming" else {}) or {}
            data = last.get("data") or {}
            out.append({
                "name": original,
                "stored_as": f.name,
                "folder": key,
                "state": _FOLDER_STATE[key],
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                # Fall back to the filename when no run recorded a kind: the
                # live marker below needs a kind for every file, and the name is
                # what the loader itself classifies on.
                "kind": last.get("kind") or detect_kind(original),
                "step": last.get("step"),
                "detail": last.get("detail"),
                "rows": data.get("rows"),
                "run_id": last.get("run_id"),
                "live": False,
            })

    out.sort(key=lambda r: r["mtime"], reverse=True)
    _mark_live(out)
    return {
        "files": out,
        "counts": {
            **{s: sum(1 for r in out if r["state"] == s) for s in ("wait", "ok", "bad")},
            "live": sum(1 for r in out if r["live"]),
        },
        "poll_seconds": await cache.get_poll_seconds(),
        "enabled": await cache.get_ingest_enabled(),
    }


@router.get("/sftp/file/{name}/history", dependencies=[Depends(require_super_admin)])
async def sftp_file_history(name: str, run_id: Optional[str] = None) -> Dict:
    """Every step of one file's most recent attempt."""

    from app import ingest_events

    leaf = _original_name(Path(name).name)
    return {"file": leaf, "events": await ingest_events.history(leaf, run_id)}


@router.get("/sftp/file/{name}", dependencies=[Depends(require_super_admin)])
async def sftp_file_download(name: str):
    """Download the copy we kept. super_admin only — these are partner exports."""

    from fastapi.responses import FileResponse

    path = _resolve_sftp_file(name)
    return FileResponse(
        path,
        filename=_original_name(path.name),
        media_type="application/octet-stream",
    )


class RetryFile(BaseModel):
    allow_shrink: bool = False


@router.post("/sftp/file/{name}/retry", dependencies=[Depends(require_super_admin)])
async def sftp_file_retry(name: str, body: RetryFile = Body(default=RetryFile())) -> Dict:
    """Put a file back in the drop folder and read it again, now.

    ``allow_shrink`` overrides the guard that refuses a file which would delete
    more than half a table — the ONE place that override is reachable from the
    console, and deliberately per-file rather than a global setting. A file is
    refused because of what *it* contains, so the decision to accept it anyway
    belongs to that file and expires with it.

    The retried file keeps its original name: the loader decides what a file is
    from its name, so restoring the timestamp-prefixed archive name would make a
    previously-recognised file unrecognisable.
    """

    from app.watcher import scan_once

    path = _resolve_sftp_file(name)
    incoming = _sftp_dirs()["incoming"]
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / _original_name(path.name)

    if path.parent.resolve() != incoming.resolve():
        if dest.exists():
            raise HTTPException(
                status_code=409,
                detail=f"{dest.name} is already waiting in the drop folder",
            )
        path.rename(dest)

    # stable_only=False: a human pressed the button, so there is no half-written
    # upload to wait for — the file has been sitting still since it was refused.
    summary = await scan_once(stable_only=False, allow_shrink=body.allow_shrink)
    return {"file": dest.name, "allow_shrink": body.allow_shrink, **summary}


@router.delete("/sftp/file/{name}", dependencies=[Depends(require_super_admin)])
async def sftp_file_delete(name: str) -> Dict:
    """Delete the kept copy. Loaded data is untouched — this only removes the file."""

    path = _resolve_sftp_file(name)
    path.unlink()
    return {"deleted": path.name, "note": "loaded data unchanged"}


# ---- SFTP partner handoff --------------------------------------------------

# Extensions the pipeline will pick up. Kept next to the rules it describes and
# asserted against the real ingest surface in tests/test_sftp_page.py.
INGEST_EXTENSIONS = [".csv", ".xlsx"]


def _detect_kind_keywords() -> Dict[str, List[str]]:
    """The substrings ``ingest.detect_kind`` keys on, read out of the function itself.

    The filename contract is the single thing a partner most needs and the one
    thing the UI never told them. Retyping "article -> catalog, balance|stock|
    inventory -> inventory" into this endpoint would create a second copy that
    silently drifts the first time someone edits ``detect_kind`` — and the way it
    fails is a partner's file landing in ``failed/`` while the page insists the
    name was fine.

    So the keywords are read from ``detect_kind``'s AST: each ``"lit" in name``
    test, paired with the kind its branch returns. A rewrite that no longer looks
    like a chain of substring tests yields an EMPTY mapping — loudly wrong on the
    page and caught by ``test_rules_cover_every_detect_kind_branch`` — rather than
    a stale one that looks right.
    """

    import ast
    import inspect
    import textwrap

    from app.ingest import detect_kind

    tree = ast.parse(textwrap.dedent(inspect.getsource(detect_kind)))
    out: Dict[str, List[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # The branch's kind: `return "catalog"`.
        kinds = [
            n.value.value
            for n in node.body
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        ]
        if not kinds:
            continue

        # Its tests: `"article" in name`, or `"a" in name or "b" in name`.
        tests = node.test.values if isinstance(node.test, ast.BoolOp) else [node.test]
        for t in tests:
            if (
                isinstance(t, ast.Compare)
                and len(t.ops) == 1
                and isinstance(t.ops[0], ast.In)
                and isinstance(t.left, ast.Constant)
                and isinstance(t.left.value, str)
            ):
                out.setdefault(kinds[0], []).append(t.left.value)

    return out


def filename_rules() -> Dict:
    """The ingest filename contract, derived — never retyped.

    ``kind`` on every example is computed by calling ``detect_kind`` on the name
    shown, so the page cannot advertise a "good" name the watcher would reject.
    """

    from app.ingest import detect_kind

    keywords = _detect_kind_keywords()
    good = ["articles-export-2026-07-13.csv", "balance_stock_20260713.xlsx"]
    bad = ["data.csv", "export (1).xlsx"]

    return {
        "extensions": INGEST_EXTENSIONS,
        # e.g. [{"kind": "catalog", "keywords": ["article"]}, …]
        "kinds": [{"kind": k, "keywords": v} for k, v in keywords.items()],
        "good": [{"name": n, "kind": detect_kind(n)} for n in good],
        "bad": [{"name": n, "kind": detect_kind(n)} for n in bad],
        "unmatched_dir": "failed/",
        "archive_dir": "archive/",
    }


def _detect_host(request: Request) -> str:
    """The hostname this request arrived on — hostname only, no scheme, no port.

    A *suggestion*, never an answer. Behind a proxy that does not forward the
    original Host, or when the admin console is reached on a name the sftp port
    is not published under, this is confidently wrong — which is why the caller
    tags it ``host_source="detected"`` and the page asks the operator to confirm
    it rather than printing it as fact.
    """

    raw = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not raw:
        raw = (request.headers.get("host") or "").strip()
    if not raw:
        return ""

    # Tolerate a full URL landing in the header (some proxies do this).
    if "//" in raw:
        raw = raw.split("//", 1)[1]
    raw = raw.split("/", 1)[0].strip()

    # IPv6 literal: [::1]:2222 -> ::1. Splitting on ":" would shred it.
    if raw.startswith("["):
        return raw[1:].split("]", 1)[0]
    return raw.split(":", 1)[0]


@router.get("/sftp/connection", dependencies=[Depends(require_super_admin)])
async def sftp_connection(request: Request) -> Dict:
    """Everything a partner needs to connect — including the shared password.

    super_admin ONLY (``require_super_admin``): the response body carries the
    SFTP account's password, and the router-level ``require_admin`` would hand
    that to every branch admin.

    ``host_source`` says how much to trust ``host``:

    * ``env``      — ``SFTP_PUBLIC_HOST`` is set. Authoritative.
    * ``detected`` — inferred from this request's forwarded/Host header. A
      starting point the operator confirms; it can be flatly wrong behind a
      proxy, and the sftp port need not even be published on that name.
    * ``none``     — neither. The page asks for it.
    """

    s = get_settings()
    env_host = (s.sftp_public_host or "").strip()
    host = env_host or _detect_host(request)
    source = "env" if env_host else ("detected" if host else "none")

    root = Path(s.sftp_keys_dir)

    return {
        "host": host,
        "host_source": source,
        # Kept for the page's existing "is this trustworthy" checks. Only an
        # env-configured host counts as configured — a detected one does not.
        "host_configured": bool(env_host),
        "port": s.sftp_public_port,
        "username": s.sftp_username,
        "password": s.sftp_password,
        "upload_path": "upload/",
        "incoming_dir": s.incoming_dir,
        "poll_seconds": await cache.get_poll_seconds(),
        "rules": filename_rules(),
        # Keys are now registered from this console and take effect on the
        # partner's NEXT connection: sshd re-reads authorized_keys every time,
        # and the api container writes into the same volume the sftp container
        # serves. `available` is False when that volume is not mounted (a dev
        # stack), which is the one case the endpoints below refuse.
        "key_auth": {
            "manageable": True,
            "available": root.is_dir(),
            "keys_dir": str(root),
            "needs_service_restart": False,
        },
    }


# ---- SFTP partner keys ------------------------------------------------------
#
# authorized_keys is remote-code-access control for the sftp container, and the
# key material arrives from a partner over email. Everything below exists to
# keep the two apart.
#
# The line format is `<type> <base64> [comment]`, but OpenSSH ALSO accepts a
# leading options field: `command="…",environment="…" ssh-ed25519 AAAA…` runs
# that command on every login. A partner-supplied (or partner-relayed) key
# carrying options is remote code execution, so the parser below accepts a line
# ONLY if it begins with a known key type — anything else, including options, is
# rejected before it reaches disk. An embedded newline is the same attack in a
# second line, so it is rejected too.
#
# We never write the partner's comment field; the canonical line we store is
# `<type> <base64> pharma:<label>`, built from a label that matches
# _SFTP_LABEL_RE. Nothing an attacker controls ends up outside the base64 blob,
# whose bytes we have already decoded and type-checked.

SFTP_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)
MAX_SFTP_KEYS = 50
MAX_SFTP_KEY_CHARS = 4096          # a 4096-bit RSA key is ~740 chars
_SFTP_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")

# The uid/gid atmoz/sftp runs the pharma account as. sshd refuses an
# authorized_keys it does not own, or one that is group/world-writable.
_SFTP_UID = 1001
_SFTP_GID = 1001

# Serialises the read-modify-write of authorized_keys. Two concurrent POSTs
# would otherwise both read the old file and the second would erase the first.
_sftp_keys_lock = asyncio.Lock()


class SftpKey(BaseModel):
    label: str
    public_key: str


def _keys_root() -> Path:
    """The mounted .ssh directory, or a 503 that says exactly what is missing."""

    root = Path(get_settings().sftp_keys_dir)
    if not root.is_dir():
        raise HTTPException(
            status_code=503,
            detail=(
                f"SFTP key directory {root} is not mounted, so a key registered here "
                "would never reach sshd. Mount the `sftp_ssh` volume into the api "
                "container at that path (env SFTP_KEYS_DIR) and into the sftp "
                "container at /home/pharma/.ssh."
            ),
        )
    return root


def _clean_label(label: str) -> str:
    """Labels become filenames (``keys/<label>.pub``), so keep them boring."""

    label = (label or "").strip()
    if not _SFTP_LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail=(
                "label must be 1-48 chars of letters, digits, dot, dash or underscore "
                "and start with a letter or digit (it becomes a filename)"
            ),
        )
    return label


def _parse_public_key(raw: str) -> Dict[str, str]:
    """Validate ONE OpenSSH public key line. Rejects everything else.

    Returns ``{"type", "b64", "fingerprint"}``. Raises 400 with a reason a human
    can act on — an operator pasting a key needs to know *which* thing was wrong.
    """

    raw = raw or ""
    if len(raw) > MAX_SFTP_KEY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"public key is too long (max {MAX_SFTP_KEY_CHARS} characters)",
        )

    # Surrounding whitespace is normal — a .pub file ends in a newline and that
    # is what an operator pastes. An INTERIOR newline is not: it makes this two
    # authorized_keys entries, and the second one is whatever the sender wants,
    # unvalidated. Same for any other control byte.
    key = raw.strip()
    # A real .pub is one line of space-separated ASCII. Reject every control
    # char INCLUDING tab: a tab lets `type\t<blob>\tcommand="…"` split into
    # three clean fields, so the options ride in disguised as a comment. The
    # write path discards the comment anyway, but an honest parser must not
    # return "ok" for a line carrying an injection attempt.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise HTTPException(
            status_code=400,
            detail=(
                "a public key is a single line — this contains a line break, tab, or "
                "control character. Paste exactly one line from the partner's .pub file."
            ),
        )

    parts = key.split()
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail="expected an OpenSSH public key line: '<type> <base64> [comment]'",
        )

    ktype = parts[0]
    if ktype not in SFTP_KEY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"key must start with one of {', '.join(SFTP_KEY_TYPES)}. A line that "
                "starts with anything else — in particular authorized_keys options "
                'like command="…", environment="…" or permitopen="…" — is refused: '
                "those execute on the SFTP server at login."
            ),
        )

    b64 = parts[1]
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001 — binascii.Error and friends
        raise HTTPException(status_code=400, detail="key material is not valid base64")

    # An OpenSSH key blob opens with its own type as a length-prefixed string.
    # If that disagrees with the line's prefix, the line is lying about what it
    # is — reject rather than guess which half to believe.
    if len(blob) < 4:
        raise HTTPException(status_code=400, detail="key material is truncated")
    n = int.from_bytes(blob[:4], "big")
    if n <= 0 or n > 64 or len(blob) < 4 + n:
        raise HTTPException(status_code=400, detail="key material is not an OpenSSH key blob")
    embedded = blob[4 : 4 + n].decode("ascii", "replace")
    if embedded != ktype:
        raise HTTPException(
            status_code=400,
            detail=f"key type mismatch: the line says '{ktype}' but the key material is '{embedded}'",
        )

    # Exactly what `ssh-keygen -lf key.pub` prints, so an operator can read it
    # back to the partner over the phone and compare, character for character.
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return {"type": ktype, "b64": b64, "fingerprint": f"SHA256:{digest}"}


def _canonical_line(ktype: str, b64: str, label: str) -> str:
    """The only shape we ever write. The comment is OUR label, not the partner's."""

    return f"{ktype} {b64} pharma:{label}"


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write + fsync a temp file, then rename over the target.

    authorized_keys is read by sshd on every connection, including while we are
    rewriting it. A rename is atomic, so a partner mid-connect sees the old file
    or the new one — never a half-written one that locks everybody out.
    """

    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    # sshd ignores an authorized_keys owned by the wrong user. The sftp
    # container's boot script fixes ownership too, so a failure here (we are not
    # root) is survivable, not fatal.
    try:
        os.chown(tmp, _SFTP_UID, _SFTP_GID)
    except (PermissionError, OSError):
        pass
    os.replace(tmp, path)


def _read_keys(root: Path) -> List[Dict]:
    """Registered keys, read back from ``keys/*.pub`` — the durable half.

    keys/ is the registry (it survives a container rebuild); authorized_keys is
    the live copy sshd reads. Listing from keys/ means the page shows what will
    still be there after a restart.
    """

    kdir = root / "keys"
    if not kdir.is_dir():
        return []

    out: List[Dict] = []
    for pub in sorted(kdir.glob("*.pub")):
        try:
            parsed = _parse_public_key(pub.read_text(encoding="utf-8", errors="replace").strip())
        except HTTPException:
            continue  # a hand-placed file we did not write; don't crash the page
        except OSError:
            continue
        out.append(
            {
                "label": pub.stem,
                "type": parsed["type"],
                "fingerprint": parsed["fingerprint"],
                "added_at": int(pub.stat().st_mtime),
            }
        )
    return out


@router.get("/sftp/keys", dependencies=[Depends(require_super_admin)])
async def sftp_keys_list() -> List[Dict]:
    """Registered partner keys. Fingerprints only — never the raw key material.

    The fingerprint is what an operator can actually verify (the partner reads
    theirs off `ssh-keygen -lf id_ed25519.pub`); echoing the blob back just
    invites pasting it somewhere else.
    """

    return _read_keys(_keys_root())


@router.post("/sftp/keys", dependencies=[Depends(require_super_admin)])
async def sftp_keys_add(k: SftpKey) -> Dict:
    """Register a partner's public key. Live on their next connection.

    Writes BOTH halves under one lock: ``authorized_keys`` (sshd re-reads it per
    connection → no restart) and ``keys/<label>.pub`` (atmoz/sftp rebuilds
    authorized_keys from this dir at boot → without it the key dies at the next
    restart).
    """

    root = _keys_root()
    label = _clean_label(k.label)
    parsed = _parse_public_key(k.public_key)

    async with _sftp_keys_lock:
        existing = _read_keys(root)
        if len(existing) >= MAX_SFTP_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"key limit reached ({MAX_SFTP_KEYS}); remove an unused key first",
            )
        for e in existing:
            if e["label"] == label:
                raise HTTPException(status_code=409, detail=f"a key labelled '{label}' already exists")
            if e["fingerprint"] == parsed["fingerprint"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"this key is already registered as '{e['label']}'",
                )

        line = _canonical_line(parsed["type"], parsed["b64"], label)

        (root / "keys").mkdir(parents=True, exist_ok=True)
        _atomic_write(root / "keys" / f"{label}.pub", line + "\n", 0o644)

        ak = root / "authorized_keys"
        try:
            current = ak.read_text(encoding="utf-8") if ak.exists() else ""
        except OSError:
            current = ""
        # Keep any line an operator put there by hand; append ours.
        lines = [ln for ln in current.splitlines() if ln.strip()]
        lines.append(line)
        _atomic_write(ak, "\n".join(lines) + "\n", 0o600)

    return {
        "status": "ok",
        "label": label,
        "type": parsed["type"],
        "fingerprint": parsed["fingerprint"],
        "active": "immediately",
    }


# ---- generating the keypair for the partner --------------------------------
#
# Why this exists next to the paste-a-public-key path above, and why it is
# STRICTLY THE WEAKER OF THE TWO.
#
# In the manual flow the partner runs `ssh-keygen` on their own machine and
# sends us the `.pub` half. The private key never exists anywhere but their
# disk — not on our server, not in an email, not in a support chat. That is the
# property that makes SSH key auth better than the shared password it replaces,
# and nothing below improves on it.
#
# In THIS flow the private key is generated on our server and then has to travel
# to the partner. However carefully it is handled, for the length of that trip
# it exists in a second place and passes through whatever channel the operator
# picks. It is a real, permanent downgrade in the trust model.
#
# It exists anyway because the manual flow has an adoption cost we measured in
# the field: partners who cannot get a keypair generated do not fall back to
# doing it properly, they fall back to asking for the password. A generated key
# that is scoped to one label and revocable in one click beats a shared
# password. That is the comparison this endpoint wins — not the comparison
# against the manual path, which it loses.
#
# Prefer POST /admin/sftp/keys. Reach for this one when the partner cannot.


class SftpKeyGenerate(BaseModel):
    label: str


def _generated_keypair() -> Tuple[str, str]:
    """A fresh Ed25519 keypair as ``(openssh_private_pem, openssh_public_line)``.

    Ed25519 rather than RSA: fixed small size, no key-size parameter for an
    operator to get wrong, and accepted by `ssh-ed25519` in SFTP_KEY_TYPES
    above, so the public half goes through the SAME `_parse_public_key`
    validation as a pasted one rather than around it.

    ⚠️ The private half is returned as a plain string and is never given a name
    outside the request that asked for it. It is NEVER written to disk, NEVER
    logged, NEVER stored in the database, and NEVER returned by any other
    endpoint. Nothing in this function touches a file, a logger or a pool.
    """

    # Imported here rather than at module scope: `cryptography` is only needed
    # by this one route, and a missing wheel should fail this endpoint, not
    # every import of the admin router.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_line = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return private_pem, public_line


async def _script_for_response(
    request: Request, label: str, fingerprint: str, private_pem: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """`(script, reason_it_could_not_be_built)` — exactly one of the two is set.

    Used by the two endpoints that hold key material, and it must never be able
    to fail them. Registering a key is the thing the caller asked for and it has
    already happened by the time we get here; turning an unusable *address* into
    a 500 would throw away a private half for a key that is live in
    `authorized_keys` — the exact failure `sftp_keys_generate` orders its steps
    to avoid.

    So the address guard still runs, in full, through `_pack_address` — same
    resolver, same refusals as the pack — but its 400 is caught and returned as
    a REASON next to a null script rather than raised. The caller still gets
    every field it got before; it just does not get a script that would point a
    partner at `localhost` every night for a year.
    """

    try:
        addr = _pack_address(await sftp_connection(request))
    except HTTPException as exc:      # 400 only: the address cannot be shipped
        return None, str(exc.detail)

    return (
        _partner_script(
            addr, label, fingerprint, private_pem, await cache.get_ingest_enabled()
        ),
        None,
    )


@router.post("/sftp/keys/generate", dependencies=[Depends(require_super_admin)])
async def sftp_keys_generate(k: SftpKeyGenerate, request: Request) -> Dict:
    """Generate a keypair for a partner, register the public half, return the private half ONCE.

    Everything about registration — the label charset rule, the public key
    parse, the duplicate-label and duplicate-fingerprint checks, the key limit,
    the 503 when the keys directory is not mounted, the single lock around the
    read-modify-write, and the atomic rewrite of BOTH `authorized_keys` and
    `keys/<label>.pub` — is `sftp_keys_add` above, called directly. This route
    adds exactly one thing to that path: it makes the key material instead of
    being handed it.

    ⚠️ **Order is load-bearing.** Generate, then register, and only then build
    the response. If registration raises — duplicate label, key limit, missing
    mount — the caller gets that error and NO private key, because a private key
    for a key that was never installed is a secret in circulation that buys
    nobody anything. The generated key is simply dropped on the floor.
    """

    # 1. Generate. Nothing is persisted yet, so a failure here changes nothing.
    private_pem, public_line = _generated_keypair()

    # 2. Register the PUBLIC half through the manual path, unchanged. Any
    #    HTTPException it raises (400 bad label, 409 duplicate, 503 not mounted)
    #    propagates to the caller as-is and we never reach step 3.
    registered = await sftp_keys_add(SftpKey(label=k.label, public_key=public_line))

    # The exact line that was written to authorized_keys and keys/<label>.pub —
    # built the same way the writer built it, so what we hand the partner and
    # what sshd will read cannot drift.
    public_key = _canonical_line(
        registered["type"], public_line.split()[1], registered["label"]
    )

    # 3. Audit. THAT a key was generated, for WHICH label, with the fingerprint
    #    — all three are public facts and are exactly what a reviewer needs. The
    #    private key is not passed here and must never be: `record_event`
    #    redacts by key NAME, which is a second line of defence, not a licence
    #    to hand it a secret.
    #
    #    Recorded explicitly because `activity._ROUTES` has no entry for this
    #    path, so the `activity_audit` middleware records route + status with an
    #    empty `detail` and the label would be lost. (The one-line fix on the
    #    other side is an entry `("POST", r"^/admin/sftp/keys/generate$",
    #    ("keys", ("label",)))`; until then this row is the one carrying the
    #    label, and the middleware's is a bare companion under the same action.)
    from app import activity
    from app import auth as authmod

    actor_email, actor_role = None, None
    try:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            claims = authmod.decode_token(header.split(" ", 1)[1])
            actor_email, actor_role = claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an unreadable token is an anonymous actor
        pass

    await activity.record_event(
        activity.action_for("POST", "/admin/sftp/keys/generate"),
        actor_email=actor_email,
        actor_role=actor_role,
        target=registered["label"],
        method="POST",
        path="/admin/sftp/keys/generate",
        status=200,
        detail={"label": registered["label"], "fingerprint": registered["fingerprint"]},
    )

    # 4. The one and only time the private key leaves this process.
    #
    #    ⚠️ Nothing captures this response body. Verified rather than assumed:
    #    the app has three HTTP middlewares (`app/api.py`) and none reads a
    #    response body. `observability` logs method/path/status/latency only;
    #    `activity_audit` reads the REQUEST body — bounded to allowlisted JSON
    #    routes — and writes `detail=summarize_body(...)`, never the response;
    #    `spa_deep_link` only diverts GET/HEAD document navigations. The request
    #    body it may read here is `{"label": …}`, which holds no secret. There
    #    is no access log that records bodies and no response-body capture
    #    anywhere in the app, so this route needs no opt-out.
    #
    #    It is also why `private_key` is assembled here and not stored on
    #    `registered`: the dict returned by `sftp_keys_add` is the shape the
    #    list/add endpoints already return, and it stays free of key material.
    #    `script` is built LAST, from the values that are already public plus
    #    the private half, and only after the key is registered — so it can
    #    never be handed over for a key that was not installed. It is a second
    #    rendering of the same secret and is subject to every rule above: never
    #    logged, never stored, never audited. The audit row written in step 3
    #    carries label + fingerprint and nothing else.
    script, script_unavailable = await _script_for_response(
        request, registered["label"], registered["fingerprint"], private_pem
    )

    return {
        "label": registered["label"],
        "fingerprint": registered["fingerprint"],
        "public_key": public_key,
        # Shown once, by the console, and then gone. We keep no copy.
        "private_key": private_pem,
        # One ready-to-run .sh with the key already in it. `None` (with a reason
        # beside it) when the SFTP address is not fit to ship — see
        # `_script_for_response`. Everything above is unchanged either way.
        "script": script,
        "script_unavailable": script_unavailable,
    }


@router.post("/sftp/keys/{label}/regenerate", dependencies=[Depends(require_super_admin)])
async def sftp_keys_regenerate(label: str, request: Request) -> Dict:
    """Replace an existing partner's key in place: same row, same label, new credential.

    Why this exists rather than revoke-then-add. The registry answers one
    question an auditor actually asks — "who could send us files, and when?" —
    and it answers it by label. Deleting `Rahul` and adding `Rahul` back drops
    that row and re-creates it, so the timeline reads as a partner who left and
    a different partner who arrived, and `added_at` on the new row says nothing
    about when this partner was first trusted. Rotating in place keeps the name,
    keeps its place in the audit trail, and records the rotation as one event
    carrying both fingerprints — the one it replaced and the one that replaced
    it.

    ⚠️ **The old key stops working immediately.** sshd re-reads
    `authorized_keys` on every connection, so the moment this returns, the
    partner's current private key is refused and they are cut off until they
    install the one in this response. That is the point of a rotation, but it
    means this is not a background maintenance action: do it when somebody on
    the partner's side is ready to receive the new key.

    Everything in the long note above `SftpKeyGenerate` applies here too — the
    private half is generated on our server and has to travel, which is a real
    downgrade against a partner-generated keypair. It is returned exactly once
    and never written, logged or stored.
    """

    # ---- validate, in the order that keeps a failure harmless ---------------
    #
    # Same rule as `sftp_keys_generate`: nothing is generated and nothing is
    # written until every check that can refuse has refused. A 400 or a 404 here
    # leaves the partner's existing key exactly where it was.
    label = _clean_label(label)

    # The mount check physically has to precede the existence check — "is a key
    # registered under this label?" is a read of `keys/` and there is no `keys/`
    # to read when the volume is not mounted. A 503 here is therefore "I cannot
    # tell you", not "no such key", which is why it must not be softened into a
    # 404.
    root = _keys_root()

    async with _sftp_keys_lock:
        pub = root / "keys" / f"{label}.pub"
        if not pub.is_file():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no key labelled '{label}' is registered, so there is nothing to "
                    "rotate. Register one with POST /admin/sftp/keys (or /generate)."
                ),
            )

        try:
            previous = _parse_public_key(pub.read_text(encoding="utf-8").strip())
        except (HTTPException, OSError):
            # A file under this label that we did not write. Refusing is not
            # pedantry: the swap below removes the old line by matching its key
            # MATERIAL, and a file we cannot parse yields no material to match.
            # Rotating anyway would leave a line we could not identify still in
            # authorized_keys — a rotation that revokes nothing while reporting
            # success. Revoke and re-add instead, where the operator sees it.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the file registered as '{label}' is not a public key this console "
                    "wrote, so its old line cannot be identified and removed. Revoke it "
                    f"(DELETE /admin/sftp/keys/{label}) and register a new key instead."
                ),
            )

        # ---- generate ------------------------------------------------------
        # Still nothing on disk has changed; a failure here (a missing
        # `cryptography` wheel) leaves the old key live.
        private_pem, public_line = _generated_keypair()
        parsed = _parse_public_key(public_line)      # same validation as a pasted key
        line = _canonical_line(parsed["type"], parsed["b64"], label)

        # ---- swap ----------------------------------------------------------
        #
        # ⚠️ Written out longhand rather than as `sftp_keys_delete` then
        # `sftp_keys_add`. Those are two rewrites of authorized_keys with a
        # window between them in which the file contains neither this partner's
        # old key nor their new one — and if the second write fails, that window
        # never closes. sshd reads the file on EVERY connection, so during that
        # window this partner cannot connect at all. The read-modify-write below
        # is one pass: drop the old line, append the new one, and hand the whole
        # result to a single `_atomic_write`, whose rename is atomic. A partner
        # connecting mid-swap reads either the complete old file or the complete
        # new one.
        #
        # `keys/<label>.pub` is written FIRST, and deliberately: it is the
        # durable registry (atmoz/sftp rebuilds authorized_keys from it at boot)
        # while authorized_keys is the live copy. If the .pub write fails we
        # raise before touching authorized_keys and the old key keeps working,
        # live and after a restart. If the .pub write succeeds and the
        # authorized_keys write fails, the old key is STILL live — sshd is
        # reading the untouched authorized_keys — and the caller gets a 500 with
        # no private key, so nobody is holding a credential for a key that was
        # not installed. Neither order is transactional across two files; this
        # one fails towards the partner keeping access. Re-running the rotation
        # is the fix (note that on that retry `previous_fingerprint` reports the
        # half-written key, since that is what is on disk).
        (root / "keys").mkdir(parents=True, exist_ok=True)
        _atomic_write(root / "keys" / f"{label}.pub", line + "\n", 0o644)

        ak = root / "authorized_keys"
        try:
            current = ak.read_text(encoding="utf-8") if ak.exists() else ""
        except OSError:
            current = ""

        kept: List[str] = []
        for ln in current.splitlines():
            if not ln.strip():
                continue
            parts = ln.split()
            # Match on the key material first — that is what sshd actually
            # authenticates against — then on our own comment, which catches a
            # line we wrote whose blob has since been edited by hand. Same pair
            # of tests `sftp_keys_delete` uses, for the same reason: miss one
            # and the old key survives the rotation and still opens the door.
            if len(parts) >= 2 and parts[1] == previous["b64"]:
                continue
            if len(parts) >= 3 and parts[2] == f"pharma:{label}":
                continue
            kept.append(ln)          # anything an operator added by hand stays
        kept.append(line)

        # ONE write. Old line gone and new line present in the same rename.
        _atomic_write(ak, "\n".join(kept) + "\n", 0o600)

    # ---- audit -------------------------------------------------------------
    #
    # Label, new fingerprint, previous fingerprint. All three are public facts
    # and together they are the whole story a reviewer needs: this partner's
    # access moved from that key to this one, at this time, by this actor. The
    # private key is not passed here and must never be.
    #
    # Recorded explicitly for the same reason `sftp_keys_generate` does it:
    # `activity._ROUTES` has no entry for this path, so the `activity_audit`
    # middleware would record route + status with an empty `detail` and both
    # fingerprints would be lost. `target=label` is passed to `action_for` so
    # the slug collapses to one filterable action instead of one per partner.
    from app import activity
    from app import auth as authmod

    actor_email, actor_role = None, None
    try:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            claims = authmod.decode_token(header.split(" ", 1)[1])
            actor_email, actor_role = claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an unreadable token is an anonymous actor
        pass

    path = f"/admin/sftp/keys/{label}/regenerate"
    await activity.record_event(
        activity.action_for("POST", path, target=label),
        actor_email=actor_email,
        actor_role=actor_role,
        target=label,
        method="POST",
        path=path,
        status=200,
        detail={
            "label": label,
            "fingerprint": parsed["fingerprint"],
            "previous_fingerprint": previous["fingerprint"],
        },
    )

    # The one and only time this private key leaves the process. See the note in
    # `sftp_keys_generate` step 4 for why no middleware captures this body.
    # Built after the swap and after the audit, from a key that is already live
    # in authorized_keys. Same rules as the private half it embeds: never
    # logged, never stored, never passed to `record_event`.
    script, script_unavailable = await _script_for_response(
        request, label, parsed["fingerprint"], private_pem
    )

    return {
        "label": label,
        "fingerprint": parsed["fingerprint"],
        "previous_fingerprint": previous["fingerprint"],
        "public_key": line,
        # Shown once, by the console, and then gone. We keep no copy.
        "private_key": private_pem,
        # The replacement script, key included — this is what the partner has to
        # install for their old one to stop mattering. `None` with a reason when
        # the address is not fit to ship; see `_script_for_response`.
        "script": script,
        "script_unavailable": script_unavailable,
    }


# ---- the partner pack (a ZIP the partner can unzip and run) -----------------
#
# The console can already show an operator every value a partner needs, and the
# operator then retypes them into an email. That email is where the mistakes
# live: a port dropped, a folder invented, `chmod 600` left out, and — the one
# that costs a week — the address copied from a screen that was only ever a
# GUESS about how the outside world reaches this box.
#
# So the pack is generated, not written, and it refuses to exist when the values
# in it would be wrong. A ZIP is worse than a copied command here: it looks
# authoritative, it gets saved next to the partner's other credentials, and it
# gets opened again in eight months by somebody who was not on the call.
#
# TWO MODES, and the default is the one that carries no secret.
#
#   include_private_key=false  Documentation with the real values in it. The
#                             partner runs ssh-keygen themselves and sends the
#                             public half back, so the private key never exists
#                             anywhere but their disk. Nothing is created
#                             server-side. Safe to email.
#   include_private_key=true   We generate the keypair (through
#                             `sftp_keys_generate`, unchanged) and ship the
#                             private half inside the archive. The archive IS
#                             the credential from that moment on. See the long
#                             note above `SftpKeyGenerate` for why this mode is
#                             strictly the weaker of the two.


class SftpPartnerPack(BaseModel):
    """What to build the pack for. `label` is the same label the key registry uses."""

    label: str
    include_private_key: bool = False


# Addresses that are real hostnames as far as any parser is concerned and are
# useless — or actively misleading — in a file that leaves this building. A pack
# full of commands pointing at the reader's own laptop must not be downloadable.
_PACK_BAD_HOSTS = frozenset({"localhost", "0.0.0.0", "::", "::1", "[::1]", "0:0:0:0:0:0:0:1"})


def _pack_address(conn: Dict) -> Dict:
    """The connection values, or a 400 that says why they cannot be shipped.

    Takes the dict `GET /sftp/connection` already returns, so the address in the
    pack is resolved by exactly the code the console reads — there is no second
    resolver here to drift from it. The password it carries is deliberately NOT
    copied out: this pack is the key-auth flow and adding the shared secret to a
    file that gets emailed would undo the point of it.

    Refused, all with 400:

    * no address at all;
    * an address that only ever loops back to the machine reading it;
    * ``host_source != "env"`` — i.e. the value was DETECTED from the admin
      request's Host header rather than saved in ``SFTP_PUBLIC_HOST``. The
      console is allowed to show a detected host with a "confirm this" prompt
      next to it. A file cannot carry that prompt, and by the time it is wrong
      the partner has been failing to connect for a week.
    """

    host = (conn.get("host") or "").strip()
    source = conn.get("host_source") or "none"

    # `[::1]` and `::1` are the same address wearing different brackets.
    bare = host.strip("[]").lower()

    if not host:
        raise HTTPException(
            status_code=400,
            detail=(
                "no SFTP address is configured, so a pack would contain commands "
                "that connect to nothing. Set SFTP_PUBLIC_HOST to the hostname "
                "partners reach this server on and try again."
            ),
        )
    if bare in _PACK_BAD_HOSTS or bare.startswith("127.") or bare.endswith(".localhost"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"the configured SFTP address is '{host}', which only ever means "
                "'the machine running this command'. A partner following the pack "
                "would connect to their own laptop. Set SFTP_PUBLIC_HOST to the "
                "public hostname of this server."
            ),
        )
    if source != "env":
        raise HTTPException(
            status_code=400,
            detail=(
                f"the address '{host}' was DETECTED from this browser request, not "
                "configured. It is a reasonable guess for the console to show, but "
                "it can be flatly wrong behind a proxy and the SFTP port need not "
                "be published on that name at all. A downloaded pack outlives the "
                "guess, so set SFTP_PUBLIC_HOST to the confirmed hostname first."
            ),
        )

    return {
        "host": host,
        "port": int(conn.get("port") or 22),
        "username": conn.get("username") or "pharma",
        "upload_path": (conn.get("upload_path") or "upload/").rstrip("/") or "upload",
    }


def _pack_write(zf: Any, name: str, text: str, mode: int) -> None:
    """One entry, with a POSIX mode recorded in `external_attr`.

    ⚠️ **Measured, not assumed: this does not survive most extractions.** Both
    `zf.writestr(name, text)` and a `ZipInfo` carrying `0o600 << 16` extract as
    0644 under Python's own `extractall` and under most GUI unzippers. The bits
    are set anyway for the tools that do honour them, but nothing in the pack is
    allowed to DEPEND on them — which is why `chmod 600` is the first line of
    setup.sh rather than a sentence in the README, and why every README says
    `sh setup.sh` (which needs no executable bit) instead of `./setup.sh`.
    """

    import zipfile

    info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, text)


def _pack_filename_help() -> str:
    """The ingest naming contract, derived from `filename_rules()` — never retyped.

    A partner who sends `data.csv` has done everything right and still gets
    nothing, because the watcher cannot tell what the file is. Deriving this
    from the same helper the admin page uses means the pack cannot advertise a
    name the watcher would drop into `failed/`.
    """

    rules = filename_rules()
    lines = [
        "FILE NAMES MATTER",
        "  We work out what a file IS from its name. A name we cannot read is",
        f"  moved to {rules['unmatched_dir']} and nothing is imported.",
        "",
        "    extension   " + ", ".join(rules["extensions"]),
    ]
    for kind in rules["kinds"]:
        words = ", ".join(kind["keywords"])
        if words:
            lines.append(f"    {kind['kind']:<11} name must contain: {words}")
    good = ", ".join(g["name"] for g in rules["good"])
    bad = ", ".join(b["name"] for b in rules["bad"])
    lines += ["", f"    good        {good}", f"    rejected    {bad}"]
    return "\n".join(lines)


def _pack_conn_block(addr: Dict) -> str:
    """The four values, formatted identically in every README in every mode."""

    return (
        "YOUR CONNECTION\n"
        f"  host       {addr['host']}\n"
        f"  port       {addr['port']}\n"
        f"  username   {addr['username']}\n"
        f"  folder     {addr['upload_path']}/\n"
        "  protocol   SFTP over SSH, public-key authentication (no password)\n"
    )


def _pack_send_sh(addr: Dict, key_rel: str, after_upload_line: str) -> str:
    """`send.sh` — upload exactly one file. Identical in both modes.

    ``after_upload_line`` is passed in rather than hardcoded because what
    happens after an upload is a SETTING. See the caller.
    """

    return f"""#!/bin/sh
# Upload one file to CityCare. Usage:  sh send.sh <file>
#
# POSIX sh on purpose: this has to run on whatever the partner has.
set -e

KEY="$(dirname "$0")/{key_rel}"

if [ $# -ne 1 ]; then
    echo "usage: sh send.sh <file>" >&2
    echo "example: sh send.sh articles-export-2026-07-13.csv" >&2
    exit 2
fi

FILE="$1"

if [ ! -f "$FILE" ]; then
    echo "no such file: $FILE" >&2
    exit 1
fi

if [ ! -f "$KEY" ]; then
    echo "private key not found at $KEY" >&2
    echo "run 'sh setup.sh' first." >&2
    exit 1
fi

# A ZIP cannot carry file permissions, so the key almost certainly extracted as
# 0644 and ssh refuses to use it ("Permissions 0644 ... are too open"). Fix it
# here as well as in setup.sh, so send.sh works even if setup.sh was skipped.
chmod 600 "$KEY" 2>/dev/null || true

echo "uploading $FILE to {addr['host']} ..."

sftp -b - \\
    -i "$KEY" \\
    -o IdentitiesOnly=yes \\
    -o StrictHostKeyChecking=accept-new \\
    -P "{addr['port']}" \\
    "{addr['username']}@{addr['host']}" <<SFTP_COMMANDS
cd "{addr['upload_path']}"
put "$FILE"
bye
SFTP_COMMANDS

{after_upload_line}

# If you have lftp instead of sftp, the equivalent one-liner is:
#   lftp -u "{addr['username']}", -p {addr['port']} \\
#        -e 'set sftp:connect-program "ssh -i '"$KEY"' -o IdentitiesOnly=yes"; \\
#            cd {addr['upload_path']}; put "'"$FILE"'"; bye' \\
#        sftp://{addr['host']}
"""


# ---- the one-file partner script -------------------------------------------
#
# The pack above is four files in a ZIP and it works, but the partner still has
# to unzip it keeping its folders, run setup.sh, then run send.sh once PER FILE
# and remember what each file is called. `_partner_script` collapses that into a
# single `.sh` that takes no arguments: it installs its own key on first run and
# then uploads every data file sitting next to it.
#
# The part that earns its keep is the REFUSAL. Our watcher works out what a file
# is from its NAME; a name it cannot read is moved to `failed/` and nothing is
# imported. From the partner's side that upload succeeded — clean transfer, no
# error, no reply — so they believe the job is done, and the one party who could
# notice cannot open this console to find out. The script therefore applies the
# SAME rules on THEIR machine, before the upload, and names the fix. A file we
# would quietly set aside never leaves their disk.
#
# Same address guard as the pack (`_pack_address`), for a stronger reason. A ZIP
# is opened once; a script is saved, pointed at by a crontab line, and re-run
# every night for months. A wrong host in it is wrong every one of those nights.


def _script_rules_sh() -> Dict[str, str]:
    """The naming contract as shell, generated from `filename_rules()`.

    Never retyped — same source the admin page and the pack README read, so the
    script cannot refuse a name the watcher would have accepted, or accept one
    it would have dropped into `failed/`.

    Two details make the translation faithful rather than approximate:

    * ``detect_kind`` lowercases the basename and tests catalog BEFORE
      inventory. A ``case`` runs its first matching arm, so emitting the kinds
      in the order ``filename_rules()`` reports them (AST order, i.e. source
      order) reproduces that precedence exactly.
    * the extension test is done on the lowercased name rather than by globbing
      ``*.csv``, because a glob is case-sensitive and ``STOCK.CSV`` is a file
      the watcher accepts.

    Returns the ``case`` arms as strings for the template below.
    """

    rules = filename_rules()

    ext_arm = "|".join("*" + str(e).lower() for e in rules["extensions"])

    kind_arms = []
    for kind in rules["kinds"]:
        words = [w for w in kind["keywords"] if w]
        if not words:
            continue
        pattern = "|".join(f"*{w.lower()}*" for w in words)
        kind_arms.append(f'        {pattern}) echo "{kind["kind"]}" ; return 0 ;;')

    good = [g["name"] for g in rules["good"]] or ["articles-export-20260713.csv"]
    while len(good) < 2:
        good.append(good[-1])

    return {
        "ext_arm": ext_arm,
        "kind_arms": "\n".join(kind_arms),
        "good_a": good[0],
        "good_b": good[1],
        "unmatched_dir": rules["unmatched_dir"],
    }


def _partner_script(
    addr: Dict,
    label: str,
    fingerprint: str,
    private_pem: Optional[str],
    auto_load: bool,
) -> str:
    """One self-contained POSIX `sh` file a partner keeps, schedules and re-runs.

    ``private_pem`` is the private half when we have just made it (the two key
    endpoints), or ``None`` for a partner whose key we do not hold — then the
    key block is a clearly-marked PLACEHOLDER and the header says the file will
    not run until a key is pasted into it. Both forms are valid `sh`: the
    placeholder sits inside a QUOTED heredoc, where it is inert text.

    ``auto_load`` is the real ingest setting (``cache.get_ingest_enabled()``),
    passed in for the same reason `_pack_send_sh` takes its closing line as an
    argument: what happens after an upload is a SETTING, and a script that says
    "there is nothing else to do" while automatic loading is off sends the
    partner away believing a half-finished job is finished.

    ⚠️ The heredoc delimiter is QUOTED — ``<<'PRIVATE_KEY'``. Unquoted, the
    shell would expand ``$`` and backticks inside the key body and write a
    corrupted key, which fails later as an unreadable-format error that names
    nothing.
    """

    r = _script_rules_sh()
    script_name = f"citycare-upload-{label}.sh"
    exts = ", ".join(str(e) for e in filename_rules()["extensions"])

    if private_pem:
        secret_warning = (
            "#  !! THIS FILE CONTAINS A PRIVATE KEY. IT IS A CREDENTIAL. !!\n"
            "#     Anyone who has this file can upload to CityCare as you. Treat it the\n"
            "#     way you treat a password: do not email it, do not paste it into a\n"
            "#     ticket or a chat channel, do not put it on a shared drive.\n"
            "#     We keep NO copy of it. If it is lost, tell us -- we will revoke this\n"
            "#     key and issue a new one, which is quick and is the correct answer."
        )
        key_block = private_pem.strip("\n")
    else:
        secret_warning = (
            "#  !! THIS FILE WILL NOT RUN YET -- THERE IS NO KEY IN IT. !!\n"
            "#     The key block below is a placeholder. CityCare holds only the PUBLIC\n"
            "#     half of your key (fingerprint below); the private half is on your\n"
            "#     machine and we have no copy of it, which is how it should be.\n"
            "#     Open this file in a PLAIN TEXT editor and replace the placeholder\n"
            "#     lines between the two PRIVATE_KEY markers with the entire contents of\n"
            "#     your private key file -- every line, including the -----BEGIN and\n"
            "#     -----END lines. Save, then run it. Until then it stops with a message\n"
            "#     instead of uploading anything."
        )
        key_block = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "### PASTE-YOUR-PRIVATE-KEY-HERE ### replace these three lines with your key\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )

    closing = (
        '    echo "The files are picked up and imported automatically at our end."\n'
        '    echo "There is nothing else for you to do."'
        if auto_load
        else '    echo "NOTE: automatic loading is currently switched OFF at our end, so"\n'
             '    echo "your files will WAIT in the folder until someone at CityCare loads"\n'
             '    echo "them. Please tell us when you have sent something."'
    )

    return f"""#!/bin/sh
# ============================================================================
#  CityCare pharmacy -- upload script
#  Prepared for: {label}
#
{secret_warning}
#
#  Key fingerprint: {fingerprint}
#     Read this back to us and we will confirm this file carries the key we
#     registered for you, and not a copy of somebody else's. It is the same
#     string 'ssh-keygen -lf' prints.
#
#  WHAT IT DOES, with no arguments at all:
#    1. installs the key into ~/.ssh (first run only, then it leaves it alone)
#    2. looks at the {exts} files sitting in the SAME FOLDER as this script
#    3. refuses any whose name we would not be able to read, and says exactly
#       how to rename it -- those are files that would otherwise upload fine
#       and then be set aside at our end without anybody telling you
#    4. uploads the rest
#
#  RUN IT:            sh {script_name}
#  EVERY NIGHT:       sh {script_name} --every-night
#                     (prints a crontab line for you to paste -- it installs
#                      nothing by itself)
#
#  POSIX sh on purpose: it has to run on whatever you have.
# ============================================================================

set -e

LABEL="{label}"
KEY="$HOME/.ssh/citycare-{label}"
HOST="{addr['host']}"
PORT="{addr['port']}"
ACCOUNT="{addr['username']}"
REMOTE_DIR="{addr['upload_path']}"
PLACEHOLDER_MARK="PASTE-YOUR-PRIVATE-KEY-HERE"

# This script's own folder, as an ABSOLUTE path. Both the file scan and the
# crontab line need it: cron does not run from the folder the script is in, so
# a relative "." would scan your home directory instead.
DIR=$(cd "$(dirname "$0")" && pwd)
SELF="$DIR/$(basename "$0")"


# ---- the key -------------------------------------------------------------
# Written once, then never touched again. Deleting $KEY is how you make this
# re-install it.
install_key() {{
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh" 2>/dev/null || true

    # QUOTED delimiter below -- <<'PRIVATE_KEY'. Without the quotes the shell
    # would try to expand the key's contents and write a broken file.
    umask 077
    cat > "$KEY.new" <<'PRIVATE_KEY'
{key_block}
PRIVATE_KEY

    if grep -q "$PLACEHOLDER_MARK" "$KEY.new" 2>/dev/null; then
        rm -f "$KEY.new"
        echo "" >&2
        echo "This copy of the script does not have a key in it yet." >&2
        echo "" >&2
        echo "Open $SELF in a plain text editor, find the two lines that say" >&2
        echo "PRIVATE_KEY, and replace everything between them with the whole" >&2
        echo "contents of your private key file (all of it, including the" >&2
        echo "-----BEGIN and -----END lines). Save it and run this again." >&2
        echo "" >&2
        echo "If you do not have a key yet, ask CityCare for one." >&2
        return 1
    fi

    mv "$KEY.new" "$KEY"
    chmod 600 "$KEY"
    echo "Installed your key at $KEY"
}}


# ---- what a file IS, worked out from its name ----------------------------
# These are the SAME rules our server uses. A name that matches nothing here is
# a name our server cannot read either: it would accept the upload and then move
# the file to {r['unmatched_dir']} without importing anything, and you would
# never hear about it. So we stop it here instead.
classify() {{
    name=$(basename "$1" | tr 'A-Z' 'a-z')
    case "$name" in
{r['kind_arms']}
    esac
    return 1
}}

is_data_file() {{
    case "$1" in
        {r['ext_arm']}) return 0 ;;
    esac
    return 1
}}


# ---- one upload ----------------------------------------------------------
# The sftp transcript is kept out of the way unless something fails; on a good
# run you get one plain sentence per file instead of a wall of "sftp>" lines.
upload_one() {{
    _file="$1"
    _log="${{TMPDIR:-/tmp}}/citycare-upload.$$.log"

    if printf 'cd %s\\nput "%s"\\nbye\\n' "$REMOTE_DIR" "$_file" | sftp -b - \\
        -i "$KEY" \\
        -o IdentitiesOnly=yes \\
        -o StrictHostKeyChecking=accept-new \\
        -P "$PORT" \\
        "$ACCOUNT@$HOST" > "$_log" 2>&1
    then
        rm -f "$_log"
        return 0
    fi

    echo "         FAILED. The server said:" >&2
    sed 's/^/           /' "$_log" >&2
    rm -f "$_log"
    return 1
}}


print_cron() {{
    echo "To send whatever is in this folder every night at 01:30, run:"
    echo ""
    echo "    crontab -e"
    echo ""
    echo "and add this ONE line exactly as it appears here:"
    echo ""
    # 'sh "$SELF"', not '$SELF'. A downloaded script almost never has its
    # executable bit set, and cron would answer that with a bare "Permission
    # denied" in a log nobody is watching yet. Running it through sh needs no
    # such bit -- the same reason every README here says 'sh setup.sh'.
    echo "30 1 * * * /bin/sh \\"$SELF\\" >> \\"$DIR/citycare-upload.log\\" 2>&1"
    echo ""
    echo "Save and close. That is the whole job."
    echo ""
    echo "Nothing has been installed or changed by running this -- the line"
    echo "above was only printed. It writes what happened each night to"
    echo "$DIR/citycare-upload.log, which is the first thing to look at if"
    echo "files ever stop arriving."
}}


# ---- arguments -----------------------------------------------------------
# There are none, on purpose. --every-night is the single exception and it
# uploads nothing.
case "$1" in
    "")
        ;;
    --every-night)
        print_cron
        exit 0
        ;;
    -h|--help)
        echo "usage: sh $(basename "$0")                 upload this folder now"
        echo "       sh $(basename "$0") --every-night   print a crontab line"
        exit 0
        ;;
    *)
        echo "This script takes no arguments -- just run:  sh $(basename "$0")" >&2
        echo "It uploads every data file in the folder it is saved in." >&2
        exit 2
        ;;
esac


if ! command -v sftp >/dev/null 2>&1; then
    echo "sftp was not found on this machine." >&2
    echo "Install OpenSSH (on Windows: Settings > Apps > Optional features >" >&2
    echo "OpenSSH Client, or use Git Bash / WSL) and run this again." >&2
    exit 1
fi

if [ ! -f "$KEY" ]; then
    install_key || exit 1
fi
chmod 600 "$KEY" 2>/dev/null || true

echo "CityCare upload -- $HOST"
echo "Looking in $DIR"
echo ""

found=0
sent=0
skipped=0
failed=0

for f in "$DIR"/*; do
    [ -f "$f" ] || continue

    base=$(basename "$f")
    lower=$(printf '%s' "$base" | tr 'A-Z' 'a-z')

    is_data_file "$lower" || continue
    found=$((found + 1))

    # A double quote in the name would break the sftp command line below. It is
    # a rare name and a one-word fix, so say so rather than send something odd.
    case "$base" in
        *'"'*)
            skipped=$((skipped + 1))
            echo "SKIPPED  $base"
            echo "         The name contains a \\" character. Please remove it."
            echo ""
            continue
            ;;
    esac

    kind=$(classify "$base") || kind=""

    if [ -z "$kind" ]; then
        skipped=$((skipped + 1))
        echo "SKIPPED  $base"
        echo "         We cannot tell what this file is from its name. If it were"
        echo "         uploaded we would set it aside and import nothing, without"
        echo "         being able to tell you. Rename it, for example:"
        echo "           {r['good_a']}"
        echo "           {r['good_b']}"
        echo "         then run this again."
        echo ""
        continue
    fi

    echo "Sending  $base  (we will read this as $kind)"
    if upload_one "$f"; then
        sent=$((sent + 1))
        echo "         done."
    else
        failed=$((failed + 1))
    fi
    echo ""
done

echo "----------------------------------------------------------------"
if [ "$found" -eq 0 ]; then
    echo "No data files found in $DIR"
    echo "Put your {exts} files in that folder, next to this script, and run"
    echo "this again."
    exit 0
fi

echo "$found file(s) looked at: $sent sent, $skipped skipped, $failed failed."

if [ "$skipped" -gt 0 ]; then
    echo ""
    echo "The skipped files were NOT uploaded. Rename them as shown above and"
    echo "run this again -- nothing you already sent will be sent twice in a way"
    echo "that causes a problem."
fi

if [ "$failed" -gt 0 ]; then
    echo ""
    echo "Some files did not send. The most common causes:"
    echo "  'Permission denied (publickey)'  we have not registered your key, or"
    echo "                                   we registered a different one. Read"
    echo "                                   us the fingerprint at the top of this"
    echo "                                   file."
    echo "  'Connection refused' / timeout   your firewall may block outbound"
    echo "                                   port $PORT. Ask your network team."
    exit 1
fi

if [ "$sent" -gt 0 ]; then
    echo ""
{closing}
fi
"""


def _pack_setup_sh_partner_key(addr: Dict, label: str) -> str:
    """`setup.sh` for the SAFE mode: the partner makes their own key."""

    return f"""#!/bin/sh
# Make the SSH key CityCare will register for you. Run this once.
#   sh setup.sh
set -e

KEY="$(dirname "$0")/citycare_sftp"

if [ -f "$KEY" ]; then
    echo "$KEY already exists." >&2
    echo "Delete it first if you really want a NEW key -- the old one will stop" >&2
    echo "working once we register the new one." >&2
    exit 1
fi

if ! command -v ssh-keygen >/dev/null 2>&1; then
    echo "ssh-keygen was not found. Install OpenSSH (on Windows: 'Settings >" >&2
    echo "Apps > Optional features > OpenSSH Client', or use Git Bash/WSL)." >&2
    exit 1
fi

ssh-keygen -t ed25519 -f "$KEY" -C "{label}"

chmod 600 "$KEY" 2>/dev/null || true

echo ""
echo "=============================================================="
echo "SEND BOTH OF THE FOLLOWING BACK TO CITYCARE"
echo "=============================================================="
echo ""
echo "1. the public key (one line, safe to email):"
echo ""
cat "$KEY.pub"
echo ""
echo "2. its fingerprint, so we can read it back to you and confirm we"
echo "   registered YOUR key and not a mistyped one:"
echo ""
ssh-keygen -lf "$KEY.pub"
echo ""
echo "=============================================================="
echo "DO NOT SEND $KEY -- the file WITHOUT the .pub ending."
echo "That is your private key. Nobody, including us, ever needs it."
echo "=============================================================="
"""


def _pack_setup_sh_generated_key(addr: Dict) -> str:
    """`setup.sh` for the generated-key mode: fix the mode, then prove it works."""

    return f"""#!/bin/sh
# Prepare the key that came in this archive and test the connection.
#   sh setup.sh
set -e

KEY="$(dirname "$0")/key/citycare_sftp"

if [ ! -f "$KEY" ]; then
    echo "expected the private key at $KEY but it is not there." >&2
    echo "Unzip the whole archive, keeping its folders, and run this from" >&2
    echo "the folder that contains setup.sh." >&2
    exit 1
fi

# FIRST, before anything touches it. A ZIP cannot carry file permissions, so
# this key extracted as 0644 and ssh will refuse it outright with
#   "Permissions 0644 for 'citycare_sftp' are too open."
chmod 600 "$KEY"
echo "key permissions set to 600."

echo "testing the connection to {addr['host']} ..."

sftp -b - \\
    -i "$KEY" \\
    -o IdentitiesOnly=yes \\
    -o StrictHostKeyChecking=accept-new \\
    -P "{addr['port']}" \\
    "{addr['username']}@{addr['host']}" <<SFTP_COMMANDS
cd "{addr['upload_path']}"
pwd
bye
SFTP_COMMANDS

echo ""
echo "connection OK. Send a file with:  sh send.sh <file>"
"""


def _pack_readme_partner_key(addr: Dict, label: str) -> str:
    """`README.txt` for the safe mode. Contains no secret."""

    return f"""CityCare pharmacy -- SFTP upload pack
Prepared for: {label}

WHAT IS IN HERE
  README.txt   this file
  setup.sh     makes your SSH key (run this first)
  send.sh      uploads one file

  There is NO password and NO key in this archive. Nothing in it is secret,
  so it is safe to forward internally.

  Run the scripts with 'sh setup.sh' and 'sh send.sh'. Unzipping usually
  drops the executable bit, so './setup.sh' may say "permission denied" --
  'sh setup.sh' always works.

{_pack_conn_block(addr)}
STEP 1 -- make your key (once)

    sh setup.sh

  It creates two files next to this README:
    citycare_sftp        PRIVATE. Stays on your machine forever. Never send
                         it to anyone, including us. We never ask for it.
    citycare_sftp.pub    public. This is the half you send back.

STEP 2 -- send us two things

  1. the whole contents of citycare_sftp.pub -- one line, starting
     "ssh-ed25519 "
  2. the fingerprint line setup.sh prints, starting "256 SHA256:"

  We register the public key under the label "{label}". It works from your
  very next connection -- nothing needs restarting on either side. We read
  the fingerprint back to you so you can confirm we registered your key and
  not a mistyped copy of it.

STEP 3 -- send a file

    sh send.sh articles-export-2026-07-13.csv

  The file lands in {addr['upload_path']}/ and is imported automatically.

{_pack_filename_help()}

IF SOMETHING GOES WRONG
  "Permissions ... are too open"    run: chmod 600 citycare_sftp
  "Permission denied (publickey)"   we have not registered your key yet, or
                                    we registered a different one -- check the
                                    fingerprint with: ssh-keygen -lf citycare_sftp.pub
  "Connection refused" / timeout    your firewall may block outbound
                                    port {addr['port']}; ask your network team.
"""


def _pack_readme_generated_key(addr: Dict, label: str, fingerprint: str) -> str:
    """`README-FIRST.txt` for the generated-key mode. This archive IS the secret."""

    return f"""!! READ THIS BEFORE YOU DO ANYTHING ELSE !!

THIS ARCHIVE IS THE CREDENTIAL.

  It contains a private key that opens a CityCare upload account. Anyone who
  has this file can upload as you. Treat it exactly the way you would treat a
  password:

    * DO NOT email it, and do not forward the message it arrived in.
    * DO NOT put it in a ticket, a chat channel, or a shared drive.
    * Hand it over the way you hand over a password, and delete the copy you
      were handed once it is in place.

  IT CANNOT BE DOWNLOADED AGAIN. We keep no copy of the private half. If it
  is lost, tell us and we will revoke this key and issue a new one -- that is
  the correct response, and it is quick. Do not go looking for a spare copy.

  Registered under the label: {label}
  Key fingerprint:            {fingerprint}

  Read that fingerprint back to us and we will confirm it matches what we
  registered. It is the same string 'ssh-keygen -lf' prints.

WHAT IS IN HERE
  README-FIRST.txt    this file
  key/citycare_sftp   the private key -- the secret
  setup.sh            fixes the key's file permissions, then tests the login
  send.sh             uploads one file

  Run the scripts with 'sh setup.sh' and 'sh send.sh'. Unzipping usually
  drops the executable bit, so './setup.sh' may say "permission denied" --
  'sh setup.sh' always works.

{_pack_conn_block(addr)}
STEP 1 -- prepare the key and test the login

    sh setup.sh

  A ZIP file cannot carry file permissions, so the key has almost certainly
  arrived world-readable and ssh will refuse to use it. setup.sh runs
  'chmod 600' on it first -- that is why it exists.

STEP 2 -- send a file

    sh send.sh articles-export-2026-07-13.csv

  The file lands in {addr['upload_path']}/ and is imported automatically.

{_pack_filename_help()}

IF SOMETHING GOES WRONG
  "Permissions ... are too open"    run: chmod 600 key/citycare_sftp
  "Permission denied (publickey)"   the key may have been revoked -- ask us to
                                    check the fingerprint above.
  "Connection refused" / timeout    your firewall may block outbound
                                    port {addr['port']}; ask your network team.

WOULD YOU RATHER MAKE YOUR OWN KEY?
  You can, and it is better: the private key then never leaves your machine
  at all. Ask us for the pack WITHOUT a key and we will revoke this one.
"""


@router.post("/sftp/partner-pack", dependencies=[Depends(require_super_admin)])
async def sftp_partner_pack(p: SftpPartnerPack, request: Request) -> Any:
    """Build the ZIP a partner unzips and runs. Two modes; the default is safe.

    Reuses, rather than restates:

    * `sftp_connection` for the address, port, username, folder AND the
      `host_source` that says how much to trust the address. One resolver.
    * `_clean_label` for the label rule -- the label becomes both a filename
      inside the archive's name and, in generated-key mode, `keys/<label>.pub`.
    * `_keys_root` for the 503 when the key volume is not mounted.
    * `sftp_keys_generate` -- unchanged, in full -- for the generated-key mode,
      so the duplicate-label 409, the key limit, the atomic write of BOTH
      `authorized_keys` and `keys/<label>.pub`, and the "register first, hand
      over the secret second" ordering all come from one place.
    * `filename_rules` for the naming contract printed in the README.

    ⚠️ **Order is load-bearing, for the same reason it is in
    `sftp_keys_generate`.** Label, mount and address are all validated BEFORE
    anything is generated or registered. A pack that 400s on its address after
    having registered a key would leave a live credential behind for an archive
    that was never downloaded.
    """

    import io
    import zipfile

    from fastapi import Response

    label = _clean_label(p.label)
    _keys_root()                       # 503 here, not after generating a key
    addr = _pack_address(await sftp_connection(request))

    # What happens AFTER the upload is a setting, not a constant. Automatic
    # loading can be switched off, and when it is a file sits in the folder as
    # Waiting until somebody presses Load. Telling a partner "there is nothing
    # else to do" in that state sends them away believing the job is finished
    # when it is half done -- and they are the one party who cannot open this
    # console to find out otherwise. Same defect the page carried between its
    # status strip and its stale-file note; in a zip it is worse, because the
    # zip is kept and re-read for months after the setting has moved.
    after_upload_line = (
        'echo "sent. It is picked up automatically; there is nothing else to do."'
        if await cache.get_ingest_enabled()
        else 'echo "sent. Automatic loading is currently OFF at our end, so it will "\n'
             '     "wait in the folder until someone loads it. Tell us when you have sent one."'
    )

    fingerprint = None
    buf = io.BytesIO()

    if p.include_private_key:
        # Generate + register through the SAME path, unchanged. Anything it
        # raises -- 409 duplicate label, 400 key limit, 503 not mounted --
        # propagates as-is and no archive is built.
        issued = await sftp_keys_generate(SftpKeyGenerate(label=label), request)
        fingerprint = issued["fingerprint"]

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            _pack_write(zf, "README-FIRST.txt",
                        _pack_readme_generated_key(addr, label, fingerprint), 0o644)
            # 0o600 is recorded and will very probably be ignored on extraction;
            # setup.sh does the real work. See _pack_write.
            _pack_write(zf, "key/citycare_sftp", issued["private_key"], 0o600)
            _pack_write(zf, "setup.sh", _pack_setup_sh_generated_key(addr), 0o755)
            _pack_write(zf, "send.sh", _pack_send_sh(addr, "key/citycare_sftp", after_upload_line), 0o755)
    else:
        # Documentation with the real values in it. Nothing is created,
        # registered or written server-side by this branch.
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            _pack_write(zf, "README.txt", _pack_readme_partner_key(addr, label), 0o644)
            _pack_write(zf, "setup.sh", _pack_setup_sh_partner_key(addr, label), 0o755)
            _pack_write(zf, "send.sh", _pack_send_sh(addr, "citycare_sftp", after_upload_line), 0o755)

    # Audit: label + mode + (when we made one) fingerprint. All three are public
    # facts. The private key is not passed here and never must be.
    #
    # The `activity_audit` middleware DOES see this route -- `should_record`
    # takes every mutating /admin/ path whose last segment has no "." in it, and
    # "partner-pack" has none (this is exactly why /admin/embed/snippets.zip is
    # NOT audited). Being unlisted in `activity._ROUTES`, the middleware's row
    # carries route + status and an empty `detail`, so the row below is the one
    # holding the label and the mode. The middleware reads the REQUEST body only
    # -- `{"label": ..., "include_private_key": ...}`, no secret -- and no
    # middleware in this app reads a response body, so the ZIP itself is never
    # captured anywhere.
    from app import activity
    from app import auth as authmod

    actor_email, actor_role = None, None
    try:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            claims = authmod.decode_token(header.split(" ", 1)[1])
            actor_email, actor_role = claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an unreadable token is an anonymous actor
        pass

    detail = {
        "label": label,
        "mode": "generated-key" if p.include_private_key else "instructions",
    }
    if fingerprint:
        detail["fingerprint"] = fingerprint

    await activity.record_event(
        activity.action_for("POST", "/admin/sftp/partner-pack"),
        actor_email=actor_email,
        actor_role=actor_role,
        target=label,
        method="POST",
        path="/admin/sftp/partner-pack",
        status=200,
        detail=detail,
    )

    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={
            # `label` passed _clean_label, so it is [A-Za-z0-9._-] only: nothing
            # here can break out of the quotes or inject a header line.
            "Content-Disposition": f'attachment; filename="citycare-sftp-{label}.zip"',
            # A generated pack is never a cacheable document, and in
            # generated-key mode it is a secret sitting in a browser download.
            "Cache-Control": "no-store",
        },
    )


class SftpPartnerScript(BaseModel):
    """Which registered partner to write the script for. Same label the registry uses."""

    label: str


@router.post("/sftp/partner-script", dependencies=[Depends(require_super_admin)])
async def sftp_partner_script(p: SftpPartnerScript, request: Request) -> Dict:
    """The one-file upload script for a partner we ALREADY have a key for.

    The two key endpoints return this script with the key baked in, because that
    is the one moment the private half exists here. This route is for every
    moment after it: a partner registered six months ago, working fine, who now
    wants the nightly upload — or who has lost the script and still has their
    key. We hold only their PUBLIC half, so the script comes out with a
    placeholder key block and a header telling them to paste their existing
    private key into it. That is not a downgrade; it is the flow where the
    private key never travels at all.

    ⚠️ **Nothing is created, registered or written by this route.** It reads
    `keys/<label>.pub` to get a fingerprint the partner can check the file
    against, and renders text. It cannot change who can connect.

    Refusals, in the order they are cheapest and most certain:

    * 400 — the label is not a label (`_clean_label`).
    * 503 — the keys volume is not mounted, so "is this partner registered?" has
      no answer. Deliberately not softened to 404: "I cannot tell you" and "no
      such partner" are different sentences and only one of them is true.
    * 404 — no key under that label. Writing a script for a partner who cannot
      connect produces a file that fails every night with a message about keys.
    * 400 — the address is not fit to ship (`_pack_address`, unchanged: empty,
      loopback, or merely DETECTED). Stricter here than anywhere else in the
      console for the reason in the block comment above `_partner_script`: a
      script is kept, scheduled and re-run, so a guessed hostname in it is a
      guess that keeps being wrong long after everyone has forgotten it was one.
    """

    label = _clean_label(p.label)
    root = _keys_root()                       # 503 before any "does it exist?"

    pub = root / "keys" / f"{label}.pub"
    if not pub.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"no key labelled '{label}' is registered, so a script for them would "
                "fail on every run. Register one first with POST /admin/sftp/keys "
                "(or /generate, which returns the script with the key already in it)."
            ),
        )

    try:
        raw = pub.read_text(encoding="utf-8").strip()
    except OSError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the file registered as '{label}' could not be read, so its fingerprint "
                "cannot be printed in the script. Re-register the key."
            ),
        )

    # 400 with a specific reason if this is not a key line we wrote. The
    # fingerprint is the whole point of the header — a script carrying a
    # fingerprint that matches nothing is worse than one carrying none, because
    # the partner reads it back and we confirm it.
    parsed = _parse_public_key(raw)

    addr = _pack_address(await sftp_connection(request))
    script = _partner_script(
        addr, label, parsed["fingerprint"], None, await cache.get_ingest_enabled()
    )

    # Audit: label + fingerprint, both public facts, and that is the whole row.
    # The script is NEVER logged — here it holds no key, but the same call shape
    # is used by the endpoints where it does, and a rule with an exception in it
    # is a rule that gets copied wrong.
    #
    # Recorded explicitly for the same reason the key routes do it: this path is
    # not in `activity._ROUTES`, so the middleware's row would carry route +
    # status and an empty `detail`. The middleware reads the REQUEST body only
    # (`{"label": ...}`) and no middleware in this app reads a response body.
    from app import activity
    from app import auth as authmod

    actor_email, actor_role = None, None
    try:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            claims = authmod.decode_token(header.split(" ", 1)[1])
            actor_email, actor_role = claims.get("email"), claims.get("role")
    except Exception:  # noqa: BLE001 — an unreadable token is an anonymous actor
        pass

    await activity.record_event(
        activity.action_for("POST", "/admin/sftp/partner-script"),
        actor_email=actor_email,
        actor_role=actor_role,
        target=label,
        method="POST",
        path="/admin/sftp/partner-script",
        status=200,
        detail={"label": label, "fingerprint": parsed["fingerprint"]},
    )

    return {
        "label": label,
        "fingerprint": parsed["fingerprint"],
        "script": script,
    }


@router.delete("/sftp/keys/{label}", dependencies=[Depends(require_super_admin)])
async def sftp_keys_delete(label: str) -> Dict:
    """Revoke a key. Removed from authorized_keys AND keys/ — or it comes back.

    Deleting only the .pub file leaves the partner connecting until the next
    restart; deleting only the authorized_keys line lets the boot rebuild
    resurrect it. Both, always.
    """

    root = _keys_root()
    label = _clean_label(label)

    async with _sftp_keys_lock:
        pub = root / "keys" / f"{label}.pub"
        if not pub.is_file():
            raise HTTPException(status_code=404, detail=f"no key labelled '{label}'")

        try:
            b64 = _parse_public_key(pub.read_text(encoding="utf-8").strip())["b64"]
        except HTTPException:
            b64 = ""

        pub.unlink()

        ak = root / "authorized_keys"
        if ak.exists():
            kept = []
            for ln in ak.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                parts = ln.split()
                # Match on the key material, not the comment: that is the thing
                # sshd actually authenticates against.
                if b64 and len(parts) >= 2 and parts[1] == b64:
                    continue
                if len(parts) >= 3 and parts[2] == f"pharma:{label}":
                    continue
                kept.append(ln)
            _atomic_write(ak, ("\n".join(kept) + "\n") if kept else "", 0o600)

    return {"status": "ok", "removed": label}


# ---- user management -------------------------------------------------------


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: Optional[str] = None
    role: str = "user"


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    approved: Optional[bool] = None
    password: Optional[str] = None
    # Pin this account to one branch (see caller_store_scope). "" clears it back
    # to the global view. Distinct from None, which means "leave unchanged".
    store_id: Optional[str] = None


# ---- privilege boundary on the users surface -------------------------------
#
# Everything below that MUTATES an account requires `require_super_admin`, not
# the router-level `require_admin`. The hole this closes was a full privilege
# escalation: `POST /users` passed `role` straight through to `auth.create_user`,
# so any plain `admin` — including one pinned to a single branch — could mint a
# `super_admin`, approve it, and log in as it. `PATCH /users/{id}` could promote
# an existing account the same way, and `DELETE` could remove the real one.
#
# `GET /users` deliberately stays at `require_admin`. Reading the roster is what
# the Users page is for and an admin already sees every colleague's name in the
# console; the damage is a name and a role, not a new super_admin. Promotion,
# approval and deletion are the irreversible half, and only those are narrowed.
# Keeping the read open also means the page still renders for a plain admin —
# the mutation buttons 403 rather than the whole screen going blank.
#
# Role is re-read from the `users` table per request (see require_super_admin),
# never from the JWT, so a demotion takes effect immediately rather than at token
# expiry.


@router.get("/users")
async def users_list() -> List[Dict]:
    """The account roster. Intentionally `require_admin`, not super_admin — see
    the boundary note above: reading who exists is not the dangerous half."""

    from app import auth

    users = await auth.list_users()
    # auth._public() does not carry store_id, and auth.py is off-limits here, so
    # the scope is joined on rather than smuggled into the auth module.
    try:
        scopes = {r["email"]: r["store_id"] for r in await q("SELECT email, store_id FROM users")}
    except Exception:  # noqa: BLE001 — column not added yet
        scopes = {}
    for u in users:
        u["store_id"] = scopes.get(u["email"])
    return users


@router.post("/users", dependencies=[Depends(require_super_admin)])
async def users_create(u: NewUser) -> Dict:
    """Create an account. **super_admin only** — this is the escalation path.

    `role` is caller-supplied and reaches `auth.create_user` unchanged, so while
    this route inherited only `require_admin` any admin could create a
    `super_admin`, approve it and sign in as it. Gating the route (rather than
    filtering the `role` value) is the narrower fix: it also closes creating a
    peer `admin`, which a branch-pinned manager has no business doing either.
    """

    from app import auth

    try:
        return await auth.create_user(u.email, u.name, u.password, u.role)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/users/{user_id}", dependencies=[Depends(require_super_admin)])
async def users_update(user_id: int, p: UserPatch) -> Dict:
    """Change role / active / approved / password / branch pin. **super_admin only.**

    Same escalation as `POST /users` by a different door: `role` is passed to
    `auth.update_user` unchanged, so a plain admin could have promoted their own
    account. `approved` and `store_id` matter just as much — approval is the
    console gate, and clearing `store_id` removes a branch manager's boundary.
    """

    from app import auth

    out: Dict = {}
    if any(v is not None for v in (p.role, p.active, p.approved, p.password)):
        try:
            out = await auth.update_user(user_id, role=p.role, active=p.active,
                                         approved=p.approved, password=p.password)
        except auth.AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if p.store_id is not None:
        # "" clears the pin (back to the global view); any other value scopes the
        # account to that branch. Enforcement is in caller_store_scope, not here.
        store = p.store_id.strip() or None
        rows = await q(
            "UPDATE users SET store_id=$1 WHERE id=$2 RETURNING email, role, store_id",
            store, user_id,
        )
        if not rows:
            raise HTTPException(status_code=400, detail="user not found")
        out = {**out, "store_id": rows[0]["store_id"]}

    if not out:
        raise HTTPException(status_code=400, detail="nothing to update")
    return out


@router.delete("/users/{user_id}", dependencies=[Depends(require_super_admin)])
async def users_delete(user_id: int) -> Dict:
    """Remove an account. **super_admin only** — deleting the last super_admin,
    or an auditor's account, is not a branch manager's call."""

    from app import auth

    n = await auth.delete_user(user_id)
    return {"status": "ok", "removed": n}


# ---- chat logging helper (used by api.py) ---------------------------------


async def ensure_chat_logs() -> None:
    """Create/extend the chat_logs table (non-destructive, idempotent).

    **The trap this function exists to close.** The table is created with
    ``CREATE TABLE IF NOT EXISTS``, so on any database that already has
    ``chat_logs`` — every deployed one — the create is a no-op and a column added
    to the literal below would never appear. The audit columns therefore have to
    be added again as ``ALTER … ADD COLUMN IF NOT EXISTS``, so a fresh boot and
    an existing database converge on the same schema. ``migrations/
    0003_chat_logs_audit.sql`` carries the identical statements for an operator
    who would rather run it by hand; neither one is redundant.

    The five audit columns answer the questions the original shape could not:
    WHICH embed credential asked (``embed_id``), in WHICH conversation
    (``session_id``), WHICH model answered (``model``), WHICH tools it actually
    called (``tools``), and by WHICH route (``path``: agent / fast_path / cache).

    They are NULL on every pre-existing row, and NULL means *unattributed*. It is
    not backfilled with a guess — inventing an embed for a historical turn would
    make the audit log lie about the one thing it exists to answer.

    The six **turn-metric** columns (``input_tokens`` … ``ttft_ms``,
    ``migrations/0006_turn_metrics.sql``) are added the same way and for exactly
    the same reason — they are the third generation of columns on this table and
    the ``CREATE TABLE IF NOT EXISTS`` trap has now bitten twice. NULL there is
    "not captured", never zero: a turn logged before the metrics existed did not
    cost $0.00, and the cost panel has to be able to tell those apart.
    """

    await q(
        """CREATE TABLE IF NOT EXISTS chat_logs (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               lang TEXT, store_id TEXT,
               question TEXT, answer TEXT,
               cached BOOLEAN, latency_ms INT
           )"""
    )
    for col, typ in (
        ("embed_id", "TEXT"),
        ("session_id", "TEXT"),
        ("model", "TEXT"),
        ("tools", "JSONB"),
        ("path", "TEXT"),
        # turn metrics — see migrations/0006_turn_metrics.sql
        ("input_tokens", "INT"),
        ("output_tokens", "INT"),
        ("total_tokens", "INT"),
        ("reasoning_tokens", "INT"),
        ("cost_usd", "NUMERIC(12,6)"),
        ("ttft_ms", "INT"),
    ):
        await execute(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")
    await execute("CREATE INDEX IF NOT EXISTS idx_chat_logs_ts ON chat_logs (ts DESC)")
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_logs_embed_ts ON chat_logs (embed_id, ts DESC)"
    )


async def prune_chat_logs() -> int:
    """Delete chat logs older than the configured retention. Returns rows removed.

    ``chat_log_retention_days = 0`` means **keep forever** and skips the DELETE
    entirely. Without that escape hatch the audit log had a hard ceiling of 30
    days and no way to opt out short of editing code — a retention *policy* an
    operator cannot set is not a policy. The default stays 30, so nothing is
    deleted differently unless somebody deliberately changes it.

    A negative value is treated the same as 0 rather than as
    ``now() - (-5 days)``, which would delete every row that is not in the
    future.
    """

    days = get_settings().chat_log_retention_days
    if days <= 0:  # keep forever — never issue the DELETE
        return 0
    try:
        rows = await q(
            "DELETE FROM chat_logs WHERE ts < now() - ($1 || ' days')::interval RETURNING id",
            str(days),
        )
        return len(rows)
    except Exception:
        return 0


async def log_chat(
    question: str,
    answer: str,
    store_id,
    cached: bool,
    latency_ms: int,
    *,
    embed_id: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    tools: Optional[List[str]] = None,
    path: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    ttft_ms: Optional[int] = None,
) -> Optional[int]:
    """Persist one turn for the conversations + analytics views. Best-effort.

    Returns the new ``chat_logs.id``, or ``None`` if nothing could be written.

    **The id is a bonus, never a requirement.** ``tool_calls.turn_id`` and
    ``llm_calls.turn_id`` are ``NOT NULL REFERENCES chat_logs(id)``, so the
    per-call instrumentation cannot write a row until it knows this id — but a
    caller that gets ``None`` back must drop its buffered calls and answer the
    user anyway. Losing a trace is a gap in the analytics; refusing an answer
    because the trace could not be filed is a pharmacy left waiting.

    Every existing caller ignores the return value, so this is source-compatible:
    the signature and the keyword-only arguments are unchanged.

    The audit fields are keyword-only and default to ``None`` so every existing
    caller keeps working unchanged, and a caller that genuinely does not know a
    field (the cache path knows no model's tools because no model ran) records
    NULL rather than a plausible-looking blank.

    The same rule is load-bearing for the six turn metrics: **a missing cost is
    NULL, never 0.** A cached turn cost nothing to serve and a turn from before
    metrics capture existed has an unknown cost; writing 0.0 for both would make
    them indistinguishable, and ``/analytics/cost`` would draw a confident chart
    of zeros over history it never measured. Anything falsy-but-real (0 tokens on
    a refusal) is preserved — the coercion below is ``is None``, not ``or None``.

    **This must never break an answer.** The whole write is wrapped, exactly as
    ``history.record_turn`` and ``ingest_events.record`` are: a turn that is
    answered but not logged is a gap in the audit trail; a turn that FAILS
    because the audit trail was down is a pharmacy that got no answer. That
    ordering is not negotiable, and it is why a new column may not be allowed to
    raise out of here — an INSERT naming ``path`` against a database where
    ``ensure_chat_logs`` has not yet run would otherwise turn a schema drift into
    a chat outage.
    """

    lang = "MY" if any("က" <= ch <= "႟" for ch in (question or "")) else "EN"
    tools_json = json.dumps(list(tools)) if tools is not None else None
    # NUMERIC is bound through text: asyncpg maps `numeric` to Decimal and
    # refuses a bare float, and rounding it here would invent precision.
    cost_txt = None if cost_usd is None else str(cost_usd)

    def _id(rows) -> Optional[int]:
        """The id out of a RETURNING result, tolerating an empty one.

        `q` is the only thing between here and asyncpg, and an INSERT that wrote
        nothing hands back `[]`. Indexing it blindly would turn "the row was not
        written" into an IndexError escaping a function whose entire contract is
        that it never raises.
        """

        return int(rows[0]["id"]) if rows else None

    try:
        return _id(await q(
            """INSERT INTO chat_logs
                   (lang, store_id, question, answer, cached, latency_ms,
                    embed_id, session_id, model, tools, path,
                    input_tokens, output_tokens, total_tokens, reasoning_tokens,
                    cost_usd, ttft_ms)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,
                       $12,$13,$14,$15,$16::text::numeric,$17)
               RETURNING id""",
            lang, store_id, question, answer, cached, latency_ms,
            embed_id or None, session_id or None, model or None,
            tools_json, path or None,
            input_tokens, output_tokens, total_tokens, reasoning_tokens,
            cost_txt, ttft_ms,
        ))
    except Exception:
        pass
    # Fall back one schema generation at a time, so a database that is missing
    # only the metric columns still records WHO asked WHAT through WHICH embed.
    # The tiers are NOT collapsed: each one names fewer columns than the last, so
    # a schema drift costs detail instead of costing the turn.
    try:
        return _id(await q(
            """INSERT INTO chat_logs
                   (lang, store_id, question, answer, cached, latency_ms,
                    embed_id, session_id, model, tools, path)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
               RETURNING id""",
            lang, store_id, question, answer, cached, latency_ms,
            embed_id or None, session_id or None, model or None,
            tools_json, path or None,
        ))
    except Exception:
        pass
    # Last resort: the pre-audit column set. On a database where none of the
    # newer columns exist the turn is still recorded, just unattributed.
    try:
        return _id(await q(
            """INSERT INTO chat_logs (lang, store_id, question, answer, cached, latency_ms)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
            lang, store_id, question, answer, cached, latency_ms,
        ))
    except Exception:
        pass
    # All three tiers failed. The caller gets None and answers the user anyway.
    return None


# ---- self-learning audit + chat feedback -----------------------------------


class FeedbackIn(BaseModel):
    session_id: str = ""
    store_id: Optional[str] = None
    model: str = ""
    question: str = ""
    answer: str = ""
    tools: List[str] = []
    verdict: str = "up"  # 'up' | 'down'
    correction: str = ""
    # The turn being rated. Optional so every existing caller keeps working; a
    # rating that arrives without it is stored unattributed rather than refused,
    # and is then invisible to `?rated=` — which is the truth about it.
    turn_id: Optional[int] = None


async def ensure_feedback() -> None:
    """Create/extend the chat_feedback table if missing (non-destructive).

    ``turn_id`` is the real foreign key to the rated turn (contract §5, amended).
    Before it, the only way to connect a rating to a turn was to match the
    question and answer TEXT that ``chat_feedback`` stores its own copies of —
    which guesses (two turns with the same words become one rating) and which
    produced the ``?rated=down`` bug that returned the whole table.

    It is added with ``ALTER … ADD COLUMN IF NOT EXISTS`` rather than being put
    in the ``CREATE TABLE`` literal, for the reason ``ensure_chat_logs``
    documents at length: the create is a no-op on every database that already has
    the table, so a column added only to the literal would never appear on any
    deployed system. That trap has now bitten this file three times.

    Existing rows keep ``NULL`` and are matched by nothing. They are genuinely
    unattributable, and the contract is explicit that they must not be guessed at.
    """

    await q(
        """CREATE TABLE IF NOT EXISTS chat_feedback (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               session_id TEXT, store_id TEXT, model TEXT,
               question TEXT, answer TEXT, tools JSONB,
               verdict TEXT, correction TEXT
           )"""
    )
    await execute(
        "ALTER TABLE chat_feedback ADD COLUMN IF NOT EXISTS turn_id BIGINT"
        " REFERENCES chat_logs(id) ON DELETE CASCADE"
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_feedback_turn"
        " ON chat_feedback (turn_id)"
    )


@router.post("/feedback")
async def post_feedback(fb: FeedbackIn) -> Dict:
    """Capture a thumbs up/down (and optional correction) on one answer."""

    verdict = fb.verdict if fb.verdict in ("up", "down") else "up"
    try:
        rows = await q(
            """INSERT INTO chat_feedback
                   (session_id, store_id, model, question, answer, tools,
                    verdict, correction, turn_id)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9) RETURNING id""",
            fb.session_id, fb.store_id, fb.model, fb.question, fb.answer,
            json.dumps(fb.tools or []), verdict, fb.correction, fb.turn_id,
        )
        return {"ok": True, "id": rows[0]["id"] if rows else None}
    except Exception:  # noqa: BLE001
        pass
    # Pre-`turn_id` schema: still record the rating rather than losing it. A
    # rating is a human taking the trouble to tell us something; dropping it
    # because a column is missing is the wrong trade, and the row is simply
    # unattributed, which the analytics layer already knows how to say.
    try:
        rows = await q(
            """INSERT INTO chat_feedback
                   (session_id, store_id, model, question, answer, tools,
                    verdict, correction)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8) RETURNING id""",
            fb.session_id, fb.store_id, fb.model, fb.question, fb.answer,
            json.dumps(fb.tools or []), verdict, fb.correction,
        )
        return {"ok": True, "id": rows[0]["id"] if rows else None}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="could not save feedback")


@router.get("/feedback")
async def get_feedback(limit: int = 100, verdict: str = "") -> List[Dict]:
    """Recent feedback rows, newest first; optional verdict filter."""

    limit = min(max(limit, 1), 500)
    conds, params = [], []
    if verdict in ("up", "down"):
        params.append(verdict)
        conds.append(f"verdict = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    try:
        return await q(
            f"""SELECT id, ts, verdict, model, question, answer, correction, store_id, session_id
                  FROM chat_feedback {where} ORDER BY id DESC LIMIT ${len(params)}""",
            *params,
        )
    except Exception:  # noqa: BLE001 - table not created yet
        return []


@router.get("/feedback/stats")
async def feedback_stats() -> Dict:
    """Up / down / correction counts."""

    try:
        rows = await q("SELECT verdict, count(*) c FROM chat_feedback GROUP BY verdict")
        up = sum(int(r["c"]) for r in rows if r["verdict"] == "up")
        down = sum(int(r["c"]) for r in rows if r["verdict"] == "down")
        corr = await q(
            "SELECT count(*) c FROM chat_feedback WHERE correction IS NOT NULL AND correction <> ''"
        )
        return {"up": up, "down": down, "corrections": int(corr[0]["c"]) if corr else 0, "total": up + down}
    except Exception:  # noqa: BLE001
        return {"up": 0, "down": 0, "corrections": 0, "total": 0}


# Epoch (s / ms / µs) -> timestamp, tolerant of whichever unit Agno wrote.
_AGNO_TS = (
    "to_char(to_timestamp(CASE "
    "WHEN COALESCE(updated_at, created_at) > 1000000000000000 THEN COALESCE(updated_at, created_at)/1000000.0 "
    "WHEN COALESCE(updated_at, created_at) > 1000000000000 THEN COALESCE(updated_at, created_at)/1000.0 "
    "ELSE COALESCE(updated_at, created_at)::float8 END), 'YYYY-MM-DD HH24:MI')"
)


@router.get("/learning")
async def list_learning(limit: int = 200) -> List[Dict]:
    """What the agent has learned (Agno's agno_learnings store), newest first.

    ``summary`` pulls the most human field from the JSON content with fallbacks.
    """

    limit = min(max(limit, 1), 500)
    try:
        return await q(
            f"""SELECT learning_id AS id, learning_type, user_id,
                       COALESCE(content->>'summary', content->>'context',
                                content->'memories'->0->>'content',
                                content->>'memory', content->>'content',
                                content->>'text', left(content::text, 300)) AS summary,
                       {_AGNO_TS} AS updated_at
                  FROM agno_learnings
                 ORDER BY COALESCE(updated_at, created_at) DESC
                 LIMIT $1""",
            limit,
        )
    except Exception:  # noqa: BLE001 - learning disabled / table absent
        return []


@router.get("/learning/stats")
async def learning_stats() -> Dict:
    """Memory counts by learning type + distinct users learned about."""

    try:
        by = await q("SELECT learning_type, count(*) c FROM agno_learnings GROUP BY learning_type")
        by_type = {r["learning_type"]: int(r["c"]) for r in by}
        users = await q("SELECT count(DISTINCT user_id) c FROM agno_learnings WHERE user_id IS NOT NULL")
        return {"total": sum(by_type.values()), "users": int(users[0]["c"]) if users else 0, "by_type": by_type}
    except Exception:  # noqa: BLE001
        return {"total": 0, "users": 0, "by_type": {}}


@router.delete("/learning/{learning_id}")
async def delete_learning(learning_id: str) -> Dict:
    """Forget one learned memory."""

    try:
        await q("DELETE FROM agno_learnings WHERE learning_id = $1", learning_id)
        return {"ok": True}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="could not delete")


# ---- MySQL source sync (pull client app DB -> our Postgres) ----------------


@router.get("/sync/mysql/config")
async def sync_config() -> Dict:
    """Show the MySQL sync configuration (password redacted)."""

    s = get_settings()
    return {
        "enabled": s.mysql_sync_enabled,
        "host": s.mysql_host,
        "port": s.mysql_port,
        "db": s.mysql_db,
        "user": s.mysql_user,
        "password_set": bool(s.mysql_password),
        "catalog_sql": s.mysql_catalog_sql,
        "inventory_sql": s.mysql_inventory_sql,
    }


@router.post("/sync/mysql")
async def run_sync(pipeline: bool = True) -> Dict:
    """Pull catalog + inventory from the client's MySQL into Postgres.

    Read-only on the client side. ``pipeline=false`` skips the embed/graph
    rebuild (faster; just refresh the rows).
    """

    from app.cache import bump_data_version
    from app.sync_mysql import sync_mysql

    res = await sync_mysql(run_pipeline=pipeline)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "sync failed"))

    # This path rewrites inventory. Every other writer (the SFTP watcher, the
    # reload endpoint) bumps the data version; this one did not, so cached stock
    # answers survived a sync and were served for up to CACHE_TTL_SECONDS.
    res["data_version"] = await bump_data_version()
    return res


# ---- white-label branding ---------------------------------------------------
#
# Every endpoint here is **super_admin only**. Branding is not a per-branch
# preference: it renames the product on the login screen every user of this
# deployment sees, so it sits with the other tenant-wide switches (CORS, the
# security config, the ingest config) rather than with the per-store data a
# plain admin manages.
#
# ⚠️ The upload path decides an image's type by MAGIC BYTES only. There is no
# filename check and no trust in the browser's `Content-Type` — both are chosen
# by whoever is uploading. `logo.png`, declared `image/png`, containing
# `<svg onload=fetch(...)>` is the actual attack: these files are served from
# `GET /brand/asset/{key}` on the SAME ORIGIN as the admin console, whose bearer
# token lives in `localStorage`, so storing one is stored XSS against a
# super_admin. See `app/brand.py::validate_image`.
#
# ⚠️ **Nothing re-encodes the uploaded bytes.** Pillow is not in
# `requirements.txt` and this feature is not a good enough reason to add an
# image codec — plus its C decoders are exactly the attack surface a branding
# upload should not introduce. What we serve is byte-identical to what was
# uploaded, so `validate_image` compensates with a structural check that the
# file ENDS where the format says it does (PNG chunk walk to IEND, JPEG EOI),
# refusing an appended payload that every decoder would silently ignore. EXIF is
# consequently preserved, not stripped: a JPEG logo may still carry the camera
# and GPS tags of whoever produced it.


@router.get("/branding", dependencies=[Depends(require_super_admin)])
async def get_branding() -> Dict:
    """The full branding document + what the editor needs to render controls.

    Adds per-asset metadata and, per text field, whether it is a stored override
    or the shipped default, which is the difference between showing a revert
    control and not.
    """

    from app import brand

    return await brand.admin_document()


@router.put("/branding", dependencies=[Depends(require_super_admin)])
async def put_branding(updates: Dict = Body(...)) -> Dict:
    """Partial text update. Send only the fields that changed.

    Unknown keys are a 400 rather than being ignored: a silently dropped field
    reports success and changes nothing, which is the failure mode
    ``set_ingest_config`` and ``PUT /admin/security-config`` both exist to avoid.
    """

    from app import brand

    try:
        clean = brand.validate_updates(updates)
    except brand.BrandError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await brand.apply_updates(clean)
    return await brand.admin_document()


@router.post("/branding/asset/{key}", dependencies=[Depends(require_super_admin)])
async def put_branding_asset(key: str, file: UploadFile = File(...)) -> Dict:
    """Upload one logo. PNG or JPEG, ≤1 MB, ≤1024px a side.

    The body is read through a capped reader rather than ``file.read()`` so an
    oversized upload is abandoned mid-stream instead of being materialised and
    then rejected, and the type/dimension checks run on the header bytes before
    anything decodes a pixel.
    """

    from app import brand

    if key not in brand.ASSET_KEYS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown asset {key!r}; expected one of "
                   f"{', '.join(brand.ASSET_KEYS)}",
        )
    try:
        payload = await brand.read_capped(file)
        image = brand.validate_image(payload)
    except brand.BrandTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except brand.BrandError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await brand.store_asset(key, image)
    return await brand.admin_document()


@router.delete("/branding/asset/{key}", dependencies=[Depends(require_super_admin)])
async def delete_branding_asset(key: str) -> Dict:
    """Remove one logo; the UI falls back to the icon + the product name."""

    from app import brand

    if key not in brand.ASSET_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown asset {key!r}")
    await brand.delete_asset(key)
    return await brand.admin_document()


@router.post("/branding/reset", dependencies=[Depends(require_super_admin)])
async def reset_branding() -> Dict:
    """Drop every override and every asset — back to the shipped look."""

    from app import brand

    await brand.reset_all()
    return await brand.admin_document()


__all__ = [
    "router",
    "ensure_chat_logs",
    "ensure_feedback",
    "ensure_admin_schema",
    "caller_store_scope",
    "log_chat",
]
