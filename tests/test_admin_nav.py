"""The rail, and every destination it does or does not name.

A navigation reorganisation is the easiest place in a console to delete
something by accident: a page merged into a tab keeps working, keeps its URL,
and simply stops being findable. Nobody notices, because nothing errors.

So the rail is checked against the routes on disk in both directions — a row
pointing at nothing, and a route nobody can reach by name, are both failures.
The rail's own colours are checked too: it is dark in BOTH themes on purpose,
and that is an invariant rather than an accident of the palette.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_SRC = ROOT / "admin" / "src"
LAYOUT = ADMIN_SRC / "routes" / "+layout.svelte"
CSS = ADMIN_SRC / "app.css"


@pytest.fixture(scope="module")
def layout() -> str:
    return LAYOUT.read_text()


@pytest.fixture(scope="module")
def rail(layout) -> list[tuple[str, str]]:
    """(href, label) for every row the rail draws."""
    block = re.search(r"const SECTIONS = \[(.*?)\n  \];", layout, re.S)
    assert block, "the rail's section list is gone"
    rows = re.findall(r"\{ href: '([^']+)', label: '([^']+)'", block.group(1))
    assert rows, "the rail draws no rows"
    return rows


@pytest.fixture(scope="module")
def off_rail(layout) -> list[tuple[str, str]]:
    block = re.search(r"const OFF_RAIL = \[(.*?)\n  \];", layout, re.S)
    assert block, "the off-rail destination list is gone"
    return re.findall(r"\{ href: '([^']+)', label: '([^']+)'", block.group(1))


@pytest.fixture(scope="module")
def routes() -> set[str]:
    """Every route that exists on disk, as the path the rail would use."""
    out = set()
    for p in (ADMIN_SRC / "routes").rglob("+page.svelte"):
        rel = p.parent.relative_to(ADMIN_SRC / "routes").as_posix()
        out.add("/" if rel == "." else f"/{rel}")
    return out


def test_every_rail_row_points_at_a_page_that_exists(rail, routes):
    dead = [(h, l) for h, l in rail if h.split("?")[0] not in routes]
    assert not dead, f"rail row(s) point at nothing: {dead}"


def test_every_off_rail_destination_exists(off_rail, routes):
    dead = [(h, l) for h, l in off_rail if h.split("?")[0] not in routes]
    assert not dead, f"off-rail destination(s) point at nothing: {dead}"


@pytest.fixture(scope="module")
def redirects(rail, off_rail) -> dict[str, str]:
    """Routes that exist ONLY to forward an old bookmark, and where they go.

    A merged page keeps its URL. Losing the shim turns every bookmark and every
    link in an old email into a blank screen — the same deletion as dropping the
    row, arriving later.

    A page that is still in the rail and also forwards some old query strings
    (`/analytics` does) is not a shim; its targets are computed and are checked
    through `SECTION_ROUTE` instead."""
    named = {h.split("?")[0] for h, _ in rail} | {h.split("?")[0] for h, _ in off_rail}
    out = {}
    for p in (ADMIN_SRC / "routes").rglob("+page.js"):
        rel = "/" + p.parent.relative_to(ADMIN_SRC / "routes").as_posix()
        if rel in named:
            continue
        m = re.search(r"redirect\(\s*\d+,\s*[`'\"]?[^`'\"]*?base[}]?\s*[+]?\s*[`'\"]?(/[^`'\"]*)", p.read_text())
        if m:
            out[rel] = m.group(1)
    return out


def test_no_page_is_reachable_only_by_typing_its_url(rail, off_rail, redirects, routes):
    """A page in neither list and forwarding nowhere can be opened and cannot be
    FOUND. That is a deletion wearing a tidier label."""
    named = {h.split("?")[0] for h, _ in rail} | {h.split("?")[0] for h, _ in off_rail}
    orphans = sorted(routes - named - set(redirects))
    assert not orphans, (
        f"route(s) {orphans} appear in neither the rail nor the search index and "
        f"forward nowhere, so the only way to reach them is to know the URL"
    )


def test_every_old_url_still_lands_somewhere_real(redirects, routes):
    assert redirects, "no redirect shims found — every merged URL now 404s"
    dead = {src: dst for src, dst in redirects.items() if dst.split("?")[0] not in routes}
    assert not dead, f"redirect(s) point at a page that does not exist: {dead}"


def test_every_analytics_section_is_drawn_by_a_page_that_exists(routes):
    """The section→page map is how forty-six links resolve. A section pointing
    at a page that no longer exists is a click that looks like it worked."""
    src = (ADMIN_SRC / "lib" / "analytics" / "routes.js").read_text()
    block = re.search(r"SECTION_ROUTE = \{(.*?)\n\};", src, re.S)
    assert block, "the section→page map is gone"
    targets = re.findall(r"\w+: '([^']+)'", block.group(1))
    assert targets, "the map is empty"
    dead = sorted({t for t in targets if t.split("?")[0] not in routes})
    assert not dead, f"analytics section(s) point at a page that does not exist: {dead}"


def test_a_merged_url_keeps_the_base_path(redirects):
    """This is a static SPA served under /admin. A redirect target without the
    base lands outside the app entirely."""
    for p in (ADMIN_SRC / "routes").rglob("+page.js"):
        src = p.read_text()
        if "redirect(" not in src:
            continue
        assert "base" in src, (
            f"{p.relative_to(ROOT)} redirects without the base path, so it "
            f"navigates out of the console"
        )


def test_the_rail_rows_are_unique(rail):
    hrefs = [h for h, _ in rail]
    assert len(set(hrefs)) == len(hrefs), f"a destination is in the rail twice: {hrefs}"
    labels = [l for _, l in rail]
    assert len(set(labels)) == len(labels), f"two rail rows share a label: {labels}"


def test_every_rail_row_sits_under_a_section(layout, rail):
    """A row outside a group renders without its heading and reads as belonging
    to whatever is above it."""
    block = re.search(r"const SECTIONS = \[(.*?)\n  \];", layout, re.S).group(1)
    groups = re.findall(r"label: '([^']*)',\s*\n?\s*items:", block)
    assert len(groups) >= 6, f"the rail has collapsed to {len(groups)} group(s)"
    assert groups[0] == "", "the first group is no longer the ungrouped Today row"
    assert all(g for g in groups[1:]), "a group after the first has no heading"


def test_the_search_index_is_the_rail_plus_the_merged_pages(layout):
    assert "const ALL_PAGES" in layout, "the search index is gone"
    assert re.search(r"ALL_PAGES = \[\s*\.\.\.SECTIONS", layout), (
        "the search index is no longer built from the rail, so the two can "
        "disagree about what exists"
    )
    assert "...OFF_RAIL" in layout, (
        "the merged destinations are no longer in search — a page folded into a "
        "tab would then have no way to be found by name"
    )


# ---- the rail's own colours -------------------------------------------------


def _decls(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--c-[\w-]+):\s*([^;]+);", block))


def _block(css: str, sel: str) -> str:
    i = css.index(sel)
    return css[i : css.index("}", css.index("{", i))]


@pytest.fixture(scope="module")
def themes() -> tuple[dict[str, str], dict[str, str]]:
    css = CSS.read_text()
    light = _decls(_block(css, ":root"))
    dark = dict(light)
    dark.update(_decls(_block(css, "html.dark")))
    return light, dark


def test_the_rail_does_not_follow_the_reader(themes):
    """The rail is a fixed piece of the product, not a surface that switches.
    The theme toggle moves the page BESIDE it."""
    light, dark = themes
    rail = [k for k in light if k.startswith("--c-rail")]
    assert rail, "the rail has no tokens"
    moved = {k: (light[k], dark[k]) for k in rail if light[k] != dark[k]}
    assert not moved, (
        f"rail token(s) change with the theme: {moved}. The rail is dark in "
        f"both themes by design, and a half-switching rail is worse than either"
    )


def test_the_sign_in_showcase_does_not_follow_the_reader_either(themes):
    light, dark = themes
    show = [k for k in light if k.startswith("--c-show")]
    assert show, "the sign-in showcase has no tokens"
    moved = {k: (light[k], dark[k]) for k in show if light[k] != dark[k]}
    assert not moved, f"showcase token(s) change with the theme: {moved}"


def test_the_rail_is_still_drawn_on_the_one_full_bleed_screen(layout):
    """Chat owns its viewport. It used to hide the rail and hand-build a
    "← Console" link, which is a second navigation for one screen."""
    assert "fullBleed" in layout, "the full-bleed case is gone"
    m = re.search(r"The RAIL still stands", layout)
    assert m, (
        "the note explaining that the rail survives full-bleed is gone; without "
        "it the next person hides the rail again"
    )


# ---- the row you clicked and the page you landed on --------------------------

#: Rail rows whose page announces itself by a different name.
#:
#: Every one of these is a screen the redesign has not reached yet, and the
#: mismatch is the symptom: you click "Branches" and arrive somewhere titled
#: "Stores". The list is here so it can SHRINK visibly and can never grow by
#: accident — a new mismatch fails this test rather than joining a backlog
#: nobody reads.
#:
#: `/` is the one deliberate entry: the Today page opens with a sentence about
#: the state of the system, not with the word "Today", because the first thing
#: on the console should say what needs a person.
KNOWN_TITLE_MISMATCHES = {
    "/": "opens with a sentence, by design",
}


def _declared_title(href: str) -> str | None:
    """The title a route hands its page header, when it hands it a literal."""
    rel = href.strip("/") or ""
    page = (ADMIN_SRC / "routes" / rel / "+page.svelte") if rel else (ADMIN_SRC / "routes" / "+page.svelte")
    if not page.exists():
        return None
    src = page.read_text()
    m = re.search(r"<PageHeader\b[^>]*?title=\{?['\"]([^'\"]+)['\"]", src, re.S)
    return m.group(1) if m else None


def test_the_page_you_land_on_is_the_row_you_clicked(rail):
    """A rail row is a promise about where it goes."""
    wrong = {}
    for href, label in rail:
        title = _declared_title(href)
        if title is not None and title != label:
            wrong[href] = f"row {label!r} lands on {title!r}"
    new = {h: v for h, v in wrong.items() if h not in KNOWN_TITLE_MISMATCHES}
    assert not new, f"new rail/page title mismatch(es): {new}"

    # And the list must shrink honestly: an entry that is no longer wrong is an
    # entry somebody fixed and forgot to remove, which makes the rest look stale.
    stale = [
        h
        for h in KNOWN_TITLE_MISMATCHES
        if h != "/" and _declared_title(h) is not None and h not in wrong
    ]
    assert not stale, (
        f"{stale} no longer mismatch and are still listed as known. Remove them "
        f"so the list means what it says"
    )
