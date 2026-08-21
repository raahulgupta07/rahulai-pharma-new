"""Agent tools for the CitCare pharmacy domain.

These twelve async tools form the agent's capabilities. The docstrings here are
load-bearing — Agno surfaces them to the model as tool descriptions, so they
describe behaviour precisely.

All database access goes through ``app.db.q(sql, *args)``, an async helper that
returns ``List[Dict]`` using asyncpg positional placeholders ($1, $2, ...).
Numeric columns (price) arrive as ``Decimal`` from asyncpg and are converted to
``float`` here so the returned dicts are JSON-serializable.

The twelve tools:
    1.  get_article_info         - look up one article by its catalog code.
    2.  search_by_name           - fuzzy-search articles by (partial) product name.
    3.  get_stock                - current stock for an article, optionally per site.
    4.  top_by_stock             - top-N best-stocked articles at a site.
    5.  filter_by_price          - articles within a price range, optionally per site.
    6.  get_substitutes          - therapeutic / generic substitutes for an article.
    7.  summarize_article        - one combined info + stock + price summary.
    8.  search_by_meaning        - semantic (pgvector) search by need/symptom.
    9.  related_drugs            - knowledge-graph traversal for related products.
    10. drugs_for_same_condition - graph hop to products treating the same condition.
    11. find_at_other_stores     - which OTHER branches stock an article.
    12. list_sites               - resolve a named branch to a real site code.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from app.db import q

logger = logging.getLogger("pharmacy.tools")

# Per-request store scope. When set (from a signed session token), site-aware
# tools are forced to this store so the model cannot read another branch's data.
# Empty/None means unscoped (public mode) — the model's requested site applies.
_STORE_SCOPE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "store_scope", default=None
)


def set_store_scope(store_id: Optional[str]):
    """Bind the authenticated store for the current request/context.

    Returns the contextvars ``Token`` so callers can ``reset_store_scope`` it.
    """

    return _STORE_SCOPE.set(store_id or None)


def reset_store_scope(token) -> None:
    """Restore the previous store scope using a token from :func:`set_store_scope`."""

    _STORE_SCOPE.reset(token)


def get_store_scope() -> Optional[str]:
    """Return the store this context is locked to, or ``None`` if unscoped."""

    return _STORE_SCOPE.get()


def _effective_site(requested: str) -> str:
    """Return the forced store scope if set, else the model-requested site."""

    scope = _STORE_SCOPE.get()
    return scope if scope else requested


def _site_clause(col: str, param: str) -> str:
    """SQL predicate matching ``col`` against a site token in ``param``.

    Site codes look like ``20005-CCYK``. We match the FULL code, the numeric
    prefix (``20005``), or the alpha suffix (``CCYK``) — all case-insensitive and
    ANCHORED. This avoids the old ``ILIKE '%x%'`` substring trap where a token
    like ``200`` matched every site (wrong/aggregated stock answers).
    """

    return (
        f"(upper({col}) = upper({param}) "
        f"OR split_part({col}, '-', 1) = {param} "
        f"OR upper(split_part({col}, '-', 2)) = upper({param}))"
    )


# ---------------------------------------------------------------------------
# Branch registry visibility (1.7.0)
# ---------------------------------------------------------------------------
#
# TWO INDEPENDENT FILTERS NOW APPLY TO EVERY INVENTORY READ, and they answer
# different questions:
#
#   `_site_clause` / `_effective_site` — "which branch may THIS SESSION see?"
#       A per-request store scope from a signed embed token. One branch, chosen
#       by the caller's credential. Absent in public mode.
#
#   `_visible_site_clause`             — "which branches may ANY customer see?"
#       The `stores` registry. A branch with `status='disabled'` is meant to be
#       ABSENT, not "closed" or "unavailable" — it must not appear in a stock
#       row, a branch list, a branch count or a company-wide total.
#
# They compose by AND: a scoped session still cannot see a disabled branch, and
# an unscoped one still cannot see it either. Neither replaces the other, so
# every site-aware query carries both.
#
# ⚠️ THE SHAPE OF THIS FILTER IS LOAD-BEARING — it is an EXCLUSION, never a join.
# The fragment itself is OWNED BY `app.stores.not_disabled_clause`, which this
# module delegates to rather than spelling out, so the registry's owner and its
# readers can never drift apart on what "visible" means:
#
#   NOT EXISTS (SELECT 1 FROM stores _s WHERE _s.site_code = <col>
#                                         AND _s.status = 'disabled')
#
# The obvious alternative, `JOIN stores s ON s.site_code = i.site_code AND
# s.status = 'active'`, returns NOTHING whenever the registry is empty or lags
# behind inventory — the whole product would answer "we have no stock anywhere",
# which is far worse than the bug being fixed. Written as an exclusion, the
# failure mode inverts and becomes harmless:
#
#   * registry EMPTY            -> no row is disabled -> nothing excluded ->
#                                  behaviour identical to 1.6.x.
#   * a site in `inventory` but
#     not yet in `stores`       -> nothing excluded -> the branch stays visible.
#                                  Matches the owner's "missing must never hide a
#                                  live branch" rule, and `ensure_stores_table()`
#                                  seeds it as active shortly after anyway.
#   * `stores` table ABSENT     -> `_registry_ready()` returns False and the
#                                  predicate degrades to literal TRUE. A missing
#                                  table can therefore never raise mid-answer.
#
# Only an explicit `status='disabled'` row can ever remove anything. There is no
# input to these queries that makes them return less than 1.6.x did EXCEPT an
# admin having disabled that exact branch on purpose.

_REGISTRY_READY: bool = False
_REGISTRY_CHECKED_AT: float = float("-inf")
_REGISTRY_RECHECK_SECONDS = 30.0


async def _registry_ready() -> bool:
    """Is the `stores` registry table present? Cached, and never re-checked once
    True (the table is created at boot and is never dropped).

    A negative answer is re-checked at most every
    ``_REGISTRY_RECHECK_SECONDS`` so a fresh database that gains the table part
    way through a process starts filtering without a restart — and so a probe
    is not paid on every single tool call while it is genuinely absent.
    """

    global _REGISTRY_READY, _REGISTRY_CHECKED_AT

    if _REGISTRY_READY:
        return True
    now = time.monotonic()
    if now - _REGISTRY_CHECKED_AT < _REGISTRY_RECHECK_SECONDS:
        return False
    _REGISTRY_CHECKED_AT = now
    try:
        rows = await q("SELECT to_regclass('public.stores') IS NOT NULL AS present")
    except Exception:  # noqa: BLE001 — a probe failure must not break an answer
        logger.warning("stores registry probe failed; not filtering", exc_info=True)
        return False
    _REGISTRY_READY = bool(rows and rows[0].get("present"))
    return _REGISTRY_READY


async def _visible_site_clause(col: str) -> str:
    """SQL predicate: ``col`` names a branch customers are allowed to see.

    Takes NO query parameter, so it can be spliced into any query without
    disturbing its ``$n`` numbering. Returns the literal ``TRUE`` when the
    registry is unavailable — see the note above on why this direction of
    failure is the only acceptable one.

    ⚠️ ``col`` MUST be table-qualified (``i.site_code``, ``inventory.site_code``)
    and this is enforced here, not merely documented. The correlated subquery
    selects from `stores`, which HAS a `site_code` column of its own, so a bare
    ``site_code`` in the predicate binds to the INNER scope: the clause silently
    becomes ``_s.site_code = _s.site_code``, which is true for every registry
    row, so `NOT EXISTS` turns false for EVERY branch as soon as a single branch
    is disabled — the whole chain answers "no stock anywhere". That is exactly
    the catastrophe this module is supposed to make impossible, arriving through
    a different door. It was written that way first and caught by running it.

    `app.stores.not_disabled_clause` RAISES on an unqualified column too, so
    this check is deliberately duplicated rather than delegated: raising here
    names THIS caller and the argument it passed, while the helper's copy is the
    backstop for callers that reach it without going through this wrapper. Do
    not delete either one on the grounds that the other exists — one guard would
    still be correct, and the reader who removes the "redundant" one has to pick
    the right one, from the wrong file, to keep the message useful.

    The two responsibilities are split on purpose: `app.stores` owns the SQL
    SHAPE (it owns the table), and this wrapper owns the CALLING CONDITIONS —
    the column is qualified, and the table actually exists.
    """

    if "." not in col:
        raise ValueError(
            f"_visible_site_clause needs a table-qualified column, got {col!r} — "
            "an unqualified name binds to stores.site_code and hides every branch"
        )
    if not await _registry_ready():
        return "TRUE"
    # ⚠️ FUNCTION-LOCAL ON PURPOSE — `app.tools` and `app.stores` IMPORT EACH
    # OTHER, and neither import may be lifted to module scope.
    #
    #   app.tools   -> app.stores.not_disabled_clause   (this line)
    #   app.stores  -> app.tools._site_clause           (in `list_stores`, which
    #                                                    matches a caller's store
    #                                                    pin with the same anchored
    #                                                    site-token rules)
    #
    # The cycle is real. Measured, all four arrangements, both import orders:
    # moving ONE of the two to module scope still imports fine — it is only when
    # BOTH are at module scope that Python raises
    #
    #   ImportError: cannot import name '_site_clause' from partially
    #   initialized module 'app.tools' (most likely due to a circular import)
    #
    # and the process refuses to START — not a test failure, not a first-chat
    # failure. That is what makes this dangerous rather than merely untidy: the
    # person who lifts the FIRST one to module scope sees everything work, ships
    # it, and arms the trap for whoever later does the same to the other side.
    # So the rule is per-side and unconditional — keep this one local even if
    # the other side currently looks safe to move. `app.stores` carries the
    # mirror of this note.
    from app.stores import not_disabled_clause

    return not_disabled_clause(col)


def _to_float(value: Any) -> Optional[float]:
    """Coerce a possibly-``Decimal`` numeric value to ``float`` (or ``None``).

    asyncpg returns NUMERIC columns as ``decimal.Decimal``, which is not
    JSON-serializable. This normalises any numeric (or ``None``) to a plain
    ``float`` so tool outputs serialize cleanly for the model.
    """

    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _floatify_rows(rows: List[Dict], *fields: str) -> List[Dict]:
    """Return ``rows`` with the named numeric ``fields`` coerced to ``float``."""

    for row in rows:
        for field in fields:
            if field in row:
                row[field] = _to_float(row[field])
    return rows


# ---------------------------------------------------------------------------
# Per-call instrumentation — the three-state outcome
# ---------------------------------------------------------------------------
#
# Every tool below is wrapped so one row lands in `tool_calls` per invocation
# (see `migrations/0008_turn_calls.sql`). The wrapper is a pure pass-through:
# it changes no argument, no return value and no exception. What it adds is the
# OUTCOME, and the outcome is decided HERE — at the return site, where the
# reason is still known — never by matching an error string later.
#
#   failed    — the tool raised. A database blip, an embedding call that could
#               not reach the provider, a malformed row. Nothing else is a
#               failure.
#
#   refused   — a STORE-SCOPE DECLINE, and today that is the only deliberate
#               decline these tools make. A scoped session asks for site X;
#               `_effective_site` silently answers for the session's own store
#               instead. The tool did not do what it was asked and did something
#               else on purpose — which is precisely "declined and redirected".
#               It is also the one number worth watching: how often the model
#               tries to read a branch the session may not see.
#               Same for `list_sites(query=...)` under scope, where the query is
#               discarded and only the permitted store comes back.
#
#   succeeded — everything else, INCLUDING an empty result set. "No branch
#               stocks this" and "that article code does not exist" are answers.
#               `summarize_article` returning `{'found': False}` is a successful
#               lookup with a negative result, and counting it as a failure is
#               exactly how a working tool acquires a 56% failure rate.
#
# ⚠️ Nothing here ADDS a refusal. There is no new guard, no new early return and
# no changed message: a tool that today runs a broad query still runs it. Adding
# guards would change answers, and the instrumentation is not allowed to.
#
# ⚠️ The wrapper must keep the tool's signature intact. Agno builds the model's
# tool schema from `inspect.signature` and `__doc__`, so a wrapper that dropped
# either would silently change which arguments the model may send. `functools
# .wraps` sets `__wrapped__`, which `inspect.signature` follows.

# Tool name -> the parameter naming a site, for tools whose site argument
# `_effective_site` may override. Tools absent from this table take no site
# argument at all (they read the scope directly) and can never refuse.
_SITE_ARG: Dict[str, str] = {
    "get_stock": "site",
    "top_by_stock": "site",
    "filter_by_price": "site",
    "search_by_meaning": "site",
    "related_drugs": "in_stock_site",
    "drugs_for_same_condition": "in_stock_site",
}


def _site_matches(requested: str, scope: str) -> bool:
    """Python twin of :func:`_site_clause` — does ``requested`` name ``scope``?

    Full code, numeric prefix, or alpha suffix, case-insensitive and anchored.
    Kept deliberately identical to the SQL so a session asking for its OWN store
    by prefix is not recorded as an out-of-scope attempt.
    """

    req, sc = (requested or "").strip().upper(), (scope or "").strip().upper()
    if not req or not sc:
        return False
    parts = sc.split("-")
    return req == sc or req == parts[0] or (len(parts) > 1 and req == parts[1])


def _refused_reason(name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Why this call was a deliberate decline, or None if it was not one."""

    scope = get_store_scope()
    if not scope:
        return None
    site_arg = _SITE_ARG.get(name)
    if site_arg:
        requested = str(arguments.get(site_arg) or "").strip()
        if requested and not _site_matches(requested, scope):
            return "store scope: answered for the session's own store instead"
    if name == "list_sites" and str(arguments.get("query") or "").strip():
        return "store scope: site search declined, only the session's store is visible"
    return None


def _instrument(fn: Callable) -> Callable:
    """Wrap one tool so its call is recorded. Pure pass-through; never raises."""

    from app import activity

    signature = inspect.signature(fn)
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments: Dict[str, Any] = dict(bound.arguments)
        except Exception:  # noqa: BLE001 — a call agno bound will bind here too
            arguments = {}
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            # Classified at the RAISE site: this is the only place that knows
            # the call broke rather than declined. Recorded, then re-raised
            # completely unchanged — instrumentation never swallows a tool error.
            activity.record_tool_call(
                name,
                outcome=activity.FAILED,
                arguments=arguments,
                error_message=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        reason = _refused_reason(name, arguments)
        activity.record_tool_call(
            name,
            outcome=activity.REFUSED if reason else activity.SUCCEEDED,
            arguments=arguments,
            error_message=reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    return wrapper


async def get_article_info(code: str) -> List[Dict]:
    """Return catalog details for a single article, with per-site availability.

    Joins the catalog with inventory so each returned row carries the article's
    catalog fields plus one site's stock and price.

    Args:
        code: The exact article/catalog code (kept literal, never translated).

    Returns:
        A list with one row per stocking site, each containing the catalog
        fields (article_code, brand_name, generic_name, composition, category,
        pack_size) plus site_code, site_name, stock_qty, price and uom. If the
        article exists but has no inventory, a single row is returned with the
        site/stock/price fields set to ``None`` (LEFT JOIN). Empty list if no
        article matches the code.
    """

    scope = get_store_scope()
    visible = await _visible_site_clause("i.site_code")
    rows = await q(
        """
        SELECT i.article_code,
               COALESCE(c.brand_name, i.article_code) AS brand_name,
               c.generic_name,
               c.composition,
               c.category,
               c.indication,
               c.dosage,
               c.side_effect,
               -- Myanmar registration fields. No tool selected these until
               -- 2026-08-02, so "what is the MM-Reg for X" was unanswerable no
               -- matter how well the search worked: the agent found the row,
               -- could not see the column, and reported it "not available in
               -- the catalog" while a value sat in the database (FB-13).
               c.mm_reg,
               c.mm_label,
               c.status,
               i.site_code,
               i.site_name,
               i.stock_qty,
               i.price,
               i.uom
          FROM inventory i
          LEFT JOIN catalog c USING (article_code)
         WHERE i.article_code = $1
           AND ($2::text IS NULL OR """ + _site_clause("i.site_code", "$2") + """)
           AND """ + visible + """
         ORDER BY i.site_code
        """,
        code,
        scope,
    )
    if rows:
        return _floatify_rows(rows, "price")

    # Fallback: the article exists in the catalog but has no inventory rows
    # (unstocked, or none in the current store scope). Return catalog identity
    # with null site/stock/price fields so indication/dosage/composition are
    # still available for "what is this drug for" questions.
    rows = await q(
        """
        SELECT article_code,
               brand_name,
               generic_name,
               composition,
               category,
               indication,
               dosage,
               side_effect,
               mm_reg,
               mm_label,
               status,
               NULL::text AS site_code,
               NULL::text AS site_name,
               NULL::int AS stock_qty,
               NULL::numeric AS price,
               NULL::text AS uom
          FROM catalog
         WHERE article_code = $1
        """,
        code,
    )
    return _floatify_rows(rows, "price")


async def search_by_name(name: str) -> List[Dict]:
    """Search the catalog for articles whose name matches the query.

    Fuzzy-matches the query against both brand_name and generic_name using a
    case-insensitive substring (ILIKE) comparison.

    Args:
        name: Full or partial product name (any language as stored).

    Returns:
        A list of up to 50 matching articles (article_code, brand_name,
        generic_name, category), ordered by brand_name. Empty if
        nothing matches.
    """

    rows = await q(
        """
        SELECT article_code,
               brand_name,
               generic_name,
               category
          FROM catalog
         WHERE brand_name ILIKE '%' || $1 || '%'
            OR generic_name ILIKE '%' || $1 || '%'
         ORDER BY (brand_name ILIKE $1 || '%') DESC, brand_name
         LIMIT 50
        """,
        name,
    )
    return rows


async def get_stock(code: str, site: str = "") -> List[Dict]:
    """Return current stock levels for an article.

    Args:
        code: The article/catalog code (literal).
        site: Optional pharmacy site code. When empty, returns stock across all
            sites; when given, scopes results to that single site.

    Returns:
        When ``site`` is given: a list of rows with site_name and stock_qty for
        that site. Otherwise: a list of rows with site_code, site_name and
        stock_qty across all sites, ordered by stock_qty descending. Empty if
        the article has no stock records for the requested scope.
    """

    site = _effective_site(site)
    visible = await _visible_site_clause("inventory.site_code")
    if site:
        rows = await q(
            """
            SELECT site_code,
                   site_name,
                   stock_qty
              FROM inventory
             WHERE article_code = $1
               AND """ + _site_clause("site_code", "$2") + """
               AND """ + visible + """
             ORDER BY stock_qty DESC NULLS LAST
            """,
            code,
            site,
        )
    else:
        rows = await q(
            """
            SELECT site_code,
                   site_name,
                   stock_qty
              FROM inventory
             WHERE article_code = $1
               AND """ + visible + """
             ORDER BY stock_qty DESC NULLS LAST
            """,
            code,
        )
    return rows


async def top_by_stock(site: str, n: int = 5) -> List[Dict]:
    """Return the top-N best-stocked articles at a given site.

    Args:
        site: Pharmacy site code to rank within.
        n: Number of articles to return (default 5; capped at 50).

    Returns:
        A list of up to ``n`` articles ordered by quantity on hand
        (descending), each with article_code, brand_name and stock_qty.
    """

    site = _effective_site(site)
    visible = await _visible_site_clause("i.site_code")
    limit = min(max(int(n), 1), 50)
    rows = await q(
        """
        SELECT i.article_code,
               c.brand_name,
               i.stock_qty
          FROM inventory i
          JOIN catalog c USING (article_code)
         WHERE """ + _site_clause("i.site_code", "$1") + """
           AND """ + visible + """
         ORDER BY i.stock_qty DESC NULLS LAST
         LIMIT $2
        """,
        site,
        limit,
    )
    return rows


async def filter_by_price(
    min_price: float,
    max_price: Optional[float] = None,
    site: str = "",
) -> List[Dict]:
    """Return articles whose price falls within a range.

    Args:
        min_price: Inclusive lower price bound.
        max_price: Inclusive upper price bound. When ``None``, no upper limit.
        site: Optional site code to scope availability/pricing to.

    Returns:
        A list of up to 100 matching rows (article_code, brand_name, site_code,
        site_name, price), ordered by price descending. Empty if none fall
        within the range.
    """

    site = _effective_site(site)
    # Takes no parameter, so it can sit in `conditions` without disturbing the
    # $n numbering built below.
    conditions = ["i.price >= $1", await _visible_site_clause("i.site_code")]
    params: List[Any] = [min_price]

    if max_price is not None:
        params.append(max_price)
        conditions.append(f"i.price <= ${len(params)}")

    if site:
        params.append(site)
        conditions.append(_site_clause("i.site_code", f"${len(params)}"))

    where_clause = " AND ".join(conditions)
    rows = await q(
        f"""
        SELECT i.article_code,
               c.brand_name,
               i.site_code,
               i.site_name,
               i.price
          FROM inventory i
          JOIN catalog c USING (article_code)
         WHERE {where_clause}
         ORDER BY i.price DESC
         LIMIT 100
        """,
        *params,
    )
    return _floatify_rows(rows, "price")


async def get_substitutes(code: str) -> List[Dict]:
    """Return substitute articles for the given article.

    Finds alternatives that share the source article's active ingredient,
    generic name, indication or category — ranked in that order, closest match
    first. Prefer the top rows: a `composition` match is a true substitute, a
    `category` match is merely the same shelf.

    Args:
        code: The article/catalog code to find substitutes for (literal).

    Returns:
        Up to 50 candidates, each with article_code, brand_name, generic_name,
        composition, category, `match_rank` (1 = closest) and `matched_on`
        (which field matched). Empty if the article is unknown or nothing
        shares any of its four fields.
    """

    # Ranked, not just filtered. Acceptance rule 5 from the 2026-07-28 feedback
    # asks for substitutes by "Same Composition, Same Generic Name, Same
    # Indication, Same Category" in that priority order. Matching on
    # generic_name alone (the original) missed every product that shares an
    # active ingredient under a different generic label — and generic_name is
    # NULL for 32% of the catalog, where it returned nothing at all.
    rows = await q(
        """
        WITH src AS (
            SELECT generic_name, composition, indication, category
              FROM catalog WHERE article_code = $1
        )
        SELECT c.article_code,
               c.brand_name,
               c.generic_name,
               c.composition,
               c.category,
               CASE
                   WHEN src.composition  IS NOT NULL AND c.composition  = src.composition  THEN 1
                   WHEN src.generic_name IS NOT NULL AND c.generic_name = src.generic_name THEN 2
                   WHEN src.indication   IS NOT NULL AND c.indication   = src.indication   THEN 3
                   ELSE 4
               END AS match_rank,
               CASE
                   WHEN src.composition  IS NOT NULL AND c.composition  = src.composition  THEN 'composition'
                   WHEN src.generic_name IS NOT NULL AND c.generic_name = src.generic_name THEN 'generic_name'
                   WHEN src.indication   IS NOT NULL AND c.indication   = src.indication   THEN 'indication'
                   ELSE 'category'
               END AS matched_on
          FROM catalog c CROSS JOIN src
         WHERE c.article_code <> $1
           AND c.brand_name <> c.article_code   -- never suggest a nameless stub
           AND (
                   (src.composition  IS NOT NULL AND c.composition  = src.composition)
                OR (src.generic_name IS NOT NULL AND c.generic_name = src.generic_name)
                OR (src.indication   IS NOT NULL AND c.indication   = src.indication)
                OR (src.category     IS NOT NULL AND c.category     = src.category)
               )
         ORDER BY match_rank, c.brand_name
         LIMIT 50
        """,
        code,
    )
    return rows


async def summarize_article(code: str) -> Dict:
    """Return a combined summary for a single article.

    Aggregates catalog info with per-site stock and price into one record —
    convenient for answering "tell me everything about X" in a single tool
    call.

    Args:
        code: The article/catalog code (literal).

    Returns:
        A single dict with article_code, found, brand_name, generic_name,
        total_stock, unknown_sites, weighted_avg_price (rounded to 2 decimals)
        and site_count. If the article is not found, returns
        ``{'article_code': code, 'found': False}``.

        ``total_stock`` is ``None`` when no branch has a known quantity, and
        otherwise sums only the branches that do — ``unknown_sites`` counts the
        rest. Zero and negative quantities are REAL VALUES and are included in
        the sum: a 0 means the branch is out, a negative means the branch's
        books disagree with its shelf, and hiding either would misreport stock.
        Only an unknown (NULL) is excluded, because it is not a number.
    """

    scope = get_store_scope()
    # Disabled branches are excluded from the SUM as well as from the row list —
    # a disabled branch's units are not part of the company total. The owner
    # decided totals move when a branch is disabled; that is this line.
    visible = await _visible_site_clause("i.site_code")
    rows = await q(
        """
        SELECT i.article_code,
               COALESCE(c.brand_name, i.article_code) AS brand_name,
               c.generic_name,
               -- NOT COALESCE(..., 0). A NULL stock_qty means UNKNOWN, never
               -- "none on hand", and a pharmacist reading 0 does not dispense.
               -- The same coercion was already removed from admin.catalog_one;
               -- this was the last one left. SUM ignores NULLs, so the total
               -- covers the branches we have a figure for and is NULL when we
               -- have a figure for none — with unknown_sites saying how many
               -- were left out, so a partial total is never read as complete.
               SUM(i.stock_qty) AS total_stock,
               COUNT(*) FILTER (WHERE i.stock_qty IS NULL) AS unknown_sites,
               SUM(i.price * i.stock_qty)
                   / NULLIF(SUM(i.stock_qty), 0) AS weighted_avg_price,
               COUNT(DISTINCT i.site_code) AS site_count
          FROM inventory i
          LEFT JOIN catalog c USING (article_code)
         WHERE i.article_code = $1
           AND ($2::text IS NULL OR """ + _site_clause("i.site_code", "$2") + """)
           AND """ + visible + """
         GROUP BY i.article_code, c.brand_name, c.generic_name
        """,
        code,
        scope,
    )

    if not rows:
        # Fallback: no inventory for this code (unstocked or none in scope).
        # Pull catalog identity so the article is still reported as found, with
        # zeroed stock/pricing fields.
        cat = await q(
            """
            SELECT article_code, brand_name, generic_name
              FROM catalog
             WHERE article_code = $1
            """,
            code,
        )
        if cat:
            crow = cat[0]
            return {
                "article_code": crow["article_code"],
                "found": True,
                "brand_name": crow.get("brand_name"),
                "generic_name": crow.get("generic_name"),
                "total_stock": 0,
                "weighted_avg_price": None,
                "site_count": 0,
            }
        return {"article_code": code, "found": False}

    row = rows[0]
    weighted = _to_float(row.get("weighted_avg_price"))
    return {
        "article_code": row["article_code"],
        "found": True,
        "brand_name": row.get("brand_name"),
        "generic_name": row.get("generic_name"),
        "total_stock": int(row.get("total_stock") or 0),
        "weighted_avg_price": round(weighted, 2) if weighted is not None else None,
        "site_count": int(row.get("site_count") or 0),
    }


async def search_by_meaning(query: str, site: str = "") -> List[Dict]:
    """Semantic search — find products by meaning or symptom, not exact name/code.

    Use this for natural-language needs like "medicine for fever", "something for
    diabetes", or Burmese equivalents ("ဖျားနာအတွက် ဆေး"). It matches the user's
    intent against product purpose/indication, returning the closest items.
    Prefer exact tools (get_stock, get_article_info) when the user gives a code.

    Args:
        query: Free-text need or symptom (English or Burmese).
        site: Optional site code to limit results to products stocked there.

    Returns:
        Up to 10 closest catalog matches (article_code, brand_name,
        generic_name, indication), ordered by semantic similarity.
    """

    from app.embeddings import embed_query_cached, to_pgvector

    qv = to_pgvector(await embed_query_cached(query))
    site = _effective_site(site)
    if site:
        # "stocked at this site" must not be satisfied by a disabled branch.
        visible = await _visible_site_clause("i.site_code")
        return await q(
            """
            SELECT c.article_code, c.brand_name, c.generic_name, c.indication
              FROM catalog c
             WHERE c.embedding IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM inventory i
                    WHERE i.article_code = c.article_code
                      AND """ + _site_clause("i.site_code", "$2") + """
                      AND """ + visible + """
               )
             ORDER BY c.embedding <=> $1::vector
             LIMIT 10
            """,
            qv,
            site,
        )
    return await q(
        """
        SELECT article_code, brand_name, generic_name, indication
          FROM catalog
         WHERE embedding IS NOT NULL
         ORDER BY embedding <=> $1::vector
         LIMIT 10
        """,
        qv,
    )


async def related_drugs(code: str, hops: int = 2, in_stock_site: str = "") -> List[Dict]:
    """Graph traversal — find related products via the knowledge graph.

    Walks the drug knowledge graph (shared generic, shared ingredient, same
    category) up to ``hops`` steps from the given article, so you can answer
    relational questions like "alternatives related to X" or "products like X
    that share an ingredient". Use this for discovery/relationship questions;
    use get_substitutes for the strict same-generic list.

    Args:
        code: The article/catalog code to start from (literal).
        hops: How many graph hops to traverse (1-4; default 2).
        in_stock_site: Optional site code — keep only related products stocked
            there (hybrid: graph relation + live stock at the branch).

    Returns:
        Related articles with: article_code, brand_name, generic_name, hops
        (graph distance). When in_stock_site is set, also stock_qty at that site.
    """

    from app.graph import related

    site = _effective_site(in_stock_site)
    # has_generic + contains are meaningful relations; in_category is a huge hub
    # (e.g. "OTC MEDICINE") that links everything, so it's excluded from traversal.
    rows = await related(code, rels=["has_generic", "contains"], hops=hops, limit=25)
    if not site or not rows:
        return rows
    # hybrid: keep only those stocked at the site, attach stock
    codes = [r["article_code"] for r in rows]
    stock = await q(
        """SELECT article_code, stock_qty FROM inventory
            WHERE """ + _site_clause("site_code", "$1") + """ AND article_code = ANY($2)
              AND """ + await _visible_site_clause("inventory.site_code"),
        site, codes,
    )
    smap = {s["article_code"]: s["stock_qty"] for s in stock}
    out = []
    for r in rows:
        if r["article_code"] in smap:
            out.append({**r, "stock_qty": smap[r["article_code"]]})
    return out


async def drugs_for_same_condition(code: str, in_stock_site: str = "") -> List[Dict]:
    """Graph (clinical) — other products that treat the SAME conditions as this one.

    Uses the drug knowledge graph's treats-edges (extracted from indication text)
    to hop article -> condition -> article. Answers "what else treats what X
    treats?" / therapeutic alternatives by purpose. Optionally limited to a site.

    Args:
        code: The article/catalog code to start from (literal).
        in_stock_site: Optional site code to keep only those stocked there.

    Returns:
        Articles sharing a treated condition, with article_code, brand_name, and
        the shared condition(s). With in_stock_site, also stock_qty.
    """

    site = _effective_site(in_stock_site)
    rows = await q(
        """
        SELECT e2.src AS article_code, c.brand_name,
               array_agg(DISTINCT e1.dst) AS shared_conditions
          FROM drug_edges e1
          JOIN drug_edges e2 ON e2.dst = e1.dst AND e2.rel = 'treats' AND e2.src <> e1.src
          JOIN catalog c ON c.article_code = e2.src
         WHERE e1.src = $1 AND e1.rel = 'treats'
         GROUP BY e2.src, c.brand_name
         ORDER BY c.brand_name
         LIMIT 25
        """,
        code,
    )
    if not site or not rows:
        return rows
    codes = [r["article_code"] for r in rows]
    stock = await q(
        """SELECT article_code, stock_qty FROM inventory
            WHERE """ + _site_clause("site_code", "$1") + """ AND article_code = ANY($2)
              AND """ + await _visible_site_clause("inventory.site_code"),
        site, codes,
    )
    smap = {s["article_code"]: s["stock_qty"] for s in stock}
    return [{**r, "stock_qty": smap[r["article_code"]]} for r in rows if r["article_code"] in smap]


async def find_at_other_stores(code: str) -> List[Dict]:
    """Check which OTHER pharmacy branches stock an article.

    Use when the current store is out of stock or low, to tell the user where
    else the product is available. Deliberately narrow: availability only — no
    prices.

    Args:
        code: The exact article/catalog code (literal, never translated).

    Returns:
        Rows of site_code, site_name and stock_qty for branches with
        stock_qty > 0, ordered by stock_qty descending, limit 15. Excludes the
        caller's own scoped store when the session is store-scoped. Empty list
        if no other branch has the article in stock.
    """

    scope = get_store_scope()
    visible = await _visible_site_clause("inventory.site_code")
    if scope:
        rows = await q(
            """
            SELECT site_code,
                   site_name,
                   stock_qty
              FROM inventory
             WHERE article_code = $1
               AND stock_qty > 0
               AND NOT """ + _site_clause("site_code", "$2") + """
               AND """ + visible + """
             ORDER BY stock_qty DESC
             LIMIT 15
            """,
            code,
            scope,
        )
    else:
        rows = await q(
            """
            SELECT site_code,
                   site_name,
                   stock_qty
              FROM inventory
             WHERE article_code = $1
               AND stock_qty > 0
               AND """ + visible + """
             ORDER BY stock_qty DESC
             LIMIT 15
            """,
            code,
        )
    return rows


async def list_sites(query: str = "") -> List[Dict]:
    """List pharmacy site codes, to resolve a site the user named to a real code.

    Use this BEFORE answering any site-specific stock/price question when the
    user refers to a branch by anything other than its exact code — match their
    wording to one of these codes, then pass that exact code to the stock/price
    tool. If nothing matches, tell the user and show the options instead of
    guessing.

    Args:
        query: Optional filter (matches the code or its alpha suffix). Empty
            returns all sites.

    Returns:
        A list of rows: site_code, site_name (may be blank), sku_count (distinct
        articles stocked). Ordered by site_code.
    """

    # A disabled branch must not be nameable, so it is absent from all three
    # branches here — including the count of rows the model sees, which is what
    # it uses to answer "how many branches do you have".
    visible = await _visible_site_clause("inventory.site_code")
    if get_store_scope():
        # Locked to one store — only ever expose that one.
        rows = await q(
            """SELECT site_code, max(site_name) AS site_name,
                      count(DISTINCT article_code) AS sku_count
                 FROM inventory
                WHERE """ + _site_clause("site_code", "$1") + """
                  AND """ + visible + """
                GROUP BY site_code ORDER BY site_code""",
            get_store_scope(),
        )
        return rows
    if query:
        return await q(
            """SELECT site_code, max(site_name) AS site_name,
                      count(DISTINCT article_code) AS sku_count
                 FROM inventory
                WHERE site_code ILIKE '%' || $1 || '%'
                  AND """ + visible + """
                GROUP BY site_code ORDER BY site_code LIMIT 50""",
            query,
        )
    return await q(
        """SELECT site_code, max(site_name) AS site_name,
                  count(DISTINCT article_code) AS sku_count
             FROM inventory
            WHERE """ + visible + """
            GROUP BY site_code ORDER BY site_code LIMIT 100"""
    )


#: The twelve tools, wrapped in place. Rebinding the module globals (rather than
#: decorating each definition) keeps every caller working unchanged — `app.agent`
#: imports these names, `app.fastpath` reaches them as `tools.get_stock`, and
#: both get the instrumented version. It happens at import, so no caller can ever
#: hold the unwrapped function.
INSTRUMENTED_TOOLS = (
    "get_article_info",
    "search_by_name",
    "get_stock",
    "top_by_stock",
    "filter_by_price",
    "get_substitutes",
    "summarize_article",
    "search_by_meaning",
    "related_drugs",
    "drugs_for_same_condition",
    "find_at_other_stores",
    "list_sites",
)

for _tool_name in INSTRUMENTED_TOOLS:
    globals()[_tool_name] = _instrument(globals()[_tool_name])
del _tool_name


__all__ = [
    "list_sites",
    "get_article_info",
    "search_by_name",
    "get_stock",
    "top_by_stock",
    "filter_by_price",
    "get_substitutes",
    "summarize_article",
    "search_by_meaning",
    "related_drugs",
    "drugs_for_same_condition",
    "find_at_other_stores",
]
