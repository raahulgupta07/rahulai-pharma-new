"""The Chat screen's heading, which is deliberately not drawn.

Every other page on this console announces itself. Chat does not, and that is
the design's decision, not an oversight: the redesign's chat artboard has no
title bar — the thread starts at the top of the pane and the composer docks at
the bottom. Adding a header to satisfy a house rule would be the rule changing
the design.

The document still owes a heading, so there is a visually-hidden `h1` carrying
the same word the rail row does. This file pins both halves: the h1 exists and
is hidden, and no visible page header creeps back in.

The other thing pinned here is the rail. This is the one full-bleed screen, and
it used to hide the rail and hand-build a "← Console" link — a second
navigation, for one screen, that the design does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "admin" / "src" / "routes" / "chat" / "+page.svelte"
LAYOUT = ROOT / "admin" / "src" / "routes" / "+layout.svelte"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_the_document_still_has_a_heading(page):
    m = re.search(r'<h1 class="([^"]*)">([^<]+)</h1>', page)
    assert m, "the chat screen has no h1 at all, so the document has no heading"
    assert "sr-only" in m.group(1), (
        f"the chat h1 is visible ({m.group(1)!r}). The design gives this screen "
        f"no title bar; drawing one is the rule changing the design"
    )


def test_the_heading_is_the_word_on_the_rail(page):
    m = re.search(r'<h1 class="[^"]*">([^<]+)</h1>', page)
    assert m and m.group(1).strip() == "Chat", (
        "the hidden heading no longer matches the rail row, so a screen reader "
        "and a sighted reader are told different names for this screen"
    )


def test_no_page_header_creeps_in(page):
    assert "PageHeader" not in page, (
        "a page header has been added to Chat. Every other screen has one and "
        "this one does not, on purpose — see the note above the h1"
    )


def test_the_reason_is_written_down_next_to_it(page):
    assert "inventing one to satisfy a rule" in page, (
        "the note explaining why this screen has no visible heading is gone. "
        "Without it the next person adds one and thinks they fixed something"
    )


def test_the_rail_still_stands_on_this_screen():
    layout = LAYOUT.read_text()
    m = re.search(r"let fullBleed = \$derived\(([^)]*)\)", layout)
    assert m and "chat" in m.group(1), "chat is no longer the full-bleed screen"
    # The rail is drawn outside whatever `fullBleed` switches off.
    assert "The RAIL still stands" in layout, (
        "the note that the rail survives full-bleed is gone; it is what stops "
        "the hand-built back-link coming back"
    )
