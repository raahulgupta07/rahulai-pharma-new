"""The Today page's "Needs you" triage list.

Two failure modes are guarded here, and neither is a rendering bug.

The first is a list that quietly drops findings. The design gives this section
three cards; the data does not agree to produce exactly three. Truncating to fit
turns a layout decision into a filter, and a truncated list reads as "that is
everything" — the reader has no way to tell.

The second is a page that claims health it did not measure. An empty triage list
means "we looked and found nothing" only when something was actually read; when
the calls that feed it failed, the same empty list means "we did not look". The
markup must say which, and must never print "Nothing needs you." over a pair of
failed requests.

These read the file, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TODAY = Path(__file__).resolve().parents[1] / "admin" / "src" / "routes" / "+page.svelte"


@pytest.fixture(scope="module")
def src() -> str:
    return TODAY.read_text()


@pytest.fixture(scope="module")
def needs_body(src: str) -> str:
    m = re.search(r"let needs = \$derived\.by\(\(\) => \{(.*?)\n  \}\);", src, re.S)
    assert m, "the needs list is gone"
    return m.group(1)


def test_the_card_cap_does_not_hide_a_finding(src):
    """Cards are capped at three; the rest are listed, not dropped."""
    assert "needs.slice(0, SHOWN)" in src, "the card cap is gone"
    assert "needs.slice(SHOWN)" in src, (
        "findings past the third are computed nowhere, so the cap is silently "
        "deleting them — a triage list that hides an item is worse than no list"
    )
    assert "restNeeds" in src.split("<!-- NEEDS YOU -->")[1], (
        "the overflow findings are never rendered"
    )


def test_the_headline_counts_every_finding_not_the_visible_ones(src):
    """`Four things need you.` over three cards is only honest if the fourth is
    reachable — and the count must come from the full list either way."""
    m = re.search(r"let needsClause = \$derived\((.*?)\n  \);", src, re.S)
    assert m, "the headline clause is gone"
    body = m.group(1)
    assert "needs.length" in body, "the clause counts something other than the findings"
    assert "shownNeeds" not in body and "SHOWN" not in body, (
        "the headline counts only the cards on screen, so a fourth finding "
        "disappears from the sentence as well as from the grid"
    )


def test_nothing_needs_you_is_never_printed_over_a_failed_call(src):
    """The empty state is a measurement, so it must be guarded by one."""
    section = src.split("<!-- NEEDS YOU -->")[1].split("<!-- hero -->")[0]
    assert "healthError && summaryError" in section, (
        "there is no branch for both sources failing, so 'Nothing needs you.' "
        "renders when nothing was read"
    )
    empty = section.split("Nothing needs you.")[1][:600]
    assert "healthError" in empty, (
        "the all-clear does not mention that the data checks failed, so a page "
        "that only read ratings claims to have read everything"
    )


def test_the_head_and_the_stale_card_cannot_disagree(src):
    """One constant decides when a stock file is old.

    The subhead's "so answers are current" and the stale-file card are the same
    claim from two sides. Two literals would eventually drift into a page that
    says both."""
    assert src.count("const STALE_HOURS = 24;") == 1, "STALE_HOURS is gone or duplicated"
    assert "hours < 24" not in src, (
        "the subhead still carries its own staleness literal, so the head and "
        "the card can contradict each other"
    )


def test_every_finding_carries_its_own_number_and_a_place_to_go(needs_body):
    """A card that says "check the catalog" with no count is a chore, not a
    finding — and one with no link is a dead end."""
    entries = re.findall(r"out\.push\(\{(.*?)\n      \}\);", needs_body, re.S)
    assert len(entries) >= 5, f"expected the full triage set, found {len(entries)}"
    for e in entries:
        ident = re.search(r"id: '([\w-]+)'", e).group(1)
        assert "href:" in e, f"{ident} has no link"
        assert "cta:" in e, f"{ident} has no call to action"
        assert re.search(r"tone: '(danger|warning|info)'", e), f"{ident} has no severity"


def test_thumbs_down_outranks_the_row_counts(needs_body):
    """Ordering is by how directly a finding is already making an answer wrong.

    A single thumbs-down is a person saying the answer WAS wrong; four hundred
    stub rows are rows that MIGHT produce one. Push order is display order, so
    this is the ordering."""
    order = re.findall(r"id: '([\w-]+)'", needs_body)
    assert order[0] == "rated-down", f"ratings are no longer ranked first: {order}"
    assert order.index("stub-rows") == len(order) - 1, (
        f"the standing catalog backlog is no longer ranked last: {order}"
    )
