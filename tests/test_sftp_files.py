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

        async def _no_events():
            return {}

        monkeypatch.setattr("app.ingest_events.latest", _no_events)
        monkeypatch.setattr(admin.cache, "get_poll_seconds", _num(15))
        monkeypatch.setattr(admin.cache, "get_ingest_enabled", _num(True))

        res = await admin.sftp_files()
        by_name = {f["name"]: f for f in res["files"]}

        assert by_name["still-arriving_stock.xlsx"]["state"] == "wait"
        assert by_name["balance_stock.xlsx"]["state"] == "ok"
        assert by_name["articles-export.csv"]["state"] == "bad"
        assert res["counts"] == {"wait": 1, "ok": 1, "bad": 1}

    @pytest.mark.asyncio
    async def test_history_is_matched_on_the_partner_name(self, drop, monkeypatch):
        """The event log keys on what arrived; the file on disk carries our prefix.

        Match them on the stamped name and every archived file looks like it has
        no history at all — the drawer would be empty for exactly the files that
        have the most to show.
        """

        (drop / "archive" / "1782107161_balance_stock.xlsx").write_text("x")

        async def _events():
            return {"balance_stock.xlsx": {
                "kind": "inventory", "step": "cache_cleared", "detail": "Cleared saved answers.",
                "data": {"rows": 111654}, "run_id": "abc",
            }}

        monkeypatch.setattr("app.ingest_events.latest", _events)
        monkeypatch.setattr(admin.cache, "get_poll_seconds", _num(15))
        monkeypatch.setattr(admin.cache, "get_ingest_enabled", _num(True))

        row = (await admin.sftp_files())["files"][0]
        assert row["kind"] == "inventory"
        assert row["rows"] == 111654
        assert row["stored_as"] == "1782107161_balance_stock.xlsx"

    @pytest.mark.asyncio
    async def test_a_file_with_no_history_still_lists(self, drop, monkeypatch):
        """Files predating the table, or written while the recorder was down."""

        (drop / "archive" / "1700000000_balance_stock.xlsx").write_text("x")

        async def _none():
            return {}

        monkeypatch.setattr("app.ingest_events.latest", _none)
        monkeypatch.setattr(admin.cache, "get_poll_seconds", _num(15))
        monkeypatch.setattr(admin.cache, "get_ingest_enabled", _num(True))

        row = (await admin.sftp_files())["files"][0]
        assert row["state"] == "ok" and row["kind"] is None and row["detail"] is None


def _num(value):
    async def _f():
        return value
    return _f
