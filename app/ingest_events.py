"""What happened to each file that arrived — kept, instead of thrown away.

``watcher.scan_once`` already knows the whole story of every file: that it
arrived, what we decided it was, whether it passed, how many rows it replaced,
which version it invalidated, and — when it failed — exactly why. Every one of
those went to ``logger`` and nowhere else. Restart the container and the reason
a partner's file was rejected is gone, so the only answer anyone can give is
"try sending it again".

This module writes that story to a table so the console can show it.

Two rules the rest of the code depends on:

1. **Logging must never break an ingest.** Every write here is wrapped and
   degrades to a warning. A file loading correctly but not being recorded is a
   bad day; a file failing to load *because* the recorder was down is a much
   worse one. This mirrors ``app.history.record_turn``.

2. **One run, not one file.** A file can be retried after a rejection, so the
   unit of history is an attempt (``run_id``), not a filename. The listing shows
   the latest run per file; the drawer shows every step of that run.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Dict, List, Optional

from app.db import execute, q

logger = logging.getLogger(__name__)

# How long a file's history is worth keeping. Long enough to answer "what
# happened last month", short enough that the table never needs thinking about.
RETENTION_DAYS = 120

# Steps, in the order scan_once performs them. The console renders `detail`
# verbatim, so these keys are for filtering and tests, not for display.
STEP_ARRIVED = "arrived"        # seen in the drop folder
STEP_WAITING = "waiting"        # still being written — size changed since last look
STEP_DETECTED = "detected"      # the name told us what it is
STEP_UNRECOGNISED = "unrecognised"
STEP_CHECKED = "checked"        # validation passed (warnings may be attached)
STEP_REJECTED = "rejected"      # validation failed — carries the reason
STEP_LOADED = "loaded"          # rows replaced
STEP_INDEXED = "indexed"        # stubs / views / embeddings / edges
STEP_CACHE = "cache_cleared"    # data_version bumped
STEP_STORED = "stored"          # moved to archive/
STEP_SET_ASIDE = "set_aside"    # moved to failed/

OK, BAD, WAIT = "ok", "bad", "wait"


async def ensure_schema() -> None:
    """Create the table. Idempotent; called from the API lifespan."""

    await execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_events (
            id       BIGSERIAL PRIMARY KEY,
            run_id   UUID        NOT NULL,
            file     TEXT        NOT NULL,
            stamped  TEXT,
            kind     TEXT,
            step     TEXT        NOT NULL,
            status   TEXT        NOT NULL,
            detail   TEXT,
            data     JSONB,
            at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # The listing asks "latest run per file"; the drawer asks "every step of
    # this run, in order".
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_ingest_events_file ON ingest_events (file, id DESC)"
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_ingest_events_run ON ingest_events (run_id, id)"
    )


def new_run() -> str:
    """A fresh attempt id. Cheap and offline — no DB round trip to start a run."""

    return str(uuid.uuid4())


async def record(
    run_id: str,
    file: str,
    step: str,
    status: str,
    detail: str = "",
    *,
    kind: Optional[str] = None,
    stamped: Optional[str] = None,
    data: Optional[Dict] = None,
) -> None:
    """Append one step. Never raises — a failure here must not fail the ingest."""

    try:
        await execute(
            """
            INSERT INTO ingest_events (run_id, file, stamped, kind, step, status, detail, data)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            run_id,
            file,
            stamped,
            kind,
            step,
            status,
            detail,
            json.dumps(data) if data is not None else None,
        )
    except Exception:  # noqa: BLE001 — the ingest outranks its own audit trail
        logger.warning("could not record ingest event %s for %s", step, file, exc_info=True)


async def record_many(run_ids: List[str], files: List[str], *args, **kwargs) -> None:
    """Record the same step against several runs.

    The tail of a scan — refreshing views, re-embedding, bumping the version —
    happens once for the whole batch, not once per file. Attributing it to every
    file in that batch is the honest description: each of them caused it.
    """

    for run_id, file in zip(run_ids, files):
        await record(run_id, file, *args, **kwargs)


async def history(file: str, run_id: Optional[str] = None) -> List[Dict]:
    """Every step of one attempt, oldest first.

    Without ``run_id``, the most recent attempt at that filename.
    """

    try:
        if run_id is None:
            rows = await q(
                """
                SELECT * FROM ingest_events
                 WHERE run_id = (
                     SELECT run_id FROM ingest_events WHERE file = $1
                      ORDER BY id DESC LIMIT 1
                 )
                 ORDER BY id
                """,
                file,
            )
        else:
            rows = await q(
                "SELECT * FROM ingest_events WHERE run_id = $1::uuid ORDER BY id",
                run_id,
            )
    except Exception:  # noqa: BLE001 — a missing history is not a broken page
        logger.warning("could not read ingest history for %s", file, exc_info=True)
        return []

    return [_row(r) for r in rows]


# The step whose `detail` best describes an attempt, most-telling first. A
# rejection is the headline whenever there is one; nobody scanning the list
# wants "Kept so you can look at it" where the reason should be.
_HEADLINE = (STEP_REJECTED, STEP_UNRECOGNISED, STEP_LOADED, STEP_WAITING)


async def latest() -> Dict[str, Dict]:
    """A summary of the most recent attempt per filename, as ``{file: {...}}``.

    Summarising the whole run, not just its last row. Taking the last event
    looked right and was wrong: a successful load ends on ``cache_cleared``,
    which carries no ``kind`` and no row count, so every file that loaded
    correctly listed as "Unknown, — rows" — the ones with the most to say
    looked like the ones we knew nothing about.
    """

    try:
        rows = await q(
            """
            WITH last_run AS (
                SELECT DISTINCT ON (file) file, run_id
                  FROM ingest_events
                 ORDER BY file, id DESC
            )
            SELECT e.*
              FROM ingest_events e
              JOIN last_run r ON r.run_id = e.run_id
             ORDER BY e.file, e.id
            """
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not read ingest summaries", exc_info=True)
        return {}

    out: Dict[str, Dict] = {}
    for raw in rows:
        e = _row(raw)
        cur = out.setdefault(e["file"], {
            "file": e["file"], "run_id": e["run_id"], "kind": None,
            "stamped": None, "step": None, "status": None, "detail": None,
            "data": {}, "at": e["at"],
        })
        # Any step that knew the kind or the stored name is the authority on it;
        # later steps simply leave those fields empty.
        cur["kind"] = e.get("kind") or cur["kind"]
        cur["stamped"] = e.get("stamped") or cur["stamped"]
        if isinstance(e.get("data"), dict):
            cur["data"] = {**cur["data"], **e["data"]}
        cur["at"] = e["at"]

        better = _HEADLINE.index(e["step"]) if e["step"] in _HEADLINE else len(_HEADLINE)
        current = (
            _HEADLINE.index(cur["step"]) if cur["step"] in _HEADLINE else len(_HEADLINE)
        )
        if cur["step"] is None or better < current:
            cur["step"], cur["status"], cur["detail"] = e["step"], e["status"], e["detail"]

    return out


async def latest_by_stamped() -> Dict[str, Dict]:
    """A summary of the run that produced each STORED file, as ``{stamped: {...}}``.

    ``latest()`` answers "what happened to a file called X", which is the wrong
    question once X has arrived five times: every stored copy inherits the newest
    run for the name, so four superseded uploads all displayed the story — and
    the row count — of the fifth. On 2026-08-13 the newest run for
    ``balance_stock.xlsx`` was a REJECTION two minutes after a successful load,
    so five archived copies that had loaded 111,654 rows each showed a rejected
    run and an em-dash where their row count should be.

    ``stamped`` is written only on the terminal step (``stored``/``set_aside``),
    because the archive name is not chosen until the file is moved — but every
    step of an attempt shares its ``run_id``, so the stamp identifies the run and
    the run carries the whole story.
    """

    try:
        rows = await q(
            """
            WITH stamped_run AS (
                SELECT DISTINCT ON (stamped) stamped, run_id
                  FROM ingest_events
                 WHERE stamped IS NOT NULL
                 ORDER BY stamped, id DESC
            )
            SELECT s.stamped AS stamped_key, e.*
              FROM stamped_run s
              JOIN ingest_events e ON e.run_id = s.run_id
             ORDER BY s.stamped, e.id
            """
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not read per-file ingest summaries", exc_info=True)
        return {}

    out: Dict[str, Dict] = {}
    for raw in rows:
        key = raw["stamped_key"]
        e = _row({k: v for k, v in dict(raw).items() if k != "stamped_key"})
        cur = out.setdefault(key, {
            "file": e["file"], "run_id": e["run_id"], "kind": None,
            "stamped": key, "step": None, "status": None, "detail": None,
            "data": {}, "at": e["at"],
        })
        cur["kind"] = e.get("kind") or cur["kind"]
        if isinstance(e.get("data"), dict):
            cur["data"] = {**cur["data"], **e["data"]}
        cur["at"] = e["at"]

        better = _HEADLINE.index(e["step"]) if e["step"] in _HEADLINE else len(_HEADLINE)
        current = (
            _HEADLINE.index(cur["step"]) if cur["step"] in _HEADLINE else len(_HEADLINE)
        )
        if cur["step"] is None or better < current:
            cur["step"], cur["status"], cur["detail"] = e["step"], e["status"], e["detail"]

    return out


async def prune(days: int = RETENTION_DAYS) -> int:
    """Drop history older than ``days``. Returns the number of rows removed."""

    try:
        status = await execute(
            "DELETE FROM ingest_events WHERE at < now() - ($1 || ' days')::interval",
            str(int(days)),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0
    except Exception:  # noqa: BLE001
        logger.warning("could not prune ingest events", exc_info=True)
        return 0


def _row(r: Dict) -> Dict:
    """Normalise one row for JSON: uuid -> str, timestamp -> iso, jsonb -> dict."""

    out = dict(r)
    if out.get("run_id") is not None:
        out["run_id"] = str(out["run_id"])
    if out.get("at") is not None:
        out["at"] = out["at"].isoformat()
    raw = out.get("data")
    # asyncpg hands back jsonb as a string unless a codec is registered.
    if isinstance(raw, str):
        try:
            out["data"] = json.loads(raw)
        except ValueError:
            out["data"] = None
    return out
