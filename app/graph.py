"""GraphRAG (stage 1) — a knowledge graph derived from the catalog, in Postgres.

No separate graph DB: edges live in a `drug_edges` table and multi-hop traversal
uses a recursive CTE. The graph is bipartite — articles linked to attribute nodes:

    article --has_generic--> <generic_name>
    article --contains-----> <ingredient>      (parsed from composition text)
    article --in_category--> <category>

Two articles are "related" when they share an attribute node. Traversing
article -> attribute -> article (and repeating) gives multi-hop relations:
substitutes (shared generic), ingredient siblings, therapeutic class.

Stage 2 (later) adds article --treats--> <condition> edges extracted from the
indication text, enabling "what else treats what X treats".
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.db import execute, q

REL_KINDS = ("has_generic", "contains", "in_category")

# tokens that are strengths/units/fillers, not ingredients
_STOP = {
    "mg", "ml", "mcg", "g", "kg", "iu", "tablet", "tablets", "capsule", "syrup",
    "suspension", "oral", "injection", "solution", "and", "of", "the", "each",
    "per", "acid", "usp", "bp", "w", "v", "gm", "%",
}


async def ensure_edges_table() -> None:
    await execute(
        """
        CREATE TABLE IF NOT EXISTS drug_edges (
            src TEXT NOT NULL,
            rel TEXT NOT NULL,
            dst TEXT NOT NULL
        )
        """
    )
    await execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON drug_edges (src, rel)")
    await execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON drug_edges (rel, dst)")


def _ingredients(composition: Optional[str]) -> List[str]:
    """Crude ingredient extraction from free-text composition."""

    if not composition:
        return []
    text = composition.lower()
    parts = re.split(r"[+,;/()]", text)
    out = set()
    for p in parts:
        # drop numbers + units, keep alpha words
        words = re.findall(r"[a-zက-႟]{3,}", p)
        words = [w for w in words if w not in _STOP]
        if not words:
            continue
        token = " ".join(words).strip()
        if 3 <= len(token) <= 40:
            out.add(token)
    return list(out)[:6]  # cap per article


async def build_edges() -> Dict[str, int]:
    """Rebuild drug_edges from the current catalog. Idempotent (truncate + insert)."""

    await ensure_edges_table()
    rows = await q("SELECT article_code, generic_name, category, composition FROM catalog")
    edges: List[tuple] = []
    for r in rows:
        code = r["article_code"]
        g = (r.get("generic_name") or "").strip()
        if g and g not in ("-", ""):
            edges.append((code, "has_generic", g.lower()))
        cat = (r.get("category") or "").strip()
        if cat:
            edges.append((code, "in_category", cat.lower()))
        for ing in _ingredients(r.get("composition")):
            edges.append((code, "contains", ing))

    from app.db import get_pool

    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # Replace ONLY structured edges — never the (expensive) treats edges.
            await conn.execute(
                "DELETE FROM drug_edges WHERE rel IN ('has_generic','contains','in_category')"
            )
            if edges:
                await conn.copy_records_to_table(
                    "drug_edges", records=edges, columns=["src", "rel", "dst"]
                )
    counts = {}
    for k in REL_KINDS:
        counts[k] = (await q("SELECT count(*) n FROM drug_edges WHERE rel=$1", k))[0]["n"]
    counts["total"] = sum(counts.values())
    return counts


async def _extract_conditions(batch: List[Dict]) -> Dict[str, List[str]]:
    """One LLM call: map each article_code -> up to 4 short English condition tags
    extracted from its (often Burmese) indication text."""

    import json as _json

    from openai import AsyncOpenAI

    from app.config import get_settings

    s = get_settings()
    items = [{"code": b["article_code"], "indication": (b.get("indication") or "")[:400]} for b in batch]
    prompt = (
        "For each pharmacy product below, extract up to 4 SHORT English condition/symptom "
        "tags it treats (lowercase, e.g. 'fever','pain','diabetes','cough'). The indication "
        "may be in Burmese. Return ONLY JSON: {\"<code>\":[\"tag\",...], ...}. No prose.\n\n"
        + _json.dumps(items, ensure_ascii=False)
    )
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=s.openrouter_api_key)
    resp = await client.chat.completions.create(
        model=s.openrouter_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    txt = resp.choices[0].message.content or ""
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    try:
        return _json.loads(txt)
    except Exception:
        return {}


async def build_treats_edges(limit: int = 200, batch_size: int = 15) -> Dict:
    """Stage 2: LLM-extract condition tags from indication text → article--treats-->condition
    edges. Bounded by ``limit`` (stocked articles with indication, no treats edge yet)."""

    await ensure_edges_table()
    rows = await q(
        """
        SELECT c.article_code, c.indication
          FROM catalog c
         WHERE c.indication IS NOT NULL AND length(c.indication) > 15
           AND EXISTS (SELECT 1 FROM inventory i WHERE i.article_code = c.article_code)
           AND NOT EXISTS (SELECT 1 FROM drug_edges e WHERE e.src = c.article_code AND e.rel = 'treats')
         LIMIT $1
        """,
        limit,
    )
    edges, done = [], 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        mapping = await _extract_conditions(chunk)
        for code, tags in mapping.items():
            for t in (tags or [])[:4]:
                t = str(t).strip().lower()
                if 2 <= len(t) <= 30:
                    edges.append((code, "treats", t))
        done += len(chunk)
    if edges:
        from app.db import get_pool

        p = await get_pool()
        async with p.acquire() as conn:
            await conn.copy_records_to_table("drug_edges", records=edges, columns=["src", "rel", "dst"])
    total = (await q("SELECT count(*) n FROM drug_edges WHERE rel='treats'"))[0]["n"]
    return {"articles_processed": done, "treats_edges_added": len(edges), "treats_edges_total": total}


async def related(code: str, rels: List[str], hops: int = 1, limit: int = 20) -> List[Dict]:
    """Recursive multi-hop traversal: articles reachable from ``code`` via shared
    attribute nodes (of the given rels), with hop depth + the shared attribute.
    """

    rels = [r for r in rels if r in REL_KINDS] or list(REL_KINDS)
    hops = max(1, min(hops, 4))
    rows = await q(
        """
        WITH RECURSIVE reach(code, depth) AS (
            SELECT $1::text, 0
          UNION
            SELECT e2.src, r.depth + 1
              FROM reach r
              JOIN drug_edges e1 ON e1.src = r.code AND e1.rel = ANY($2)
              JOIN drug_edges e2 ON e2.dst = e1.dst AND e2.rel = e1.rel AND e2.src <> r.code
             WHERE r.depth < $3
        )
        SELECT reach.code AS article_code,
               MIN(reach.depth) AS hops,
               c.brand_name, c.generic_name
          FROM reach
          JOIN catalog c ON c.article_code = reach.code
         WHERE reach.code <> $1
         GROUP BY reach.code, c.brand_name, c.generic_name
         ORDER BY hops, c.brand_name
         LIMIT $4
        """,
        code, rels, hops, limit,
    )
    return rows


__all__ = ["ensure_edges_table", "build_edges", "related", "REL_KINDS"]
