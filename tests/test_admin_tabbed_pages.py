"""The four screens that are one page made of panels that used to be pages.

Embed, Configuration, People & access and Answer quality each merged several
former pages into tabs. The panels moved unchanged, and each still brought its
own `h1` — so the PAGE's name was whichever tab you happened to be on. You
clicked "Embed & integration" and landed on something called "Embed widget",
and clicking a tab renamed the page again.

The shell owns the `h1` and names the page the way the rail does; a panel
passes `level={2}`.

The other thing pinned here is a spacing trap. `TabStrip`'s sticky wrapper
pulls itself up 24px with `-mt-6` to cover `main`'s top padding, which assumes
the strip is the FIRST thing in `main`. Put a header above it and that -24px
eats the header's bottom margin and then its last line of text — which is what
it did to Embed's subtitle the first time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_admin_nav import KNOWN_TITLE_MISMATCHES

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "admin" / "src" / "routes"
LAYOUT = ROUTES / "+layout.svelte"


@pytest.fixture(scope="module")
def rail_labels() -> dict[str, str]:
    block = re.search(r"const SECTIONS = \[(.*?)\n  \];", LAYOUT.read_text(), re.S)
    assert block, "the rail's section list is gone"
    return dict(re.findall(r"\{ href: '([^']+)', label: '([^']+)'", block.group(1)))


def _shells() -> list[Path]:
    """Every route whose own page draws a tab strip."""
    out = [p for p in ROUTES.rglob("+page.svelte") if "<TabStrip" in p.read_text()]
    assert out, "no tabbed shells found — the scan is wrong, not the console"
    return sorted(out)


def _href(shell: Path) -> str:
    rel = shell.parent.relative_to(ROUTES).as_posix()
    return "/" if rel == "." else f"/{rel}"


@pytest.mark.parametrize("shell", _shells(), ids=lambda p: _href(p))
def test_a_tabbed_shell_owns_the_page_name(shell, rail_labels):
    """Skipped only for the shells still queued for conversion, and that queue
    is the same list the rail test keeps — so this tightens on its own as each
    one is done, rather than needing a second list to remember."""
    href = _href(shell)
    if href in KNOWN_TITLE_MISMATCHES:
        pytest.skip(f"{href} is still queued for its page header")
    src = shell.read_text()
    m = re.search(r"<PageHeader\b[^>]*?title=\{?['\"]([^'\"]+)['\"]", src, re.S)
    assert m, f"{href} draws tabs and no page header, so its name is a panel's"
    assert m.group(1) == rail_labels.get(href), (
        f"{href} calls itself {m.group(1)!r} and the rail row says "
        f"{rail_labels.get(href)!r}"
    )
    assert "level=" not in src.split("<TabStrip")[0].split("<PageHeader")[-1], (
        f"{href}'s own header is not the page's h1"
    )


@pytest.mark.parametrize("shell", _shells(), ids=lambda p: _href(p))
def test_the_panels_under_it_are_one_level_down(shell, rail_labels):
    href = _href(shell)
    if href in KNOWN_TITLE_MISMATCHES:
        pytest.skip(f"{href} is still queued for its page header")
    stray = []
    for panel in sorted(shell.parent.glob("*.svelte")):
        if panel.name == "+page.svelte":
            continue
        src = panel.read_text(errors="replace")
        for m in re.finditer(r"<(?:PageHeader|AnalyticsPage)\b", src):
            i, depth = m.end(), 0
            while i < len(src):
                ch = src[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                elif ch == ">" and depth == 0:
                    break
                i += 1
            if "level=" not in src[m.start() : i]:
                stray.append(f"{panel.name}:{src[: m.start()].count(chr(10)) + 1}")
    assert not stray, (
        f"panel header(s) under {href} still draw an h1, so the page has more "
        f"than one name: {stray}"
    )


def test_a_header_above_a_sticky_strip_leaves_room_for_it():
    """The -mt-6 trap. Nothing errors when it bites — the header's last line is
    simply painted over, and only a screenshot shows it."""
    bad = []
    for p in ROUTES.rglob("*.svelte"):
        src = p.read_text(errors="replace")
        i = src.find("<TabStrip")
        if i < 0:
            continue
        tag = src[i : src.find(">", i)]
        if "sticky" not in tag:
            continue
        before = src[:i]
        j = before.rfind("<PageHeader")
        if j < 0:
            continue
        # The header must sit inside a wrapper that reserves the 24px.
        if "pb-6" not in before[max(0, j - 300) : i]:
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, (
        "a page header sits directly above a sticky tab strip with no space "
        f"reserved for its -mt-6, so the strip will cover the header's last "
        f"line: {bad}"
    )
