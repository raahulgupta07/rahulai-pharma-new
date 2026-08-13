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

import asyncio
import logging
import math
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.db import apply_schema, counts, execute, get_pool, q

logger = logging.getLogger(__name__)

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

# Refuse a catalog file when more than this share of its rows carry no brand
# name. On healthy CityCare data the figure is 0% (the export names every one of
# its 4,892 rows); the backfill stubs that legitimately exist come from
# inventory codes absent from the export, not from the file itself.
CATALOG_FALLBACK_LIMIT = 0.20

# Above this share of stub rows the catalog is not fit to answer questions from:
# search_by_name cannot match a name that is a 13-digit code. Surfaced by
# catalog_health() on /ready and the admin Data page.
CATALOG_STUB_WARN = 0.20


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


def parse_catalog(path: str, report: Optional[Dict] = None) -> List[Dict]:
    """Parse the article export into catalog rows (banner skipped, columns mapped).

    Pass ``report`` to receive parse diagnostics in place: which mapped columns
    the header actually matched, which are missing, and how many rows fell back
    to ``brand_name = article_code``. A load that silently produces nameless
    rows is the failure mode behind the 2026-07-28 field reports — the catalog
    on that host held only stubs, every ``search_by_name`` returned ``[]``, and
    nothing anywhere said so. The parse is unchanged; only the reporting is new.
    """

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
    matched = sorted(c for c in _CATALOG_MAP if c in df.columns)
    df = df.rename(columns=_CATALOG_MAP)

    # Same last-wins rule as parse_inventory, for the same reason: a repeated
    # article_code means the export disagrees with itself, and the later row is
    # the more recent record. First-wins was silent here too.
    by_code: "OrderedDict[str, Dict]" = OrderedDict()
    fallback_codes: set = set()
    duplicates = 0
    for _, r in df.iterrows():
        code = _clean(r.get("article_code"))
        if not code:
            continue
        if code in by_code:
            duplicates += 1
        brand = _clean(r.get("brand_name"))
        if not brand:
            # brand_name is NOT NULL, so a blank has to become something. Using
            # the code keeps the row loadable, but it is indistinguishable from
            # a backfill stub downstream — count it so callers can alarm on it.
            brand = code
            fallback_codes.add(code)
        else:
            # A later named row supersedes an earlier nameless one, so the
            # fallback must be un-counted rather than left to inflate the stub
            # figure that /ready alarms on.
            fallback_codes.discard(code)
        by_code[code] = {
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
        }

    rows = list(by_code.values())
    brand_fallbacks = len(fallback_codes)

    if report is not None:
        report.update({
            "file": Path(path).name,
            "rows_in_file": int(len(df)),
            "rows_parsed": len(rows),
            "columns_matched": matched,
            "columns_missing": sorted(set(_CATALOG_MAP) - set(matched)),
            "brand_fallbacks": brand_fallbacks,
            "duplicates": duplicates,
        })
    if duplicates:
        logger.warning(
            "catalog parse: %s repeated article_code(s) in %s — kept the LAST "
            "row for each. %d rows in file, %d loaded.",
            duplicates, Path(path).name, len(df), len(rows),
        )
    if "Brand Name" not in matched:
        # Every row will land nameless. Loud, because the symptom downstream is
        # a chat agent that answers "not found" for products that are in stock.
        logger.error(
            "catalog parse: no 'Brand Name' column in %s — matched %s. "
            "Every row will load with brand_name = article_code.",
            Path(path).name, matched or "nothing",
        )
    return rows


def parse_inventory(path: str, report: Optional[Dict] = None) -> List[Tuple]:
    """Parse balance_stock into inventory tuples (deduped on article+site)."""

    if _is_csv(path):
        df = pd.read_csv(path, encoding="utf-8-sig")
    else:
        df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]

    # Keyed by (article, site) so a repeat overwrites: LAST occurrence wins.
    #
    # It used to be first-wins (`if key in seen: continue`) and silent. Both
    # halves were wrong. Measured live on 2026-08-13 by sending a file with a
    # repeated (article, site) pair: the later quantity was discarded, the
    # earlier one loaded, and nothing anywhere said so — the check reported
    # "111,605 usable rows" and the load reported 111,604, a difference the
    # operator had no way to explain.
    #
    # Last-wins is the right default for this export. The file carries an
    # ascending `id` column, so a later row is the more recently written
    # record: a correction, or a second movement for the same product at the
    # same branch. Keeping the earlier one means shipping a stock number the
    # partner's own system has already superseded.
    #
    # The count is returned to the caller either way, because the real defect
    # was the silence, not the choice — a duplicate means the partner's export
    # disagrees with itself, and somebody should see that.
    by_key: "OrderedDict[Tuple[str, str], Tuple]" = OrderedDict()
    duplicates = 0
    for _, r in df.iterrows():
        code = _clean(r.get("article_code"))
        site = _clean(r.get("site_code"))
        if not code or not site:
            continue
        key = (code, site)
        if key in by_key:
            duplicates += 1
        try:
            qty = int(float(r.get("stock_qty")))
        except (TypeError, ValueError):
            qty = None
        try:
            price = float(r.get("weighted_cost_price"))
            if math.isnan(price):
                price = None
        except (TypeError, ValueError):
            price = None
        by_key[key] = (code, site, None, qty, price, "MMK")

    if report is not None:
        report.update({
            "file": Path(path).name,
            "rows_in_file": int(len(df)),
            "rows_parsed": len(by_key),
            "duplicates": duplicates,
        })
    if duplicates:
        # WARNING, not debug: the partner's export contradicts itself, and the
        # row counts the operator sees will not add up without this line.
        logger.warning(
            "inventory parse: %s repeated (article, site) row(s) in %s — kept the "
            "LAST value for each. %d rows in file, %d loaded.",
            duplicates, Path(path).name, len(df), len(by_key),
        )
    return list(by_key.values())


# ---- loading ---------------------------------------------------------------


async def ingest_catalog(path: str, mode: str = "full_sync") -> Dict:
    """Load catalog rows from one file. Returns ``{"rows": upserted, "deleted": removed}``.

    Every upserted row is stamped ``last_seen = <run_start>`` (one timestamp
    captured at the top of the call, shared by every row from this file).

    * ``mode="full_sync"`` (default): the file is authoritative. After the upsert, any row
      whose ``last_seen`` is not this run's timestamp was absent from the file, so
      it is discontinued and deleted — ``DELETE ... WHERE last_seen IS DISTINCT
      FROM <run_start>``. This is the "replace the world" semantic WITHOUT a
      ``TRUNCATE``: a truncate would cascade the ``drug_alias`` FK and blow away
      unrelated rows; this touches only the rows the file dropped.
    * ``mode="merge"``: upsert only — add new, update existing, delete NOTHING.
      The historical behaviour; select it on the SFTP page to keep discontinued
      drugs.

    Guard: if the file parses to ZERO rows (empty or partial upload), full_sync
    deletes NOTHING — a delete keyed on a timestamp no row carries would match
    every row and wipe the whole catalog. Upsert + delete run in ONE transaction.
    """

    run_start = datetime.now(timezone.utc)
    report: Dict = {}
    # Parsing 100k+ rows with pandas is CPU-bound; keep it off the event loop.
    rows = await asyncio.to_thread(parse_catalog, path, report)
    if not rows:
        # An empty/partial upload is never authoritative. Skipping the delete here
        # is the guard that stops a bad file from emptying the catalog in
        # full_sync — see tests/test_catalog_full_sync.py.
        if mode == "full_sync":
            logger.warning(
                "full_sync: parsed 0 catalog rows from %s; skipping delete "
                "(empty/partial upload guard)", Path(path).name,
            )
        return {"rows": 0, "deleted": 0, "report": report, "ok": False}

    # A file whose every row is nameless is never authoritative. Loading it
    # would overwrite good rows with codes, and in full_sync the delete would
    # then discard everything the file did not name. Refuse instead.
    fallback_ratio = report.get("brand_fallbacks", 0) / max(len(rows), 1)
    if fallback_ratio > CATALOG_FALLBACK_LIMIT:
        logger.error(
            "catalog ingest REFUSED for %s: %.1f%% of %s rows have no brand name "
            "(limit %.0f%%). Columns matched: %s",
            Path(path).name, fallback_ratio * 100, len(rows),
            CATALOG_FALLBACK_LIMIT * 100, report.get("columns_matched"),
        )
        return {
            "rows": 0, "deleted": 0, "report": report, "ok": False,
            "error": (
                f"refused: {fallback_ratio:.0%} of rows have no brand name — "
                f"check the header row and column names"
            ),
        }

    fields = _CATALOG_FIELDS
    cols = ", ".join(fields) + ", last_seen"
    placeholders = ", ".join(f"${i+1}" for i in range(len(fields))) + f", ${len(fields)+1}"
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields if f != "article_code")
    updates += ", last_seen = EXCLUDED.last_seen"
    # Reset the vector to NULL only when embedded source text actually changed,
    # so embed_catalog(only_missing=True) re-embeds it. IS DISTINCT FROM treats
    # NULLs sanely; unchanged rows keep their embedding (no needless re-embed).
    _EMBED_SRC = ("brand_name", "generic_name", "composition", "indication")
    changed = " OR ".join(f"catalog.{f} IS DISTINCT FROM EXCLUDED.{f}" for f in _EMBED_SRC)
    sql = (
        f"INSERT INTO catalog ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (article_code) DO UPDATE SET {updates}, "
        f"embedding = CASE WHEN {changed} THEN NULL ELSE catalog.embedding END"
    )
    params = [tuple(r[f] for f in fields) + (run_start,) for r in rows]

    pool = await get_pool()
    deleted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, params)
            if mode == "full_sync":
                gone = await conn.fetch(
                    "DELETE FROM catalog WHERE last_seen IS DISTINCT FROM $1 "
                    "RETURNING article_code",
                    run_start,
                )
                deleted = len(gone)
    if deleted:
        logger.info("full_sync removed %s discontinued catalog rows", deleted)
    logger.info(
        "catalog ingest %s: %s rows, %s deleted, %s brand fallbacks, columns %s",
        Path(path).name, len(rows), deleted,
        report.get("brand_fallbacks"), report.get("columns_matched"),
    )
    return {
        "rows": len(rows),
        "deleted": deleted,
        "report": report,
        # Lifted out of the report so the watcher's operator-facing line can
        # reach it without knowing the report's shape.
        "duplicates": int(report.get("duplicates") or 0),
        "ok": True,
    }


async def ingest_inventory(path: str, report: Optional[Dict] = None) -> int:
    """Full-replace inventory (truncate + bulk COPY), then backfill stubs.

    Returns inventory row count. Pass ``report`` to receive parse diagnostics
    in place — ``rows_in_file``, ``rows_parsed`` and ``duplicates`` — which is
    how the operator-facing ingest event explains why those two counts differ.
    """

    records = await asyncio.to_thread(parse_inventory, path, report)
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

    # CONCURRENTLY avoids the ACCESS EXCLUSIVE lock that blocks all readers,
    # but it is illegal until the MV has been populated once (both MVs are
    # created WITH NO DATA). Fall back to a plain refresh in that case.
    for mv in ("mv_store_summary", "mv_article_summary"):
        try:
            await execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}")
        except Exception:  # noqa: BLE001 - not yet populated, or view missing
            try:
                await execute(f"REFRESH MATERIALIZED VIEW {mv}")
            except Exception:  # noqa: BLE001 - view may not exist yet
                pass


async def catalog_health() -> Dict:
    """Report how much of the catalog is nameless stub rows.

    A stub is ``brand_name = article_code``: a row carrying no product name and
    no clinical text, so ``search_by_name`` can never match it and
    ``embed_catalog`` skips it. A handful is normal — the article export does
    not cover every stocked code. A catalog that is *mostly* stubs means the
    export never loaded, and the only visible symptom is a chat agent that
    answers "not found" for products sitting on the shelf. That is exactly what
    the 2026-07-28 field reports describe, and nothing in the app said a word
    about it. This makes it a number anyone can read.
    """

    rows = await q(
        """
        SELECT count(*)                                        AS total,
               count(*) FILTER (WHERE brand_name = article_code) AS stubs,
               count(*) FILTER (WHERE composition IS NOT NULL)   AS with_composition,
               count(*) FILTER (WHERE dosage IS NOT NULL)        AS with_dosage
          FROM catalog
        """
    )
    r = rows[0] if rows else {"total": 0, "stubs": 0, "with_composition": 0, "with_dosage": 0}
    total = r["total"] or 0
    stubs = r["stubs"] or 0
    ratio = (stubs / total) if total else 0.0
    return {
        "total": total,
        "stubs": stubs,
        "stub_ratio": round(ratio, 4),
        "with_composition": r["with_composition"] or 0,
        "with_dosage": r["with_dosage"] or 0,
        "healthy": total > 0 and ratio <= CATALOG_STUB_WARN,
    }


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
    ``only_missing`` is True, only rows with NULL embedding are processed. The
    catalog upsert resets embedding to NULL whenever the embedded source text
    changes, so daily reloads re-embed exactly the new and changed articles.

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


async def build_edges_safe() -> Optional[int]:
    """Rebuild the drug graph. Returns the edge count, or None if it failed.

    The graph is an optional enrichment (substitutes, generics), so a failure
    here must not fail an ingest that already landed its rows.
    """

    try:
        from app.graph import build_edges

        return int((await build_edges()).get("total", 0))
    except Exception:  # noqa: BLE001 - graph optional
        logger.exception("graph rebuild failed after ingest")
        return None


class FileRejected(ValueError):
    """A file was refused. Carries the report that explains why.

    A ``ValueError`` subclass on purpose: every existing caller catches
    ``ValueError`` (or ``Exception``) and reads ``str(exc)``, which still gives
    the same one-line summary as before. The difference is that a caller which
    wants to *store* the reason — the console's file history — can reach the
    structured errors, warnings and row counts instead of re-parsing prose.
    """

    def __init__(self, message: str, report: Optional[Dict] = None):
        super().__init__(message)
        self.report = report or {}


async def ingest_file(
    path: str, catalog_mode: str = "full_sync", allow_shrink: bool = False
) -> Dict:
    """Validate one file, then load it. Rejects raise :class:`FileRejected`.

    Both kinds REPLACE rather than merge — the newest file is authoritative:

    * inventory: ``TRUNCATE`` + bulk copy, a clean snapshot every time.
    * catalog: upsert every row in the file, then delete every row the file did
      not contain. The end state is identical to truncate-and-reload, but it is
      NOT a truncate on purpose. ``drug_alias`` carries an FK to ``catalog``, so
      a truncate would cascade and destroy the learned aliases; and a truncate
      would drop every ``embedding``, forcing a full ~3-minute re-embed of 4,892
      rows on every upload instead of only the rows whose text changed.

    ``allow_shrink`` overrides the guard that refuses a file which would delete
    more than half the existing rows.
    """

    # Validate BEFORE anything destructive runs. Both loaders replace rather
    # than merge, so a bad file is not a failed import — it is data loss.
    from app.validation import check_shrink, validate_file

    report = await check_shrink(
        await asyncio.to_thread(validate_file, path), allow_shrink=allow_shrink
    )
    if not report.ok:
        raise FileRejected(report.summary, report.as_dict())
    for w in report.warnings:
        logger.warning("%s: %s", Path(path).name, w)

    kind = detect_kind(path)
    if kind == "catalog":
        res = await ingest_catalog(path, mode=catalog_mode)
        if not res.get("ok", True):
            # A refused or empty catalog file must NOT look like a success.
            # The watcher archives whatever ingest_file returns without raising,
            # so swallowing this would file a rejected upload away as "done" —
            # the same silent-success bug that let an unrecognised filename
            # through before (see the comment in watcher.scan_once).
            raise FileRejected(
                res.get("error")
                or "catalog file parsed to 0 rows (empty or partial upload)",
                {**report.as_dict(), "parse": res.get("report", {})},
            )
        return {"file": Path(path).name, "kind": "catalog",
                "rows": res["rows"], "deleted": res["deleted"],
                "duplicates": res.get("duplicates", 0),
                "report": res.get("report", {}),
                "validation": report.as_dict()}
    if kind == "inventory":
        parse_report: Dict = {}
        n = await ingest_inventory(path, report=parse_report)
        return {"file": Path(path).name, "kind": "inventory", "rows": n,
                "duplicates": int(parse_report.get("duplicates") or 0),
                "report": parse_report,
                "validation": report.as_dict()}
    return {"file": Path(path).name, "kind": "unknown", "rows": 0,
            "validation": report.as_dict()}


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
        result["catalog_loaded"] = (await ingest_catalog(cat))["rows"]
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
        result["catalog"] = (await ingest_catalog(catalog_path))["rows"]
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
