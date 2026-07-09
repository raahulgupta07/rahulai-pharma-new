"""Embeddings via OpenRouter (google/gemini-embedding-2, 3072-dim).

Used for semantic catalog search (Burmese symptom -> drug). Query embeddings
are cached in Redis (same question -> reuse vector) to cut cost/latency at
scale. Catalog embeddings are generated in batches at ingest time.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Optional

import httpx

from app.config import get_settings

_URL = "https://openrouter.ai/api/v1/embeddings"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_settings().openrouter_api_key}",
        "Content-Type": "application/json",
    }


async def embed_many(texts: List[str], batch_size: int = 64) -> List[List[float]]:
    """Embed a list of texts (batched). Returns one vector per input, in order."""

    model = get_settings().embedding_model
    out: List[List[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            r = await client.post(
                _URL, headers=_headers(), json={"model": model, "input": chunk}
            )
            r.raise_for_status()
            data = r.json()["data"]
            # API may not preserve order; sort by index to be safe.
            data.sort(key=lambda d: d.get("index", 0))
            out.extend(d["embedding"] for d in data)
    return out


async def embed_one(text: str) -> List[float]:
    """Embed a single text."""

    return (await embed_many([text]))[0]


def to_pgvector(vec: List[float]) -> str:
    """Format a vector as a pgvector literal string: '[1,2,3]'."""

    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def embed_query_cached(text: str) -> List[float]:
    """Embed a query, caching the vector in Redis (keyed by normalised text)."""

    from app.cache import get_cached, set_cached

    norm = " ".join(text.strip().lower().split())
    key = "pharmacy:emb:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()
    hit = await get_cached(key)
    if hit:
        return json.loads(hit)
    vec = await embed_one(text)
    await set_cached(key, json.dumps(vec), ttl=86400)  # 1 day
    return vec


__all__ = ["embed_many", "embed_one", "embed_query_cached", "to_pgvector"]
