"""The five states on Foundations, and the components that really draw them.

A reference page that redraws what it documents is worse than no reference
page: it is a confident description of something that stopped being true the
first time the component changed. So the page renders `ErrorState` itself and
calls `charts/format.js` itself, and these tests hold it to that.

The load-bearing claim on the page is the one about colour: **a refusal is not
a failure.** 401, 403 and 404 are facts about who you are or what this build
has — painting them red says the server broke, which the reader then has to
un-learn. That rule lives in a single `$derived` in a single component, and no
amount of prose on the reference page would notice if it were flattened back.
So the page reads the two groups' colours off the rendered panels, and this
file checks that the page's own list of what counts as a refusal still matches
the component's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "admin" / "src" / "routes" / "foundations" / "+page.svelte"
ERROR_STATE = ROOT / "admin" / "src" / "lib" / "ErrorState.svelte"
FORMAT = ROOT / "admin" / "src" / "lib" / "charts" / "format.js"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def component() -> str:
    return ERROR_STATE.read_text()


@pytest.fixture(scope="module")
def samples(page) -> list[tuple[int, str]]:
    """The failure samples the PAGE declares: (status, Refused | Failed)."""
    block = re.search(r"const FAILURES = \[(.*?)\n  \];", page, re.S)
    assert block, "the page no longer declares its failure samples"
    found = re.findall(r"status: (\d+), kind: '(\w+)'", block.group(1))
    assert found, "the samples carry no status/kind"
    return [(int(a), b) for a, b in found]


def test_the_samples_are_the_real_component(page):
    assert "import ErrorState from '$lib/ErrorState.svelte'" in page, (
        "the page no longer renders the real failure panel"
    )
    assert "<ErrorState" in page, "ErrorState is imported and never rendered"
    # The component owns this wording. A copy of it on the reference page is a
    # second source of truth that nothing keeps in step.
    for owned in ("Session expired", "Not permitted", "Cannot reach the backend"):
        assert page.count(owned) == 0, (
            f"the page has {owned!r} written into it. That string belongs to "
            f"ErrorState; a copy here drifts the moment the component changes"
        )


def test_every_branch_the_component_takes_is_shown(component, samples):
    """A status the component styles differently and the page never shows is a
    state nobody reviews."""
    branched = {int(m) for m in re.findall(r"status === (\d+)", component)}
    shown = {s for s, _ in samples}
    assert branched <= shown, f"status {sorted(branched - shown)} is never sampled"
    assert 0 in shown, "the no-response state is not sampled"
    assert any(s >= 500 for s in shown), "no 5xx state is sampled"


def test_the_page_and_the_component_agree_on_what_a_refusal_is(component, samples):
    """The component decides with one `$derived`. The page labels its samples
    by hand. If those two ever disagree, the page is teaching the wrong rule."""
    m = re.search(r"let broke = \$derived\(([^)]*)\)", component)
    assert m, "the refusal/failure split is gone from ErrorState"
    rule = m.group(1)
    assert "status === 0" in rule and "status >= 500" in rule, (
        f"the component's failure rule changed to: {rule.strip()}"
    )
    for status, kind in samples:
        expected = "Failed" if (status == 0 or status >= 500) else "Refused"
        assert kind == expected, (
            f"the page calls {status} {kind!r} and the component treats it as "
            f"{expected.lower()}"
        )


def test_the_colours_are_read_off_the_panels_and_not_typed(page):
    block = re.search(r"function measurePanels\(\)(.*?)\n  \}", page, re.S)
    assert block, "the panel colours are no longer measured"
    body = block.group(1)
    assert "getComputedStyle" in body and 'role="alert"' in body, (
        "the colours no longer come from the rendered panels"
    )
    section = page[page.index("The five states") :]
    typed = re.findall(r"(?:#[0-9A-Fa-f]{6}\b|rgba?\([\d\s,.]+\))", section)
    assert not typed, (
        f"the states section has colour(s) {typed} typed into it. What the "
        f"panels are painted must be read from the panels"
    )


def test_a_refusal_wearing_the_failure_colour_is_called_out(page):
    assert "let confused = $derived(" in page, (
        "the page no longer checks whether a refusal is painted as a failure — "
        "which is the one regression this section exists to catch"
    )
    assert "painted in the failure colour" in page, (
        "there is no message for a refusal drawn red"
    )


def test_a_panel_that_did_not_render_is_unknown_and_not_passing(page):
    assert "let unpainted = $derived(" in page, (
        "a sample that fails to render is not tracked, so a missing panel would "
        "silently shrink the comparison instead of being reported"
    )
    assert re.search(r"kind === 'Refused' && p\.bg", page), (
        "the counts in the verdict include panels whose colour was never read"
    )


def test_the_live_samples_cannot_act(page):
    """They are the real component, so their links and buttons work. A
    reference page must not sign somebody out because they clicked a sample."""
    assert re.search(r"data-state-sample=\{f\.status\} inert", page), (
        "the sample panels are live and not inert"
    )


def test_the_missing_value_dash_comes_from_the_module_that_owns_it(page):
    assert "import { UNKNOWN" in page, "the page no longer uses the shared dash"
    assert "'—'" not in page and '"—"' not in page, (
        "the page types the missing-value dash instead of importing UNKNOWN, so "
        "a change to the shared symbol would leave this page behind"
    )


def test_the_zero_and_the_unknown_are_shown_side_by_side(page):
    """The distinction is the point, and it is only visible as a pair."""
    for call in ("int(null)", "int(0)", "pct(null)", "pct(0)"):
        assert call in page, f"{call} is no longer demonstrated"
    assert "would read as free" in page, (
        "the consequence of collapsing the two is gone; without it the pair "
        "reads as a formatting curiosity"
    )


def test_the_state_with_no_owner_is_named_as_such(page):
    """`Nothing to show` has no shared component. Drawing a plausible one here
    would invent a standard the product does not have."""
    assert "no shared component" in page, (
        "the page no longer says that the empty state is unowned, so its sample "
        "reads as a component somebody can import"
    )


def test_the_formatters_still_refuse_to_coerce(page):
    """The page's claim about `format.js` has to stay true of `format.js`."""
    src = FORMAT.read_text()
    assert "export const UNKNOWN" in src, "UNKNOWN is gone from format.js"
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)
    assert "|| 0" not in body, (
        "`x || 0` is back in format.js — it turns a value nobody measured into "
        "a measured zero, which is exactly what the page says cannot happen"
    )
    assert "Number(null)" not in body, "format.js coerces null"
