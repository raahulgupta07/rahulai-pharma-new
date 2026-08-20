"""Scrims, and rows that claim to be buttons.

**The scrim.** Nine full-viewport overlays in this console were written as real
`<button>`s. A `<button class="fixed inset-0">` is 1440px wide and covers the
screen, so it takes a tab stop whose focus ring is drawn around the window edge
— or clipped away entirely — and whose Enter closes the very thing the user had
just opened. Measured on the running console:

    tab1 BUTTON "Close menu" x=0 y=0 w=1440 bg=rgba(0, 0, 0, 0)

Four of the nine painted **nothing at all** (no background class), so a keyboard
user hit an invisible stop over a page that looked unchanged, pressed Enter to
find out what it was, and the popover vanished. A scrim is a pointer
affordance: the mouse route out. The keyboard route out is Escape, which
`lib/aurora/dialog.js` owns for every dialog and `+layout.svelte`'s
`onGlobalKey` owns for the shell's three overlays. So a scrim is an
`aria-hidden` `<div>` with an `onclick` and no tabindex, and the tests below
guard that a new one cannot go back to being a focus stop.

The keyboard-route-out test is deliberately coarse — it asserts that a file
containing a scrim also contains a keyboard dismissal (`use:dialog`, or an
Escape branch), which static analysis can see. It cannot prove the Escape closes
*that* overlay, so `test_layout_escape_closes_all_three_shell_overlays` pins the
one case where the scrim was the only dismissal: the mobile menu, whose scrim
could not be demoted until Escape closed it.

**The row.** `role="button"` is a promise to a screen reader that the thing
behaves like a button, and Space is a button's primary activation key. Two rows
on `/data` handled Enter only, so Space scrolled the table under a row the
reader had just announced as a button.

Both scanners are brace- and quote-aware (`onclick={() => f(x)}` contains a
`>`, which ends a naive `<button[^>]*>` match mid-attribute) and blank comments
before scanning. The prose in this repo describes the defects it guards — the
comment above a fixed scrim contains the words `fixed inset-0` — so a scanner
that reads comments reports its own documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"
LAYOUT = SRC / "routes" / "+layout.svelte"

# Tags that are focusable with no tabindex at all.
NATIVELY_FOCUSABLE = {"button", "a", "input", "select", "textarea", "summary"}


def _blank_js_comments(text: str) -> str:
    """Blank `//` and `/* */` bodies inside <script>, keeping every offset.

    Copied from `test_a11y_names.py`, for the reason recorded there: a scanner
    that skips `<!-- -->` but not `//` reports prose as markup. String-awareness
    is required too — `'https://…'` is not a comment.
    """
    out = list(text)
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.S):
        body, base = m.group(1), m.start(1)
        i, n = 0, len(body)
        while i < n:
            c = body[i]
            if c in "\"'`":
                q, i = c, i + 1
                while i < n:
                    if body[i] == "\\":
                        i += 2
                        continue
                    if body[i] == q:
                        i += 1
                        break
                    i += 1
                continue
            if c == "/" and i + 1 < n and body[i + 1] == "/":
                while i < n and body[i] != "\n":
                    out[base + i] = " "
                    i += 1
                continue
            if c == "/" and i + 1 < n and body[i + 1] == "*":
                j = body.find("*/", i + 2)
                j = n if j == -1 else j + 2
                for k in range(i, j):
                    if body[k] != "\n":
                        out[base + k] = " "
                i = j
                continue
            i += 1
    return "".join(out)


def _blank_html_comments(text: str) -> str:
    """Blank `<!-- -->` bodies, keeping every offset.

    `_scan` skips comments as it walks, but the "is there a keyboard way out"
    test greps the file as a string, and this caught it: `ftp/+page.svelte`
    passed with `use:dialog` deleted, because two comments there — one of them
    the comment ABOVE the scrim explaining that Escape is the route out —
    contain the word Escape. The test was reading its own documentation.
    """
    out = list(text)
    for m in re.finditer(r"<!--.*?-->", text, re.S):
        for k in range(m.start(), m.end()):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _scan(raw: str):
    """Yield (offset, tag, attrs_text, is_close_tag) for every element.

    Brace- and quote-aware, so `{() => f(x)}` does not end the tag, and
    `<!-- -->` is skipped whole.
    """
    text = _blank_js_comments(raw)
    i, n = 0, len(text)
    while i < n:
        if text[i] != "<":
            i += 1
            continue
        if text.startswith("<!--", i):
            j = text.find("-->", i)
            i = n if j == -1 else j + 3
            continue
        m = re.match(r"<(/?)([A-Za-z][\w.\-]*(?::[\w.\-]+)?)", text[i:])
        if not m:
            i += 1
            continue
        j, depth, quote = i + m.end(), 0, None
        while j < n:
            ch = text[j]
            if quote:
                quote = None if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            j += 1
        yield i, m.group(2), text[i + m.end() : j], m.group(1) == "/"
        i = j + 1


def _attr(attrs: str, name: str) -> str | None:
    """The raw source of one attribute value, or None if absent."""
    m = re.search(
        rf"(?<![\w-]){re.escape(name)}\s*=\s*(\{{.*\}}|\"[^\"]*\"|'[^']*')",
        attrs,
        re.S,
    )
    if m:
        return m.group(1).strip("\"'")
    return "" if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", attrs) else None


def _line(text: str, off: int) -> int:
    return text.count("\n", 0, off) + 1


def _is_focusable(tag: str, attrs: str) -> bool:
    ti = _attr(attrs, "tabindex")
    if ti is not None:
        # `tabindex={onpick ? 0 : undefined}` is conditionally focusable, which
        # is focusable. Only a literal -1 takes an element out of tab order.
        return "-1" not in ti
    if tag == "a":
        return _attr(attrs, "href") is not None
    return tag in NATIVELY_FOCUSABLE


def _covers_the_viewport(attrs: str) -> bool:
    cls = _attr(attrs, "class") or ""
    return "inset-0" in cls and ("fixed" in cls or "absolute" in cls)


def _fn_bodies(text: str) -> dict[str, str]:
    """`{name: body}` for each `function name(...) { … }`, brace-matched.

    Needed because the correct in-repo handler is delegated:
    `onkeydown={(e) => rowEnter(e, fn)}` says nothing about Space on its own.
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", text):
        i, depth = m.end() - 1, 0
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[m.group(1)] = text[m.end() : i]
    return out


@pytest.fixture(scope="module")
def files() -> list[Path]:
    out = sorted(p for p in SRC.rglob("*.svelte") if ".bak" not in p.name)
    assert len(out) > 50, f"only {len(out)} .svelte files found — the glob is wrong"
    return out


def test_no_full_viewport_overlay_is_a_focus_stop(files):
    """The one that matters: a scrim must never take a tab stop.

    Catches a NEW one anywhere under `admin/src`, not only the nine that were
    fixed — the shape is what is wrong, and it is one copy-paste from returning.
    """
    bad = []
    for path in files:
        text = path.read_text()
        for off, tag, attrs, close in _scan(text):
            if close or not _covers_the_viewport(attrs) or not _is_focusable(tag, attrs):
                continue
            name = _attr(attrs, "aria-label") or ""
            bad.append(
                f"  {path.relative_to(ROOT)}:{_line(text, off)} <{tag}>"
                + (f' aria-label="{name}"' if name else "")
            )
    assert not bad, (
        "full-viewport overlay(s) that a keyboard has to tab THROUGH: the focus "
        "ring lands on the window edge or is clipped away, and Enter dismisses "
        "the thing the user just opened. Make it an aria-hidden <div> with the "
        "onclick and no tabindex — the keyboard route out is Escape:\n"
        + "\n".join(bad)
    )


def test_every_scrim_is_out_of_the_accessibility_tree(files):
    """A dismissed-by-pointer overlay must not be announced.

    A scrim with no name and no role reads as a blank group sitting over the
    dialog. `aria-hidden="true"` or `role="presentation"` both say the same
    thing; which one is right depends on whether the dialog is its child
    (`aria-hidden` on an ancestor would hide the dialog too).
    """
    bad = []
    for path in files:
        text = path.read_text()
        for off, tag, attrs, close in _scan(text):
            if close or tag != "div" or not _covers_the_viewport(attrs):
                continue
            if _attr(attrs, "onclick") is None:
                continue  # a positioning wrapper, not a scrim
            if (_attr(attrs, "aria-hidden") or "").strip() == "true":
                continue
            if (_attr(attrs, "role") or "") in ("presentation", "none"):
                continue
            bad.append(f"  {path.relative_to(ROOT)}:{_line(text, off)}")
    assert not bad, (
        "click-to-dismiss overlay(s) still in the accessibility tree — a screen "
        "reader announces an unnamed region covering the dialog:\n" + "\n".join(bad)
    )


def test_a_file_with_a_scrim_has_a_keyboard_way_out(files):
    """Demoting the scrim removes a keyboard dismissal. Something must replace it.

    Coarse on purpose: static analysis can see that the file provides Escape
    (`use:dialog`, or an explicit Escape branch), not that this particular
    overlay listens to it. The precise case is the next test.
    """
    bad = []
    for path in files:
        text = path.read_text()
        scrims = [
            _line(text, off)
            for off, tag, attrs, close in _scan(text)
            if not close and tag == "div" and _covers_the_viewport(attrs)
            and _attr(attrs, "onclick") is not None
        ]
        if not scrims:
            continue
        body = _blank_html_comments(_blank_js_comments(text))
        if "use:dialog" in body or "'Escape'" in body or '"Escape"' in body:
            continue
        bad.append(f"  {path.relative_to(ROOT)} lines {scrims}")
    assert not bad, (
        "overlay(s) that can only be dismissed with a mouse — the scrim takes "
        "no focus (correct) and nothing in the file handles Escape (not):\n"
        + "\n".join(bad)
    )


def test_layout_escape_closes_every_shell_overlay():
    """The mobile menu's scrim WAS its only dismissal.

    Search was already closed by `onGlobalKey`. The mobile menu was not: its
    scrim was the only way out that was not a mouse, so demoting it before
    adding Escape would have trapped the menu open for a keyboard.

    `bellOpen` used to be listed here too. The bell popover no longer exists —
    it carried the build stamp and the latest release notes, both of which the
    What's new sheet now owns, and that sheet closes on Escape through
    `use:dialog` rather than through this handler.
    """
    text = _blank_js_comments(LAYOUT.read_text())
    m = re.search(r"if\s*\(\s*e\.key\s*===\s*'Escape'\s*\)\s*\{(.*?)\n\s*\}", text, re.S)
    assert m, "+layout.svelte no longer has an Escape branch in its window keydown handler"
    branch = m.group(1)
    missing = [
        v for v in ("searchOpen", "menuOpen") if f"{v} = false" not in branch
    ]
    assert not missing, (
        "the shell's Escape handler leaves "
        + ", ".join(missing)
        + " open. Their scrims take no focus, so Escape is the ONLY keyboard "
        "dismissal — without it the overlay cannot be closed from a keyboard "
        "at all."
    )


def test_role_button_activates_on_space_as_well_as_enter(files):
    """`role="button"` is a promise. Space is what a button does.

    Without `preventDefault` Space also scrolls the page, so the row the reader
    just announced scrolls away instead of opening.
    """
    bad = []
    for path in files:
        text = path.read_text()
        fns = _fn_bodies(_blank_js_comments(text))
        for off, tag, attrs, close in _scan(text):
            if close or "button" not in (_attr(attrs, "role") or ""):
                continue
            if tag == "svelte:element" and "'button'" in (_attr(attrs, "this") or ""):
                # Renders a REAL <button> when it is interactive (the charts do
                # this so a non-pickable legend row stays a <div>). A native
                # button already activates on Space; only the ARIA impostor has
                # to implement it.
                continue
            where = f"  {path.relative_to(ROOT)}:{_line(text, off)} <{tag}>"
            handler = _attr(attrs, "onkeydown")
            if handler is None:
                bad.append(f"{where} — no onkeydown at all")
                continue
            sources = [handler] + [
                body for name, body in fns.items() if re.search(rf"\b{name}\s*\(", handler)
            ]
            if not any(re.search(r"===\s*(?:' '|\" \")", s) for s in sources):
                bad.append(f"{where} — onkeydown handles Enter only")
    assert not bad, (
        "element(s) announced as a button that ignore Space — the key a screen "
        "reader user is told to press. Today it scrolls the page instead:\n"
        + "\n".join(bad)
    )
