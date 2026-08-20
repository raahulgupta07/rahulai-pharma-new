"""Guards on where the console's pages are, and on the links between them.

One defect is pinned here, because it shipped and nothing caught it for a
release. The analytics page began as ten TABS whose ids were section names —
`?tab=questions`, `?tab=cost`. Those ten were later grouped into six, and the
group ids replaced the section ids. All forty-six links inside the page still
named a SECTION:

    drillTo('path', slowest.key, 'performance')   -> ?tab=performance
    onclick={() => setTab('questions')}           -> ?tab=questions

`tab` falls back to `overview` when it does not recognise the value, so every
one of those links navigated, applied its filter, and drew the WRONG panel. The
numbers on screen changed, which is exactly why nobody reported it.

The sections are pages now and every link resolves through
`$lib/analytics/routes.js`. These tests hold that arrangement together:

  * a section the shared component can draw must have a page that draws it,
  * exactly one page draws it, so a link has one destination,
  * every link target is a section that exists,
  * every rail row and every redirect points at a route that exists,
  * a section's data feeds are declared, or it renders empty and silent.

They read the files, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "admin" / "src"
ROUTES_JS = SRC / "lib" / "analytics" / "routes.js"
PAGE = SRC / "lib" / "analytics" / "AnalyticsPage.svelte"
LAYOUT = SRC / "routes" / "+layout.svelte"
ROUTES_DIR = SRC / "routes"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section_route() -> dict[str, str]:
    """section id -> the route that draws it, straight out of routes.js."""
    body = _text(ROUTES_JS)
    block = re.search(r"export const SECTION_ROUTE = \{(.*?)\n\};", body, re.S)
    assert block, "SECTION_ROUTE is not where the tests expect it"
    return dict(re.findall(r"(\w+): '([^']+)'", block.group(1)))


@pytest.fixture(scope="module")
def drawn() -> set[str]:
    """Every section id the shared component can render."""
    ids = set(re.findall(r"has\('(\w+)'\)", _text(PAGE)))
    assert ids, "no has('…') blocks found — did the component change shape?"
    return ids


@pytest.fixture(scope="module")
def pages() -> dict[Path, list[str]]:
    """Every component that mounts AnalyticsPage -> the sections it asks for."""
    out: dict[Path, list[str]] = {}
    for p in sorted(SRC.rglob("*.svelte")):
        m = re.search(r"sections=\{\[([^\]]+)\]\}", _text(p))
        if m:
            out[p] = re.findall(r"'(\w+)'", m.group(1))
    assert out, "nothing mounts AnalyticsPage"
    return out


def _route_dirs() -> set[str]:
    """Every route path the app defines, as it appears in an href."""
    found = {"/"}
    for p in ROUTES_DIR.rglob("+page.svelte"):
        rel = p.parent.relative_to(ROUTES_DIR).as_posix()
        found.add("/" if rel == "." else "/" + rel)
    return found


# ---------------------------------------------------------------- sections


def test_every_section_the_component_draws_has_a_route(drawn, section_route):
    orphans = sorted(drawn - set(section_route))
    assert not orphans, (
        "these sections can be rendered but no route claims them, so a link to "
        "one has nowhere to go: " + ", ".join(orphans)
    )


def test_every_routed_section_is_one_the_component_draws(drawn, section_route):
    ghosts = sorted(set(section_route) - drawn)
    assert not ghosts, (
        "SECTION_ROUTE names sections the component cannot draw — a link to one "
        "navigates to a page that renders nothing: " + ", ".join(ghosts)
    )


def test_every_section_is_drawn_somewhere(drawn, pages):
    where: dict[str, list[str]] = {}
    for path, ids in pages.items():
        for i in ids:
            where.setdefault(i, []).append(path.relative_to(SRC).as_posix())
    missing = sorted(drawn - set(where))
    assert not missing, (
        "these sections exist in the component but no page asks for them, so "
        "they are dead markup: " + ", ".join(missing)
    )


def test_each_section_is_drawn_by_the_route_that_claims_it(pages, section_route):
    """The map has to agree with the pages. If `/cost` draws `cost` but the map
    sends `cost` to `/analytics`, every cost link lands on the wrong page — the
    original defect, with the pieces rearranged.

    A section may appear on MORE than one page: /security-log is the auth slice
    of the same Audit section the event feed draws. What must hold is that the
    page named by the map is one of them — that is where a link goes.
    """
    where: dict[str, set[str]] = {}
    for path, ids in pages.items():
        # The route a page belongs to: its own directory, or — for a panel
        # mounted as a tab — the directory of the page that mounts it.
        route = "/" + path.parent.relative_to(ROUTES_DIR).as_posix()
        for i in ids:
            where.setdefault(i, set()).add(route)
    wrong = {
        i: (target.split("?")[0], sorted(where.get(i, ())))
        for i, target in section_route.items()
        if target.split("?")[0] not in where.get(i, ())
    }
    assert not wrong, (
        "SECTION_ROUTE points these sections at a page that does not draw them "
        f"(section: (map target, pages that draw it)): {wrong}"
    )


# ------------------------------------------------------------------- links


def test_every_cross_link_names_a_section_that_exists(section_route):
    """THE shipped defect: a link naming an id nothing resolves."""
    src = _text(PAGE)
    targets = set(re.findall(r"crossTo\('(\w+)'", src))
    targets |= set(re.findall(r"drillTo\([^,]+,[^,]+,\s*'(\w+)'", src))
    targets |= set(re.findall(r"section: '(\w+)'", src))
    assert targets, "no cross-links found — did they change shape?"
    unknown = sorted(targets - set(section_route))
    assert not unknown, (
        "these links name a destination no route draws. This is the defect that "
        "shipped: the link navigates, the filter applies, and the reader lands "
        "on the wrong panel with numbers that changed — so it reads as working. "
        + ", ".join(unknown)
    )


def test_no_link_still_writes_a_tab_parameter():
    """`?tab=` on the analytics page is how the dead links were spelled. The
    tabbed shells (/quality, /embed, /settings, /users) still use it; the shared
    analytics component must not."""
    src = _text(PAGE)
    assert "p.set('tab'" not in src, (
        "AnalyticsPage writes `tab` again. Sections are routes now — use "
        "crossTo(section), which resolves through routes.js."
    )


# -------------------------------------------------------------------- rail


def test_every_rail_row_points_at_a_route():
    src = _text(LAYOUT)
    block = re.search(r"const SECTIONS = \[(.*?)\n  \];", src, re.S)
    assert block, "the rail is not where the test expects it"
    hrefs = re.findall(r"href: '([^']+)'", block.group(1))
    assert len(hrefs) >= 10, f"only {len(hrefs)} rail rows — did the rail move?"
    known = _route_dirs()
    dead = sorted({h for h in hrefs if h.split("?")[0] not in known})
    assert not dead, "rail rows pointing at routes that do not exist: " + ", ".join(dead)


def test_off_rail_destinations_stay_reachable_by_name():
    """A destination dropped from the rail has to stay in "/" search, or the
    reorganisation deleted it while looking like a tidy-up."""
    src = _text(LAYOUT)
    block = re.search(r"const OFF_RAIL = \[(.*?)\n  \];", src, re.S)
    assert block, "OFF_RAIL is gone — the pages it listed are now unreachable by name"
    hrefs = re.findall(r"href: '([^']+)'", block.group(1))
    known = _route_dirs()
    dead = sorted({h for h in hrefs if h.split("?")[0] not in known})
    assert not dead, "off-rail entries pointing nowhere: " + ", ".join(dead)
    assert "/activity" in hrefs, (
        "the event feed is not a rail row; if it is not in OFF_RAIL either, the "
        "only way to reach it is a URL somebody remembers"
    )


def _redirect_args(src: str) -> str:
    """The target expressions of every redirect() in a file, concatenated."""
    return " ".join(
        m.group(1) for m in re.finditer(r"redirect\(\d+,\s*(.+?)\);", src, re.S)
    )


def test_every_redirect_points_at_a_route_that_exists():
    known = _route_dirs()
    bad = {}
    for p in sorted(ROUTES_DIR.rglob("+page.js")):
        # `base + '/x'` and `` `${base}/x` `` are both in use.
        for target in re.findall(r"/(?:[\w-]+)(?:\?[^`'\"]*)?", _redirect_args(_text(p))):
            path = target.split("?")[0].rstrip("/")
            if path and path not in known:
                bad[p.relative_to(SRC).as_posix()] = path
    assert not bad, f"redirects pointing at routes that do not exist: {bad}"


# ------------------------------------------------------------------ feeds


def test_every_section_declares_the_feeds_it_reads(drawn):
    """A section left out of SECTION_FEEDS renders from `blank()` — every panel
    draws an em-dash rather than crashing, so the page looks like a quiet day
    instead of a page that asked for nothing."""
    src = _text(PAGE)
    block = re.search(r"const SECTION_FEEDS = \{(.*?)\n  \};", src, re.S)
    assert block, "SECTION_FEEDS is gone — every page would fetch all eighteen feeds"
    declared = set(re.findall(r"^\s{4}(\w+):", block.group(1), re.M))
    # The event sections read /admin/activity/* through their own components and
    # deliberately declare nothing here.
    events = set(re.findall(r"'(\w+)'", re.search(
        r"ACTIVITY_SECTIONS = new Set\(\[([^\]]+)\]", _text(ROUTES_JS)).group(1)))
    missing = sorted(drawn - declared - events)
    assert not missing, (
        "these sections read the analytics endpoints but declare no feeds, so "
        "every number on them will render as an em-dash: " + ", ".join(missing)
    )
