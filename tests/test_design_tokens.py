"""Guards on the console's colour tokens.

Two failures in this file's subject matter have already shipped, so both are
pinned here rather than left to review:

1. `--c-ink-3` carried the SAME value (#6B7280) in the light block and the dark
   block. A value chosen to pass on white cannot also pass on near-black, and it
   didn't — measured across 18 destinations it was responsible for most of 820
   WCAG AA failures, 735 of them in dark mode. A theme token needs a value per
   theme; one value for two themes is the bug, not a shortcut.

2. `BrandingPanel.svelte` holds a deliberate SECOND copy of part of the palette,
   because its preview has to show both themes at once on a page that is itself
   in one of them. Its own comment says to keep the copy in sync. A comment is
   not a guard.

These tests read the files, so they cost nothing and cannot flake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ADMIN = Path(__file__).resolve().parents[1] / "admin"
APP_CSS = ADMIN / "src" / "app.css"
BRANDING = ADMIN / "src" / "routes" / "settings" / "BrandingPanel.svelte"

# Two parts of the palette are deliberately fixed in BOTH themes. Both are
# defined once, in :root, and inherited by html.dark on purpose, so both are
# exempt from the pair rule below:
#
#   --c-show-*  the sign-in panel is a product demo, not a page, and it is dark
#               on a light console too.
#   --c-rail-*  the navigation rail is dark in both themes. It is a fixed piece
#               of the product's identity rather than a surface that follows the
#               reader's preference — the theme switch moves the page beside it.
#
# Adding a token here is a claim that ONE value is legible on ONE background in
# every theme. That is only true because these two live on their own fixed
# ground; it is never true of a token drawn on --c-surface.
FIXED_IN_BOTH_THEMES = ("--c-show-", "--c-rail-")


def _block(css: str, selector: str, where: str = "the stylesheet") -> dict[str, str]:
    """Return the `--c-*` declarations inside one rule.

    The closing brace may be indented — the preview blocks live inside a
    `<style>` element, so theirs is not in column zero.
    """
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\s*\}", css, re.S)
    assert m, f"no {selector} block found in {where}"
    return {
        k: v.strip()
        for k, v in re.findall(r"(--c-[\w-]+)\s*:\s*([^;]+);", m.group(1))
    }


@pytest.fixture(scope="module")
def light() -> dict[str, str]:
    return _block(APP_CSS.read_text(), ":root", str(APP_CSS))


@pytest.fixture(scope="module")
def dark() -> dict[str, str]:
    return _block(APP_CSS.read_text(), "html.dark", str(APP_CSS))


def test_every_theme_token_has_a_dark_value(light, dark):
    """The --c-ink-3 bug class: a token defined only for light."""
    missing = sorted(
        k
        for k in light
        if not k.startswith(FIXED_IN_BOTH_THEMES) and k not in dark
    )
    assert not missing, (
        "these tokens are defined for light but never redefined for dark, so "
        "dark inherits a value chosen against a white background: "
        + ", ".join(missing)
    )


def test_no_token_carries_the_same_value_in_both_themes(light, dark):
    """The exact shape of the shipped bug: one literal serving two themes."""
    same = sorted(
        k
        for k, v in light.items()
        if k in dark and not k.startswith(FIXED_IN_BOTH_THEMES)
        # var() indirection is fine — `--c-series-1: var(--c-accent)` resolves
        # to a different colour per theme precisely because it is indirect.
        and not v.startswith("var(")
        and v.lower() == dark[k].lower()
    )
    assert not same, (
        "these tokens hold one literal for both themes; a colour that passes "
        "contrast on white cannot also pass on near-black: " + ", ".join(same)
    )


def test_branding_preview_copy_matches_the_real_palette(light, dark):
    """BrandingPanel duplicates part of the palette. Pin the duplicate."""
    src = BRANDING.read_text()
    for cls, truth, label in ((".pv-light", light, "light"), (".pv-dark", dark, "dark")):
        copy = _block(src, cls, str(BRANDING))
        assert copy, f"{cls} defines no tokens — did the preview change shape?"
        drifted = {
            k: (v, truth.get(k))
            for k, v in copy.items()
            if truth.get(k, "").lower() != v.lower()
        }
        assert not drifted, (
            f"{cls} has drifted from the {label} palette in app.css. "
            f"BrandingPanel keeps a second copy on purpose (it shows both "
            f"themes at once); update it alongside app.css. Drifted: {drifted}"
        )


# --------------------------------------------------------------------------
# The type and radius scales.
#
# Before these, the console carried 28 distinct font sizes across 1,092
# declarations and 14 arbitrary corner radii across 202 — every one of them
# somebody making a local decision that looked right on its own screen. The
# scales collapse those onto 10 and 5 named steps.
#
# A scale only stays a scale if nothing bypasses it, and bypassing it is one
# keystroke: `text-[13px]` is as easy to type as `text-body-sm`. So it is
# pinned rather than trusted.
# --------------------------------------------------------------------------

SVELTE = sorted((ADMIN / "src").rglob("*.svelte"))

# `rounded-full` and `rounded-none` are not sizes and stay. The role names are
# the scale itself.
OFF_SCALE = re.compile(
    r"text-\[[\d.]+px\]"                              # text-[13px]
    r"|rounded(?:-[trbles]{1,2})?-\[[\d.]+px\]"       # rounded-[9px], rounded-t-[3px]
    r"|rounded(?:-[trbles]{1,2})?-(?:lg|xl|2xl|3xl|md|sm)\b"  # t-shirt sizes
)


def test_no_arbitrary_type_or_radius_utilities():
    offenders = {}
    for p in SVELTE:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            for hit in OFF_SCALE.findall(line):
                offenders.setdefault(f"{p.relative_to(ADMIN)}:{i}", []).append(hit)
    assert not offenders, (
        "these bypass the type/radius scale in app.css — use a named step "
        "(text-micro/label/meta/body-sm/body/title/heading/display/display-lg/"
        "hero, rounded-xs/control/card/panel/hero): "
        + "; ".join(f"{k} {v}" for k, v in sorted(offenders.items())[:12])
    )


def test_stylesheet_declares_no_literal_sizes():
    """app.css should reach for its own tokens like every other file."""
    css = APP_CSS.read_text()
    body = css.split("@theme")[1].split("}", 1)[1] if "@theme" in css else css
    literals = re.findall(r"(?:font-size|border-radius):\s*[\d.]+px", body)
    assert not literals, (
        "app.css declares literal sizes outside the @theme block: " + ", ".join(literals)
    )


def test_the_scales_are_defined_and_ordered():
    """A scale that is not monotonic is not a scale — `lg` was once 16px while
    `xl` was 12px, across 163 and 74 uses."""
    css = APP_CSS.read_text()
    theme = re.search(r"@theme\s*\{(.*?)\n\}", css, re.S)
    assert theme, "no @theme block"
    for prefix, expected in (
        ("--text-", ["micro", "label", "meta", "body-sm", "body", "title",
                     "heading", "display", "display-lg", "hero"]),
        ("--radius-", ["xs", "control", "card", "panel", "hero"]),
    ):
        found = re.findall(
            re.escape(prefix) + r"([\w-]+)\s*:\s*([\d.]+)px", theme.group(1)
        )
        names = [n for n, _ in found]
        assert names == expected, f"{prefix}* steps are {names}, expected {expected}"
        sizes = [float(v) for _, v in found]
        assert sizes == sorted(sizes), f"{prefix}* is not ascending: {list(zip(names, sizes))}"
