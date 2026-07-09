"""Deterministic drug resolution — zero LLM.

Resolves a free-text drug mention (English or Burmese) to catalog article
code(s) without calling a model, so the fast path can answer a stock/location
question with a single SQL query. Three layers, cheapest first:

    1. exact article_code match   — the mention already is a catalog code.
    2. drug_alias table lookup    — a previously-learned mention (~5ms).
    3. trigram similarity         — GIN-indexed brand_name / generic_name
       (``idx_catalog_brand_trgm`` / ``idx_catalog_generic_trgm``), matched
       with the pg_trgm ``%`` operator and scored with ``similarity()``.

Ambiguity is deliberate: many brands share one generic_name, so a bare generic
("paracetamol") resolves to dozens of products. When the top candidate is not
clearly ahead of the runner-up the resolver returns AMBIGUOUS with the
candidate list — a pharmacy must ask back, never guess.

All database access goes through :func:`app.db.q` (asyncpg positional params).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from app.db import q

# Brand name is the specific product identity; generic_name is inherently
# many-to-one (every paracetamol brand shares "Paracetamol"), so it contributes
# to the score at a discount. A single brand hit must be able to pull clear of a
# field of same-generic siblings.
_GENERIC_WEIGHT = 0.45
# Top candidate must beat the runner-up by this combined-score margin to be a
# confident single resolution; otherwise the answer is AMBIGUOUS.
_MARGIN = 0.12
# Below this combined score the best candidate is too weak to trust — treat the
# mention as not found and let the full agent handle it.
_FLOOR = 0.20
# How many candidates to surface for an ambiguous mention.
_MAX_CANDIDATES = 8


class Resolution(str, Enum):
    """Outcome of a resolution attempt."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class Candidate:
    """One catalog article a mention might refer to, with its match score."""

    article_code: str
    brand_name: Optional[str] = None
    generic_name: Optional[str] = None
    score: float = 1.0


@dataclass
class ResolveResult:
    """The resolver's verdict for one mention.

    ``candidates`` holds exactly one item when RESOLVED, the ranked shortlist
    when AMBIGUOUS, and is empty when NOT_FOUND. ``source`` records which layer
    produced the match ('code', 'alias', 'trigram', or ``None``).
    """

    status: Resolution
    candidates: List[Candidate] = field(default_factory=list)
    source: Optional[str] = None

    @property
    def article_code(self) -> Optional[str]:
        """The resolved code when RESOLVED, else ``None``."""

        if self.status is Resolution.RESOLVED and self.candidates:
            return self.candidates[0].article_code
        return None

    @property
    def confidence(self) -> float:
        """Confidence in the top candidate (1.0 for exact/alias hits)."""

        if not self.candidates:
            return 0.0
        return min(self.candidates[0].score, 1.0)


def _looks_like_code(mention: str) -> bool:
    """Return True when the mention is a bare numeric article code."""

    return mention.isdigit() and len(mention) >= 6


async def _exact_code(mention: str) -> Optional[Candidate]:
    """Return the catalog article whose code is exactly ``mention``, or ``None``."""

    rows = await q(
        """
        SELECT article_code, brand_name, generic_name
          FROM catalog
         WHERE article_code = $1
        """,
        mention,
    )
    if rows:
        r = rows[0]
        return Candidate(r["article_code"], r.get("brand_name"), r.get("generic_name"), 1.0)
    return None


async def _alias_lookup(mention: str) -> Optional[Candidate]:
    """Return the article a learned alias maps to, or ``None``.

    Degrades to ``None`` if the ``drug_alias`` table is absent (migration not
    yet applied) so resolution still falls through to trigram matching.
    """

    try:
        rows = await q(
            """
            SELECT a.article_code, c.brand_name, c.generic_name
              FROM drug_alias a
              LEFT JOIN catalog c USING (article_code)
             WHERE a.alias = lower($1)
            """,
            mention,
        )
    except Exception:  # noqa: BLE001 — missing table must not break resolution
        return None
    if rows:
        r = rows[0]
        return Candidate(r["article_code"], r.get("brand_name"), r.get("generic_name"), 1.0)
    return None


async def _trigram(mention: str, limit: int) -> List[Candidate]:
    """Return catalog candidates ranked by trigram similarity to ``mention``.

    Uses the pg_trgm ``%`` operator (GIN-indexed) to prefilter, then scores each
    row as ``brand_sim + _GENERIC_WEIGHT * generic_sim`` so a specific brand
    outranks the crowd of articles that merely share a generic_name.
    """

    return _to_candidates(
        await q(
            """
            SELECT article_code, brand_name, generic_name,
                   similarity(brand_name, $1)
                   + $2 * similarity(coalesce(generic_name, ''), $1) AS score
              FROM catalog
             WHERE brand_name % $1 OR generic_name % $1
             ORDER BY score DESC
             LIMIT $3
            """,
            mention,
            _GENERIC_WEIGHT,
            limit,
        )
    )


def _to_candidates(rows: List[dict]) -> List[Candidate]:
    """Map trigram query rows to :class:`Candidate` objects."""

    return [
        Candidate(
            r["article_code"],
            r.get("brand_name"),
            r.get("generic_name"),
            float(r.get("score") or 0.0),
        )
        for r in rows
    ]


async def resolve(mention: str, *, limit: int = _MAX_CANDIDATES) -> ResolveResult:
    """Resolve a free-text drug ``mention`` to article code(s), no LLM.

    Tries exact code, then a learned alias, then trigram similarity. A trigram
    match is RESOLVED only when the top candidate clears the runner-up by
    ``_MARGIN``; otherwise it is AMBIGUOUS. A mention that matches nothing (or
    nothing above ``_FLOOR``) is NOT_FOUND.
    """

    mention = (mention or "").strip()
    if len(mention) < 2:
        return ResolveResult(Resolution.NOT_FOUND)

    if _looks_like_code(mention):
        hit = await _exact_code(mention)
        if hit is not None:
            return ResolveResult(Resolution.RESOLVED, [hit], source="code")

    alias = await _alias_lookup(mention)
    if alias is not None:
        return ResolveResult(Resolution.RESOLVED, [alias], source="alias")

    candidates = await _trigram(mention, limit)
    if not candidates or candidates[0].score < _FLOOR:
        return ResolveResult(Resolution.NOT_FOUND, source="trigram")

    top = candidates[0]
    runner = candidates[1].score if len(candidates) > 1 else 0.0
    if top.score - runner >= _MARGIN:
        return ResolveResult(Resolution.RESOLVED, [top], source="trigram")
    return ResolveResult(Resolution.AMBIGUOUS, candidates, source="trigram")


__all__ = ["Resolution", "Candidate", "ResolveResult", "resolve"]
