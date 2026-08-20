"""The Today page's reporting-window pills.

They shipped decorative: `range` was assigned by all three buttons and read by
nothing, so clicking "90 days" moved the highlight and left every number on the
page exactly where it was. No test noticed, because there was nothing wrong with
the code that ran — the bug was code that didn't.

That is the shape guarded here: a control whose only effect is on itself. These
read the file, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TODAY = (
    Path(__file__).resolve().parents[1]
    / "admin" / "src" / "routes" / "+page.svelte"
)


@pytest.fixture(scope="module")
def src() -> str:
    return TODAY.read_text()


def test_the_pills_change_the_window_not_just_the_highlight(src):
    """Every button goes through `setRange`, which refetches."""
    assert "onclick={() => setRange(r.id)}" in src, (
        "the range buttons no longer call setRange — if they assign `range` "
        "directly, the highlight moves and nothing refetches, which is the "
        "original bug"
    )
    body = re.search(r"function setRange\(id\) \{(.*?)\n  \}", src, re.S)
    assert body, "setRange is gone"
    assert "loadSummary()" in body.group(1), (
        "setRange sets `range` without refetching, so the pills are decorative "
        "again"
    )


def test_the_window_is_actually_derived_from_the_selected_range(src):
    """`windowOf` must read `range`, not a constant."""
    assert re.search(r"windowOf\(range\)", src), (
        "the summary is fetched over a window that does not depend on `range`"
    )
    body = re.search(r"function windowOf\(id\) \{(.*?)\n  \}", src, re.S)
    assert body, "windowOf is gone"
    assert "r.id === id" in body.group(1), (
        "windowOf ignores its argument, so all three pills request one window"
    )


def test_every_pill_names_a_real_number_of_days(src):
    """A pill labelled "90 days" must carry 90, in one place, next to its id."""
    block = re.search(r"const RANGES = \[(.*?)\];", src, re.S)
    assert block, "RANGES is gone"
    found = re.findall(r"id: '(\w+)', days: (\d+), label: '([\w ]+)'", block.group(1))
    assert found, "RANGES no longer carries id/days/label together"
    for _id, days, label in found:
        assert label.split()[0] == days, (
            f"the pill labelled {label!r} asks for {days} days — the label and "
            f"the window disagree, which is worse than no pill at all"
        )


def test_the_long_windows_do_not_come_from_the_in_memory_history(src):
    """`/metrics/history` is twelve minutes of process-local buckets that reset
    on restart. It can answer "90 days" only by pretending.

    The page no longer calls it at all — the hero sparkline it fed was
    demolished in 6.5 — so the strongest form of this guard is that the call
    stays gone. If a later change brings it back for something legitimately
    live, it must still never be handed a window."""
    calls = re.findall(r"getJSON\('(/metrics/history[^']*)'\)", src)
    if not calls:
        return
    after = src.split("getJSON('/metrics/history")[1][:200]
    assert "windowOf" not in after, (
        "a range window is being passed to /metrics/history, which cannot honour it"
    )
