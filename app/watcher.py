"""SFTP drop-folder watcher + ingest.

Users upload the article / balance_stock xlsx or csv over SFTP into the incoming dir.
This module picks them up, loads them (catalog merge, inventory full-replace),
busts the query cache, and files the processed upload away.

Layout under ``incoming_dir``:
    <incoming>/                 <- users drop *.xlsx / *.csv here (SFTP)
    <incoming>/archive/         <- successfully processed files (timestamped)
    <incoming>/failed/          <- files that errored / unrecognised

A two-pass stability check (size unchanged between polls) avoids ingesting a
file that is still uploading. ``scan_once`` runs one pass (used by the manual
endpoint); ``watch`` loops forever (the ingest-worker service).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List

from app import ingest_events as ev
from app.cache import (
    bump_data_version,
    close_client,
    get_catalog_mode,
    get_ingest_enabled,
    get_poll_seconds,
)
from app.config import get_settings
from app.db import close_pool
from app.ingest import FileRejected, detect_kind, ingest_file

logger = logging.getLogger("pharmacy.watcher")


def _dirs() -> Dict[str, Path]:
    base = Path(get_settings().incoming_dir)
    d = {"incoming": base, "archive": base / "archive", "failed": base / "failed"}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _pending(incoming: Path) -> List[Path]:
    """Top-level *.xlsx / *.csv files awaiting processing (skips archive/failed subdirs)."""

    files = list(incoming.glob("*.xlsx")) + list(incoming.glob("*.csv"))
    return [p for p in files if p.is_file()]


def _stamp(name: str) -> str:
    return f"{int(time.time())}_{name}"


# ---- sentences for the file history ----------------------------------------
#
# The console renders these verbatim, so they are written for a pharmacy
# operator, not for whoever wrote the loader: "stock levels", not "inventory
# table"; "saved answers", not "data_version".

_KIND_WORDS = {"catalog": "the product list", "inventory": "stock levels"}


def _checked_line(report: Dict) -> str:
    """"Checked" plus everything the validator noticed but let through.

    The warnings and notes are the interesting half. "79 rows have a negative
    stock quantity (loaded as-is)" is the sentence that answers a pharmacist
    asking why a shelf reads below zero — and until now it went to the
    container's log and nowhere a human would ever look.
    """

    report = report or {}
    stats = report.get("stats") or {}
    bits = ["Checked."]

    rows = stats.get("usable_rows")
    if rows is not None:
        bits.append(f"{int(rows):,} usable rows.")
    sites = stats.get("distinct_sites")
    if sites:
        bits.append(f"{int(sites):,} branches.")

    # Warnings first: they are the ones closest to being a rejection.
    for line in list(report.get("warnings") or []) + list(report.get("notes") or []):
        bits.append(line if line.endswith(".") else line + ".")
    return " ".join(bits)


def _loaded_line(kind: str, result: Dict) -> str:
    rows = int(result.get("rows") or 0)

    # A repeated key means the partner's own export disagrees with itself, and
    # without this line the counts do not add up: the check reports the rows it
    # read, the load reports the rows it kept, and the operator is left staring
    # at a difference nothing explains. Silence here was the actual defect —
    # the de-duplication itself is fine and unavoidable.
    dupes = int(result.get("duplicates") or 0)
    repeated = (
        f" {dupes:,} line(s) repeated the same "
        f"{'product' if kind == 'catalog' else 'product at the same branch'}"
        f" — the last value was used."
        if dupes
        else ""
    )

    if kind == "catalog":
        deleted = int(result.get("deleted") or 0)
        gone = f" {deleted:,} no longer in the file were removed." if deleted else ""
        return f"Replaced the product list — {rows:,} products.{gone}{repeated}"
    return (
        f"Replaced all stock — {rows:,} rows. "
        f"Blanks, zeroes and negatives kept as written.{repeated}"
    )


def _indexed_line(stubs, embedded, edges) -> str:
    bits = []
    if stubs:
        bits.append(
            f"{int(stubs):,} codes had stock but no product record, so a "
            "placeholder was added — without it their stock is invisible to search"
        )
    if embedded:
        bits.append(f"{int(embedded):,} products re-indexed for meaning search")
    if edges:
        bits.append(f"{int(edges):,} related-product links rebuilt")
    return "Rebuilt search. " + ("; ".join(bits) + "." if bits else "Totals refreshed.")


async def scan_once(
    stable_only: bool = False,
    _sizes: Dict[str, int] = None,
    allow_shrink: bool = False,
) -> Dict:
    """Process all ready files once.

    Args:
        stable_only: when True, only ingest files whose size is unchanged since
            the previous scan (``_sizes`` carries prior sizes) — guards against
            partial uploads. The manual endpoint calls with False.
        _sizes: previous {path: size} map (mutated in place) for stability.
        allow_shrink: pass through to ingest_file, overriding the guard that
            refuses a file which would delete more than half a table. The SFTP
            watcher never sets it — an unattended drop must not be able to wipe
            the catalog, so shrinking uploads go through the admin API where a
            human has seen the warning.

    Returns a summary dict with processed / skipped / failed lists.
    """

    d = _dirs()
    processed, failed, skipped = [], [], []
    bumped = False
    # (run_id, filename) for every file that loaded in THIS scan. The tail below
    # — re-stubbing, refreshing, embedding, bumping — runs once for the batch,
    # and is recorded against each file that caused it.
    batch: List[tuple] = []

    # Operator-chosen catalog behaviour, read fresh from Redis each scan so a
    # change on the SFTP page takes effect on the next file with no restart.
    catalog_mode = await get_catalog_mode()

    for f in _pending(d["incoming"]):
        size = f.stat().st_size
        run = ev.new_run()

        if stable_only:
            prev = (_sizes or {}).get(str(f))
            if prev != size:
                if _sizes is not None:
                    _sizes[str(f)] = size
                skipped.append(f.name)  # still settling; next pass
                # Only announce the wait once — on the poll that first saw it.
                # Every subsequent poll would otherwise append a near-identical
                # row for as long as the upload takes.
                if prev is None:
                    await ev.record(
                        run, f.name, ev.STEP_ARRIVED, ev.OK,
                        "Arrived in the drop folder.", data={"size": size},
                    )
                    await ev.record(
                        run, f.name, ev.STEP_WAITING, ev.WAIT,
                        "Waiting for the upload to finish — the file is still growing.",
                        data={"size": size},
                    )
                continue

        await ev.record(
            run, f.name, ev.STEP_ARRIVED, ev.OK,
            "Arrived in the drop folder.", data={"size": size},
        )

        # detect_kind returns None — not "unknown" — for a name it cannot place.
        # The old `== "unknown"` test never fired, so a misnamed file fell through
        # to ingest_file, loaded 0 rows, and was ARCHIVED as a success.
        kind = detect_kind(f.name)
        if kind is None:
            stamped = _stamp(f.name)
            f.rename(d["failed"] / stamped)
            failed.append({"file": f.name, "reason": "unrecognised filename"})
            logger.warning("unrecognised filename, moved to failed/: %s", f.name)
            await ev.record(
                run, f.name, ev.STEP_UNRECOGNISED, ev.BAD,
                "Rejected — the name says nothing about what is inside. "
                "Nothing was opened.",
            )
            await ev.record(
                run, f.name, ev.STEP_SET_ASIDE, ev.OK,
                "Kept so you can look at it. Loaded data was never touched.",
                stamped=stamped,
            )
            continue

        await ev.record(
            run, f.name, ev.STEP_DETECTED, ev.OK,
            f"Read as {_KIND_WORDS.get(kind, kind)}, from the name.", kind=kind,
        )

        try:
            result = await ingest_file(
                str(f), catalog_mode=catalog_mode, allow_shrink=allow_shrink
            )
            stamped = _stamp(f.name)
            f.rename(d["archive"] / stamped)
            processed.append(result)
            bumped = True
            batch.append((run, f.name))
            if _sizes is not None:
                _sizes.pop(str(f), None)
            logger.info("ingested %s", result)

            report = result.get("validation") or {}
            await ev.record(
                run, f.name, ev.STEP_CHECKED, ev.OK,
                _checked_line(report), kind=kind, data=report,
            )
            await ev.record(
                run, f.name, ev.STEP_LOADED, ev.OK,
                _loaded_line(kind, result), kind=kind,
                data={k: result.get(k) for k in ("rows", "deleted") if k in result},
            )
            await ev.record(
                run, f.name, ev.STEP_STORED, ev.OK,
                "Copy kept.", kind=kind, stamped=stamped,
            )
        except Exception as exc:  # noqa: BLE001
            stamped = _stamp(f.name)
            f.rename(d["failed"] / stamped)
            failed.append({"file": f.name, "reason": str(exc)})
            logger.exception("ingest failed for %s", f.name)
            await ev.record(
                run, f.name, ev.STEP_REJECTED, ev.BAD, str(exc), kind=kind,
                data=getattr(exc, "report", None),
            )
            await ev.record(
                run, f.name, ev.STEP_SET_ASIDE, ev.OK,
                "Kept so you can look at it. Loaded data was never touched.",
                kind=kind, stamped=stamped,
            )

    if bumped:
        from app.ingest import (
            backfill_catalog_stubs,
            build_edges_safe,
            embed_catalog,
            refresh_views,
        )

        # Re-stub AFTER any ingest, not just an inventory one. backfill runs
        # inside ingest_inventory, so dropping a catalog file in full_sync mode
        # DELETES the stubs it had created and leaves those inventory articles
        # with no catalog row at all. Tools that INNER JOIN catalog (top_by_stock)
        # then cannot see their stock, and the article silently disappears until
        # the next inventory upload. Order of uploads should not change what the
        # agent can find. Idempotent, so a no-op when nothing is orphaned.
        stubs = await backfill_catalog_stubs()
        if stubs:
            logger.info("re-stubbed %s inventory articles absent from catalog", stubs)

        await refresh_views()        # keep materialized views in sync

        embedded = None

        # An SFTP drop must leave the catalog as complete as a manual ingest does.
        # ingest_catalog NULLs the embedding of any row whose text changed, and
        # search_by_meaning filters on `embedding IS NOT NULL` — so without this
        # a drug that arrives over SFTP is simply invisible to semantic search,
        # and no error is raised anywhere. Same for the substitute edges.
        try:
            embedded = await embed_catalog(only_missing=True)
            logger.info("embedded %s catalog rows", embedded)
        except Exception:  # noqa: BLE001 - never lose the ingest over an embed
            logger.exception("embed_catalog failed after ingest")

        edges = await build_edges_safe()
        if edges is not None:
            logger.info("rebuilt %s graph edges", edges)

        version = await bump_data_version()    # invalidate cached answers, LAST

        if batch:
            runs = [r for r, _ in batch]
            names = [n for _, n in batch]
            await ev.record_many(
                runs, names, ev.STEP_INDEXED, ev.OK, _indexed_line(stubs, embedded, edges),
                data={"stubs": stubs, "embedded": embedded, "edges": edges},
            )
            await ev.record_many(
                runs, names, ev.STEP_CACHE, ev.OK,
                "Cleared saved answers — nobody can be told a number from before "
                "this file.",
                data={"data_version": version},
            )

    return {"processed": processed, "failed": failed, "skipped": skipped}


async def watch() -> None:
    """Poll the incoming dir forever, ingesting stable files.

    The poll interval is re-read from Redis EACH iteration (``get_poll_seconds``,
    which falls back to the settings default when unset/unreadable), so an
    operator can retune the cadence from the SFTP page without restarting the
    worker. ``get_ingest_enabled`` is read the same way: turning automatic
    loading off pauses this loop, leaving files to accumulate in the drop folder
    until an operator loads them from the console. Both take effect on the next
    iteration, with no restart.
    """

    sizes: Dict[str, int] = {}
    paused = False
    logger.info("watcher started; polling %s", get_settings().incoming_dir)
    while True:
        try:
            if await get_ingest_enabled():
                if paused:
                    logger.info("automatic loading switched back on")
                    paused = False
                summary = await scan_once(stable_only=True, _sizes=sizes)
                if summary["processed"] or summary["failed"]:
                    logger.info("scan: %s", summary)
            elif not paused:
                # Say it once, not every poll — this can sit off for days.
                logger.info("automatic loading is off; files will wait in the drop folder")
                paused = True
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("scan error")
        await asyncio.sleep(await get_poll_seconds())


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(watch())
    finally:
        async def _cleanup():
            await close_pool()
            await close_client()

        asyncio.run(_cleanup())


if __name__ == "__main__":
    main()
