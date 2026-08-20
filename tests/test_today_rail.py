"""The Today page's right rail: branch assistant, read-only, latest activity.

Two failure modes are guarded here, both of which shipped once on this page.

The first is **attribution**. This console's own chat tester logs to
``chat_logs`` as an embed client (``admin-chat``), so a card that sums every
embed row reports the operator's own testing back to them as branch usage. On
the instance this was written against that is 11 of 12 turns — not a rounding
error, an inverted claim. The page's own subhead used to say "Branch staff
asked 12 questions" for exactly that reason.

The second is **permission read as absence**. ``/admin/activity`` is
super_admin only. An admin gets 403, and an empty list under a heading that
says "Latest activity" reads as "nothing has happened" rather than "you were
not shown it".

These read the file, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TODAY = (
    Path(__file__).resolve().parents[1]
    / "admin" / "src" / "routes" / "+page.svelte"
)

#: The one embed id that is this console, not a branch. Pinned against the
#: backend constant so a rename there fails here rather than silently folding
#: console traffic back into the branch count.
CONSOLE_EMBED = "admin-chat"


@pytest.fixture(scope="module")
def src() -> str:
    return TODAY.read_text()


def _branch_block(src: str) -> str:
    m = re.search(r"let branch = \$derived\.by\(\(\) => \{(.*?)\n  \}\);", src, re.S)
    assert m, "the branch split is gone"
    return m.group(1)


def test_the_console_is_not_counted_as_a_branch(src):
    """The card's whole job is the split, so the exclusion is not optional."""
    assert f"const CONSOLE_EMBED = '{CONSOLE_EMBED}';" in src, (
        "the console's own embed id is no longer named — without it every row "
        "from /analytics/embeds counts as branch traffic, and the operator's "
        "own testing is reported back to them as branch usage"
    )
    block = _branch_block(src)
    assert "r?.embed_id !== CONSOLE_EMBED" in block, (
        "the branch rows no longer exclude the console, so the card counts "
        "questions asked in this very console as questions asked by a branch"
    )


def test_the_console_id_matches_the_backend_constant():
    """A rename on either side must not silently re-merge the two counts."""
    cache = (Path(__file__).resolve().parents[1] / "app" / "cache.py").read_text()
    assert f'INTERNAL_CHAT_EMBED_ID = "{CONSOLE_EMBED}"' in cache, (
        "app/cache.py no longer calls the console embed "
        f"{CONSOLE_EMBED!r} — the Today rail filters on that literal, so the "
        "console's turns would start counting as branch traffic"
    )


def test_unattributed_turns_are_neither_branch_nor_console(src):
    """A NULL embed_id is a pre-instrumentation turn. It is its own answer."""
    block = _branch_block(src)
    assert "r?.embed_id != null" in block, (
        "rows with no embed id are being counted as branch traffic; they are "
        "unattributable, and folding them in makes the one number this card "
        "exists for untrustworthy"
    )
    assert "unattributed" in block, "the unattributed count is gone"
    assert "branch.unattributed" in src, (
        "the unattributed turns are counted and never shown, which is the same "
        "as not counting them"
    )


def test_the_subhead_no_longer_attributes_every_question_to_a_branch(src):
    """The count was right; "Branch staff asked" was invented."""
    assert "Branch staff asked" not in src, (
        "the subhead is attributing the whole turn count to branch staff again "
        "— most of it is console traffic on any instance where the operator "
        "has used the chat tester"
    )


def test_the_branch_card_and_signals_read_the_same_window(src):
    """Two counts of the same thing over different windows is a defect."""
    m = re.search(r"async function loadSummary\(\) \{(.*?)\n  \}", src, re.S)
    assert m, "loadSummary is gone"
    body = m.group(1)
    assert body.count("windowOf(range)") == 2, (
        "the embeds call no longer uses the selected window, so the branch "
        "card and Signals would count over different periods on one screen"
    )


def test_a_forbidden_feed_is_never_drawn_as_an_empty_one(src):
    """403 is an answer about the reader, not about the system."""
    m = re.search(r'aria-labelledby="feed-heading"(.*?)</section>', src, re.S)
    assert m, "the activity section is gone"
    block = m.group(1)
    assert "feedError.status === 403" in block, (
        "the feed no longer distinguishes 403 from any other failure — an "
        "admin would be told the feed 'did not answer' when in fact it "
        "answered, and said no"
    )
    assert "not because nothing has happened" in block, (
        "the 403 branch no longer says the list is empty for a permission "
        "reason, which is the only thing that separates it from a quiet day"
    )
    # The empty branch must be reachable ONLY when the call succeeded.
    assert "{:else if feed && feed.length}" in block, (
        "the feed renders rows without first proving the call succeeded"
    )


def test_the_feed_link_names_the_section_not_a_tab(src):
    """`?tab=` falls back to Overview without erroring — see the routes test."""
    assert "openSection('feed')" in src, (
        "the Full feed link no longer resolves through openSection, so it will "
        "not follow the section when it moves and may quietly land on Overview"
    )
