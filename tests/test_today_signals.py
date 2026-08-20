"""The Today page's four Signals.

Three ways a KPI row lies, and each is guarded here.

The first is a sparkline drawn from the wrong series. `deltas` carries movement,
not shape, so the only daily series available is `by_day` — and `by_day` holds
`n` and `p50_ms` and nothing else. A p95 headline over a p50 line is two numbers
sharing one card, and no reader can tell.

The second is a direction that calls a regression an improvement. `good` is
per-metric: spend and latency going up is bad, and usage going up is neither.
Reading the sign instead would paint a quiet week red.

The third is an invented benchmark. Nobody has given this product a target for
cost or speed, so the footnotes state what the window measured and stop. A
threshold written here would be indistinguishable from an agreed one.

These read the file, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TODAY = Path(__file__).resolve().parents[1] / "admin" / "src" / "routes" / "+page.svelte"

# `by_day` rows from `/admin/analytics/summary` carry exactly these. Anything
# else passed to `seriesOf` draws a flat line rather than failing, which is why
# this list is pinned rather than inferred.
BY_DAY_KEYS = {"day", "n", "p50_ms"}


@pytest.fixture(scope="module")
def src() -> str:
    return TODAY.read_text()


@pytest.fixture(scope="module")
def sig_body(src: str) -> str:
    m = re.search(r"let sig = \$derived\.by\(\(\) => \{(.*?)\n  \}\);", src, re.S)
    assert m, "the signals list is gone"
    return m.group(1)


@pytest.fixture(scope="module")
def cards(sig_body: str):
    out = []
    for block in re.findall(r"\{\n        label: '(.*?)\n      \}", sig_body, re.S):
        label = block.split("'")[0]
        out.append((label, block))
    assert len(out) == 4, f"expected four signals, found {[c[0] for c in out]}"
    return out


def test_every_sparkline_comes_from_a_series_the_api_actually_returns(sig_body):
    """`seriesOf` may only be asked for a key `by_day` carries."""
    asked = set(re.findall(r"seriesOf\('(\w+)'\)", sig_body))
    assert asked, "no sparkline is fed from by_day any more"
    unknown = asked - BY_DAY_KEYS
    assert not unknown, (
        f"a sparkline is drawn from {sorted(unknown)}, which /analytics/summary "
        f"does not return per day — the line would be flat or absent under a "
        f"number that moves"
    )


def test_the_latency_card_draws_the_same_percentile_it_prints(cards):
    """`by_day` has p50 only. So the latency headline must be p50 too."""
    label, block = next(c for c in cards if "answered within" in c[0])
    assert "seriesOf('p50_ms')" in block, f"{label} lost its series"
    assert "value: fmtMs(p50)" in block and "deltaOf(d.p50_ms)" in block, (
        f"{label} prints or compares a percentile the sparkline underneath it "
        f"does not draw — p95 is on the footnote precisely because by_day has "
        f"no series for it"
    )


def test_direction_is_declared_per_metric_and_usage_stays_neutral(cards):
    """Green must always mean "this got better"."""
    good = {label: re.search(r"good: '(\w+)'", block).group(1) for label, block in cards}
    assert good["Questions asked"] == "none", (
        "usage is scored as good or bad, so a quiet week renders as a failure "
        "and a busy one as a win — neither is a claim this page can make"
    )
    for label in ("Came back empty", "Half answered within", "Model spend"):
        assert good[label] == "down", f"{label} would paint a regression green"


def test_no_card_invents_a_target(cards):
    """Footnotes report the window, they do not grade it.

    No owner has supplied targets (that question is still open), so a card that
    said "under 8s is healthy" would be reporting a decision nobody made."""
    banned = re.compile(r"\b(target|healthy|should be|acceptable|budget of|SLA)\b", re.I)
    for label, block in cards:
        foot = re.search(r"foot:(.*)$", block, re.S).group(1)
        hit = banned.search(foot)
        assert not hit, f"{label} states a benchmark ({hit.group(0)!r}) that nobody agreed"


def test_a_failed_summary_shows_no_numbers_at_all(src):
    """Four zeros are a measurement. A failed call is not."""
    section = src.split("<!-- SIGNALS -->")[1].split("<!-- hero -->")[0]
    assert "{#if summaryError}" in section, (
        "the signals row renders unconditionally, so a failed usage call prints "
        "four cards of zero and dashes as though the window were quiet"
    )
    guard = section.split("{#if summaryError}")[1].split("{:else}")[0]
    assert "missing rather than zero" in guard, (
        "the failure branch does not say the numbers are missing rather than zero"
    )
