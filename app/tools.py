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
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.db import q

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
               i.site_code,
               i.site_name,
               i.stock_qty,
               i.price,
               i.uom
          FROM inventory i
          LEFT JOIN catalog c USING (article_code)
         WHERE i.article_code = $1
           AND ($2::text IS NULL OR """ + _site_clause("i.site_code", "$2") + """)
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
    if site:
        rows = await q(
            """
            SELECT site_code,
                   site_name,
                   stock_qty
              FROM inventory
             WHERE article_code = $1
               AND """ + _site_clause("site_code", "$2") + """
             ORDER BY stock_qty DESC
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
             ORDER BY stock_qty DESC
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
    limit = min(max(int(n), 1), 50)
    rows = await q(
        """
        SELECT i.article_code,
               c.brand_name,
               i.stock_qty
          FROM inventory i
          JOIN catalog c USING (article_code)
         WHERE """ + _site_clause("i.site_code", "$1") + """
         ORDER BY i.stock_qty DESC
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
    conditions = ["i.price >= $1"]
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

    Finds generic alternatives — catalog entries sharing the same
    generic_name but with a different article_code.

    Args:
        code: The article/catalog code to find substitutes for (literal).

    Returns:
        A list of candidate substitutes (article_code, brand_name, generic_name).
        Empty if the source article has no/empty generic_name or no other
        article shares its generic_name.
    """

    rows = await q(
        """
        SELECT article_code,
               brand_name,
               generic_name
          FROM catalog
         WHERE generic_name = (
                   SELECT generic_name
                     FROM catalog
                    WHERE article_code = $1
               )
           AND generic_name IS NOT NULL
           AND generic_name <> ''
           AND article_code <> $1
         ORDER BY brand_name
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
        total_stock, weighted_avg_price (rounded to 2 decimals) and site_count.
        If the article is not found, returns
        ``{'article_code': code, 'found': False}``.
    """

    scope = get_store_scope()
    rows = await q(
        """
        SELECT i.article_code,
               COALESCE(c.brand_name, i.article_code) AS brand_name,
               c.generic_name,
               COALESCE(SUM(i.stock_qty), 0) AS total_stock,
               SUM(i.price * i.stock_qty)
                   / NULLIF(SUM(i.stock_qty), 0) AS weighted_avg_price,
               COUNT(DISTINCT i.site_code) AS site_count
          FROM inventory i
          LEFT JOIN catalog c USING (article_code)
         WHERE i.article_code = $1
           AND ($2::text IS NULL OR """ + _site_clause("i.site_code", "$2") + """)
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
        return await q(
            """
            SELECT c.article_code, c.brand_name, c.generic_name, c.indication
              FROM catalog c
             WHERE c.embedding IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM inventory i
                    WHERE i.article_code = c.article_code
                      AND """ + _site_clause("i.site_code", "$2") + """
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
            WHERE """ + _site_clause("site_code", "$1") + """ AND article_code = ANY($2)""",
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
            WHERE """ + _site_clause("site_code", "$1") + """ AND article_code = ANY($2)""",
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

    if get_store_scope():
        # Locked to one store — only ever expose that one.
        rows = await q(
            """SELECT site_code, max(site_name) AS site_name,
                      count(DISTINCT article_code) AS sku_count
                 FROM inventory
                WHERE """ + _site_clause("site_code", "$1") + """
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
                GROUP BY site_code ORDER BY site_code LIMIT 50""",
            query,
        )
    return await q(
        """SELECT site_code, max(site_name) AS site_name,
                  count(DISTINCT article_code) AS sku_count
             FROM inventory
            GROUP BY site_code ORDER BY site_code LIMIT 100"""
    )


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
