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

from app.cache import bump_data_version, close_client
from app.config import get_settings
from app.db import close_pool
from app.ingest import detect_kind, ingest_file

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


async def scan_once(stable_only: bool = False, _sizes: Dict[str, int] = None) -> Dict:
    """Process all ready files once.

    Args:
        stable_only: when True, only ingest files whose size is unchanged since
            the previous scan (``_sizes`` carries prior sizes) — guards against
            partial uploads. The manual endpoint calls with False.
        _sizes: previous {path: size} map (mutated in place) for stability.

    Returns a summary dict with processed / skipped / failed lists.
    """

    d = _dirs()
    processed, failed, skipped = [], [], []
    bumped = False

    for f in _pending(d["incoming"]):
        size = f.stat().st_size
        if stable_only:
            prev = (_sizes or {}).get(str(f))
            if prev != size:
                if _sizes is not None:
                    _sizes[str(f)] = size
                skipped.append(f.name)  # still settling; next pass
                continue

        kind = detect_kind(f.name)
        if kind == "unknown":
            dest = d["failed"] / _stamp(f.name)
            f.rename(dest)
            failed.append({"file": f.name, "reason": "unrecognised filename"})
            continue

        try:
            result = await ingest_file(str(f))
            f.rename(d["archive"] / _stamp(f.name))
            processed.append(result)
            bumped = True
            if _sizes is not None:
                _sizes.pop(str(f), None)
            logger.info("ingested %s", result)
        except Exception as exc:  # noqa: BLE001
            f.rename(d["failed"] / _stamp(f.name))
            failed.append({"file": f.name, "reason": str(exc)})
            logger.exception("ingest failed for %s", f.name)

    if bumped:
        from app.ingest import refresh_views

        await refresh_views()        # keep materialized views in sync
        await bump_data_version()    # invalidate cached answers

    return {"processed": processed, "failed": failed, "skipped": skipped}


async def watch() -> None:
    """Poll the incoming dir forever, ingesting stable files."""

    interval = get_settings().watch_interval_seconds
    sizes: Dict[str, int] = {}
    logger.info("watcher started; polling %s every %ss", get_settings().incoming_dir, interval)
    while True:
        try:
            summary = await scan_once(stable_only=True, _sizes=sizes)
            if summary["processed"] or summary["failed"]:
                logger.info("scan: %s", summary)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("scan error")
        await asyncio.sleep(interval)


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
