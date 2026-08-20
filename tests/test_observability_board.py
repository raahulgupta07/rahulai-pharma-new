"""`GET /admin/architecture/observability` — the signals, each proved by a count.

A list of signals with ticks against them is worth nothing on its own. This
system has already had the failure it guards against: the capture layer for
``tool_calls`` shipped wired into one of the two places it needed, so the table
stayed empty forever, nothing errored, and 804 tests passed. "We record a tool
trace" was true of the code and false of the database.

So a signal that claims to be in place must name a table and be answered by
counting it, and a zero must downgrade the claim without anybody remembering to
look.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "app" / "admin.py"
PAGE = ROOT / "admin" / "src" / "routes" / "architecture" / "+page.svelte"

STATES = {"in_place", "partial", "unknown", "none"}


@pytest.fixture(scope="module")
def handler() -> str:
    src = ADMIN.read_text()
    i = src.index("# ---- architecture: what we can see")
    j = src.index("# ---- architecture: the three routes")
    return src[i:j]


@pytest.fixture(scope="module")
def prose(handler) -> str:
    """The handler with adjacent string literals joined.

    Python concatenation puts a `" "` + newline + indent between halves of
    every sentence in here, so a search for a phrase that happens to straddle
    one fails for a reason that has nothing to do with the copy."""
    return re.sub(r'"\s*\n\s*"', "", handler)


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_every_in_place_claim_names_a_table(handler):
    """A signal with no evidence query cannot be contradicted by the data."""
    m = re.search(r"_OBS_SIGNALS = \[(.*?)\n\]", handler, re.S)
    assert m, "the signal list is gone"
    entries = re.findall(r'\{\s*"id": "(\w+)".*?"sql": (None|".*?"),', m.group(1), re.S)
    assert entries, "the signal list no longer carries an evidence query per entry"
    without = [i for i, sql in entries if sql == "None"]
    # `deps` is the one honest exception: its evidence is this page's own probe,
    # not a table. Anything else claiming to be in place with no query is a
    # tick nothing can ever disprove.
    assert without == ["deps"], (
        f"signal(s) {without} claim to be recorded but name no table, so an "
        f"empty pipe would still read as working"
    )


def test_an_empty_table_downgrades_the_claim(handler):
    """The count decides the state, not the presence of the code."""
    assert re.search(r'"in_place" if n > 0\s*\n?\s*else "partial"', handler), (
        "a signal whose table is empty is no longer downgraded — this is the "
        "exact shape of the tool_calls failure: code present, table empty, "
        "nothing raised"
    )


def test_could_not_look_is_not_the_same_as_empty(handler):
    """A missing table and an empty one are different answers."""
    assert '"unknown" if n is None' in handler, (
        "a count that could not be read is being treated as a measured zero, "
        "so a dropped table would report as 'nothing recorded yet'"
    )
    assert "unknown: {" in PAGE.read_text(), "the page has no styling for `unknown`"


def test_the_gaps_are_not_quietly_omitted(handler):
    """A board of things that work is not an observability board."""
    m = re.search(r"_OBS_GAPS = \[(.*?)\n\]", handler, re.S)
    assert m, "the gap list is gone"
    ids = set(re.findall(r'"id": "(\w+)"', m.group(1)))
    for required in ("accuracy", "alerting"):
        assert required in ids, (
            f"{required!r} is no longer listed as a gap. It is the one that "
            f"decides whether anybody finds out, and dropping it makes this "
            f"page a list of things that work"
        )


def test_accuracy_is_named_as_unmeasured(prose):
    """Every number on this console is about speed or money. None is about
    whether the answer was true, and that must be said rather than implied by
    absence."""
    assert "none of them says whether it was true" in prose, (
        "the accuracy gap no longer says what the other numbers do NOT cover"
    )


def test_cost_says_whether_it_was_billed_or_worked_out(handler):
    """`cost_is_estimated` is a real column and the difference is not
    cosmetic: an estimate is our arithmetic over a price typed into the code,
    and it drifts silently when the provider changes it."""
    assert "cost_is_estimated" in handler, (
        "the spend row no longer distinguishes a provider-reported cost from "
        "an estimate"
    )


def test_every_state_the_api_emits_is_drawn(handler, page):
    emitted = set(re.findall(r'"(?:state)": "(\w+)"', handler)) | set(
        re.findall(r'else "(\w+)"', handler)
    )
    emitted &= STATES
    assert emitted, "no states found — the extraction is wrong, not the code"
    for st in emitted:
        assert re.search(rf"\b{st}: \{{", page), f"the page has no styling for {st!r}"


def test_the_page_explains_why_the_count_is_there(page):
    assert "wired to nothing" in page, (
        "the page no longer says why every claim carries a count; without it "
        "the column reads as trivia rather than as the check it is"
    )
