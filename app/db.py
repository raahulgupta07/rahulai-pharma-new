"""Data layer: async Postgres access for the pharmacy agent.

Provides a lazily-created module-level :mod:`asyncpg` connection pool plus the
thin query helpers (:func:`q`, :func:`execute`) that every tool relies on, and
loaders that populate the backing tables from Excel spreadsheets or a seed SQL
file.

Connection settings come from :func:`app.config.get_settings` — the
``postgres_url`` (a ``postgresql://`` DSN, which asyncpg accepts directly).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Dict, List, Optional

import asyncpg
import pandas as pd

from app.config import get_settings

# ---------------------------------------------------------------------------
# Connection pool (module-level, lazily created)
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    """Create the shared :class:`asyncpg.Pool` if it does not already exist.

    Reads the DSN from :func:`app.config.get_settings`. Idempotent: repeated
    calls return the already-created pool.

    Returns:
        The module-level :class:`asyncpg.Pool`.
    """

    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(dsn=settings.postgres_url)
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Return the shared pool, creating it via :func:`init_pool` if needed."""

    if _pool is None:
        return await init_pool()
    return _pool


async def close_pool() -> None:
    """Close the shared pool and reset the module global."""

    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def q(sql: str, *args) -> List[Dict]:
    """Run a parameterised SELECT and return rows as plain dicts.

    This is THE query helper every tool uses.

    Args:
        sql: SQL using ``$1, $2, ...`` positional placeholders.
        *args: Positional bind parameters.

    Returns:
        A list of rows, each a ``dict`` keyed by column name.
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def execute(sql: str, *args) -> str:
    """Run a non-SELECT statement (DDL/INSERT/UPDATE/DELETE).

    Args:
        sql: SQL statement, optionally using ``$1, $2, ...`` placeholders.
        *args: Positional bind parameters.

    Returns:
        The asyncpg status string (e.g. ``"INSERT 0 1"``).
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def run_sql_file(path: str) -> None:
    """Execute every statement in a ``.sql`` file as a single script.

    asyncpg's ``conn.execute`` accepts multi-statement strings when no bind
    parameters are supplied, so the whole file is run at once.

    Args:
        path: Filesystem path to the ``.sql`` file.
    """

    script = Path(path).read_text(encoding="utf-8")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(script)


async def apply_schema() -> None:
    """Apply ``app/schema.sql`` (resolved relative to this file)."""

    schema_path = Path(__file__).parent / "schema.sql"
    await run_sql_file(str(schema_path))


# ---------------------------------------------------------------------------
# Excel loading
# ---------------------------------------------------------------------------

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with columns lowercased + snake_cased."""

    def norm(name: object) -> str:
        s = str(name).strip().lower()
        s = re.sub(r"[\s\-]+", "_", s)
        s = re.sub(r"[^0-9a-z_]", "", s)
        return s

    out = df.copy()
    out.columns = [norm(c) for c in out.columns]
    return out


# Columns expected in each spreadsheet.
_CATALOG_COLS = [
    "article_code",
    "brand_name",
    "generic_name",
    "composition",
    "category",
    "pack_size",
]
_INVENTORY_COLS = [
    "article_code",
    "site_code",
    "site_name",
    "stock_qty",
    "price",
    "uom",
]


def _coerce_str(value: object) -> Optional[str]:
    """Coerce *value* to a stripped string, or ``None`` when missing."""

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).strip()
    return s if s != "" else None


def _coerce_int(value: object) -> int:
    """Coerce *value* to int, defaulting to ``0`` on missing/invalid."""

    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: object) -> float:
    """Coerce *value* to float, defaulting to ``0.0`` on missing/invalid."""

    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def load_excel_to_postgres(
    catalog_path: str, inventory_path: str
) -> Dict[str, int]:
    """Load catalog + inventory spreadsheets into Postgres.

    Applies the schema, reads both Excel files with pandas, normalises and
    coerces columns tolerantly (missing columns are skipped/defaulted), then
    bulk-upserts the rows.

    Args:
        catalog_path: Path to the product catalog ``.xlsx``.
        inventory_path: Path to the per-site inventory ``.xlsx``.

    Returns:
        Mapping ``{"catalog_rows": N, "inventory_rows": M}`` with the actual
        row counts in each table afterwards.
    """

    await apply_schema()

    catalog_df = _normalize_columns(pd.read_excel(catalog_path))
    inventory_df = _normalize_columns(pd.read_excel(inventory_path))

    # --- Build catalog rows ------------------------------------------------
    catalog_rows = []
    for _, row in catalog_df.iterrows():
        code = _coerce_str(row.get("article_code"))
        if code is None:
            continue
        catalog_rows.append(
            (
                code,
                _coerce_str(row.get("brand_name")),
                _coerce_str(row.get("generic_name")),
                _coerce_str(row.get("composition")),
                _coerce_str(row.get("category")),
                _coerce_str(row.get("pack_size")),
            )
        )

    # --- Build inventory rows ----------------------------------------------
    inventory_rows = []
    for _, row in inventory_df.iterrows():
        code = _coerce_str(row.get("article_code"))
        site = _coerce_str(row.get("site_code"))
        if code is None or site is None:
            continue
        inventory_rows.append(
            (
                code,
                site,
                _coerce_str(row.get("site_name")),
                _coerce_int(row.get("stock_qty")),
                _coerce_float(row.get("price")),
                _coerce_str(row.get("uom")),
            )
        )

    catalog_sql = """
        INSERT INTO catalog
            (article_code, brand_name, generic_name, composition, category, pack_size)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (article_code) DO UPDATE SET
            brand_name = EXCLUDED.brand_name,
            generic_name = EXCLUDED.generic_name,
            composition = EXCLUDED.composition,
            category = EXCLUDED.category,
            pack_size = EXCLUDED.pack_size
    """
    inventory_sql = """
        INSERT INTO inventory
            (article_code, site_code, site_name, stock_qty, price, uom)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (article_code, site_code) DO UPDATE SET
            site_name = EXCLUDED.site_name,
            stock_qty = EXCLUDED.stock_qty,
            price = EXCLUDED.price,
            uom = EXCLUDED.uom
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if catalog_rows:
                await conn.executemany(catalog_sql, catalog_rows)
            if inventory_rows:
                await conn.executemany(inventory_sql, inventory_rows)

    return await counts()


# ---------------------------------------------------------------------------
# Seed SQL loading
# ---------------------------------------------------------------------------

async def load_seed_sql(seed_path: Optional[str] = None) -> Dict[str, int]:
    """Apply the schema then run a seed ``.sql`` file.

    Args:
        seed_path: Path to the seed file. Defaults to
            ``<project root>/data/seed_sample.sql``.

    Returns:
        Mapping ``{"catalog_rows": N, "inventory_rows": M}``.
    """

    await apply_schema()
    if seed_path is None:
        seed_path = str(Path(__file__).parent.parent / "data" / "seed_sample.sql")
    await run_sql_file(seed_path)
    return await counts()


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

async def counts() -> Dict[str, int]:
    """Return current row counts for the catalog and inventory tables."""

    catalog_rows = await q("SELECT count(*) AS n FROM catalog")
    inventory_rows = await q("SELECT count(*) AS n FROM inventory")
    return {
        "catalog_rows": int(catalog_rows[0]["n"]) if catalog_rows else 0,
        "inventory_rows": int(inventory_rows[0]["n"]) if inventory_rows else 0,
    }


__all__ = [
    "init_pool",
    "get_pool",
    "close_pool",
    "q",
    "execute",
    "run_sql_file",
    "apply_schema",
    "load_excel_to_postgres",
    "load_seed_sql",
    "counts",
]


if __name__ == "__main__":

    async def _main() -> None:
        # Load real article/balance xlsx from the configured data dir.
        from app.ingest import reload_from_data_dir

        print(await reload_from_data_dir())
        await close_pool()

    asyncio.run(_main())
