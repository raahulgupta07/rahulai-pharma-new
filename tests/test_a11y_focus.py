"""The keyboard focus ring, measured against every surface it lands on.

WCAG 1.4.11 asks a focus indicator to reach 3:1 against what is behind it. The
console makes that easy to get wrong in one specific way: two surfaces are dark
in BOTH themes — the rail (``--c-rail-*``) and the sign-in showcase
(``--c-show-*``) — while the accent the ring was drawn in is a PAGE colour.

That is not hypothetical. ``outline: 2px solid var(--color-accent)`` measured
**1.57:1** on the rail (#2F3293 on #171A47) and **1.27:1** on the deepest
showcase stop. Nothing errored, the ring was present in the DOM and in the
computed style, and every existing contrast test passed — because they all
measured TEXT. The rail is the first nineteen tab stops on every screen, so a
keyboard user could not see where they were for the first nineteen presses of
every page in the product.

So the ring is a token, ``--focus-ring``, and each always-dark scope re-points
it at a colour from its own scale. These tests do the arithmetic rather than
grepping for the token: a scope that re-points the ring at something that still
fails is the same defect wearing the fix's clothes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "admin" / "src" / "app.css"
LAYOUT = ROOT / "admin" / "src" / "routes" / "+layout.svelte"

# Every (scope, surface behind the ring) the console actually puts a focusable
# control on. The rail and the showcase are fixed dark in both themes; the page
# scopes follow the theme, so both palettes are checked for them.
# Every (scope, surface behind the ring) the console actually puts a focusable
# control on. The ring COLOUR is not written here — it is read out of the
# stylesheet's own `--focus-ring` declaration for that scope, so re-pointing a
# scope at a colour that still fails is caught rather than rubber-stamped.
# The rail and the showcase are fixed dark in both themes; the page scopes
# follow the theme, so both palettes are checked for them.
RING_ON = [
    # scope selector, surface token, theme, what sits there
    (".rail", "--c-rail-bg", "light", "rail nav rows, brand link, sign-out"),
    (".rail", "--c-rail-bg", "dark", "rail nav rows, brand link, sign-out"),
    (".showcase-panel", "--c-show-bg", "light", "showcase, outer stop"),
    (".showcase-panel", "--c-show-bg-2", "light", "showcase, mid stop"),
    (".showcase-panel", "--c-show-bg-3", "light", "showcase, lightest stop"),
    (".skip-link", "--c-accent", "light", "the skip link, which sits on the accent"),
    (".skip-link", "--c-accent", "dark", "the skip link, which sits on the accent"),
    (":root", "--c-page", "light", "page background"),
    (":root", "--c-surface", "light", "cards, panels, table rows"),
    (":root", "--c-surface-2", "light", "inset panels, chips"),
    (":root", "--c-page", "dark", "page background"),
    (":root", "--c-surface", "dark", "cards, panels, table rows"),
    (":root", "--c-surface-2", "dark", "inset panels, chips"),
]

MIN_RATIO = 3.0  # WCAG 2.1 SC 1.4.11, non-text contrast


# ---- token resolution -------------------------------------------------------


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


@pytest.fixture(scope="module")
def palettes(css: str) -> dict[str, dict[str, str]]:
    """`{theme: {token: '#rrggbb'}}` for the two palette blocks.

    Only the palette `:root` counts — the file has a second, later `:root`
    that declares `--focus-ring` and nothing else. Anchoring on the block that
    actually contains `--c-page` keeps them apart.
    """
    root_start = css.rindex(":root {", 0, css.index("--c-page:"))
    light = css[css.index("{", root_start) + 1 : css.index("\n}", root_start)]
    dark_start = css.index("html.dark {")
    dark = css[css.index("{", dark_start) + 1 : css.index("\n}", dark_start)]

    def parse(body: str) -> dict[str, str]:
        return dict(re.findall(r"(--c-[\w-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})", body))

    lt = parse(light)
    dk = dict(lt)  # dark REDEFINES a subset; anything it does not touch carries over
    dk.update(parse(dark))
    assert "--c-page" in lt and "--c-rail-bg" in lt, "the light palette did not parse"
    assert dk["--c-page"] != lt["--c-page"], "the dark palette did not parse"
    return {"light": lt, "dark": dk}


@pytest.fixture(scope="module")
def ring_tokens(css: str) -> dict[str, str]:
    """`{scope: '--c-something'}` — what each scope actually paints the ring in.

    `:root` is the default and is declared as `var(--color-accent)`, which is
    an alias for `--c-accent`; the `@theme` block maps every `--color-x` onto
    a `--c-x`, so one hop of indirection resolves it.
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"([.:][\w-]+) \{([^}]*--focus-ring:[^;]*;[^}]*)\}", css):
        scope, body = m.group(1), m.group(2)
        val = re.search(r"--focus-ring:\s*var\((--[\w-]+)\)", body)
        assert val, (
            f"{scope} sets --focus-ring to something that is not a token: "
            f"a literal colour here cannot be re-themed and cannot be measured "
            f"against the palette"
        )
        token = val.group(1)
        if token.startswith("--color-"):
            alias = re.search(
                rf"{re.escape(token)}:\s*var\((--c-[\w-]+)\)", css
            )
            assert alias, f"{token} is not aliased onto a --c-* token in @theme"
            token = alias.group(1)
        out[scope] = token
    assert out, "no scope declares --focus-ring at all"
    return out


def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _lum(hex_: str) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex_)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---- the tests --------------------------------------------------------------


def test_the_ring_reaches_three_to_one_on_every_surface(palettes, ring_tokens):
    """The measurement, not the intention — and of the colour the CSS names."""
    failures = []
    for scope, surface, theme, where in RING_ON:
        assert scope in ring_tokens, (
            f"{scope} no longer declares --focus-ring, so it inherits the page "
            f"accent; nothing else in this file would notice"
        )
        ring = ring_tokens[scope]
        pal = palettes[theme]
        assert ring in pal, f"{ring} does not exist in the {theme} palette"
        assert surface in pal, f"{surface} does not exist in the {theme} palette"
        ratio = _contrast(pal[ring], pal[surface])
        if ratio < MIN_RATIO:
            failures.append(
                f"  {scope:17s} {theme:5s} {ring} {pal[ring]} on {surface} "
                f"{pal[surface]} = {ratio:.2f}:1  ({where})"
            )
    assert not failures, (
        "the focus ring is below 3:1 and a keyboard user cannot see where they "
        "are:\n" + "\n".join(failures)
    )


def test_no_focus_rule_paints_the_ring_with_the_page_accent(css):
    """`--color-accent` is a PAGE colour. On the rail it measures 1.57:1."""
    offenders = []
    for m in re.finditer(r"[^\n{}]*:focus-visible[^{]*\{([^}]*)\}", css):
        body = m.group(1)
        if "outline" in body and "--color-accent" in body:
            offenders.append(re.sub(r"\s+", " ", m.group(0)).strip()[:140])
    assert not offenders, (
        "a focus ring is drawn in the page accent rather than --focus-ring, so "
        "it goes invisible on the two always-dark surfaces:\n"
        + "\n".join(offenders)
    )


def test_the_ring_token_has_a_default(css):
    assert re.search(r":root \{\s*--focus-ring:", css), (
        "--focus-ring has no :root default, so every scope that does not set it "
        "gets an invalid outline colour and the ring falls back to the UA's"
    )


@pytest.mark.parametrize("scope", [".rail", ".showcase-panel"])
def test_every_always_dark_scope_repoints_the_ring(css, scope):
    """These two are dark in BOTH themes, so the page accent never fits them."""
    assert re.search(rf"{re.escape(scope)} \{{[^}}]*--focus-ring:", css), (
        f"{scope} does not re-point --focus-ring. It is dark in both themes, so "
        f"it inherits the page accent and the ring goes to 1.57:1 or worse"
    )


def test_the_rail_carries_the_scope_class():
    """The CSS override is inert unless the element actually has the class.

    This is the half of the fix that fails silently: `.rail { --focus-ring }`
    parses, ships, and does nothing at all if the `<aside>` never carries
    `rail`. Nothing in the stylesheet can catch that.
    """
    src = LAYOUT.read_text()
    m = re.search(r"<div\s+class=\"(rail[^\"]*)\"", src, re.S)
    assert m, "the rail element is gone or no longer carries a literal class"
    classes = m.group(1).split()
    assert "rail" in classes, (
        "the rail element lost its `rail` class, so the rail's --focus-ring "
        "override applies to nothing and the ring is back to 1.57:1"
    )
    assert "bg-rail-bg" in classes, (
        "this is no longer the rail element — the assertion above is now "
        "guarding the wrong node"
    )
