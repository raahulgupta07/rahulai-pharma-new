"""The Foundations type scale, and the faces it claims to be drawn in.

The page measures both in the browser. These tests recompute the same things
from `app.css` in Python, so a token edit fails here rather than waiting for
somebody to open the page.

Two failure modes are specific to type and are guarded below.

**A step that does not exist still looks like a size.** `font-size: var(--gone)`
is invalid at computed-value time, so the element INHERITS — a plausible number
for a step nobody defined. The page must check the property itself, not the
rendered size.

**A face that is declared and never arrives.** Nothing errors, the text stays
readable, and everything is quietly a little wider than it was drawn to be. The
page proves each face by measuring pixels — and it must LOAD the face before
measuring it, because a webfont nothing on the page has used yet is not there
yet. Measuring first accused Noto Sans Myanmar of being missing when it was on
its way, which is the same class of wrong answer the page exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "admin" / "src" / "app.css"
PAGE = ROOT / "admin" / "src" / "routes" / "foundations" / "+page.svelte"


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def scale(css) -> dict[str, float]:
    """`--text-*`, in declaration order, as numbers."""
    found = re.findall(r"(--text-[\w-]+):\s*([\d.]+)px", css)
    assert found, "the type scale is gone from app.css"
    return {n: float(v) for n, v in found}


@pytest.fixture(scope="module")
def steps(page) -> list[str]:
    """The steps the PAGE lists, not a copy of them."""
    block = re.search(r"const STEPS = \[(.*?)\n  \];", page, re.S)
    assert block, "the page no longer declares its type steps"
    names = re.findall(r"\['(--text-[\w-]+)'", block.group(1))
    assert names, "the step list carries no token names"
    return names


def test_every_step_the_page_lists_is_defined(steps, scale):
    missing = [s for s in steps if s not in scale]
    assert not missing, (
        f"the page names {missing}, which app.css does not define. `font-size: "
        f"var()` on an undefined token INHERITS, so those rows would print a "
        f"plausible size for a step that is not there"
    )


def test_every_step_that_exists_is_shown(steps, scale):
    """A step defined and not drawn is a step nobody reviews."""
    absent = [s for s in scale if s not in steps]
    assert not absent, f"step(s) {absent} exist in app.css and are not on the page"


def test_the_scale_only_goes_up(steps, scale):
    sizes = [scale[s] for s in steps]
    assert sizes == sorted(sizes), (
        f"the page lists the steps out of order: {list(zip(steps, sizes))}. The "
        f"'up from the step below' column is a ratio to the previous row and "
        f"means nothing if the order is not the scale's"
    )
    assert len(set(sizes)) == len(sizes), "two steps hold the same size"


def test_the_sizes_are_read_and_not_typed(page, scale):
    """The whole point of the page. A number typed beside a token is a number
    somebody wrote down once, and the token can move without it."""
    typed = sorted(
        {
            m
            for m in re.findall(r"\b\d+(?:\.\d+)?px\b", page)
            if float(m[:-2]) in set(scale.values())
        }
    )
    assert not typed, (
        f"the page has step size(s) {typed} typed into it. Every size must come "
        f"from the running stylesheet"
    )


def test_a_missing_step_is_reported_missing_and_not_inherited(page):
    assert "getPropertyValue(name)" in page, (
        "the page no longer checks whether a step is DEFINED, so an undefined "
        "one would report whatever the element inherited"
    )
    assert "declared ? parseFloat" in page, (
        "the measured size is no longer gated on the token existing"
    )
    assert "not defined" in page, "a missing step no longer says so in the table"


def test_the_dense_band_is_counted_and_not_claimed(page, scale):
    """"dense where the console lives" is a claim about 11–14px. The page counts
    the steps in that band rather than asserting the shape of the scale."""
    assert re.search(r"px >= 11 && s\.px <= 14", page), (
        "the page no longer counts the steps in the band it claims to be dense"
    )
    band = [n for n, v in scale.items() if 11 <= v <= 14]
    assert len(band) >= 3, (
        f"only {len(band)} step(s) sit between 11px and 14px, where 92.5% of "
        f"this console's type was measured to be: {band}"
    )


@pytest.fixture(scope="module")
def faces(page) -> list[tuple[str, str]]:
    found = re.findall(r"family: '([^']+)',\s*\n\s*via: '(--font-[\w-]+)'", page)
    assert found, "the page no longer names the faces it checks"
    return found


def test_every_face_is_actually_in_the_stack_it_claims(faces, css):
    for family, via in faces:
        stack = re.search(rf"{re.escape(via)}:\s*([^;]+);", css)
        assert stack, f"{via} is not defined in app.css"
        assert family in stack.group(1), (
            f"the page says {family!r} is drawn via {via}, and {via} does not "
            f"name it — so the sample is rendered in something else"
        )


def test_every_face_is_one_the_stylesheet_actually_asks_for(faces, css):
    """A family in the stack that no @import requests can only ever come from
    the reader's own machine."""
    imports = " ".join(re.findall(r"@import url\(([^)]+)\)", css))
    for family, _ in faces:
        assert family.replace(" ", "+") in imports, (
            f"{family!r} is named in a font stack and requested by nothing. On a "
            f"branch machine without it installed, that text falls back silently"
        )


def test_every_stack_still_has_a_fallback(css):
    """This console runs on branch machines behind a pharmacy's network. If the
    font CDN is unreachable, the stack has to end somewhere sane."""
    for token in ("--font-sans", "--font-mono"):
        stack = re.search(rf"{token}:\s*([^;]+);", css)
        assert stack, f"{token} is gone"
        tail = stack.group(1).strip().rstrip(",").split(",")[-1].strip()
        assert tail in ("sans-serif", "serif", "monospace"), (
            f"{token} ends in {tail!r} rather than a generic family, so an "
            f"unreachable font CDN leaves it with nothing to fall back to"
        )


def test_a_face_is_loaded_before_it_is_measured(page):
    """The regression this page already had: Noto Sans Myanmar was reported
    substituted because nothing on the page had used it yet."""
    body = re.search(r"async function measureFaces\(\)(.*?)\n  \}", page, re.S)
    assert body, "measureFaces is gone, or is no longer async"
    body = body.group(1)
    load = body.find("document.fonts.load")
    width = body.find("getBoundingClientRect")
    assert load != -1, (
        "the faces are measured without being requested first, so any face the "
        "rest of the page does not already use reads as missing"
    )
    assert load < width, "the width is taken before the face is asked for"


def test_the_font_list_is_not_treated_as_evidence(page):
    """`document.fonts.check` answers about the font list; some browsers answer
    yes for a family they have never seen. The pixels are the evidence."""
    assert "the widths are the evidence" in page, (
        "the page no longer says why the font-list answer is shown but not "
        "trusted, so a wrong 'present' reads as confirmation"
    )
    assert re.search(r"painted\s*=\s*Math\.abs\(withFace - withoutFace\) > 0\.5", page), (
        "the drawn/substituted verdict no longer comes from the two widths"
    )
