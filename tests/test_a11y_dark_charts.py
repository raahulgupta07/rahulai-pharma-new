"""The chart palette: what a reader can actually tell apart, measured.

Three defects, all measured on the running console in DARK mode, and the two
worst are not contrast failures at all — they are charts that lie about their
own categories.

* Two event sources, one swatch. ``COLOR.app`` was ``--color-accent`` and
  ``COLOR.ingest`` was ``--color-success``, and in dark mode those two tokens
  are the same colour: rgb(155,160,240) both, ratio **1.00**, CIEDE2000
  **0.0**. Every Feed chart drew admin actions and file ingests in one fill,
  and nothing on screen said so. A reader counting "how much of last week was
  ingest" was reading a bar that was partly something else.

* A ten-category stacked bar painted in FIVE fills. The wheel had six entries,
  two of which were the same colour in dark, and it *cycled* — ``i % 6``. The
  measured fill sequence in DOM order on /activity "Events by Action, daily"
  was danger, series-1, accent-2, series-1, line-2, warning, danger, series-1,
  accent-2, series-1. Five pairs of categories sharing a colour, silently.

* Fills nobody can see. ``--color-line-2`` is a HAIRLINE token and it was
  painting the "Miss" bar and the "Target 30%" line — things a reader is meant
  to read off the chart. It measured 1.18:1 on a card and 1.29:1 on the page in
  dark, and 1.14:1 / 1.05:1 in LIGHT, which is worse. ``--color-accent-2`` is a
  de-emphasis colour and was carrying a data series at 2.24:1.

WHAT THIS FILE CAN AND CANNOT ASSERT
------------------------------------

The obvious rule — "every series is 3:1 from every other series" — cannot be
satisfied by any palette, and asserting it would have meant shipping something
that only looked like a fix. WCAG contrast is a pure relative-luminance ratio,
so k mutually-3:1 colours require ``L(i+1) >= 3*(L(i)+0.05) - 0.05``. Starting
from black the chain is 0 -> 0.100 -> 0.400 -> **1.300**, and white is 1.0:
THREE colours is the ceiling for the entire sRGB gamut. Add the requirement
that each fill also clears 3:1 against a near-black page and the floor rises to
L >= 0.115, making the chain 0.115 -> 0.445 -> **1.436** — a ceiling of TWO.
``test_three_to_one_between_series_is_impossible_by_arithmetic`` pins that, so
the rule cannot be "restored" later by somebody who has not done the sums.

What IS asserted instead, and what the console now actually does:

1. every chart fill clears WCAG 1.4.11 (3:1) against all three surfaces a chart
   is drawn on, in BOTH themes;
2. fills are separated PERCEPTUALLY from each other — CIEDE2000, which reads
   hue and chroma as well as lightness — at a measured threshold;
3. two segments that TOUCH are separated by a surface-coloured stroke, which
   makes the boundary contrast segment-vs-surface, a constraint every fill
   already clears;
4. the wheel cannot silently reuse a colour: it does not cycle, and a chart with
   more members than the palette groups the tail into one labelled band.
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "admin" / "src" / "app.css"
SHARED = ROOT / "admin" / "src" / "lib" / "activity" / "shared.js"
ANALYTICS = ROOT / "admin" / "src" / "lib" / "analytics" / "AnalyticsPage.svelte"
STACKED = ROOT / "admin" / "src" / "lib" / "charts" / "StackedBars.svelte"
DONUT = ROOT / "admin" / "src" / "lib" / "charts" / "Donut.svelte"

# WCAG 2.1 SC 1.4.11, non-text contrast. A chart fill is a graphical object.
MIN_RATIO = 3.0

# CIEDE2000 between two fills that can appear in one chart.
#
# This is not a standards number — there is no standard for "two categories a
# reader can tell apart", which is exactly why the luminance rule got reached
# for and why it could not be met. It is set from the palette that was built to
# satisfy it: the tightest pair measures 19.7 in light (series-3/series-4) and
# 19.3 in dark (series-6/warning), so 18 is the floor with a real margin rather
# than a threshold drawn around whatever shipped. For scale, the defect this
# replaced measured 0.0.
MIN_DE = 18.0

# The three backgrounds a chart is drawn on. `--c-surface-2` is the hover band
# behind a bar column and the inset panel, and it is the TIGHTEST of the three
# in dark mode, so a palette checked only against the page passes while the
# thing a reader actually hovers fails.
SURFACES = ("--c-page", "--c-surface", "--c-surface-2")


# ---- palette -----------------------------------------------------------------


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


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


@pytest.fixture(scope="module")
def themes(css: str) -> dict[str, dict[str, str]]:
    """Light, and dark as a BROWSER resolves it.

    `html.dark` redefines a subset; a token it does not touch keeps its `:root`
    value and must be measured as one. Reading only the dark block would let a
    token that exists in light and not in dark look absent rather than wrong.
    """

    def decls(b: str) -> dict[str, str]:
        return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--c-[\w-]+)\s*:\s*([^;]+);", b)}

    light = decls(_block(css, ":root"))
    dark = dict(light)
    dark.update(decls(_block(css, "html.dark")))
    assert "--c-series-1" in light and "--c-page" in light, "the light palette did not parse"
    assert dark["--c-page"] != light["--c-page"], "the dark palette did not parse"
    return {"light": light, "dark": dark}


def _resolve(name: str, table: dict[str, str], seen: frozenset[str] = frozenset()) -> str | None:
    """A token's literal colour, following `var()` indirection.

    `--c-series-1` is `var(--c-accent)` on purpose, and in a browser
    `getPropertyValue` on it returns the UNRESOLVED string. Reading the value
    without following the hop is how a test comes to measure the literal text
    `var(--c-accent)` and report a plausible number for nothing at all.
    """
    v = table.get(name)
    if v is None or name in seen:
        return None
    m = re.fullmatch(r"var\((--c-[\w-]+)\)", v.strip())
    if m:
        return _resolve(m.group(1), table, seen | {name})
    return v.strip()


def _rgb(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value.strip())
    if not m:
        return None  # translucent or a gradient; it has no ratio of its own
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# ---- WCAG (copied from tests/test_a11y_focus.py, deliberately) ---------------


def _srgb(v: float) -> float:
    s = v / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _lum(c: tuple[int, int, int]) -> float:
    return 0.2126 * _srgb(c[0]) + 0.7152 * _srgb(c[1]) + 0.0722 * _srgb(c[2])


def _ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---- CIEDE2000 ---------------------------------------------------------------


def _lab(c: tuple[int, int, int]) -> tuple[float, float, float]:
    def lin(v: float) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(x) for x in c)
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _de2000(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    l1, a1, b1 = _lab(c1)
    l2, a2, b2 = _lab(c2)
    cc1, cc2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (cc1 + cc2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7))) if cbar > 0 else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dlp, dcp = l2 - l1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dhhp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)
    lbar, cbarp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbar = (h1p + h2p + 360) / 2
    else:
        hbar = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbar - 30))
        + 0.24 * math.cos(math.radians(2 * hbar))
        + 0.32 * math.cos(math.radians(3 * hbar + 6))
        - 0.20 * math.cos(math.radians(4 * hbar - 63))
    )
    dtheta = 30 * math.exp(-(((hbar - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbarp**7 / (cbarp**7 + 25**7)) if cbarp > 0 else 0.0
    sl = 1 + (0.015 * (lbar - 50) ** 2) / math.sqrt(20 + (lbar - 50) ** 2)
    sc, sh = 1 + 0.045 * cbarp, 1 + 0.015 * cbarp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhhp / sh) ** 2 + rt * (dcp / sc) * (dhhp / sh)
    )


# ---- the fills the console actually paints -----------------------------------


def _alias(alias: str, css_src: str) -> str:
    m = re.search(rf"{re.escape(alias)}:\s*var\((--c-[\w-]+)\)", css_src)
    assert m, f"{alias} is not aliased onto a --c-* token in @theme"
    return m.group(1)


def _fills_of(src: str, opener: str, what: str, css_src: str) -> dict[str, str]:
    """`{name: '--c-token'}` for one chart palette, read out of its own source.

    A copy of the palette here would drift, and it would drift in the direction
    that hides the bug: a fill repointed back at a hairline would keep passing
    a test that still knew about the good token.
    """
    assert opener in src, f"{what} no longer declares a palette starting `{opener}`"
    i = src.index(opener)
    body = src[i : src.index("};", i)]
    found = re.findall(r"(\w+):\s*'var\((--color-[\w-]+)\)'", body)
    assert found, f"{what} no longer declares its fills as --color-* tokens"
    return {f"{what}.{name}": _alias(alias, css_src) for name, alias in found}


@pytest.fixture(scope="module")
def fills() -> dict[str, str]:
    """Every fill a chart in this console can paint.

    Three sources: `COLOR` in the Activity shared module, `C` in AnalyticsPage,
    and the anonymous-category palette — the one that was cycling.

    That third lookup is deliberately tolerant of the palette being called
    something else. The first version of this fixture indexed straight for
    `export const SERIES = [` and raised `ValueError: substring not found` on
    any tree that did not have it — which meant that against the OLD code, the
    three tests that do the actual arithmetic ERRORED in setup instead of
    failing with numbers. A test that cannot run on the broken input is not
    evidence that it would have caught it.
    """
    css_src = CSS.read_text()
    shared = SHARED.read_text()
    out = _fills_of(shared, "export const COLOR = {", "COLOR", css_src)
    out.update(_fills_of(ANALYTICS.read_text(), "const C = {", "C", css_src))
    for opener, label in (("export const SERIES = [", "SERIES"), ("const WHEEL = [", "WHEEL")):
        if opener not in shared:
            continue
        tail = shared[shared.index(opener) :]
        for alias in re.findall(r"'var\((--color-[\w-]+)\)'", tail[: tail.index("]")]):
            token = _alias(alias, css_src)
            out[f"{label}.{token}"] = token
        break
    else:  # pragma: no cover - the anonymous palette is gone entirely
        raise AssertionError(
            "no palette for unnamed categories exists in shared.js under any "
            "known name, so nothing here measures what an unrecognised series "
            "is painted with"
        )
    return out


def _colour(token: str, table: dict[str, str], theme: str) -> tuple[int, int, int]:
    """Resolve, and fail loudly on a token that does not exist.

    A missing custom property makes the declaration INVALID, so the element
    inherits and paints something plausible. `--color-surface-3` shipped that
    way. An absent token must be an error here, never a skip.
    """
    raw = table.get(token)
    assert raw is not None, (
        f"{token} does not exist in the {theme} palette, so anything painted "
        f"with it silently inherits instead of erroring"
    )
    c = _rgb(_resolve(token, table))
    assert c is not None, f"{token} resolves to {_resolve(token, table)!r}, which is not an opaque colour"
    return c


# ---- 1. every fill is visible on every surface it is drawn on -----------------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_chart_fill_clears_three_to_one_on_every_surface(themes, fills, theme):
    """WCAG 1.4.11. A bar nobody can see is not a bar.

    Both themes, because the numbers that started this were taken in dark and
    the light ones had never been taken at all — and when they were, light was
    WORSE for the two de-emphasis tokens (--c-line-2 at 1.14:1 on a card
    against dark's 1.18:1).
    """
    table = themes[theme]
    bad = []
    for name, token in sorted(fills.items()):
        fg = _colour(token, table, theme)
        for surface in SURFACES:
            bg = _colour(surface, table, theme)
            r = _ratio(fg, bg)
            if r < MIN_RATIO:
                bad.append(
                    f"  {name} ({token} {_resolve(token, table)}) on {surface} "
                    f"{_resolve(surface, table)} = {r:.2f}:1"
                )
    assert not bad, (
        f"{theme}: a chart fill is below {MIN_RATIO}:1 against a surface it is "
        f"drawn on, so the series is invisible there:\n" + "\n".join(bad)
    )


# ---- 2. fills are told apart from each other ---------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_two_chart_fills_are_the_same_colour(themes, fills, theme):
    """Finding A, and the half of finding B that is a token collision.

    Two names for one colour is worse than a low ratio: the chart does not look
    broken, it looks like there are fewer categories than there are.
    """
    table = themes[theme]
    by_colour: dict[tuple[int, int, int], list[str]] = {}
    for name, token in sorted(fills.items()):
        by_colour.setdefault(_colour(token, table, theme), []).append(f"{name} ({token})")
    clashes = [
        f"  {_resolve(names[0].split('(')[1].rstrip(') '), table)} = " + " and ".join(names)
        for c, names in by_colour.items()
        if len({n.split(" (")[1] for n in names}) > 1
    ]
    assert not clashes, (
        f"{theme}: two DIFFERENT chart fills resolve to one colour (ratio 1.00, "
        f"dE2000 0.0). The chart shows fewer categories than it has and says "
        f"nothing:\n" + "\n".join(clashes)
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_pair_of_series_colours_is_perceptibly_apart(themes, fills, theme):
    """Distinguishability, measured the only way it can be measured.

    Not a luminance ratio — see
    `test_three_to_one_between_series_is_impossible_by_arithmetic` for why that
    rule caps out at two colours on this page. CIEDE2000 reads hue and chroma
    as well as lightness, which is what actually separates a teal bar from a
    blue one.
    """
    table = themes[theme]
    seen = {}
    for name, token in sorted(fills.items()):
        seen.setdefault(token, name)
    tokens = sorted(seen)
    bad = []
    tightest = (999.0, "")
    for a, b in itertools.combinations(tokens, 2):
        ca, cb = _colour(a, table, theme), _colour(b, table, theme)
        d = _de2000(ca, cb)
        line = f"{a} {_resolve(a, table)} vs {b} {_resolve(b, table)} = dE {d:.1f}"
        if d < tightest[0]:
            tightest = (d, line)
        if d < MIN_DE:
            bad.append(f"  {line} (WCAG between them: {_ratio(ca, cb):.2f}:1)")
    assert not bad, (
        f"{theme}: two chart fills are within dE2000 {MIN_DE} of each other, so "
        f"a reader cannot reliably tell those categories apart:\n"
        + "\n".join(bad)
        + f"\n  tightest pair overall: {tightest[1]}"
    )


def test_three_to_one_between_series_is_impossible_by_arithmetic(themes):
    """Why this file measures dE and not contrast, kept as arithmetic.

    This exists so the rule cannot be "restored". WCAG contrast is a pure
    relative-luminance ratio, so a set of mutually-3:1 colours is a chain:
    each one's luminance must be at least 3*(previous + 0.05) - 0.05. The chain
    runs off the top of the gamut almost immediately, and a palette that also
    has to sit on this console's dark page runs off it one step sooner.
    """
    dark = themes["dark"]
    floor = 3 * (_lum(_colour("--c-page", dark, "dark")) + 0.05) - 0.05

    def chain(start: float) -> list[float]:
        out = [start]
        while out[-1] <= 1.0:
            out.append(3 * (out[-1] + 0.05) - 0.05)
        return out

    whole_gamut = chain(0.0)
    on_this_page = chain(floor)
    assert len(whole_gamut) - 1 == 3, (
        f"the sRGB gamut now holds {len(whole_gamut) - 1} mutually-3:1 colours, "
        f"chain {['%.3f' % v for v in whole_gamut]} — the arithmetic changed, "
        f"re-read this file"
    )
    assert len(on_this_page) - 1 == 2, (
        f"on --c-page (relative luminance "
        f"{_lum(_colour('--c-page', dark, 'dark')):.5f}) a fill must reach "
        f"L >= {floor:.4f} to clear 3:1, and mutual 3:1 then allows "
        f"{len(on_this_page) - 1} colours, chain "
        f"{['%.3f' % v for v in on_this_page]}. A palette of six claiming "
        f"mutual 3:1 does not exist"
    )


# ---- 3. touching segments are separated structurally -------------------------


@pytest.mark.parametrize("path", [STACKED, DONUT], ids=["StackedBars", "Donut"])
def test_touching_segments_carry_a_surface_coloured_separator(path):
    """The 1.01:1 / 1.06:1 / 1.29:1 rows: two fills that share an EDGE.

    Every fill clears 3:1 against --color-surface, so a surface-coloured stroke
    between two of them is >= 3:1 against both by construction. That is the
    whole trick: an unsatisfiable segment-vs-segment constraint traded for a
    satisfiable segment-vs-surface one.
    """
    src = path.read_text()
    assert "const SEPARATOR = 'var(--color-surface)';" in src, (
        f"{path.name} no longer separates adjacent segments with the surface "
        f"colour, so the boundary is back to segment-vs-segment contrast, "
        f"which measured 1.01:1 in dark"
    )
    assert "stroke={SEPARATOR}" in src or "SEPARATOR : 'none'" in src, (
        f"{path.name} declares a separator and never paints with it"
    )


def test_the_separator_is_skipped_on_a_segment_too_thin_to_survive_it():
    """A separator that eats the segment is not an improvement.

    A stroke is centred on the edge, so it costs half its width to EACH side.
    Measured on the running console (viewBox `0 0 1292 200`, one view unit =
    one CSS pixel), /cost "Tokens per day" draws a 147.03px segment sitting
    directly on a 0.23px one, and /activity "Events by Action, daily" has a
    median segment height of 3.24px with five of thirteen under 2px. A 1.5px
    stroke there deletes the small categories in order to make the boundaries
    between the large ones visible, which trades one unreadable chart for
    another.

    Hence 1px, the narrowest line that renders, and a threshold checked on the
    NEIGHBOURS as well as on the segment itself — the 147px bar must not stroke
    its own edge over the 0.23px band beneath it.
    """
    src = STACKED.read_text()
    assert re.search(r"const SEPARATOR_WIDTH = 1;", src), (
        "the separator is no longer 1px. It is centred on the edge, so anything "
        "wider costs each neighbour more than half a pixel, and the measured "
        "median segment on /activity is 3.24px"
    )
    assert re.search(r"const SEPARABLE = \d", src), (
        "StackedBars no longer has a minimum height for the separator, so a "
        "0.23px segment is now drawn entirely in the separator colour"
    )
    assert "p.h >= SEPARABLE" in src, "the threshold is declared and never applied"
    assert "drawn[k - 1].h >= SEPARABLE" in src and "drawn[k + 1].h >= SEPARABLE" in src, (
        "the separator threshold is checked on the segment only, not on the "
        "segments it touches. /cost draws a 147.03px bar on a 0.23px one: "
        "stroking the big one's edge covers the small one completely"
    )


def test_the_donut_separator_is_skipped_on_a_slice_too_narrow_to_survive_it():
    """The same arithmetic on the other axis.

    A donut slice's narrowest edge is its INNER arc — r 44 in a 132-unit box,
    so 276.5 units for the whole ring, 2.77 per percent. A 1px stroke on both
    radial edges costs a slice 1 unit of arc, so a slice under about 1.1% of
    the total would be mostly separator.
    """
    src = DONUT.read_text()
    assert re.search(r"const MIN_ARC = ", src), (
        "Donut has no minimum slice width for the separator, so a sliver slice "
        "is drawn mostly in the separator colour"
    )
    assert re.search(r"sep:\s*frac >= MIN_ARC", src), (
        "the minimum is declared and never compared against a slice's actual "
        "fraction — a threshold nothing reads is not a threshold"
    )
    assert "a.sep ? SEPARATOR : 'none'" in src, (
        "the per-slice decision is computed and then not used when painting"
    )


# ---- 4. the wheel cannot silently reuse a colour -----------------------------


def test_the_series_wheel_does_not_cycle():
    """Finding B's mechanism: `WHEEL[i % WHEEL.length]`.

    Ten categories, six entries, two of which were the same colour: five pairs
    sharing a swatch. Modulo indexing into a palette is the bug — an index past
    the end has to become something a reader can see is a fold, never
    category 1's colour a second time.
    """
    # Comments are stripped first. The block comment on SERIES quotes the bug
    # verbatim so the next reader knows what not to write, and a scanner that
    # cannot tell a warning from the code fails on its own documentation.
    src = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", SHARED.read_text(), flags=re.S))
    offenders = re.findall(r"\w+\[\s*i\s*%\s*\w+(?:\.length)?\s*\]", src)
    assert not offenders, (
        "a palette is indexed modulo its own length, so category 7 is painted "
        f"in category 1's colour with nothing saying so: {offenders}"
    )
    assert "export const seriesColor" in src, "there is no single place that maps an index to a fill"


def test_a_chart_with_more_members_than_colours_folds_the_tail_and_says_how_many():
    """The honest answer to "more categories than the palette can hold".

    Grouping, not cycling and not dropping: the bars still total to the whole,
    and the band's LABEL carries the count of what went into it, because
    "Other" on its own does not tell a reader how much of the chart it stands
    for. The panel's table still lists every member by name, so the fold costs
    a colour and never a number.
    """
    src = SHARED.read_text()
    assert "export const SERIES_LIMIT" in src, "nothing declares how many categories can be told apart"
    assert "OTHER_KEY" in src, "there is no fold band"
    assert re.search(r"`Other \(\$\{folded\.length\} member", src), (
        "the fold band does not name how many members it is standing for, so a "
        "reader cannot tell whether it hides two categories or twenty"
    )
    assert "rest.length > room" in src, (
        "pivotRows no longer decides whether the tail overflows the palette; "
        "without that it is back to painting every member and reusing colours"
    )


# ---- 5. the de-emphasis tokens stay out of the data ---------------------------


DE_EMPHASIS = ("--color-line-2", "--color-accent-2")


@pytest.mark.parametrize("path", [SHARED, ANALYTICS], ids=["shared.js", "AnalyticsPage"])
def test_no_data_series_is_painted_in_a_de_emphasis_colour(path):
    """`C.muted` and `auth`, which were a hairline and a disabled-state grey.

    Measured in dark: --color-line-2 at 1.18:1 on a card, --color-accent-2 at
    2.24:1. Both were carrying meaning — a "Miss" bar, a "Target 30%" line,
    "Blocked by IP", "not recorded", and one of the three event sources. A
    de-emphasis colour is fine for de-emphasis; a band a reader is asked to
    read is not de-emphasis.
    """
    src = path.read_text()
    bad = [t for t in DE_EMPHASIS if f"var({t})" in src]
    assert not bad, (
        f"{path.name} paints a chart series with {bad}, which are de-emphasis "
        f"tokens: --color-line-2 measures 1.18:1 and --color-accent-2 2.24:1 "
        f"against the card they are drawn on"
    )


def test_the_semantic_collision_is_kept_out_of_the_charts(themes):
    """--c-success and --c-accent ARE the same colour, on purpose, in dark.

    "Good news is quiet" — the palette has no green and a healthy service is
    painted in the accent family. That is a defensible decision about CHROME
    and it is measured here rather than assumed: dE2000 0.0 in dark. It becomes
    a defect the moment a CHART paints two categories with the two names, which
    is precisely how COLOR.app and COLOR.ingest came to be one swatch.

    So the fix is not to re-hue the semantic scale under the design system's
    feet; it is that no chart palette reaches for either token.
    """
    dark = themes["dark"]
    d = _de2000(_colour("--c-success", dark, "dark"), _colour("--c-accent", dark, "dark"))
    assert d < 1.0, (
        f"--c-success and --c-accent are now dE {d:.1f} apart in dark. If that "
        f"is deliberate, this test is stale — but check that the reason it was "
        f"written (a chart cannot use both) has actually been re-examined"
    )
    for path in (SHARED, ANALYTICS):
        src = path.read_text()
        i = src.index("export const COLOR = {" if path is SHARED else "const C = {")
        body = src[i : src.index("};", i)]
        for token in ("--color-success", "--color-accent)", "--color-info"):
            assert token not in body, (
                f"{path.name}'s chart palette paints a series with {token}. In "
                f"dark mode --c-success, --c-accent and --c-info are one colour "
                f"(dE {d:.1f}), so two categories using two of those names are "
                f"drawn identically and nothing on screen admits it"
            )
