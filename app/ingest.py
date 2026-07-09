"""Real-data ingestion: parse CityCare Excel/CSV exports and load them.

Two source files (matched by filename, case-insensitive; .xlsx or .csv):

* **catalog** — "articles-export*". In the xlsx export a banner occupies the
  first rows; the real header is row 5 (``skiprows=4``). CSV exports may or may
  not carry the banner, so the header row is auto-detected. Columns are merged
  into ``catalog``.
* **inventory** — "balance_stock*". Clean tabular export; ``price`` comes
  from ``weighted_cost_price``.

Load strategy = "replace old, add new":
* catalog   -> upsert/merge (add new articles, update existing).
* inventory -> full replace (truncate + bulk COPY) — a fresh stock snapshot.

Inventory load uses asyncpg ``copy_records_to_table`` so 100k+ rows load fast.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.db import apply_schema, counts, execute, get_pool, q

# Column maps from source headers -> our schema fields.
_CATALOG_MAP = {
    "Article Code": "article_code",
    "Brand Name": "brand_name",
    "Generic Name": "generic_name",
    "Composition": "composition",
    "Category": "category",
    "Indication": "indication",
    "Dosage": "dosage",
    "Side Effect": "side_effect",
    "MM_Reg": "mm_reg",
    "MM_Label": "mm_label",
    "Status": "status",
}

_CATALOG_FIELDS = [
    "article_code", "brand_name", "generic_name", "composition", "category",
    "indication", "dosage", "side_effect", "mm_reg", "mm_label", "status",
]

_INVENTORY_FIELDS = ["article_code", "site_code", "site_name", "stock_qty", "price", "uom"]


# ---- helpers ---------------------------------------------------------------


def _clean(value) -> Optional[str]:
    """Trim a cell to a clean string, or ``None`` for blanks/NaN."""

    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return s or None


def detect_kind(filename: str) -> Optional[str]:
    """Classify a filename as 'catalog', 'inventory', or None."""

    name = Path(filename).name.lower()
    if "article" in name:
        return "catalog"
    if "balance" in name or "stock" in name or "inventory" in name:
        return "inventory"
    return None


# ---- parsing ---------------------------------------------------------------


def _is_csv(path: str) -> bool:
    """True when the path points at a CSV file (by extension)."""

    return Path(path).suffix.lower() == ".csv"


def parse_catalog(path: str) -> List[Dict]:
    """Parse the article export into catalog rows (banner skipped, columns mapped)."""

    if _is_csv(path):
        # utf-8-sig tolerates a BOM; Burmese text needs UTF-8 either way.
        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
            df.columns = [str(c).strip() for c in df.columns]
            if not any(c in df.columns for c in _CATALOG_MAP):
                raise ValueError("expected header not in row 0")
        except (ValueError, pd.errors.ParserError):
            # Header not in row 0 — assume the xlsx-style 4-row banner.
            df = pd.read_csv(path, skiprows=4, encoding="utf-8-sig")
    else:
        df = pd.read_excel(path, skiprows=4)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns=_CATALOG_MAP)

    rows: List[Dict] = []
    seen = set()
    for _, r in df.iterrows():
        code = _clean(r.get("article_code"))
        if not code or code in seen:
            continue
        seen.add(code)
        brand = _clean(r.get("brand_name")) or code  # brand_name is NOT NULL
        rows.append({
            "article_code": code,
            "brand_name": brand,
            "generic_name": _clean(r.get("generic_name")),
            "composition": _clean(r.get("composition")),
            "category": _clean(r.get("category")),
            "indication": _clean(r.get("indication")),
            "dosage": _clean(r.get("dosage")),
            "side_effect": _clean(r.get("side_effect")),
            "mm_reg": _clean(r.get("mm_reg")),
            "mm_label": _clean(r.get("mm_label")),
            "status": _clean(r.get("status")),
        })
    return rows


def parse_inventory(path: str) -> List[Tuple]:
    """Parse balance_stock into inventory tuples (deduped on article+site)."""

    if _is_csv(path):
        df = pd.read_csv(path, encoding="utf-8-sig")
    else:
        df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]

    records: List[Tuple] = []
    seen = set()
    for _, r in df.iterrows():
        code = _clean(r.get("article_code"))
        site = _clean(r.get("site_code"))
        if not code or not site:
            continue
        key = (code, site)
        if key in seen:
            continue
        seen.add(key)
        try:
            qty = int(float(r.get("stock_qty")))
        except (TypeError, ValueError):
            qty = 0
        try:
            price = float(r.get("weighted_cost_price"))
        except (TypeError, ValueError):
            price = 0.0
        records.append((code, site, None, qty, price, "MMK"))
    return records


# ---- loading ---------------------------------------------------------------


async def ingest_catalog(path: str) -> int:
    """Upsert catalog rows (merge: add new, update existing). Returns row count."""

    rows = parse_catalog(path)
    if not rows:
        return 0
    cols = ", ".join(_CATALOG_FIELDS)
    placeholders = ", ".join(f"${i+1}" for i in range(len(_CATALOG_FIELDS)))
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in _CATALOG_FIELDS if f != "article_code")
    sql = (
        f"INSERT INTO catalog ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (article_code) DO UPDATE SET {updates}"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, [tuple(r[f] for f in _CATALOG_FIELDS) for r in rows])
    return len(rows)


async def ingest_inventory(path: str) -> int:
    """Full-replace inventory (truncate + bulk COPY), then backfill stubs.

    Returns inventory row count.
    """

    records = parse_inventory(path)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("TRUNCATE inventory")
            if records:
                await conn.copy_records_to_table(
                    "inventory", records=records, columns=_INVENTORY_FIELDS
                )
    await backfill_catalog_stubs()
    return len(records)


async def refresh_views() -> None:
    """Refresh materialized views after a data change (best-effort)."""

    for mv in ("mv_store_summary", "mv_article_summary"):
        try:
            await execute(f"REFRESH MATERIALIZED VIEW {mv}")
        except Exception:  # noqa: BLE001 - view may not exist yet
            pass


async def backfill_catalog_stubs() -> int:
    """Insert a catalog stub for every inventory article_code missing from catalog.

    The real article export is incomplete (thousands of stocked codes are
    absent). Stubbing them (brand_name = the code) makes catalog a superset of
    inventory.article_code, so every INNER JOIN matches — no LEFT JOIN, no NULL
    brands, no hidden stock. Idempotent. Returns number of stubs created.
    """

    rows = await q(
        """
        INSERT INTO catalog (article_code, brand_name)
        SELECT DISTINCT i.article_code, i.article_code
          FROM inventory i
          LEFT JOIN catalog c USING (article_code)
         WHERE c.article_code IS NULL
        ON CONFLICT (article_code) DO NOTHING
        RETURNING article_code
        """
    )
    return len(rows)


async def embed_catalog(only_missing: bool = True, batch_size: int = 64) -> int:
    """Generate embeddings for catalog rows that have real text.

    Builds a text blob (brand + generic + composition + indication) per article
    and stores its gemini-embedding-2 vector. Skips stub rows (brand == code,
    no clinical text) — embedding a bare code adds no semantic value. When
    ``only_missing`` is True, only rows with NULL embedding are processed, so
    daily reloads re-embed just new/changed articles.

    Returns the number of rows embedded.
    """

    from app.embeddings import embed_many, to_pgvector

    where = "embedding IS NULL AND " if only_missing else ""
    rows = await q(
        f"""
        SELECT article_code,
               concat_ws(' ', brand_name, generic_name, composition, indication) AS text
          FROM catalog
         WHERE {where}(generic_name IS NOT NULL OR indication IS NOT NULL)
        """
    )
    if not rows:
        return 0

    pool = await get_pool()
    total = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        vectors = await embed_many([r["text"] for r in chunk], batch_size=batch_size)
        params = [(r["article_code"], to_pgvector(v)) for r, v in zip(chunk, vectors)]
        async with pool.acquire() as conn:
            await conn.executemany(
                "UPDATE catalog SET embedding = $2::vector WHERE article_code = $1", params
            )
        total += len(chunk)
    return total


async def ingest_file(path: str) -> Dict:
    """Dispatch one file to the right loader based on its name."""

    kind = detect_kind(path)
    if kind == "catalog":
        n = await ingest_catalog(path)
        return {"file": Path(path).name, "kind": "catalog", "rows": n}
    if kind == "inventory":
        n = await ingest_inventory(path)
        return {"file": Path(path).name, "kind": "inventory", "rows": n}
    return {"file": Path(path).name, "kind": "unknown", "rows": 0}


def _find_latest(data_dir: str, kind: str) -> Optional[str]:
    """Return the newest matching xlsx/csv for a kind in ``data_dir``, or None."""

    d = Path(data_dir)
    if not d.is_dir():
        return None
    candidates = list(d.glob("*.xlsx")) + list(d.glob("*.csv"))
    matches = [p for p in candidates if detect_kind(p.name) == kind]
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


async def reload_from_data_dir(data_dir: Optional[str] = None) -> Dict:
    """Ingest whatever article/balance files exist in ``data_dir``.

    Safe no-op per file: only files that exist are loaded, so a missing
    inventory file never truncates existing stock. Returns load result + counts.
    """

    from app.config import get_settings

    data_dir = data_dir or get_settings().data_dir
    cat = _find_latest(data_dir, "catalog")
    inv = _find_latest(data_dir, "inventory")

    try:
        await counts()
    except Exception:  # noqa: BLE001 - tables missing
        await apply_schema()

    result: Dict = {"catalog_file": Path(cat).name if cat else None,
                    "inventory_file": Path(inv).name if inv else None}
    if cat:
        result["catalog_loaded"] = await ingest_catalog(cat)
    if inv:
        result["inventory_loaded"] = await ingest_inventory(inv)
    await refresh_views()
    try:
        from app.graph import build_edges

        result["graph_edges"] = (await build_edges()).get("total", 0)
    except Exception:  # noqa: BLE001 - graph optional
        pass
    # Embed new/changed catalog rows — in the background so uploads return fast.
    try:
        from app.config import get_settings

        if get_settings().embed_in_background:
            import asyncio

            asyncio.create_task(embed_catalog(only_missing=True))
            result["embedding"] = "scheduled (background)"
        else:
            result["embedding"] = await embed_catalog(only_missing=True)
    except Exception:  # noqa: BLE001
        pass
    result.update(await counts())
    return result


async def ingest_paths(catalog_path: Optional[str], inventory_path: Optional[str]) -> Dict:
    """Apply schema if needed, then load catalog (merge) + inventory (replace)."""

    # Ensure tables exist (first run / fresh DB).
    try:
        await counts()
    except Exception:  # noqa: BLE001 - tables missing
        await apply_schema()

    result: Dict = {}
    if catalog_path:
        result["catalog"] = await ingest_catalog(catalog_path)
    if inventory_path:
        result["inventory"] = await ingest_inventory(inventory_path)
    result.update(await counts())
    return result


__all__ = [
    "detect_kind",
    "parse_catalog",
    "parse_inventory",
    "ingest_catalog",
    "ingest_inventory",
    "ingest_file",
    "ingest_paths",
]
