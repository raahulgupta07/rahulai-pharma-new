"""Dialogs: every one of them, and the four things each has to do.

Seven dialog-ish surfaces existed and each solved focus from scratch. Two got it
right. Measured on the running console, the other five failed in different
combinations — and the worst was `lib/aurora/Modal.svelte`, which is what BOTH
delete confirmations use:

    open: True | focus: BUTTON "Delete credential…" inDlg=false
    after Escape (focus never entered dialog) still open: True
    after 6 Tabs focus: BUTTON "Close" inDlg=true
    after Escape (focus now inside) still open: False

Focus never entered, so the confirmation appeared while the keyboard was still
in the page underneath; Escape did nothing until the user had tabbed all the way
in, because the handler was bound to the overlay `<div>` and a DOM listener
cannot see a keystroke outside its own subtree; and cancelling left focus on
`<body>`.

Two drawers had a second, quieter defect: always mounted, hidden with
`translate-x-full`. A transform is a paint operation and says nothing about
focus, so a keyboard sweep of `/data` hit "Close" at x=1790 — 350px past a
1440px viewport — as a stop with the ring drawn where nobody can see it.

`lib/aurora/dialog.js` now owns all of it. These tests assert that every dialog
uses it, that it still does the four things, and that nothing goes back to
hiding a live drawer with a transform.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"
ACTION = SRC / "lib" / "aurora" / "dialog.js"


@pytest.fixture(scope="module")
def files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.svelte") if ".bak" not in p.name)


@pytest.fixture(scope="module")
def action() -> str:
    return ACTION.read_text()


def _no_comments(text: str) -> str:
    """Blank `<!-- -->` bodies, keeping offsets and newlines.

    The prose in this repo talks ABOUT the defects it guards — the comment
    explaining why a drawer must not be hidden with `translate-x-full` contains
    the string `translate-x-full`. An earlier version of the transform test
    reported those two comments as the defect they describe.
    """
    out = list(text)
    for m in re.finditer(r"<!--.*?-->", text, re.S):
        for k in range(m.start(), m.end()):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _dialog_tags(text: str) -> list[tuple[int, str]]:
    """(line, opening tag) for every element carrying role="dialog"."""
    out = []
    for m in re.finditer(r"<(\w+)\b((?:[^<>{]|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*?)>", text, re.S):
        if 'role="dialog"' in m.group(2):
            out.append((text.count("\n", 0, m.start()) + 1, m.group(0)))
    return out


def test_every_dialog_uses_the_shared_action(files):
    """A bespoke one is a bespoke set of the same four bugs."""
    bad = []
    for path in files:
        text = path.read_text()
        for line, tag in _dialog_tags(text):
            if "use:dialog" not in tag:
                bad.append(f"  {path.relative_to(ROOT)}:{line}\n      {' '.join(tag.split())[:150]}")
    assert not bad, (
        "role=\"dialog\" element(s) not wired to lib/aurora/dialog.js. Focus "
        "will not enter, Tab will not be trapped, Escape will not close it and "
        "focus will not come back:\n" + "\n".join(bad)
    )


def test_every_dialog_can_hold_focus_itself(files):
    """`tabindex=\"-1\"` is what makes the fallback focus target work."""
    bad = []
    for path in files:
        text = path.read_text()
        for line, tag in _dialog_tags(text):
            if 'tabindex="-1"' not in tag:
                bad.append(f"  {path.relative_to(ROOT)}:{line}")
    assert not bad, (
        "dialog(s) with no tabindex=\"-1\": if the dialog has no focusable "
        "child, focus stays outside it entirely:\n" + "\n".join(bad)
    )


def test_every_dialog_has_an_accessible_name(files):
    bad = []
    for path in files:
        text = path.read_text()
        for line, tag in _dialog_tags(text):
            if "aria-label" not in tag:  # covers aria-labelledby too
                bad.append(f"  {path.relative_to(ROOT)}:{line}")
    assert not bad, (
        "dialog(s) announced only as \"dialog\":\n" + "\n".join(bad)
    )


def test_no_dialog_is_an_aside(files):
    """`role=\"dialog\"` on `<aside>` is invalid — and the role supersedes the
    complementary landmark anyway, so the element gains nothing by being one."""
    bad = []
    for path in files:
        text = path.read_text()
        for line, tag in _dialog_tags(text):
            if tag.startswith("<aside"):
                bad.append(f"  {path.relative_to(ROOT)}:{line}")
    assert not bad, "<aside> carrying role=\"dialog\":\n" + "\n".join(bad)


def test_no_live_panel_is_hidden_only_by_a_transform(files):
    """The tab-order leak. `translate-x-full` hides pixels, not focus."""
    bad = []
    for path in files:
        text = path.read_text()
        for m in re.finditer(r"translate-x-full", _no_comments(text)):
            line = text.count("\n", 0, m.start()) + 1
            # Legitimate: the responsive rail, which is toggled with the same
            # class but is a real navigation region present at every width.
            window = text[max(0, m.start() - 400) : m.start()]
            if "class=\"rail" in window or "bg-rail-bg" in window:
                continue
            bad.append(f"  {path.relative_to(ROOT)}:{line}")
    assert not bad, (
        "panel(s) slid off-screen with a transform while staying mounted. "
        "Every control inside stays in the tab order, off the side of the "
        "viewport, with Enter still wired up:\n" + "\n".join(bad)
    )


def test_every_modal_call_site_passes_a_title(files):
    """`Modal`'s name is `aria-label={title}` and `title` defaults to `''`."""
    bad = []
    for path in files:
        text = path.read_text()
        for m in re.finditer(r"<Modal\b((?:[^<>{]|\{[^{}]*\})*?)>", text, re.S):
            if not re.search(r"\btitle=", m.group(1)):
                bad.append(f"  {path.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}")
    assert not bad, (
        "<Modal> used with no title. Its accessible name is that prop, and the "
        "default is an empty string — a nameless confirmation dialog:\n"
        + "\n".join(bad)
    )


# ---- the action itself ------------------------------------------------------


def test_escape_is_bound_to_the_window_not_the_node(action):
    """The original bug: a node listener cannot see a key pressed outside it."""
    m = re.search(r"window\.addEventListener\(\s*'keydown'\s*,\s*(\w+)\s*,\s*true\s*\)", action)
    assert m, (
        "the keydown listener is not on window in the capture phase. On the "
        "node it cannot fire before focus has moved in — which is exactly how "
        "Escape came to be dead on both delete confirmations"
    )
    assert re.search(r"window\.removeEventListener\(\s*'keydown'", action), (
        "the listener is never removed; every dialog opened leaves one behind"
    )


def test_focus_is_captured_before_it_moves_and_restored_after(action):
    assert re.search(r"const openedFrom = document\.activeElement", action), (
        "nothing records the trigger, so there is nothing to return focus to"
    )
    i = action.index("const openedFrom")
    j = action.index("function focusFirst")
    assert i < j, (
        "the trigger is captured after focus has already been moved, so it "
        "records the dialog's own first control instead"
    )
    assert "returnTo.isConnected" in action, (
        "focus is restored without checking the trigger still exists — the row "
        "that a delete confirmation just deleted is detached, and focusing a "
        "detached node silently drops focus to <body>"
    )


def test_the_tab_trap_wraps_in_both_directions(action):
    assert "e.shiftKey && document.activeElement === first" in action, (
        "Shift-Tab off the first control is not caught, so backwards Tab "
        "leaves the dialog"
    )
    assert "!e.shiftKey && document.activeElement === last" in action, (
        "Tab off the last control is not caught"
    )
    assert "!node.contains(document.activeElement)" in action, (
        "the trap assumes focus is already inside; if anything moved it out, "
        "Tab escapes and never comes back"
    )


def test_hidden_controls_are_not_counted_as_focusable(action):
    """`offsetParent` is null for a fixed-position element, so it cannot be the
    test — every one of these drawers is `position: fixed`."""
    assert "getClientRects" in action, (
        "focusable detection no longer uses getClientRects; if it went back to "
        "offsetParent, every control in a fixed-position drawer reads as "
        "hidden and the trap silently does nothing"
    )


def test_the_background_cannot_scroll_under_an_open_dialog(action):
    assert "document.body.style.overflow = 'hidden'" in action, "no scroll lock"
    assert "prevOverflow" in action, (
        "the previous overflow is not restored, so closing a dialog can leave "
        "the whole console unscrollable"
    )


def test_the_chained_case_resolves_its_target_at_close_not_at_open(action):
    """A dialog that opens from another dialog.

    "Continue" on the auth replace-confirmation opens the provider modal and is
    removed from the DOM in the same breath. Capturing `activeElement` at mount
    would record that button, and restoring to a detached node drops focus to
    `<body>`. The caller supplies a getter instead, and it must be called at
    teardown — at mount the caller does not yet know the answer.
    """
    assert "returnToFn?.()" in action, (
        "the returnTo override is gone or is being read at mount; a chained "
        "dialog then hands focus back to a button that no longer exists"
    )
    i = action.index("returnToFn?.()")
    j = action.index("destroy()")
    assert i > j, "returnTo is resolved before teardown, which is too early"
