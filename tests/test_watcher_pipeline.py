"""SFTP -> watcher pipeline tests — no DB, no network (every side effect stubbed).

Two defects are pinned here, both of which failed SILENTLY in production:

  1. A misnamed drop was archived as a success. `detect_kind` returns None for a
     name it cannot place, but scan_once tested `kind == "unknown"`, so the guard
     never fired: the file reached ingest_file, loaded 0 rows, and was filed under
     archive/ exactly like a good upload.

  2. An SFTP drop never embedded. ingest_catalog NULLs the embedding of any row
     whose text changed and search_by_meaning filters on `embedding IS NOT NULL`,
     so a drug that arrived over SFTP was invisible to semantic search — with no
     error raised anywhere. The manual ingest path did embed; the watcher did not.
"""

import pytest

from app import watcher


@pytest.fixture()
def incoming(tmp_path, monkeypatch):
    """Point the watcher's incoming dir at tmp_path.

    get_settings() is lru_cached, so setting INCOMING_DIR in the environment is
    too late — patch the accessor the watcher actually calls.
    """

    from types import SimpleNamespace

    monkeypatch.setattr(
        watcher, "get_settings", lambda: SimpleNamespace(incoming_dir=str(tmp_path))
    )
    return tmp_path


@pytest.fixture()
def calls(monkeypatch):
    """Stub every post-ingest side effect and record that it ran."""

    seen = {"ingested": [], "refresh": 0, "embed": 0, "edges": 0, "bump": 0, "mode": None}

    async def _ingest_file(path, catalog_mode="merge"):
        from pathlib import Path

        seen["ingested"].append(Path(path).name)
        seen["mode"] = catalog_mode
        return {"file": Path(path).name, "kind": "catalog", "rows": 3}

    async def _get_catalog_mode():
        return "merge"

    async def _refresh_views():
        seen["refresh"] += 1

    async def _embed_catalog(only_missing=True):
        seen["embed"] += 1
        return 7

    async def _build_edges_safe():
        seen["edges"] += 1
        return 42

    async def _bump():
        seen["bump"] += 1

    monkeypatch.setattr(watcher, "ingest_file", _ingest_file)
    monkeypatch.setattr(watcher, "get_catalog_mode", _get_catalog_mode)
    monkeypatch.setattr("app.ingest.refresh_views", _refresh_views)
    monkeypatch.setattr("app.ingest.embed_catalog", _embed_catalog)
    monkeypatch.setattr("app.ingest.build_edges_safe", _build_edges_safe)
    monkeypatch.setattr(watcher, "bump_data_version", _bump)
    return seen


# ---- defect 1: the unrecognised-name guard ----------------------------------


@pytest.mark.asyncio
async def test_misnamed_file_goes_to_failed_not_archive(incoming, calls):
    """A name detect_kind cannot place must FAIL, not archive as a success."""

    (incoming / "quarterly-notes.csv").write_text("a,b\n1,2\n")

    summary = await watcher.scan_once()

    assert summary["processed"] == []
    assert [f["file"] for f in summary["failed"]] == ["quarterly-notes.csv"]
    assert calls["ingested"] == []  # never handed to the loader

    assert not list(incoming.glob("archive/*"))
    assert [p.name.endswith("quarterly-notes.csv") for p in incoming.glob("failed/*")] == [True]


@pytest.mark.asyncio
async def test_misnamed_file_does_not_bust_the_cache(incoming, calls):
    """Nothing loaded => no data_version bump. A no-op must not invalidate answers."""

    (incoming / "readme.csv").write_text("x\n1\n")

    await watcher.scan_once()

    assert calls["bump"] == 0
    assert calls["refresh"] == 0
    assert calls["embed"] == 0


# ---- defect 2: an SFTP drop must embed + rebuild the graph -------------------


@pytest.mark.asyncio
async def test_sftp_ingest_embeds_and_rebuilds_graph(incoming, calls):
    """Otherwise the new drug is unreachable via search_by_meaning, silently."""

    (incoming / "articles-export.csv").write_text("Article Code\nA1\n")

    summary = await watcher.scan_once()

    assert [r["file"] for r in summary["processed"]] == ["articles-export.csv"]
    assert calls["refresh"] == 1
    assert calls["embed"] == 1, "SFTP drop did not embed — new rows invisible to semantic search"
    assert calls["edges"] == 1, "SFTP drop did not rebuild substitute edges"
    assert calls["bump"] == 1


@pytest.mark.asyncio
async def test_embed_failure_does_not_lose_the_ingest(incoming, calls, monkeypatch):
    """Rows already landed. An embedding outage must not roll the upload back."""

    async def _boom(only_missing=True):
        raise RuntimeError("embedding provider down")

    monkeypatch.setattr("app.ingest.embed_catalog", _boom)
    (incoming / "articles-export.csv").write_text("Article Code\nA1\n")

    summary = await watcher.scan_once()

    assert len(summary["processed"]) == 1
    assert list(incoming.glob("archive/*"))  # file was still filed away
    assert calls["bump"] == 1                # cache still busted for the new rows


# ---- partial-upload guard (unchanged behaviour, pinned) ----------------------


@pytest.mark.asyncio
async def test_growing_file_is_skipped_until_stable(incoming, calls):
    """stable_only: a file still being written over SFTP must not be ingested."""

    f = incoming / "articles-export.csv"
    f.write_text("Article Code\nA1\n")
    sizes = {}

    first = await watcher.scan_once(stable_only=True, _sizes=sizes)
    assert first["skipped"] == ["articles-export.csv"]
    assert calls["ingested"] == []

    second = await watcher.scan_once(stable_only=True, _sizes=sizes)  # size unchanged
    assert [r["file"] for r in second["processed"]] == ["articles-export.csv"]
