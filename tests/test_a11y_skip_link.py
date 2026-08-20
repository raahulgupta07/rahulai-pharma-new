"""The skip link, and the four ways it silently does nothing.

The rail is nineteen tab stops and it comes before the content in the DOM on
every screen in the console. Without a skip link a keyboard user pressed Tab
nineteen times, on every page, to reach the thing they were already looking at.

A skip link is easy to add and easy to add uselessly. Each test here guards one
failure that leaves the link present in the source, visible in review, and
inert in the product:

* hidden with ``display:none`` / ``visibility:hidden`` — both remove it from
  the tab order, so the one control that exists to be tabbed to cannot be;
* pointing at an element with no ``tabindex="-1"`` — the browser scrolls but
  focus stays on the link, and the next Tab goes straight back into the rail;
* letting the anchor's default jump run — this is a router, and appending
  ``#main-content`` pushes a history entry;
* placed after the rail in the DOM, where it is the twentieth tab stop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "admin" / "src" / "routes" / "+layout.svelte"
CSS = ROOT / "admin" / "src" / "app.css"

TARGET = "main-content"


@pytest.fixture(scope="module")
def layout() -> str:
    return LAYOUT.read_text()


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


def test_the_skip_link_exists_and_names_the_target(layout):
    m = re.search(r'<a href="#([\w-]+)" class="skip-link"', layout)
    assert m, "there is no skip link in the shell"
    assert m.group(1) == TARGET, f"the skip link points at #{m.group(1)}, not #{TARGET}"


def test_it_comes_before_the_rail_in_the_dom(layout):
    """Otherwise it is the twentieth tab stop and skips nothing."""
    link = layout.index('class="skip-link"')
    rail = layout.index('<div\n      class="rail')
    assert link < rail, (
        "the skip link is after the rail in the DOM, so a keyboard user reaches "
        "it only after tabbing through every rail row — which is the thing it "
        "exists to avoid"
    )


def test_every_main_is_a_focusable_target(layout):
    """Both branches of the shell, not just the one that was open at the time."""
    # Anchored at line start: this file's own prose says `<main>` twice, and an
    # unanchored scan reported those comments as un-targeted mains.
    mains = re.findall(r"^\s*(<main\b[^>]*>)", layout, re.M)
    assert mains, "the shell has no <main>"
    for tag in mains:
        assert f'id="{TARGET}"' in tag, (
            f"a <main> is not the skip target, so on the routes that render it "
            f"the link jumps to nothing:\n  {tag}"
        )
        assert 'tabindex="-1"' in tag, (
            f"<main> is not focusable, so .focus() is a no-op: the page scrolls "
            f"but focus stays on the link and the next Tab returns to the rail\n"
            f"  {tag}"
        )


def test_focus_is_moved_by_hand_and_the_default_jump_is_prevented(layout):
    m = re.search(r"function skipToContent\(.*?\n  \}", layout, re.S)
    assert m, "the skip handler is gone"
    body = m.group(0)
    assert "preventDefault" in body, (
        "the anchor's default jump still runs; in a router that pushes a "
        "history entry and can change the route on any page that reads the hash"
    )
    assert ".focus()" in body, (
        "nothing moves focus, so the link scrolls the page and leaves the "
        "keyboard user exactly where they were"
    )
    assert TARGET in body, "the handler no longer looks up the skip target"


def test_it_is_hidden_by_transform_and_not_removed_from_the_tab_order(css):
    """`display:none` and `visibility:hidden` un-focus it. So does `hidden`."""
    m = re.search(r"\.skip-link \{([^}]*)\}", css)
    assert m, "the .skip-link rule is gone"
    body = m.group(1)
    for banned in ("display: none", "display:none", "visibility: hidden", "visibility:hidden"):
        assert banned not in body, (
            f"the skip link is hidden with `{banned}`, which takes it out of "
            f"the accessibility tree AND the tab order — it can no longer be "
            f"reached by the keyboard at all"
        )
    assert "transform:" in body, (
        "the skip link is no longer moved off-screen by transform; if it is "
        "hidden some other way, check that the way chosen keeps it focusable"
    )
    assert re.search(r"\.skip-link:focus(?!-visible) \{[^}]*transform:", css), (
        "nothing brings the skip link back on screen when it takes focus, so "
        "it is a focus stop the user cannot see. Note `:focus`, not "
        "`:focus-visible` — this control exists only to be focused"
    )


def test_the_skip_links_ring_is_drawn_inside_the_pill(css):
    """The global ring sits 2px OUTSIDE the control it marks.

    For every other control that is correct — the surface behind it is a page
    surface, and the ring is measured against those. The skip link is the one
    control that carries its own filled background, and its ring colour is
    `--c-on-accent`: white in light mode. At the global +2px offset that white
    ring landed on the near-white page, measured at **1.08:1**. A negative
    offset puts it on the accent, which is the only background `--c-on-accent`
    is guaranteed against — and it is what tests/test_a11y_focus.py measures
    it against, so this assertion is what keeps that measurement honest.
    """
    m = re.search(r"\.skip-link:focus-visible \{([^}]*)\}", css)
    assert m, (
        "the skip link no longer pulls its focus ring inside the pill, so the "
        "ring is drawn on the page behind it at 1.08:1 in light mode"
    )
    off = re.search(r"outline-offset:\s*(-?[\d.]+)px", m.group(1))
    assert off and float(off.group(1)) < 0, (
        f"the skip link's outline-offset is {m.group(1).strip()!r}; it must be "
        f"negative or the ring lands on the page instead of on the accent"
    )
