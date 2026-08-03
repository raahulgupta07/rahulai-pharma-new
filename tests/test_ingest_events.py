"""The file history — what it says, and that writing it can never break an ingest.

Every step of an ingest used to go to ``logger`` and nowhere else, so restarting
the container erased the reason a partner's file was refused. These pin the two
things that matter about keeping it instead:

1. The sentences are written for a pharmacy operator, and they carry the detail
   that was previously log-only — the negative quantities, the branch count, the
   reason for a rejection.
2. A recorder that is down degrades to "the history is missing", never to "the
   file failed to load". The data outranks its own audit trail.
"""

import pytest

from app import ingest_events as ev
from app import watcher


class TestSentences:
    """`detail` is rendered verbatim in the console, so it is the product."""

    def test_a_stock_load_says_what_it_did_to_the_shelf(self):
        line = watcher._loaded_line("inventory", {"rows": 111654})
        assert "111,654" in line
        # The promise made when 0/negative/blank were made first-class values.
        assert "negatives kept as written" in line

    def test_a_catalog_load_names_what_it_removed(self):
        line = watcher._loaded_line("catalog", {"rows": 5292, "deleted": 400})
        assert "5,292" in line and "400" in line

    def test_a_catalog_load_that_removed_nothing_says_nothing_about_removal(self):
        """Silence beats "0 removed" — it reads as an incident that did not happen."""

        line = watcher._loaded_line("catalog", {"rows": 5292, "deleted": 0})
        assert "removed" not in line

    def test_the_check_carries_the_notes_not_just_the_row_count(self):
        """These were the log-only lines. They are the whole reason for the table.

        A pharmacist seeing a shelf below zero asks why; "79 rows have a negative
        stock quantity (loaded as-is)" is the answer, and it used to be visible
        only to somebody tailing a container.
        """

        line = watcher._checked_line({
            "stats": {"usable_rows": 111654, "distinct_sites": 53},
            "warnings": [],
            "notes": [
                "79 row(s) have a negative stock quantity (loaded as-is)",
                "2 row(s) have zero stock (loaded as-is)",
            ],
        })
        assert "111,654" in line and "53 branches" in line
        assert "negative stock quantity" in line and "zero stock" in line

    def test_warnings_come_before_notes(self):
        """A warning is the closest thing to a rejection, so it is read first."""

        line = watcher._checked_line({
            "stats": {}, "warnings": ["7.5% of products have no name"],
            "notes": ["2 row(s) have zero stock"],
        })
        assert line.index("no name") < line.index("zero stock")

    def test_an_empty_report_still_produces_a_sentence(self):
        assert watcher._checked_line({}).startswith("Checked")
        assert watcher._checked_line(None).startswith("Checked")

    def test_the_rebuild_line_omits_the_parts_that_did_nothing(self):
        assert "placeholder" not in watcher._indexed_line(0, 0, 16021)
        assert "16,021" in watcher._indexed_line(0, 0, 16021)
        assert watcher._indexed_line(0, 0, 0).endswith("Totals refreshed.")

    def test_the_stub_line_explains_why_stubs_exist(self):
        """A number with no reason attached is noise on a page nobody reads."""

        line = watcher._indexed_line(400, 0, 0)
        assert "400" in line and "invisible to search" in line


class TestRecordingNeverBreaksAnIngest:
    @pytest.mark.asyncio
    async def test_a_dead_database_does_not_raise(self, monkeypatch):
        """The ingest outranks its own audit trail — always.

        If this ever starts propagating, a Postgres blip stops turning into a
        missing history and starts turning into a file that would not load.
        """

        async def _boom(*a, **k):
            raise ConnectionError("postgres is down")

        monkeypatch.setattr(ev, "execute", _boom)
        await ev.record(ev.new_run(), "balance.xlsx", ev.STEP_ARRIVED, ev.OK, "Arrived.")

    @pytest.mark.asyncio
    async def test_an_unreadable_history_is_empty_not_an_error(self, monkeypatch):
        """A page with no timeline still renders; a page that 500s does not."""

        async def _boom(*a, **k):
            raise ConnectionError("postgres is down")

        monkeypatch.setattr(ev, "q", _boom)
        assert await ev.history("balance.xlsx") == []
        assert await ev.latest() == {}

    @pytest.mark.asyncio
    async def test_a_failed_prune_reports_zero(self, monkeypatch):
        async def _boom(*a, **k):
            raise ConnectionError("postgres is down")

        monkeypatch.setattr(ev, "execute", _boom)
        assert await ev.prune() == 0


class TestRunIds:
    def test_every_attempt_gets_its_own_id(self):
        """A file can be retried, so the unit of history is an attempt.

        Keying on the filename instead would interleave a rejection and the
        successful retry into one confused timeline.
        """

        assert ev.new_run() != ev.new_run()


class TestLatestSummarisesTheWholeRun:
    """Taking the LAST event looked right and was wrong.

    A successful load ends on ``cache_cleared``, which carries no kind and no
    row count — so every file that loaded correctly listed as "Unknown, — rows".
    The files with the most to say looked like the ones we knew nothing about.
    Caught live, not in review.
    """

    @pytest.mark.asyncio
    async def test_a_successful_run_reports_its_kind_and_row_count(self, monkeypatch):
        monkeypatch.setattr(ev, "q", _rows([
            _e(1, "arrived", "ok", "Arrived."),
            _e(2, "detected", "ok", "Read as stock levels.", kind="inventory"),
            _e(3, "loaded", "ok", "Replaced all stock — 111,654 rows.",
               kind="inventory", data={"rows": 111654}),
            _e(4, "stored", "ok", "Copy kept.", kind="inventory", stamped="178_b.xlsx"),
            _e(5, "cache_cleared", "ok", "Cleared saved answers.",
               data={"data_version": 271}),
        ]))

        row = (await ev.latest())["b.xlsx"]
        assert row["kind"] == "inventory"
        assert row["data"]["rows"] == 111654
        assert row["stamped"] == "178_b.xlsx"

    @pytest.mark.asyncio
    async def test_the_reason_wins_over_what_came_after_it(self, monkeypatch):
        """A rejection is the headline; "Kept so you can look at it" is not."""

        monkeypatch.setattr(ev, "q", _rows([
            _e(1, "arrived", "ok", "Arrived."),
            _e(2, "detected", "ok", "Read as the product list.", kind="catalog"),
            _e(3, "rejected", "bad", "missing required column(s): Brand Name", kind="catalog"),
            _e(4, "set_aside", "ok", "Kept so you can look at it.", kind="catalog"),
        ]))

        row = (await ev.latest())["b.xlsx"]
        assert row["step"] == "rejected"
        assert "Brand Name" in row["detail"]
        assert row["status"] == "bad"

    @pytest.mark.asyncio
    async def test_a_run_of_only_uninteresting_steps_still_summarises(self, monkeypatch):
        monkeypatch.setattr(ev, "q", _rows([_e(1, "arrived", "ok", "Arrived.")]))
        assert (await ev.latest())["b.xlsx"]["step"] == "arrived"


def _e(i, step, status, detail, *, kind=None, stamped=None, data=None):
    from datetime import datetime, timezone

    return {
        "id": i, "run_id": "11111111-1111-1111-1111-111111111111", "file": "b.xlsx",
        "stamped": stamped, "kind": kind, "step": step, "status": status,
        "detail": detail, "data": data, "at": datetime(2026, 8, 3, tzinfo=timezone.utc),
    }


def _rows(rows):
    async def _q(*a, **k):
        return rows
    return _q


class TestRowNormalisation:
    def test_jsonb_arrives_as_a_dict_however_asyncpg_hands_it_over(self):
        """asyncpg returns jsonb as a string unless a codec is registered."""

        assert ev._row({"data": '{"rows": 5}'})["data"] == {"rows": 5}
        assert ev._row({"data": {"rows": 5}})["data"] == {"rows": 5}

    def test_unparseable_json_becomes_none_rather_than_breaking_the_page(self):
        assert ev._row({"data": "not json"})["data"] is None
