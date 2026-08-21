"""The branch registry: which branches EXIST, and which ones customers may see.

Before this module a branch existed only because it had rows in ``inventory``,
and ``inventory`` is truncate-and-reload from the daily balance export
(``ingest.ingest_inventory``). So a branch dropped from one file — a till that
did not sync, a partial export, a rename — silently vanished from the console,
the embed outlet picker and every chat answer, and reappeared the next morning
with nobody the wiser. There was also no way for an admin to hide a branch.

The split this module introduces:

* **existence** comes from ``stores``, which ACCUMULATES and never auto-deletes;
* **stock** still comes from ``inventory``, still truncate-and-reload;
* ``status='disabled'`` means *pretend this branch does not exist* — absent from
  chat answers, absent from lists, and absent from company-wide totals. Not
  "closed", not "temporarily unavailable": absent;
* a branch **missing from the latest file stays ACTIVE** and is only flagged
  (``missing_since``). A broken export must never be able to hide a live branch,
  so nothing in this file writes ``status`` except :func:`set_status`, which only
  an admin can reach.

⚠️ **THE FAIL-DANGEROUS DIRECTION IS AN EMPTY REGISTRY, NOT A STALE ONE.**

The obvious way to filter customer-visible stock is
``JOIN stores s ON s.site_code = i.site_code AND s.status = 'active'``. That
returns NOTHING when ``stores`` is empty, so a registry that failed to seed — a
fresh database, a boot where ``ensure_stores_table`` raised, a migration nobody
ran — makes the whole product answer "we have no stock anywhere". That is worse
than the bug being fixed, and it is silent: every query still succeeds.

Three things here rule it out, and they are independent on purpose:

1. :func:`ensure_stores_table` seeds every ``inventory.site_code`` as active at
   boot, and :func:`sync_from_file` inserts unknown codes on every load. An empty
   registry beside a populated inventory should not be reachable.
2. :func:`active_codes` and :func:`list_stores` are written so that **absence
   from the registry never hides a branch**. Only an explicit
   ``status='disabled'`` row excludes anything; an unregistered branch reads as
   visible. An empty registry therefore hides nothing at all.
3. :func:`not_disabled_clause` exists so callers filtering stock in SQL express
   the same rule — ``NOT EXISTS (… AND status='disabled')`` rather than a join on
   active. **Use it. Do not write the join.**

The registry is small (one row per branch — 53 today), so nothing here is
optimised for size; it is optimised for being impossible to read the wrong way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from app.db import execute, get_pool, q

logger = logging.getLogger("pharmacy.stores")

ACTIVE = "active"
DISABLED = "disabled"

# The only two values `status` may hold. Mirrored by the CHECK constraint in
# migrations/0011_stores.sql — the database is the enforcement, this is the
# error message.
STATUSES = frozenset({ACTIVE, DISABLED})


def not_disabled_clause(col: str) -> str:
    """SQL predicate: rows whose site code is not an explicitly DISABLED branch.

    ``col`` is the site-code column of the table being filtered and **must be
    table-qualified**: ``not_disabled_clause("i.site_code")``, never
    ``"site_code"``.

    ⚠️ **This is the shape every customer-facing filter must use.** The
    alternative — joining ``stores`` and requiring ``status='active'`` — reads
    identically on a populated database and empties the whole product on an empty
    registry, because a join against no rows returns no rows. Written as a
    negative, an empty registry hides nothing: the only way to lose a branch is
    for somebody to have disabled it on purpose.

    ⚠️⚠️ **AN UNQUALIFIED NAME IS THE SAME CATASTROPHE THROUGH A DIFFERENT
    DOOR, so it raises.** ``stores`` has a ``site_code`` column of its own, so a
    bare ``site_code`` in the subquery resolves to the INNER scope and the
    predicate silently becomes ``_s.site_code = _s.site_code`` — true for every
    registry row. ``NOT EXISTS`` then goes false for EVERY branch the moment any
    single branch is disabled, and the product answers "no stock anywhere".
    Postgres reports nothing: the query is valid and the correlation was simply
    never made. Found by agent D, running its SQL rather than reading it.

    The caller is NOT quietly qualified for it. A bare name means the caller's
    query has a bug about which table it is filtering, and that is worth learning
    at the call site rather than having this function guess a prefix.

    ⚠️ ``col`` is interpolated straight into SQL. This is an internal API whose
    argument is a **column identifier chosen by the calling code** — a literal
    written by a developer. It must NEVER be anything derived from user input, a
    request parameter or a database value; there is no escaping here and none is
    possible for an identifier used this way. Site VALUES are bound as ``$n``
    everywhere else in this module, and that is the only way a value may reach a
    query.

    Takes no bind parameter, so it composes into any query without disturbing
    ``$n`` numbering.

    Raises:
        ValueError: if ``col`` is empty or not table-qualified.
    """

    if not col or "." not in col:
        raise ValueError(
            f"not_disabled_clause needs a table-qualified column such as "
            f"'i.site_code', not {col!r} — an unqualified name binds to "
            f"stores.site_code inside the subquery and hides EVERY branch as "
            f"soon as one is disabled"
        )

    return (
        f"NOT EXISTS (SELECT 1 FROM stores _s "
        f"WHERE _s.site_code = {col} AND _s.status = '{DISABLED}')"
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def ensure_stores_table() -> None:
    """Create ``stores`` and seed it from ``inventory``. Idempotent; from lifespan.

    Kept in step with ``migrations/0011_stores.sql`` — this codebase applies
    schema at BOOT, so the .sql file is documentation for a hand-migrated
    database and this is the code that actually reaches a running one.

    **The seed is the safety net, not a convenience.** Every branch that already
    has stock is inserted as ACTIVE, so the registry is never empty on a database
    that has data, and switching the product over to registry-driven visibility
    cannot make a single existing branch disappear. It runs on every boot and
    only ever INSERTs codes it does not already hold, so a branch an admin
    disabled yesterday is not quietly re-enabled by tomorrow's restart.

    ⚠️ **THE SEED WRITES ``first_seen = NULL``, AND THAT IS THE POINT OF THE
    COLUMN.** A branch that predates the registry has an UNKNOWN first_seen, not
    today's. Stamping ``now()`` on it is the registry claiming a fact it does not
    have, and the claim is not harmless: the console reads "New" from a
    ``first_seen`` inside the last 7 days, so a seed that stamps today makes an
    estate of long-standing branches present as brand new for a week, with the
    Active filter returning an empty table on a fresh install. Found by E, by
    looking at the rendered page — every one of us read this SQL and saw nothing
    wrong with it.

    ``sync_from_file`` still stamps a real ``now()`` for a code the registry has
    genuinely never seen, via the column DEFAULT. That distinction — *we watched
    this branch appear* versus *this branch was here before we started counting*
    — is the entire value of the column, so do not collapse the two.

    ``last_seen_in_file`` is left NULL by the seed for the same reason: those rows
    are inferred from stock that is already in the table, and we do not know which
    file put it there. ``missing_since`` stays NULL too — a seeded branch is not
    missing, it is unobserved.

    Deliberately NOT wrapped — the lifespan block that calls it already logs and
    continues, exactly like every other ``ensure_*`` there.
    """

    await execute(
        """
        CREATE TABLE IF NOT EXISTS stores (
            site_code          TEXT PRIMARY KEY,
            site_name          TEXT,
            status             TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active','disabled')),
            -- NULLABLE, with the DEFAULT retained: NULL = "predates the
            -- registry, we do not know". A genuinely new branch inserted by
            -- sync_from_file takes the default and gets a real date.
            first_seen         TIMESTAMPTZ DEFAULT now(),
            last_seen_in_file  TIMESTAMPTZ,
            missing_since      TIMESTAMPTZ,
            disabled_by        TEXT,
            disabled_at        TIMESTAMPTZ,
            note               TEXT
        )
        """
    )
    # For a database that already ran the first cut of 0011, where the column was
    # NOT NULL. Idempotent, and a no-op on a table created above.
    await execute("ALTER TABLE stores ALTER COLUMN first_seen DROP NOT NULL")

    # The seed reads `inventory`, which does not exist on a database that has
    # never had `schema.sql` applied. Checking is cheaper than letting the INSERT
    # raise: an undefined-table error would abort this function AFTER the CREATE
    # and take the rest of the lifespan's schema block down with it, on exactly
    # the fresh install this is meant to serve.
    present = await q("SELECT to_regclass('public.inventory') AS reg")
    if not present or present[0]["reg"] is None:
        logger.info("stores: no inventory table yet, registry created empty")
        return

    seeded = await execute(
        """
        INSERT INTO stores (site_code, site_name, first_seen)
        -- first_seen NULL, explicitly, overriding the column DEFAULT: we did not
        -- watch this branch appear, we found it already stocked. See the warning
        -- in the docstring — a date here reads as "new" on the console.
        SELECT i.site_code, MAX(i.site_name), NULL
          FROM inventory i
         WHERE NOT EXISTS (
                   SELECT 1 FROM stores s WHERE s.site_code = i.site_code
               )
         GROUP BY i.site_code
        ON CONFLICT (site_code) DO NOTHING
        """
    )
    if seeded and not seeded.endswith(" 0"):
        logger.info("stores: seeded from inventory (%s)", seeded)


# ---------------------------------------------------------------------------
# The write path: what the daily file tells us
# ---------------------------------------------------------------------------


async def sync_from_file(codes: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Record what the inventory file just loaded said about branches.

    ``codes`` maps ``site_code -> site_name`` for every branch in the file.

    Inserts codes the registry has never seen as ACTIVE, refreshes ``site_name``
    and ``last_seen_in_file``, and clears ``missing_since`` for everything
    present. Known codes ABSENT from the file get ``missing_since = now()``, and
    only when it is currently NULL — the flag records when a branch first went
    missing, not when it was last noticed missing.

    **Never deletes a row and never touches ``status``.** A branch that stopped
    appearing in the export stays visible to customers and is merely flagged for
    an admin to look at. The opposite policy — auto-disable — hands a broken
    export the power to take a live pharmacy off the map, which is the failure
    this whole registry exists to prevent.

    Returns ``{"new": [codes...], "seen": int, "missing": [codes...]}``.

    ⚠️ **An EMPTY mapping is a no-op, not "every branch is missing".** A file
    that parsed to zero rows means the parse failed or the upload was partial; it
    does not mean the company closed. ``ingest.ingest_inventory`` already refuses
    to TRUNCATE on a zero-row parse for the same reason, and the guard lives here
    as well because it is this function that would otherwise stamp all 53
    branches missing in one statement.
    """

    if not codes:
        logger.warning(
            "stores: sync_from_file called with no codes; leaving the registry "
            "untouched (empty/partial file guard)"
        )
        return {"new": [], "seen": 0, "missing": []}

    # Built in one pass so the two arrays cannot drift out of alignment — they
    # are zipped by position inside the INSERT.
    site_codes: List[str] = []
    names: List[Optional[str]] = []
    for code, name in codes.items():
        if code is None or not str(code).strip():
            continue
        site_codes.append(str(code).strip())
        names.append((str(name).strip() or None) if name is not None else None)
    if not site_codes:
        return {"new": [], "seen": 0, "missing": []}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # One transaction: the insert/refresh and the missing-flag are two halves
        # of one statement about the same file. Half-applied, the registry would
        # claim a branch was seen in a file that also flagged it missing.
        async with conn.transaction():
            existing = {
                r["site_code"]
                for r in await conn.fetch(
                    "SELECT site_code FROM stores WHERE site_code = ANY($1::text[])",
                    site_codes,
                )
            }
            new = [c for c in site_codes if c not in existing]

            await conn.execute(
                """
                INSERT INTO stores (site_code, site_name, last_seen_in_file)
                SELECT code, name, now()
                  FROM unnest($1::text[], $2::text[]) AS t(code, name)
                ON CONFLICT (site_code) DO UPDATE SET
                    -- COALESCE, not EXCLUDED: a file that omits the branch name
                    -- column must not erase a name we already have. A NULL here
                    -- means "the file said nothing", never "the name is gone".
                    site_name         = COALESCE(EXCLUDED.site_name, stores.site_name),
                    last_seen_in_file = EXCLUDED.last_seen_in_file,
                    -- Present in this file => not missing. This is the ONLY place
                    -- the flag is cleared, and `status` is untouched throughout.
                    missing_since     = NULL
                """,
                site_codes,
                names,
            )

            missing = [
                r["site_code"]
                for r in await conn.fetch(
                    """
                    UPDATE stores
                       SET missing_since = now()
                     WHERE site_code <> ALL($1::text[])
                       AND missing_since IS NULL
                    RETURNING site_code
                    """,
                    site_codes,
                )
            ]

    if new or missing:
        logger.info(
            "stores: file carried %d branch(es); %d new %s, %d newly missing %s",
            len(site_codes), len(new), new, len(missing), missing,
        )
    return {"new": new, "seen": len(site_codes), "missing": missing}


# ---------------------------------------------------------------------------
# The read path
# ---------------------------------------------------------------------------


async def list_stores(scope: Optional[str] = None) -> List[Dict]:
    """Every branch the registry knows, with its stock aggregates. Admin-facing.

    Returns EVERY registry row — disabled ones included, and ones with no stock
    rows at all, whose ``skus``/``units``/``value`` are ``0``. This is the admin's
    view of the estate, so a branch that is hidden from customers or that has
    dropped out of the file is precisely what the reader came to see; filtering
    it out here would hide the only screen that can put it back.

    ``scope``, when given, limits the result to that one branch. It is the
    caller's enforced store pin, so it is matched with ``tools._site_clause`` —
    the same anchored full-code / numeric-prefix / alpha-suffix matcher every
    other scoped query uses. **Never substring-match a site token**; a
    prefix-shaped scope like ``200`` once matched every sibling branch.

    ⚠️ **The join is a FULL OUTER JOIN, and that is the empty-registry defence.**
    A branch with stock but no registry row still appears, reading ``active``,
    because a row missing from the registry is an unregistered branch and not a
    hidden one. So this function cannot answer "no branches" while ``inventory``
    holds rows, whatever state the registry is in.
    """

    # ⚠️ KEEP THIS IMPORT FUNCTION-LOCAL — EVEN IF THE OTHER SIDE CURRENTLY
    # LOOKS SAFE TO MOVE. `app.stores` and `app.tools` IMPORT EACH OTHER:
    #
    #   app.stores  -> app.tools._site_clause            (this line, matching a
    #                                                     caller's store pin with
    #                                                     the anchored site-token
    #                                                     rules every scoped query
    #                                                     uses)
    #   app.tools   -> app.stores.not_disabled_clause    (in `_visible_site_clause`)
    #
    # The cycle is real. Measured, all four arrangements x both import orders:
    # moving ONE of the two to module scope still imports fine — it is only when
    # BOTH are at module scope that Python raises
    #
    #   ImportError: cannot import name '_site_clause' from partially
    #   initialized module 'app.tools' (most likely due to a circular import)
    #
    # and the process refuses to START. (That is the form seen here, because
    # every module-scope import in this codebase reaches `app.tools` first —
    # api.py, admin.py and agent.py all import it at the top and nothing imports
    # `app.stores` at module scope. Entered the other way round the error is the
    # mirror image, naming `not_disabled_clause` and `app.stores`; the symbol it
    # blames is whichever module Python happened to enter first, never the one
    # actually at fault.) That is what makes this dangerous rather
    # than merely untidy, and it is why the rule is per-side and unconditional:
    # whoever lifts the FIRST one to module scope sees everything work, and can
    # reasonably conclude the warning was stale — while having armed the trap for
    # whoever later does the same on the other side, who then gets a boot failure
    # that has nothing to do with their own change. Neither of them can see it
    # from the file in front of them. `app/tools.py` carries the mirror of this
    # note; if you are here to tidy the imports, the answer is no.
    from app.tools import _site_clause

    # $1 is always the default status, so the optional scope can take $2 without
    # renumbering anything.
    args: List[Any] = [ACTIVE]
    where = ""
    if scope:
        args.append(scope)
        # Applied to the coalesced code so the predicate reaches both sides of
        # the outer join — a registry-only row and an inventory-only row are both
        # legitimately in scope.
        where = " WHERE " + _site_clause("COALESCE(s.site_code, inv.site_code)", "$2")

    rows = await q(
        """
        WITH inv AS (
            SELECT site_code,
                   COUNT(*)                          AS skus,
                   SUM(stock_qty)                    AS units,
                   ROUND(SUM(price * stock_qty))     AS value,
                   MAX(site_name)                    AS site_name
              FROM inventory
             GROUP BY site_code
        )
        SELECT COALESCE(s.site_code, inv.site_code)  AS site_code,
               COALESCE(s.site_name, inv.site_name)  AS site_name,
               COALESCE(s.status, $1)                AS status,
               s.first_seen,
               s.last_seen_in_file,
               s.missing_since,
               s.disabled_by,
               s.disabled_at,
               s.note,
               COALESCE(inv.skus,  0)                AS skus,
               COALESCE(inv.units, 0)                AS units,
               COALESCE(inv.value, 0)                AS value
          FROM stores s
          FULL OUTER JOIN inv ON inv.site_code = s.site_code
        """
        + where
        + " ORDER BY value DESC, site_code",
        *args,
    )
    # asyncpg hands back Decimal for NUMERIC and for SUM(int). The admin console
    # has always received plain ints/floats here and still does — the page keeps
    # working through the migration.
    for r in rows:
        r["skus"] = int(r["skus"] or 0)
        r["units"] = int(r["units"] or 0)
        r["value"] = float(r["value"] or 0)
    return rows


async def active_codes() -> Set[str]:
    """The site codes customers may be shown. Disabled branches are absent.

    ⚠️ Built as *everything the data knows about, minus what is explicitly
    disabled* — never as *what the registry lists as active*. The difference only
    shows when the registry is incomplete, and then it is the difference between
    hiding one branch somebody disabled and hiding the entire company. A branch
    with stock and no registry row is an unregistered branch, and an unregistered
    branch is visible.

    Callers filtering in SQL should use :func:`not_disabled_clause` instead of
    passing this set back into a query — same rule, one round trip fewer, and no
    window where the set is stale.
    """

    rows = await q(
        """
        SELECT site_code FROM stores  WHERE status <> $1
        UNION
        SELECT DISTINCT i.site_code FROM inventory i
         WHERE NOT EXISTS (
                   SELECT 1 FROM stores s
                    WHERE s.site_code = i.site_code AND s.status = $1
               )
        """,
        DISABLED,
    )
    return {r["site_code"] for r in rows}


# ---------------------------------------------------------------------------
# The admin write path
# ---------------------------------------------------------------------------


async def set_status(
    site_code: str,
    status: str,
    *,
    actor_email: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict:
    """Show or hide one branch. The only thing in this module that writes ``status``.

    Raises ``KeyError`` for an unknown ``site_code`` and ``ValueError`` for a
    status other than ``active``/``disabled``. Both are raised rather than
    returned so the HTTP layer can map them to 404 and 400 without inspecting a
    result shape — and so a typo can never be stored as a third status that no
    filter in the codebase matches.

    ``disabled_by``/``disabled_at`` are stamped on the way down and **cleared on
    the way back up**, because they answer "who hid this branch, and when" about
    the CURRENT state. Left behind on a re-enabled branch they read as a live
    fact and are a stale one. ``note`` follows the same rule: it explains the
    change being made now, so re-enabling without one clears the reason the
    branch was hidden rather than leaving "branch closed" attached to a branch
    that is open.

    Exact-match on ``site_code`` on purpose — no ``_site_clause`` here. That
    matcher exists to interpret a loosely-typed scope token; this is an
    administrative write against a primary key, and a prefix that matched two
    branches would disable the wrong pharmacy.
    """

    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, not {status!r}")
    code = (site_code or "").strip()
    if not code:
        raise KeyError(site_code)

    disabling = status == DISABLED
    rows = await q(
        """
        UPDATE stores
           SET status      = $2,
               disabled_by = CASE WHEN $3 THEN $4 ELSE NULL END,
               disabled_at = CASE WHEN $3 THEN now() ELSE NULL END,
               note        = $5
         WHERE site_code = $1
        RETURNING site_code, site_name, status, first_seen, last_seen_in_file,
                  missing_since, disabled_by, disabled_at, note
        """,
        code,
        status,
        disabling,
        (actor_email or "").strip().lower() or None,
        (note or "").strip() or None,
    )
    if not rows:
        # Unknown branch. NOT created here: a registry row is a fact about the
        # estate, learned from a file or from the seed, and inventing one from a
        # URL path segment would let a typo add a phantom branch to the console.
        raise KeyError(code)
    return rows[0]


# ---------------------------------------------------------------------------
# The detail panel: everything the console knows about ONE branch
# ---------------------------------------------------------------------------
#
# Four tables answer four different questions about a branch and nothing joins
# them meaningfully — `inventory` is keyed by article, `chat_logs` by turn,
# `app_events` by request. They are read as separate statements on purpose. One
# giant query would need three lateral subselects and a json_agg per section to
# keep the row grain straight, and this is a panel a human opens for one branch,
# not a hot path: the reads are concurrent, so the wall clock is one round trip
# regardless, and each statement stays something you can paste into psql.

# Read more audit rows than we return, because a successful status change writes
# TWO of them (see `_dedupe_events`) and de-duplication happens after the fetch.
_EVENT_FETCH = 24
_EVENT_KEEP = 8

# Two audit rows are the same act when they land within this many seconds of
# each other AND agree on actor, HTTP status and the status/note they carry.
_EVENT_DUP_WINDOW_S = 5.0

# The action slug `activity.action_for` produces for POST /admin/stores/{code}/status.
# Summaries below are written for THIS action only; anything else falls back to
# a bare "<action> (<status>)", which is unlovely and invents nothing.
_STATUS_ACTION = "admin.stores.status.create"

# A status value echoed into a summary comes from a request body. It is already
# capped at 200 chars by `activity._redact`; capped again here because it lands
# in a sentence a human reads, not a log line.
_ECHO_MAX = 40


def _iso(value: Any) -> Optional[str]:
    """A timestamptz as ISO-8601, or None.

    Used only for the timestamps this module INVENTS (chat first/last, question
    and event times). The registry timestamps carried through from
    :func:`list_stores` are deliberately left as ``datetime`` — see the note in
    ``admin._store_row`` about two spellings of one instant. Python's
    ``isoformat()`` on a UTC-aware datetime and FastAPI's encoder both produce
    ``+00:00``, so the two halves of this dict still serialise identically.
    """

    return value.isoformat() if value is not None else None


def _detail_dict(raw: Any) -> Dict[str, Any]:
    """``app_events.detail`` as a dict. Anything unparseable reads as empty.

    asyncpg hands JSONB back as a string unless a codec is registered, and this
    module registers none. An unreadable detail must cost the row its adjectives,
    never the row itself — a summary with less in it is still a record of someone
    who tried.
    """

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _event_summary(
    action: str, status: Optional[int], detail: Dict[str, Any], *, with_note: bool
) -> str:
    """The sentence a pharmacy manager reads for one audit row.

    ⚠️ **``with_note`` is the audit trail's half of the field projection, and it
    is required for the same reason ``detail``'s ``privileged`` is.** The
    operator's free-text reason is stored on the EVENT as well as on the branch,
    so taking ``note`` off the row and leaving this function alone re-serves the
    identical sentence one section further down the same payload — measured:
    ``hid it — “licence suspended…”`` under a row with no ``note`` key at all.
    Worse than the field it was meant to hide, because the trail is durable:
    the row carries only the CURRENT reason, while the events carry every reason
    ever typed for that branch.

    ``False`` selects the note-free forms that already existed for the case where
    a row carries no note — "hid it", "switched it back on", "changed whether it
    is shown". Nothing else is suppressed: WHAT happened, WHETHER it took effect
    and the HTTP status are all still said, because a reader who may know a
    branch is hidden may know when and how it became hidden.

    ⚠️ **Every clause here has to be carried by the row itself.** The row knows
    the action, the HTTP status and the status/note that were sent; it does not
    know intent, and it does not know why a request was refused beyond what the
    route's own gate implies. So a 403 on the status route reads "not a super
    admin" — that route's only gate is ``require_super_admin`` — while a status
    this function does not recognise degrades to the numbers rather than
    guessing at a cause.

    **A refused attempt is not an error to be tidied away.** It is the record of
    someone who tried, which on a control that hides a pharmacy from customers is
    the most interesting row in the table. Refusals are phrased as attempts
    ("tried to hide it"), never as failures of the system.

    ⚠️⚠️ **"refused, not a super admin" IS ONLY TRUE WHILE
    ``require_super_admin`` IS THE ONLY GATE ON
    ``POST /admin/stores/{site_code}/status``.** That sentence does not read the
    reason out of the row — the row has no reason, only a 403 — it INFERS it from
    the fact that the route has exactly one way to refuse. Add a second gate to
    that endpoint (rate limiting, an approval check, a store-scope pin, a
    maintenance mode) and every 403 it produces still renders "not a super
    admin", now naming the wrong cause with total confidence, to the one reader
    least able to check it. Nothing fails, no test goes red, and the panel keeps
    looking right.

    If you are here because you just added a gate to that route: this sentence is
    now a guess. Either distinguish the cases at the point where the reason is
    known — the same rule ``activity.record_tool_call`` follows, classify where
    the reason exists rather than by string-matching after the fact — or degrade
    the 403 to "tried to hide it — refused", which is all a bare status code can
    honestly support.
    """

    if action != _STATUS_ACTION:
        # An action nobody has written a sentence for. The console renders this
        # verbatim, so it must still be true — hence the raw slug, not a guess
        # at what an unfamiliar route did.
        return f"{action} ({status})" if status is not None else action

    wanted = detail.get("status")
    wanted = str(wanted)[:_ECHO_MAX] if wanted is not None else None
    note = str(detail.get("note") or "").strip()[:_ECHO_MAX]

    if status == 200:
        if wanted == DISABLED:
            done = "hid it"
        elif wanted == ACTIVE:
            done = "switched it back on"
        else:
            # A 200 whose detail lost the status. Rare, and the honest reading is
            # "something about visibility changed", not a coin flip on which way.
            done = "changed whether it is shown"
        return f"{done} — “{note}”" if (note and with_note) else done

    # Everything below did NOT take effect.
    if wanted == DISABLED:
        tried = "tried to hide it"
    elif wanted == ACTIVE:
        tried = "tried to show it again"
    else:
        tried = "tried to change whether it is shown"

    if status == 400:
        # The body named a status the registry does not have. Echoing it is the
        # whole value of the row: "banana" tells the reader it was a typo or a
        # script, where "rejected" alone tells them nothing.
        return (
            f"sent an invalid status “{wanted}” — rejected"
            if wanted
            else "sent an invalid status — rejected"
        )
    if status == 401:
        return f"{tried} — refused, not signed in"
    if status == 403:
        return f"{tried} — refused, not a super admin"
    if status == 404:
        return f"{tried} — no such branch"
    if status is not None and status >= 500:
        return f"{tried} — the change failed"
    return f"{tried} — answered {status}" if status is not None else tried


def _dedupe_events(rows: List[Dict]) -> List[Dict]:
    """Collapse the TWO audit rows a successful status change writes into one.

    ⚠️ **This is not tidying; without it the panel reports every successful
    change twice.** ``POST /admin/stores/{code}/status`` is audited in two
    places, both deliberately: ``admin.store_set_status`` records an explicit row
    (so the branch and the new status survive), and the ``activity_audit``
    middleware records its own companion row for the same request (so a request
    that never reached the handler is still recorded). A refused attempt produces
    only the middleware row — which is exactly why the middleware row cannot
    simply be dropped, and why this is done here rather than by filtering the
    query.

    Two rows are one act when they agree on action, actor, HTTP status and the
    status/note they carry, within :data:`_EVENT_DUP_WINDOW_S`. All four have to
    match: two genuine attempts seconds apart differ in at least one of them —
    the pair of 403s on dev differs by ``note`` — and a hide immediately followed
    by a show differs by ``status``. The survivor is the row carrying
    ``site_code``, i.e. the handler's own row, because that field is the value
    actually written rather than the value asked for.

    ``rows`` arrives newest-first and the order is preserved.
    """

    kept: List[Dict] = []
    for row in rows:
        detail = _detail_dict(row.get("detail"))
        key = (
            row.get("action"),
            row.get("actor_email"),
            row.get("status"),
            detail.get("status"),
            (detail.get("note") or None),
        )
        ts = row.get("ts")
        twin = None
        for i, k in enumerate(kept):
            k_detail = _detail_dict(k.get("detail"))
            k_key = (
                k.get("action"),
                k.get("actor_email"),
                k.get("status"),
                k_detail.get("status"),
                (k_detail.get("note") or None),
            )
            if k_key != key:
                continue
            k_ts = k.get("ts")
            if ts is None or k_ts is None:
                continue
            if abs((k_ts - ts).total_seconds()) <= _EVENT_DUP_WINDOW_S:
                twin = i
                break
        if twin is None:
            kept.append(row)
        elif "site_code" in detail and "site_code" not in _detail_dict(kept[twin].get("detail")):
            kept[twin] = row
    return kept


#: Who hid a branch, when, and the reason they typed. See ``detail``'s
#: ``privileged`` argument and ``admin._STORE_PRIVILEGED_FIELDS`` — the two lists
#: are the same three fields and must stay that way.
_PRIVILEGED_FIELDS = ("disabled_by", "disabled_at", "note")


async def detail(site_code: str, *, privileged: bool) -> Dict:
    """Everything the console shows for one branch. Raises KeyError if unknown.

    Returns the keys a ``GET /admin/stores`` row already carries, plus the panel's
    own sections: ``rank``/``of_branches``/``pct_of_value``, ``negatives``,
    ``avg_price``, ``top_holdings``, ``chats``, ``questions``, ``events``.

    ⚠️ **``privileged`` is REQUIRED and keyword-only, and that is the whole
    point of it.** It gates ``_PRIVILEGED_FIELDS`` — ``disabled_by``,
    ``disabled_at`` and the operator's free-text ``note`` ("licence suspended",
    "pharmacist under review"), which arrive on ``base`` because this function
    builds its answer out of a :func:`list_stores` row.

    Today the only route here is ``require_super_admin``, so every caller passes
    ``True`` and nothing changes. It exists for the change the endpoint's own
    docstring names — relaxing that gate to ``require_admin`` so branch managers
    can open their own branch. ``admin._scope_permits`` guards which ROWS such a
    caller may read; it says nothing about COLUMNS, so that relaxation would
    republish the reason a pharmacy was hidden on a second surface, hours after
    it was taken off the first.

    **What ``privileged=False`` covers, exactly.** Two places, because the reason
    is stored twice:

    * the three fields above, popped off the branch row; and
    * the ``events`` trail — ``actor`` omitted, and ``_event_summary`` called
      with ``with_note=False`` so the sentence keeps its verb and loses the
      quotation.

    ⚠️ **The second one was NOT covered by the first, and the gap was invisible
    from the call site.** With only the fields projected, a payload carrying no
    ``note`` key still said ``hid it — “licence suspended…”`` in its events, and
    said it about EVERY hide in that branch's history rather than only the
    current one, because the trail is durable where the row is not. So the true
    statement about this argument is not "forgetting it is a ``TypeError``" — it
    is that forgetting is a ``TypeError`` **and passing ``False`` was itself a
    leak until the events block was projected too.** If you add a section to this
    function that reads ``base``, an ``app_events`` row or anything an operator
    typed, it is your job to decide what it says under ``False``; the argument
    does not do it for you, and nothing here will fail if you skip it.

    **``questions`` is deliberately NOT gated.** It is customer question text for
    one branch, and it is the plausible REASON someone relaxes the gate — a
    branch manager reading what their own customers ask is the feature, not a
    side effect. It is also already scoped by ``admin._scope_permits`` to the
    branch the caller may read, it names no operator, and :func:`detail` already
    refuses to carry the ANSWER (see the warning below) which is where the
    genuinely sensitive half of a conversation lives. Gating it would leave a
    relaxed caller with a panel that has no reason to exist.

    **Existence is defined exactly as the branch list defines it** — registry row
    OR stock — so a branch the console lists can always be opened, and a branch it
    does not list raises rather than returning an empty shell that reads as a real
    branch holding nothing. The match is exact, like :func:`set_status` and for
    the same reason: this identifies one pharmacy, and ``tools._site_clause``
    exists to interpret a loose scope token, not to pick a row.

    ⚠️ **TWO SITE MATCHERS RUN ON THIS ONE REQUEST AND THEY MUST STAY
    DIFFERENT.** ``GET /admin/stores/{code}/detail`` asks two questions. First
    *may this caller read this branch* — a question about a **store pin**, which
    ``admin._scope_permits`` answers through ``tools._site_clause``, because
    ``users.store_id`` legitimately holds ``20043-CCSJ``, ``20043`` or ``CCSJ``.
    Then *which branch is this* — a question of **identity**, answered here,
    exactly. Two different questions, so two different matchers, and the mismatch
    is conspicuous enough that somebody will eventually try to harmonise them.
    Both directions break something: matching the scope pin exactly locks out a
    manager whose pin is spelled as a prefix, and matching this lookup with
    ``_site_clause`` reintroduces the ambiguity where a prefix-shaped code opens
    whichever sibling branch happens to sort first. Raised by agent G from the
    other side of the seam; each half is also commented where it lives.

    **Rank and share are measured over the same rows the branch list shows**, so
    they agree with the table the panel opens from. That includes disabled
    branches. Excluding them would give a disabled branch a share of a total that
    omits its own stock, which is a number that cannot be checked against
    anything on screen.

    ``rank`` is competition ranking on stock VALUE: one plus the number of
    branches holding strictly more. Ties therefore share a rank, and — the case
    worth stating — **every branch with no stock at all ties for last**. A branch
    with no stock is not rank 53 because it is the worst; it is rank 53 because
    52 branches hold more than nothing. It is never rank 1 by default and never
    unranked.

    ⚠️ **``questions`` carries the QUESTION only, never the answer.** This is a
    management view of an estate, and an answer is written back at a customer in
    their own words. A branch manager needs to know what is being asked; nobody
    on this screen needs the reply.

    Empty is answered with zeroes and None, never a division: a branch with no
    stock returns ``0`` units and ``avg_price=None`` (no prices to average is not
    an average of zero), and a branch with no conversations returns a ``chats``
    block of zeroes rather than nulls the console would have to special-case.
    ``cost_usd`` sums only the turns whose cost the provider actually reported.
    """

    code = (site_code or "").strip()
    if not code:
        raise KeyError(site_code)

    # The estate first: it decides whether the branch exists at all, and it
    # carries the base row, the rank and the denominator of the share.
    estate = await list_stores()
    base = next((dict(r) for r in estate if r["site_code"] == code), None)
    if base is None:
        raise KeyError(code)

    # Projected HERE, on the row itself, before anything else reads it — not on
    # the way out. Everything below adds keys to `base`, so a filter applied at
    # the `return` would be one more place to keep in step with a growing dict.
    # Popped rather than nulled, for the reason `admin._store_row` records: a
    # null note is a real state (hidden without a reason), and sending one to
    # mean "you may not see this" would put "no reason given" on screen for a
    # branch that has one.
    if not privileged:
        for f in _PRIVILEGED_FIELDS:
            base.pop(f, None)

    value = float(base.get("value") or 0)
    total_value = sum(float(r.get("value") or 0) for r in estate)
    base["of_branches"] = len(estate)
    base["rank"] = 1 + sum(1 for r in estate if float(r.get("value") or 0) > value)
    base["pct_of_value"] = round(100.0 * value / total_value, 1) if total_value > 0 else 0.0

    # The four sections. Independent statements, run concurrently — `q` takes a
    # connection from the pool per call, so these do not share one.
    profile_rows, holding_rows, chat_rows, lang_rows, question_rows, event_rows = (
        await asyncio.gather(
            q(
                """
                SELECT COUNT(*) FILTER (WHERE stock_qty < 0) AS negatives,
                       ROUND(AVG(price), 2)                  AS avg_price
                  FROM inventory
                 WHERE site_code = $1
                """,
                code,
            ),
            q(
                """
                SELECT COALESCE(c.brand_name, i.article_code) AS product,
                       i.stock_qty                            AS qty,
                       i.price                                AS price
                  FROM inventory i
                  LEFT JOIN catalog c USING (article_code)
                 WHERE i.site_code = $1
                 -- NULLS LAST: stock_qty is nullable and NULL means UNKNOWN, so
                 -- an unknown quantity must not sort above a measured 2,311.
                 ORDER BY i.stock_qty DESC NULLS LAST
                 LIMIT 5
                """,
                code,
            ),
            q(
                """
                SELECT COUNT(*)                        AS chats,
                       COUNT(DISTINCT session_id)      AS visitors,
                       MIN(ts)                         AS first_ts,
                       MAX(ts)                         AS last_ts,
                       COUNT(*) FILTER (WHERE cached)  AS cached,
                       COUNT(*) FILTER (WHERE gave_up) AS gave_up,
                       ROUND(AVG(latency_ms))          AS avg_ms,
                       -- SUM ignores NULL, so this is the cost of the turns whose
                       -- price the provider reported. COALESCE only turns "no
                       -- turns at all" into 0.
                       COALESCE(SUM(cost_usd), 0)      AS cost_usd,
                       -- The DENOMINATOR of that sum, and it is not decoration.
                       -- OpenRouter returns no price on the completion response,
                       -- so `cost_usd` is NULL on a real share of turns — 5 of 19
                       -- for 20043-CCSJ on dev. Without this count the console
                       -- shows a partial total formatted exactly like a complete
                       -- one, which is the "$0.00 reads as free" failure the
                       -- honesty rules exist to stop. Same shape as
                       -- `admin.catalog_one`'s `unknown_site_count`.
                       COUNT(cost_usd)                 AS cost_known
                  FROM chat_logs
                 -- `store_id`, and the column is `ts` — chat_logs has no created_at.
                 WHERE store_id = $1
                """,
                code,
            ),
            q(
                """
                SELECT lang, COUNT(*) AS n
                  FROM chat_logs
                 WHERE store_id = $1 AND lang IS NOT NULL
                 GROUP BY lang
                 ORDER BY n DESC, lang
                """,
                code,
            ),
            q(
                """
                SELECT question, lang, cached, latency_ms, ts
                  FROM chat_logs
                 WHERE store_id = $1
                 ORDER BY ts DESC
                 LIMIT 5
                """,
                code,
            ),
            q(
                """
                SELECT action, actor_email, status, ts, detail
                  FROM app_events
                 WHERE target = $1
                 ORDER BY ts DESC
                 LIMIT $2
                """,
                code,
                _EVENT_FETCH,
            ),
        )
    )

    profile = profile_rows[0] if profile_rows else {}
    base["negatives"] = int(profile.get("negatives") or 0)
    # None, not 0.0: a branch with no rows has no average price. A 0 here would
    # read as "everything it stocks is free".
    avg_price = profile.get("avg_price")
    base["avg_price"] = float(avg_price) if avg_price is not None else None

    base["top_holdings"] = [
        {
            "product": r["product"],
            # qty stays None when the file said nothing — NULL is UNKNOWN, never 0.
            "qty": int(r["qty"]) if r["qty"] is not None else None,
            "price": float(r["price"]) if r["price"] is not None else None,
        }
        for r in holding_rows
    ]

    chat = chat_rows[0] if chat_rows else {}
    chat_count = int(chat.get("chats") or 0)
    avg_ms = chat.get("avg_ms")
    base["chats"] = {
        "count": chat_count,
        # COUNT(DISTINCT session_id) skips NULLs, so a branch whose turns carry no
        # session reads 0 visitors against a real chat count. That is the honest
        # figure: we did not observe a visitor, we observed a turn.
        "visitors": int(chat.get("visitors") or 0),
        "first": _iso(chat.get("first_ts")),
        "last": _iso(chat.get("last_ts")),
        "cached": int(chat.get("cached") or 0),
        "gave_up": int(chat.get("gave_up") or 0),
        # None rather than 0 on a branch with no chats — an average of nothing is
        # not zero milliseconds — but a real average is always an int.
        "avg_ms": int(avg_ms) if avg_ms is not None and chat_count else None,
        "cost_usd": float(chat.get("cost_usd") or 0),
        # How many of `count` turns that cost actually covers. Render the two
        # together ("$0.3220 of 14 priced"), never the total alone.
        "cost_known": int(chat.get("cost_known") or 0),
        # Not in the seam's original key list; the approved panel renders a
        # language breakdown ("EN 18 · MY 1") and nothing else can derive it.
        "langs": [{"lang": r["lang"], "count": int(r["n"])} for r in lang_rows],
    }

    base["questions"] = [
        {
            "text": r["question"],
            "lang": r["lang"],
            "cached": bool(r["cached"]),
            # int|None. A turn logged before latency capture has no wait to show,
            # and 0ms would read as instant — which is what `cached` means.
            "ms": int(r["latency_ms"]) if r["latency_ms"] is not None else None,
            "at": _iso(r["ts"]),
        }
        # NEVER `r["answer"]` — see the warning in the docstring.
        for r in question_rows
    ]

    # ⚠️ The trail is projected too, and this is not belt-and-braces — it was a
    # second copy of the same secret. `actor_email` is the operator's address and
    # `_event_summary` renders their note into its sentence, so a payload with
    # the three top-level fields removed still said who hid the branch and why,
    # about every hide in its history rather than only the current one.
    #
    # `actor` is DROPPED rather than nulled — same rule as the fields above: a
    # null actor is a real state (a row whose token could not be read), and
    # sending one to mean "you may not see this" would put an anonymous act on
    # screen where a named one belongs. `status` and `at` stay: a reader who may
    # know a branch is hidden may know when it happened and whether it took
    # effect. They just do not get the operator's name or the operator's words.
    base["events"] = [
        {
            "action": r["action"],
            **({"actor": r["actor_email"]} if privileged else {}),
            "status": int(r["status"]) if r["status"] is not None else None,
            "at": _iso(r["ts"]),
            "summary": _event_summary(
                r["action"], r["status"], _detail_dict(r.get("detail")),
                with_note=privileged,
            ),
        }
        for r in _dedupe_events([dict(r) for r in event_rows])[:_EVENT_KEEP]
    ]

    return base


__all__ = [
    "ACTIVE",
    "DISABLED",
    "STATUSES",
    "not_disabled_clause",
    "ensure_stores_table",
    "sync_from_file",
    "list_stores",
    "active_codes",
    "set_status",
    "detail",
]
