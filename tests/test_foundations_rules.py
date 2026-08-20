"""The rules block on Foundations.

The redesign's own Foundations artboard heads this block "Four rules" and then
lists five. The page keeps all five and leaves the miscount visible, because a
silent correction is how a reader stops trusting the rest of a reference page.

Each rule carries a verdict measured in the browser, or an honest note that the
check lives somewhere else. This file is that somewhere else for rule 3, and it
holds the page to measuring the other four rather than describing them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"
PAGE = SRC / "routes" / "foundations" / "+page.svelte"
CSS = SRC / "app.css"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def rules(page) -> list[str]:
    """The rule headings the page draws, in order."""
    block = page[page.index("The four rules") :]
    return re.findall(r">(\d) · ([^<]+)</h3>", block)


def test_all_five_rules_are_drawn(rules):
    assert [n for n, _ in rules] == ["1", "2", "3", "4", "5"], (
        f"the rules are no longer 1-5 in order: {rules}"
    )


def test_the_miscount_is_left_visible(page):
    """Four rules, five listed. Correcting it quietly would be the page's own
    first act of tidying away something true."""
    assert "of which there are five" in page, "the heading no longer carries the miscount"
    assert "heads this block" in page, (
        "the page no longer says where the count came from, so the discrepancy "
        "reads as this page's own arithmetic error"
    )


def test_the_verdicts_are_computed_and_not_written(page):
    block = page[page.index("The four rules") :]
    for derived in ("dashHolds", "confused", "cyanTokens", "tightLeading"):
        assert derived in block, f"rule verdict {derived} is no longer used"
    # A rule whose verdict is a fixed string is a rule that passes forever.
    assert not re.search(r">Holds</span>\s*\n\s*(?!\{)", block) or "{#if" in block, (
        "a verdict is rendered unconditionally"
    )


# ---- rule 1 ----------------------------------------------------------------


def test_rule_one_is_checked_by_calling_the_formatters(page):
    assert "int(null) === UNKNOWN && int(0) !== UNKNOWN" in page, (
        "rule 1 no longer calls the console's own formatters, so it would keep "
        "reporting 'holds' after the distinction was lost"
    )


# ---- rule 3 ----------------------------------------------------------------


def test_rule_three_every_kpi_card_carries_its_reading():
    """The page says this check lives here. It has to actually live here.

    `Kpi` takes `foot=''` by default, so nothing stops a card shipping without
    one — a number with no interpretation rule is a number nobody acts on."""
    missing: list[str] = []
    total = 0
    for f in SRC.rglob("*.svelte"):
        src = f.read_text()
        for m in re.finditer(r"<Kpi\b", src):
            total += 1
            i, depth = m.end(), 0
            while i < len(src):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                elif src[i] == ">" and depth == 0:
                    break
                i += 1
            if not re.search(r"\bfoot\b", src[m.start() : i]):
                line = src[: m.start()].count("\n") + 1
                missing.append(f"{f.relative_to(ROOT)}:{line}")
    assert total > 0, "no KPI cards found — the scan is wrong, not the console"
    assert not missing, (
        f"{len(missing)} of {total} KPI cards carry no footnote:\n  "
        + "\n  ".join(missing[:15])
    )


def test_the_page_admits_where_rule_three_is_checked(page):
    assert "Checked in the tests, not here" in page, (
        "rule 3 now claims a verdict this browser cannot reach"
    )


# ---- rule 4 ----------------------------------------------------------------


def test_the_logo_cyan_is_not_a_palette_token():
    """It measures under half of AA on white. It is a mark, not an ink."""
    css = CSS.read_text()
    hit = [ln for ln in css.splitlines() if re.search(r"00ADEF", ln, re.I) and "--" in ln]
    assert not hit, (
        f"the logo cyan is back in the palette: {hit}. It measured 2.55:1 on "
        f"white when this page last worked it out"
    )


def test_rule_four_works_the_ratio_out_rather_than_quoting_it(page):
    block = page[page.index("The four rules") :]
    assert "cyan?.onWhite" in block, "the cyan's ratio is no longer computed"
    assert not re.search(r"\d\.\d+:1", block), (
        "a contrast ratio is typed into the rules block. The design's own copy "
        "quotes 2.6:1; this page works out its own and they differ"
    )


# ---- rule 5 ----------------------------------------------------------------


def test_the_burmese_samples_exist_before_they_are_measured(page):
    """The table is driven by the sample list, not by the results.

    Driven by its own results it starts empty, finds nothing to measure, and
    stays empty — reporting no problem because it never looked."""
    assert "{#each BURMESE as b (b.id)}" in page, (
        "the leading table is no longer driven by the sample list"
    )
    assert "leading.find((x) => x.id === b.id)" in page, (
        "the measurements are no longer looked up per sample"
    )


def test_the_leading_is_a_ratio_and_not_a_pixel_count(page):
    assert "lead / size" in page, (
        "leading is reported without its size, and 24px is generous at 12px and "
        "tight at 22px"
    )


def test_the_threshold_is_read_from_the_stylesheet(page):
    """The bar used to be typed on this page, which made it the SECOND place the
    rule lived — and the second place is the one that goes stale. It is a token
    now, so the surfaces and the bar they are held to cannot drift apart."""
    assert "--leading-bilingual" in page, "the page no longer reads the leading token"
    assert "let minLeading = $state(null)" in page, (
        "the threshold is a constant again rather than something measured"
    )
    css = (SRC / "app.css").read_text()
    assert re.search(r"--leading-bilingual:\s*[\d.]+;", css), (
        "the token the page reads is not defined"
    )
    block = page[page.index("The four rules") :]
    assert "1.9" not in block, (
        "the threshold is typed into the rules copy as well as read, so the two "
        "can disagree"
    )


def test_a_missing_threshold_is_not_a_pass(page):
    """`x < null` is false, so an unread token would report every surface as
    clearing a bar that does not exist."""
    assert "minLeading === null ? [] :" in page, (
        "the below-the-bar list no longer guards against an unread threshold"
    )
    assert "barUnknown" in page, (
        "there is no state for 'the bar could not be read', so the section would "
        "show a verdict it has no basis for"
    )


def test_every_surface_that_can_hold_burmese_wears_the_shared_class():
    """The rule is about lines that CAN hold Myanmar, not lines that do today.
    Content changes without anybody revisiting the CSS."""
    css = (SRC / "app.css").read_text()
    assert ".bilingual { line-height: var(--leading-bilingual); }" in css, (
        "the shared rule for bilingual lines is gone"
    )
    users = [
        p.relative_to(ROOT).as_posix()
        for p in SRC.rglob("*.svelte")
        if "bilingual" in p.read_text(errors="replace")
    ]
    for owed in (
        "admin/src/lib/charts/TurnDrawer.svelte",
        "admin/src/lib/analytics/AnalyticsPage.svelte",
        "admin/src/routes/chat/+page.svelte",
    ):
        assert owed in users, (
            f"{owed} renders questions or answers and no longer takes the "
            f"bilingual leading"
        )


def test_every_burmese_surface_names_a_real_place(page):
    block = re.search(r"const BURMESE = \[(.*?)\n  \];", page, re.S)
    assert block, "the Burmese surfaces are gone"
    entries = re.findall(r"klass: '([^']+)'", block.group(1))
    assert len(entries) >= 4, "fewer than four surfaces are checked for leading"
    # `.md` is the assistant's own reply — the one surface that is Burmese more
    # often than not. It must never drop off this list.
    assert "md" in entries, (
        "the chat answer is no longer checked, and it is the surface that holds "
        "Burmese most of the time"
    )


def test_a_surface_that_did_not_render_is_unknown(page):
    assert "unmeasuredLeading" in page, (
        "a sample that fails to render is not tracked, so a missing surface "
        "would quietly shrink the count instead of being reported"
    )
