"""The SFTP file endpoints — path safety first, then the listing contract.

``GET /admin/sftp/file/{name}`` serves a file whose name a PARTNER chose and
uploaded over SFTP, addressed by a segment of a URL. Both halves are untrusted,
so most of this module is about the one question that matters: can any request
read a file that is not in the drop folders?

The listing tests pin the smaller promise — that the folder a file sits in is
what decides its status, and that the timestamp we prefix onto an archived file
is ours to hide, not part of the partner's filename.
"""

import pytest

from app import admin


@pytest.fixture()
def drop(tmp_path, monkeypatch):
    """Point the SFTP folders at tmp_path and create the three of them."""

    from types import SimpleNamespace

    monkeypatch.setattr(
        admin, "get_settings", lambda: SimpleNamespace(incoming_dir=str(tmp_path))
    )
    for sub in ("", "archive", "failed"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestPathSafety:
    """Every one of these is a real file the caller must NOT be able to read."""

    def test_traversal_cannot_escape_the_drop_folder(self, drop, tmp_path):
        secret = tmp_path.parent / "secret.xlsx"
        secret.write_text("private")

        for attack in (
            "../secret.xlsx",
            "../../secret.xlsx",
            "..%2Fsecret.xlsx",
            "subdir/../../secret.xlsx",
        ):
            with pytest.raises(admin.HTTPException) as e:
                admin._resolve_sftp_file(attack)
            assert e.value.status_code == 404, attack

    def test_absolute_path_is_refused(self, drop):
        with pytest.raises(admin.HTTPException) as e:
            admin._resolve_sftp_file("/etc/passwd")
        assert e.value.status_code == 404

    def test_a_symlink_out_of_the_folder_is_not_served(self, drop, tmp_path):
        """Stripping directories is not enough on its own.

        ``Path(name).name`` turns "../x" into "x" — but a symlink *inside* the
        folder needs no traversal in the URL at all. It is a plain filename that
        resolves somewhere else entirely, and only the resolved-parent check
        catches it.
        """

        outside = tmp_path.parent / "outside.xlsx"
        outside.write_text("private")
        (drop / "archive" / "innocent.xlsx").symlink_to(outside)

        with pytest.raises(admin.HTTPException) as e:
            admin._resolve_sftp_file("innocent.xlsx")
        assert e.value.status_code == 404

    def test_empty_and_dot_names_are_refused(self, drop):
        for name in ("", ".", "..", "/", "//"):
            with pytest.raises(admin.HTTPException):
                admin._resolve_sftp_file(name)

    def test_a_missing_file_and_a_forbidden_one_answer_alike(self, drop, tmp_path):
        """Both 404 — probing must not distinguish "denied" from "absent"."""

        (tmp_path.parent / "secret.xlsx").write_text("private")

        missing = pytest.raises(admin.HTTPException)
        with missing as a:
            admin._resolve_sftp_file("nope.xlsx")
        with pytest.raises(admin.HTTPException) as b:
            admin._resolve_sftp_file("../secret.xlsx")
        assert a.value.status_code == b.value.status_code == 404
        assert a.value.detail == b.value.detail

    def test_a_real_file_in_each_folder_resolves(self, drop):
        for sub in ("", "archive", "failed"):
            f = drop / sub / f"balance_stock_{sub or 'incoming'}.xlsx"
            f.write_text("x")
            assert admin._resolve_sftp_file(f.name) == f.resolve()


class TestArchiveNamePrefix:
    """``watcher._stamp`` prefixes an epoch. That prefix is ours, not theirs."""

    def test_the_timestamp_is_stripped(self):
        assert admin._original_name("1782107161_balance_stock.xlsx") == "balance_stock.xlsx"

    def test_a_name_that_merely_contains_an_underscore_is_left_alone(self):
        assert admin._original_name("balance_stock.xlsx") == "balance_stock.xlsx"
        assert admin._original_name("articles-export-2026-08-03.csv") == (
            "articles-export-2026-08-03.csv"
        )

    def test_a_non_numeric_prefix_is_not_a_timestamp(self):
        assert admin._original_name("draft_balance.xlsx") == "draft_balance.xlsx"

    def test_a_bare_prefix_with_no_remainder_is_left_alone(self):
        assert admin._original_name("1782107161_") == "1782107161_"


class TestListing:
    @pytest.mark.asyncio
    async def test_the_folder_decides_the_status(self, drop, monkeypatch):
        (drop / "still-arriving_stock.xlsx").write_text("a")
        (drop / "archive" / "1782107161_balance_stock.xlsx").write_text("bb")
        (drop / "failed" / "1782093344_articles-export.csv").write_text("ccc")
        _no_history(monkeypatch)

        res = await admin.sftp_files()
        by_name = {f["name"]: f for f in res["files"]}

        assert by_name["still-arriving_stock.xlsx"]["state"] == "wait"
        assert by_name["balance_stock.xlsx"]["state"] == "ok"
        assert by_name["articles-export.csv"]["state"] == "bad"
        assert res["counts"] == {"wait": 1, "ok": 1, "bad": 1, "live": 1}

    @pytest.mark.asyncio
    async def test_an_archived_copy_gets_ITS_OWN_run_not_the_newest_for_its_name(
        self, drop, monkeypatch
    ):
        """The bug this endpoint shipped with, pinned.

        History used to be looked up by the partner's filename, so every stored
        copy of ``balance_stock.xlsx`` inherited the newest run for that name.
        On 2026-08-13 the newest run was a REJECTION two minutes after a
        successful load, so five archived copies that had each loaded 111,654
        rows displayed a rejected run and an em-dash where their count belonged.
        """

        (drop / "archive" / "1782107161_balance_stock.xlsx").write_text("x")
        (drop / "archive" / "1782207161_balance_stock.xlsx").write_text("yy")

        _history(
            monkeypatch,
            by_stamped={
                "1782107161_balance_stock.xlsx": _run("inventory", 111654, "loaded"),
                "1782207161_balance_stock.xlsx": _run("inventory", 120628, "loaded"),
            },
            # The name-keyed summary is deliberately WRONG for both of them.
            by_name={"balance_stock.xlsx": _run("inventory", None, "rejected")},
        )

        rows = {f["stored_as"]: f for f in (await admin.sftp_files())["files"]}
        assert rows["1782107161_balance_stock.xlsx"]["rows"] == 111654
        assert rows["1782207161_balance_stock.xlsx"]["rows"] == 120628
        assert all(r["step"] == "loaded" for r in rows.values())

    @pytest.mark.asyncio
    async def test_a_file_still_in_the_drop_folder_matches_on_the_partner_name(
        self, drop, monkeypatch
    ):
        """It has no stamp yet — the stamp is chosen when the file is MOVED.

        So the name-keyed summary is the only one that can describe it, and it
        is correct there precisely because the file has not been superseded.
        """

        (drop / "balance_stock.xlsx").write_text("x")
        _history(
            monkeypatch,
            by_stamped={},
            by_name={"balance_stock.xlsx": _run("inventory", None, "waiting")},
        )

        row = (await admin.sftp_files())["files"][0]
        assert row["state"] == "wait"
        assert row["step"] == "waiting"
        assert row["live"] is False, "a file that has not loaded cannot be live"

    @pytest.mark.asyncio
    async def test_a_file_with_no_history_still_lists(self, drop, monkeypatch):
        """Files predating the table, or written while the recorder was down.

        The kind falls back to the FILENAME rather than staying unknown: the
        live marker needs a kind for every file, and the name is what the
        loader itself classifies on.
        """

        (drop / "archive" / "1700000000_balance_stock.xlsx").write_text("x")
        _no_history(monkeypatch)

        row = (await admin.sftp_files())["files"][0]
        assert row["state"] == "ok"
        assert row["kind"] == "inventory"
        assert row["detail"] is None


class TestLive:
    """Which file did the data actually come from?

    Both loaders replace their own table outright, so exactly one file per kind
    is live and every other copy is history kept for download and retry.
    """

    @pytest.mark.asyncio
    async def test_one_live_file_per_kind_and_the_rest_are_superseded(
        self, drop, monkeypatch
    ):
        for stamp in ("1782107161", "1782207161", "1782307161"):
            (drop / "archive" / f"{stamp}_balance_stock.xlsx").write_text("x")
        (drop / "archive" / "1782107000_articles-export.xlsx").write_text("y")
        _no_history(monkeypatch)

        res = await admin.sftp_files()
        live = [f["stored_as"] for f in res["files"] if f["live"]]

        assert sorted(live) == [
            "1782107000_articles-export.xlsx",
            "1782307161_balance_stock.xlsx",
        ], live
        assert res["counts"]["live"] == 2

    @pytest.mark.asyncio
    async def test_a_rejected_newer_file_does_not_take_live_from_the_loaded_one(
        self, drop, monkeypatch
    ):
        """The case that actually happened, and the reason "newest" is not enough.

        10:52 a stock file loaded; 10:54 another arrived and the shrink guard
        refused it. The newest upload is the rejected one, but the data in the
        database is still the 10:52 load — so that is what "live" has to mean.
        """

        (drop / "archive" / "1782107161_balance_stock.xlsx").write_text("x")
        (drop / "failed" / "1782107281_balance_stock.xlsx").write_text("zz")
        _no_history(monkeypatch)

        rows = {f["stored_as"]: f for f in (await admin.sftp_files())["files"]}
        assert rows["1782107161_balance_stock.xlsx"]["live"] is True
        assert rows["1782107281_balance_stock.xlsx"]["live"] is False
        assert rows["1782107281_balance_stock.xlsx"]["state"] == "bad"

    @pytest.mark.asyncio
    async def test_a_kind_that_never_loaded_has_no_live_file(self, drop, monkeypatch):
        """Nothing is live until something has actually replaced the data."""

        (drop / "balance_stock.xlsx").write_text("x")           # still arriving
        (drop / "failed" / "1782107281_articles-export.xlsx").write_text("y")
        _no_history(monkeypatch)

        res = await admin.sftp_files()
        assert res["counts"]["live"] == 0
        assert not any(f["live"] for f in res["files"])

    @pytest.mark.asyncio
    async def test_an_unrecognised_file_is_never_live(self, drop, monkeypatch):
        """No kind means it replaced no table, whatever folder it ended up in."""

        (drop / "archive" / "1782107161_quarterly-notes.xlsx").write_text("x")
        _no_history(monkeypatch)

        res = await admin.sftp_files()
        assert res["files"][0]["kind"] is None
        assert res["files"][0]["live"] is False
        assert res["counts"]["live"] == 0


def _run(kind, rows, step):
    return {
        "kind": kind, "step": step, "status": "ok", "detail": None,
        "data": {} if rows is None else {"rows": rows}, "run_id": "r-" + str(rows),
    }


def _history(monkeypatch, by_stamped, by_name):
    async def _stamped():
        return by_stamped

    async def _named():
        return by_name

    monkeypatch.setattr("app.ingest_events.latest_by_stamped", _stamped)
    monkeypatch.setattr("app.ingest_events.latest", _named)
    monkeypatch.setattr(admin.cache, "get_poll_seconds", _num(15))
    monkeypatch.setattr(admin.cache, "get_ingest_enabled", _num(True))


def _no_history(monkeypatch):
    _history(monkeypatch, by_stamped={}, by_name={})


def _num(value):
    async def _f():
        return value
    return _f
