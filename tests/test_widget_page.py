"""The Branch assistant page.

Its whole claim is "this is what a branch actually sees". The way that claim
goes wrong is by drifting: somebody rebuilds the widget's chrome in Svelte so
it looks right on this page, and from then on the one screen whose job is to
show the shipped widget is the screen most likely to disagree with it.

So the page must iframe `/embed/preview` — the same route the snippet
generator's demo page comes from, loading the same `widget.js` a customer site
loads — and must never grow its own message list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "admin" / "src" / "routes" / "widget" / "+page.svelte"


@pytest.fixture(scope="module")
def src() -> str:
    return PAGE.read_text()


def test_the_preview_is_the_shipped_widget_not_a_copy_of_it(src):
    assert "'/admin/embed/preview-link'" in src, (
        "the page no longer mints a preview link, so whatever it is showing is "
        "not the widget a branch loads"
    )
    assert "<iframe" in src, "the preview is no longer an iframe of the real page"
    # A rebuilt chat UI would need these. None of them belongs here.
    for banned in ("/api/embed/chat", "EventSource", "event: step", "messages = $state"):
        assert banned not in src, (
            f"{banned!r} suggests this page has started talking to the embed API "
            f"itself — a second widget implementation that will drift from the "
            f"one your branches actually run"
        )


def test_the_branch_comes_from_a_scoped_endpoint(src):
    """`/admin/stores` is scoped to the caller's own row; `/embed/outlets` is
    not scoped and is super_admin. Using the scoped one means a branch-pinned
    admin previews their own branch and cannot mint a link for anyone else's."""
    assert "'/admin/stores'" in src, (
        "the branch is no longer chosen through a store-scoped endpoint"
    )
    assert "/admin/embed/outlets" not in src, (
        "the branch list now comes from an unscoped endpoint, so a branch-pinned "
        "admin could mint a preview link for a branch they may not see"
    )


def test_the_page_says_when_it_cannot_preview_rather_than_showing_nothing(src):
    """Three different reasons, three different sentences. An empty frame reads
    as a broken widget in all three cases, and in exactly one of them the
    widget is fine and the reader is not a super admin."""
    assert "no-credential" in src and "no-store" in src, (
        "the blocked reasons have been collapsed, so 'nothing is registered' "
        "and 'nothing is in stock' would read the same"
    )
    assert "why?.status === 403" in src, (
        "a 403 on minting is no longer distinguished — it would be reported as "
        "the widget being broken when the widget is untouched"
    )


def test_the_expiry_is_stated(src):
    """The link is shareable while it lasts, so how long that is belongs on
    screen next to it rather than in the token."""
    assert "expires in" in src and "expiresAt" in src, (
        "the preview link's lifetime is no longer stated; it is a shareable URL "
        "to one branch's stock and its expiry is part of what it is"
    )


def test_the_iframe_keeps_the_origin_it_needs(src):
    """Same-origin, so `allow-same-origin` grants nothing new — and without it
    the widget's own fetches become cross-origin and depend on CORS."""
    m = re.search(r'sandbox="([^"]+)"', src)
    assert m, "the iframe is no longer sandboxed at all"
    grants = set(m.group(1).split())
    assert "allow-scripts" in grants and "allow-same-origin" in grants
    assert "allow-top-navigation" not in grants, (
        "the preview may not navigate the console out from under the reader"
    )
