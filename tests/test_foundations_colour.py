"""The Foundations colour board, and the palette it is drawn from.

The page itself reads every token out of the running stylesheet and works out
every ratio in the browser, so it cannot describe a palette the console is not
using. This file is the other half: it recomputes the same ratios from
`app.css` in a different language, so a token edit that breaks contrast fails a
test rather than waiting for somebody to open the page in the right theme.

Two tokens have already shipped one hundredth of a ratio under AA — `--c-ink-3`
held one value for both themes, and the rail's smallest text used a colour that
measured 4.49:1. In both cases the value looked right and the arithmetic was
never re-run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "admin" / "src" / "app.css"
PAGE = ROOT / "admin" / "src" / "routes" / "foundations" / "+page.svelte"

AA = 4.5


def _block(src: str, selector: str) -> str:
    i = src.index(selector + " {")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unterminated block for {selector!r}")


def _decls(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--c-[\w-]+)\s*:\s*([^;]+);", block)}


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def themes(css) -> dict[str, dict[str, str]]:
    """Light, and dark as the browser resolves it: dark REDEFINES a subset and
    inherits the rest from `:root`, so a dark token that nobody re-declared is
    still the light value and must be measured as one."""
    light = _decls(_block(css, ":root"))
    dark = dict(light)
    dark.update(_decls(_block(css, "html.dark")))
    return {"light": light, "dark": dark}


def _resolve(name: str, table: dict[str, str], seen: frozenset[str] = frozenset()) -> str | None:
    v = table.get(name)
    if v is None or name in seen:
        return None
    m = re.fullmatch(r"var\((--c-[\w-]+)\)", v.strip())
    if m:
        return _resolve(m.group(1), table, seen | {name})
    return v.strip()


def _rgb(value: str | None) -> tuple[float, float, float] | None:
    """None for anything that is not an opaque colour — a translucent fill has
    no ratio of its own, and inventing a backdrop for it is how a number that
    means nothing ends up on a page."""
    if not value:
        return None
    v = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.fullmatch(r"rgb\(([^)]+)\)", v)
    if m:
        parts = [float(x) for x in re.split(r"[\s,/]+", m.group(1).strip())]
        if len(parts) == 3:
            return (parts[0], parts[1], parts[2])
    return None


def _lum(c: tuple[float, float, float]) -> float:
    def f(v: float) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = c
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.fixture(scope="module")
def pairs(page) -> list[tuple[str, str]]:
    """The pairs the PAGE lists, not a copy of them.

    A second list here would drift, and the drift would be silent in exactly
    the direction that matters: a pair dropped from the page would keep passing
    a test that still knows about it."""
    found = re.findall(r"\{ fg: '(--c-[\w-]+)', bg: '(--c-[\w-]+)'", page)
    assert found, "the page no longer declares its text/background pairs"
    return found


def test_every_pair_the_page_draws_clears_aa_in_both_themes(themes, pairs):
    bad = []
    for theme, table in themes.items():
        for fg, bg in pairs:
            a, b = _rgb(_resolve(fg, table)), _rgb(_resolve(bg, table))
            if a is None or b is None:
                continue  # translucent or absent; asserted separately
            r = _ratio(a, b)
            if r < AA:
                bad.append(f"{theme}: {fg} on {bg} = {r:.2f}:1")
    assert not bad, "text below WCAG AA:\n  " + "\n  ".join(bad)


def test_both_themes_are_measured_not_just_the_one_the_page_was_opened_in(page):
    """The page prints ratios for the theme the reader is in. Without this it
    would print light-mode numbers to a dark-mode reader, which is the worse
    half of the failure it exists to catch."""
    assert "classList.contains('dark')" in page, "the page no longer knows which theme it is measuring"
    assert "MutationObserver" in page, (
        "switching the theme with this page open would leave the other theme's "
        "ratios on screen"
    )


def test_the_pairs_are_measured_and_not_typed(page):
    """A ratio written into the source is a number somebody wrote down once."""
    assert "getComputedStyle" in page, "the page no longer reads the running stylesheet"
    typed = re.findall(r"(?<![\d.])\d+\.\d+:1", page)
    assert not typed, (
        f"a contrast ratio is typed into the page ({typed}); it must be worked "
        f"out from the tokens or it will outlive the colour it describes"
    )


def test_the_two_tokens_that_shipped_under_aa_are_not_near_the_line(themes):
    """`--c-ink-3` and `--c-rail-ink-3` are the smallest text in the console and
    both have shipped a hundredth under. Passing is not enough for these two —
    a value chosen to land on 4.51 is a value nobody re-measured."""
    checks = [("light", "--c-ink-3", "--c-surface-2"), ("light", "--c-rail-ink-3", "--c-rail-bg"),
              ("dark", "--c-ink-3", "--c-surface-2"), ("dark", "--c-rail-ink-3", "--c-rail-bg")]
    for theme, fg, bg in checks:
        a, b = _rgb(_resolve(fg, themes[theme])), _rgb(_resolve(bg, themes[theme]))
        assert a and b, f"{fg}/{bg} is missing in {theme}"
        r = _ratio(a, b)
        assert r >= 4.7, f"{theme}: {fg} on {bg} = {r:.2f}:1 — passing, but with no margin"


def test_every_text_token_is_a_light_dark_pair(themes):
    """One value asked to work on both white and near-black cannot be right on
    both. That single mistake accounted for most of this console's 820 measured
    contrast failures."""
    same = [t for t in ("--c-ink", "--c-ink-2", "--c-ink-3", "--c-accent", "--c-on-accent")
            if _resolve(t, themes["light"]) == _resolve(t, themes["dark"])]
    assert not same, f"{same} carry one value for both themes"


def test_every_opaque_token_appears_on_the_page(themes, page):
    """A token defined and never shown is a colour nobody can check."""
    missing = [
        t for t, v in themes["light"].items()
        if _rgb(_resolve(t, themes["light"])) is not None and f"'{t}'" not in page
    ]
    assert not missing, f"defined but absent from Foundations: {missing}"


def test_a_translucent_colour_is_not_given_a_ratio(page):
    assert "fg.a !== 1 || bg.a !== 1" in page, (
        "a translucent colour is being handed a contrast ratio, which means "
        "inventing whatever sits behind it"
    )


def test_the_prose_is_prose_and_not_markdown(page):
    """This page renders text, not markdown. A backtick inside a `why` or a
    role string reaches the reader as a backtick — it shipped that way once,
    and `--color-surface-3` appeared on screen wearing its quotes."""
    strings = re.findall(r"why:\s*'([^']*)'", page) + re.findall(r"\['--c-[\w-]+', '([^']*)'\]", page)
    assert strings, "the group copy is no longer where this test looks for it"
    bad = [s for s in strings if "`" in s or "**" in s]
    assert not bad, f"markdown syntax in rendered copy: {bad}"
