"""Sync the client's MySQL app database into our Postgres (read-only on their side).

Option A of the integration plan: we NEVER write to the client's MySQL. A
read-only account runs two operator-defined SELECTs (``mysql_catalog_sql`` /
``mysql_inventory_sql`` in config) whose column **aliases** map their schema onto
ours. We upsert the result into our ``catalog`` / ``inventory`` tables, then run
the normal post-load pipeline (refresh views, embed new rows, rebuild graph).

The whole module imports safely even when the ``aiomysql`` driver is absent —
the import is done lazily inside :func:`sync_mysql`, which raises a clear error.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Sequence

from app.config import get_settings
from app.db import counts, get_pool

logger = logging.getLogger(__name__)

# Columns we are willing to write, per target table. First entry(/entries) is the
# conflict key. Anything the source SELECT aliases outside this set is ignored.
_CATALOG_PK = ("article_code",)
_CATALOG_COLS = (
    "article_code", "brand_name", "generic_name", "composition", "category",
    "indication", "dosage", "side_effect", "mm_reg", "mm_label", "status",
)
_INVENTORY_PK = ("article_code", "site_code")
_INVENTORY_COLS = ("article_code", "site_code", "site_name", "stock_qty", "price", "uom")


def _clean(v: Any) -> Any:
    """Normalise a MySQL cell: blank strings -> None, trim whitespace."""

    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _build_upsert(table: str, cols: Sequence[str], pk: Sequence[str]) -> str:
    """Build an INSERT ... ON CONFLICT upsert for the given columns."""

    placeholders = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
    conflict = ", ".join(pk)
    if updates:
        tail = f"DO UPDATE SET {updates}"
    else:  # all columns are key columns — nothing to update
        tail = "DO NOTHING"
    return (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) {tail}"
    )


async def _upsert(table: str, allowed: Sequence[str], pk: Sequence[str], rows: List[Dict[str, Any]]) -> int:
    """Upsert ``rows`` (list of dicts) into ``table``, keyed by ``pk``.

    Only columns present in BOTH the source rows and ``allowed`` are written, so
    the operator's SELECT can supply a subset. Rows missing any key column are
    skipped. Returns the number of rows attempted.
    """

    if not rows:
        return 0
    # Columns actually supplied by the source (intersect with allowed, keep order)
    present = [c for c in allowed if c in rows[0]]
    for k in pk:
        if k not in present:
            raise ValueError(f"{table} sync: source query is missing key column '{k}'")

    sql = _build_upsert(table, present, pk)
    tuples = []
    for r in rows:
        if any(_clean(r.get(k)) is None for k in pk):
            continue  # can't upsert without a full key
        tuples.append(tuple(_clean(r.get(c)) for c in present))

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, tuples)
    return len(tuples)


async def _fetch_mysql(sql: str) -> List[Dict[str, Any]]:
    """Run one SELECT against the configured MySQL source and return dict rows."""

    try:
        import aiomysql  # lazy: missing driver shouldn't break module import
    except ImportError as exc:  # noqa: F841
        raise RuntimeError(
            "aiomysql is not installed — add it to requirements and install it "
            "to enable MySQL sync (pip install aiomysql)."
        ) from exc

    s = get_settings()
    conn = await aiomysql.connect(
        host=s.mysql_host, port=int(s.mysql_port), user=s.mysql_user,
        password=s.mysql_password, db=s.mysql_db, charset="utf8mb4",
        connect_timeout=10,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql)
            return list(await cur.fetchall())
    finally:
        conn.close()


async def sync_mysql(run_pipeline: bool = True) -> Dict[str, Any]:
    """Pull catalog + inventory from the client's MySQL into Postgres.

    Steps: read both SELECTs -> upsert -> (optional) refresh views, embed new
    catalog rows, rebuild the drug graph. Returns a per-step report with timings
    and final row counts. The client's MySQL is only ever read.
    """

    s = get_settings()
    if not s.mysql_sync_enabled:
        return {"ok": False, "error": "mysql_sync_enabled is false"}
    if not (s.mysql_host and s.mysql_db):
        return {"ok": False, "error": "mysql_host / mysql_db not configured"}

    report: Dict[str, Any] = {"ok": True, "steps": {}}
    t0 = time.time()

    try:
        cat_rows = await _fetch_mysql(s.mysql_catalog_sql)
        inv_rows = await _fetch_mysql(s.mysql_inventory_sql)
        report["steps"]["fetch"] = {
            "catalog": len(cat_rows), "inventory": len(inv_rows),
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("mysql fetch failed")
        return {"ok": False, "error": f"fetch failed: {exc}"}

    try:
        t = time.time()
        c = await _upsert("catalog", _CATALOG_COLS, _CATALOG_PK, cat_rows)
        i = await _upsert("inventory", _INVENTORY_COLS, _INVENTORY_PK, inv_rows)
        report["steps"]["upsert"] = {"catalog": c, "inventory": i, "ms": int((time.time() - t) * 1000)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("upsert failed")
        return {"ok": False, "error": f"upsert failed: {exc}"}

    if run_pipeline:
        # Reuse the existing post-load pipeline. Each step is best-effort so a
        # slow embed/graph build never loses the freshly-synced rows.
        try:
            from app.ingest import refresh_views, embed_catalog
            t = time.time()
            await refresh_views()
            embedded = await embed_catalog(only_missing=True)
            report["steps"]["embed"] = {"new_vectors": embedded, "ms": int((time.time() - t) * 1000)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-sync embed step skipped: %s", exc)
            report["steps"]["embed"] = {"error": str(exc)}
        try:
            from app.graph import build_edges
            t = time.time()
            edges = await build_edges()
            report["steps"]["graph"] = {"edges": edges, "ms": int((time.time() - t) * 1000)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-sync graph step skipped: %s", exc)
            report["steps"]["graph"] = {"error": str(exc)}

    report["counts"] = await counts()
    report["total_ms"] = int((time.time() - t0) * 1000)
    return report


__all__ = ["sync_mysql"]
