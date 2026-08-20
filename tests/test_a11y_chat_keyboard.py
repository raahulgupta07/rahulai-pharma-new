"""The chat page from a keyboard: getting out of a menu, and opening a chat.

Four defects, all measured on the running console with the keyboard alone.

**You could not close the branch or language picker.** `routes/chat/+page.svelte`
had no Escape handler of any kind and no `<svelte:window>`; the layout's global
handler knows only its own search and bell popovers, and a DOM listener cannot
see a keystroke that lands outside its own subtree. Transcript:

    scrim present: True | focus: BUTTON "All 53 branches" (the trigger)
    tab1 BUTTON "Close menu" (invisible scrim)   tab2 INPUT "Filter branches"
    after Escape menu still open: True

So the only dismissal was to find and click a scrim nobody can see. That scrim
is the second defect: a real `<button>`, `x=0 y=0 w=1440`, background
`rgba(0,0,0,0)`. It painted nothing and still took a tab stop, with the focus
ring drawn around the edge of the window. A backdrop is a pointer affordance; it
must not be a stop in the tab order, and Escape is what a keyboard uses instead.

**The conversation rows were `<div role="button">`.** A screen reader announces
that as a button, and Space is a button's primary activation key — here it
scrolled the page. Enter alone is what a link promises, not a button. And each
row had a `<button aria-label="Delete conversation">` *inside* it: interactive
inside interactive, which `role="button"` does not permit, and which leaves the
delete control a descendant of the thing it sits next to.

These tests read the source rather than the browser, so they run in the fast
suite. Anything that scans markup here is brace-aware — `<button[^>]*>` ends at
the first `>`, and `onclick={() => f(x)}` contains one — and blanks comments
first, because the prose above says `role="button"` four times.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"
CHAT = SRC / "routes" / "chat" / "+page.svelte"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# ---- scanner (copied from tests/test_a11y_names.py; same reasons) -----------


def _blank_js_comments(text: str) -> str:
    """Blank `//` and `/* */` bodies inside <script>, preserving every offset.

    String-aware, so `'https://…'` is not read as a comment.
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


def _blank_markup_comments(text: str) -> str:
    """Blank `<!-- -->` bodies, keeping offsets and newlines."""
    out = list(text)
    for m in re.finditer(r"<!--.*?-->", text, re.S):
        for k in range(m.start(), m.end()):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _scan(raw: str):
    """Yield (offset, end, tag, attrs, self_closing, is_close_tag).

    Brace- and quote-aware, so `{() => f(x)}` does not terminate the tag. The
    tag-name pattern allows `:` — unlike the copy in `test_a11y_names.py`, which
    has no reason to care — because `<svelte:window>` is exactly the element
    this file has to find, and without it the scan reports a tag called
    `svelte` and the Escape test passes on a page with no handler at all.
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
        m = re.match(r"<(/?)([A-Za-z][\w.:\-]*)", text[i:])
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
        attrs = text[i + m.end() : j]
        yield i, j + 1, m.group(2), attrs, attrs.rstrip().endswith("/"), m.group(1) == "/"
        i = j + 1


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(
        rf"(?<![\w-]){re.escape(name)}\s*=\s*(\{{.*?\}}|\"[^\"]*\"|'[^']*')",
        attrs,
        re.S,
    )
    if m:
        return m.group(1).strip("\"'")
    return "" if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", attrs) else None


def _line(text: str, off: int) -> int:
    return text.count("\n", 0, off) + 1


@pytest.fixture(scope="module")
def chat() -> str:
    return CHAT.read_text()


@pytest.fixture(scope="module")
def chat_script(chat: str) -> str:
    """The `<script>` block with its comments blanked.

    Every assertion about the Escape handler has to run against this, not the
    raw file: the handler's own docstring names `storeOpen`, `langOpen`, Escape
    and `.focus()`, so a raw search passes on the prose describing the bug.
    """
    m = re.search(r"<script[^>]*>(.*?)</script>", _blank_js_comments(chat), re.S)
    assert m, "no <script> block in the chat page"
    return m.group(1)


@pytest.fixture(scope="module")
def svelte_files() -> list[Path]:
    out = sorted(p for p in SRC.rglob("*.svelte") if ".bak" not in p.name)
    assert len(out) > 50, f"only {len(out)} .svelte files found — the glob is wrong"
    return out


# ---- 1. Escape ---------------------------------------------------------------


def test_the_pickers_can_be_dismissed_without_a_mouse(chat, chat_script):
    """Without this the only way out of an open menu is clicking an invisible
    scrim — and a keyboard user cannot click."""
    win = [a for _o, _e, t, a, _s, close in _scan(chat) if t == "svelte:window" and not close]
    assert win, (
        "the chat page has no <svelte:window>. A keydown listener bound inside "
        "the page cannot see Escape pressed while focus is on the picker "
        "trigger, so the branch and language menus stay open"
    )
    assert any("onkeydown" in a for a in win), (
        "<svelte:window> is present but binds no keydown, so Escape still does "
        "nothing to the branch and language menus"
    )
    handler = re.search(
        r"function onWindowKeydown\(([^)]*)\)\s*\{(.*?)\n  \}", chat_script, re.S
    )
    assert handler, "onWindowKeydown is gone; nothing on the window handles Escape"
    body = handler.group(2)
    assert "'Escape'" in body or '"Escape"' in body, (
        "the window keydown handler does not test for Escape — the key users "
        "actually press to dismiss a menu"
    )
    assert "storeOpen = false" in body, "Escape does not close the branch picker"
    assert "langOpen = false" in body, "Escape does not close the language picker"


def test_dismissing_a_picker_hands_focus_back_to_its_trigger(chat, chat_script):
    """The menu is unmounted on close. Focus on a removed node falls to <body>,
    so the next Tab restarts at the top of the console."""
    tags = [(o, a) for o, _e, t, a, _s, close in _scan(chat) if t == "button" and not close]
    for var in ("storeBtn", "langBtn"):
        assert any(f"bind:this={{{var}}}" in a for _o, a in tags), (
            f"no trigger button is bound to `{var}`, so nothing can return "
            "focus to the control that opened the menu"
        )
    handler = re.search(r"function onWindowKeydown\(([^)]*)\)\s*\{(.*?)\n  \}", chat_script, re.S)
    assert handler and "storeBtn?.focus()" in handler.group(2), (
        "Escape closes the branch menu and leaves focus on the detached menu — "
        "which means <body>, and a Tab sweep that starts over"
    )
    assert "langBtn?.focus()" in handler.group(2), (
        "Escape closes the language menu without returning focus to its trigger"
    )
    for fn, btn in (("pickStore", "storeBtn"), ("pickLang", "langBtn")):
        m = re.search(rf"function {fn}\(([^)]*)\)\s*\{{(.*?)\n  \}}", chat_script, re.S)
        assert m and f"{btn}?.focus()" in m.group(2), (
            f"{fn} closes the menu without moving focus; choosing an item with "
            "the keyboard destroys the row that had focus and drops it to <body>"
        )


def test_an_opened_picker_receives_focus_but_does_not_trap_tab(chat, chat_script):
    """A menu is not a modal. Focus goes in so the filter field is usable; Tab
    must still walk out into the composer, and Escape is the way back."""
    assert "function menuFocus(node)" in chat_script, (
        "nothing moves focus into an opened picker, so a keyboard user has to "
        "Tab past the trigger to reach the branch filter"
    )
    uses = [a for _o, _e, _t, a, _s, close in _scan(chat) if not close and "use:menuFocus" in a]
    assert len(uses) == 2, (
        f"{len(uses)} of the 2 picker popovers move focus in on open"
    )
    menu_body = re.search(r"function menuFocus\(node\)\s*\{(.*?)\n  \}", chat_script, re.S)
    assert menu_body and "addEventListener" not in menu_body.group(1), (
        "menuFocus has grown a key listener. If it is trapping Tab, these "
        "popovers have become modals: the composer behind them stays live and "
        "un-dimmed, so a trap strands the user inside a branch filter"
    )


# ---- 2. the scrims -----------------------------------------------------------


def _scrims(text: str):
    """(offset, tag, attrs) for every full-viewport backdrop in a file."""
    for off, _end, tag, attrs, _s, close in _scan(text):
        if close:
            continue
        cls = _attr(attrs, "class") or ""
        if "fixed inset-0" in cls:
            yield off, tag, attrs


def test_no_backdrop_is_a_stop_in_the_tab_order(svelte_files):
    """Measured: `BUTTON "Close menu" x=0 y=0 w=1440 bg=rgba(0,0,0,0)`."""
    bad = []
    for path in svelte_files:
        text = path.read_text()
        for off, tag, attrs in _scrims(text):
            ti = _attr(attrs, "tabindex")
            focusable = tag in ("button", "a", "input", "select", "textarea") or (
                ti is not None and not ti.startswith("-")
            )
            if focusable:
                bad.append(f"  {path.relative_to(ROOT)}:{_line(text, off)} <{tag}>")
    assert not bad, (
        "full-viewport backdrop(s) that take focus. They paint nothing, so the "
        "user gets a tab stop on invisible content with the focus ring drawn "
        "around the edge of the window:\n" + "\n".join(bad)
    )


def test_every_backdrop_on_the_chat_page_is_hidden_from_assistive_tech(chat):
    """A scrim is a pointer convenience. Announcing it adds a control to the
    reader's list that does nothing a reader can use.

    Chat-scoped on purpose. Repo-wide, `lib/aurora/Modal.svelte` and
    `lib/charts/TurnDrawer.svelte` have the same clickable-but-unlabelled
    backdrop; both are outside this file's remit and are reported rather than
    changed here. All three of the chat page's backdrops — the two pickers and
    the source drawer — get the same treatment.
    """
    bad = []
    for off, tag, attrs in _scrims(chat):
        if "onclick" not in attrs:
            continue  # a plain dim layer, nothing to announce
        if _attr(attrs, "aria-hidden") != "true":
            bad.append(f"  chat/+page.svelte:{_line(chat, off)} <{tag}>")
    assert len([1 for _o, _t, a in _scrims(chat) if "onclick" in a]) == 3, (
        "the chat page no longer has its three clickable backdrops (branch "
        "picker, language picker, source drawer) — this test is not measuring "
        "what it claims to"
    )
    assert not bad, (
        "clickable backdrop(s) still exposed to screen readers:\n" + "\n".join(bad)
    )


# ---- 3 & 4. the conversation rows -------------------------------------------


def test_a_conversation_row_is_a_real_button(chat):
    """`role="button"` promises Enter AND Space. The row honoured only Enter, so
    Space scrolled the sidebar instead of opening the conversation. A real
    `<button>` gets both from the browser and cannot drift."""
    fake = [
        (_line(chat, off), tag)
        for off, _e, tag, attrs, _s, close in _scan(chat)
        if not close and _attr(attrs, "role") == "button"
    ]
    assert not fake, (
        "element(s) faking a button on the chat page. Every one of them owes "
        "the user Enter and Space by hand, and the row that had this handled "
        "only Enter:\n" + "\n".join(f"  chat/+page.svelte:{ln} <{t}>" for ln, t in fake)
    )
    opens = [
        attrs
        for _o, _e, tag, attrs, _s, close in _scan(chat)
        if tag == "button" and not close and "openChat" in (_attr(attrs, "onclick") or "")
    ]
    assert opens, (
        "no <button> opens a conversation — the sidebar row is activated by "
        "something that is not a button, which is how Space stopped working"
    )


def _element_end(text: str, start: int) -> int:
    """Offset just past the closing tag of the element opening at `start`."""
    depth = 0
    for off, end, tag, _a, selfclose, close in _scan(text):
        if off < start:
            continue
        if tag in VOID or selfclose:
            continue
        depth += -1 if close else 1
        if depth == 0:
            return end
    return len(text)


def test_the_delete_control_is_a_sibling_of_the_row_not_a_child(chat):
    """Interactive inside interactive. `role="button"` allows no interactive
    descendant, and a delete button inside the control that opens the thread is
    ambiguous to every assistive technology that flattens it."""
    text = _blank_markup_comments(chat)
    row = next(
        (
            (off, end)
            for off, end, tag, attrs, _s, close in _scan(text)
            if tag == "button" and not close and "openChat" in (_attr(attrs, "onclick") or "")
        ),
        None,
    )
    assert row, "the conversation row's open control is not a <button>"
    row_end = _element_end(text, row[0])
    dele = next(
        (
            off
            for off, _e, tag, attrs, _s, close in _scan(text)
            if tag == "button"
            and not close
            and _attr(attrs, "aria-label") == "Delete conversation"
        ),
        None,
    )
    assert dele is not None, "the per-row delete button is gone"
    assert not (row[0] < dele < row_end), (
        f"the delete button (line {_line(text, dele)}) is nested inside the "
        "control that opens the conversation. Nested interactive content is "
        "invalid and unreachable-or-ambiguous depending on the reader"
    )


def test_the_delete_control_still_appears_on_hover_and_on_focus(chat):
    """The reveal is what makes the row readable at rest. A fix for the nesting
    that dropped `group-focus-within` would hide the delete button from every
    keyboard user instead."""
    m = re.search(r'aria-label="Delete conversation"', chat)
    assert m, "the per-row delete button is gone"
    tag = next(
        attrs
        for _o, _e, t, attrs, _s, close in _scan(chat)
        if t == "button" and not close and _attr(attrs, "aria-label") == "Delete conversation"
    )
    cls = _attr(tag, "class") or ""
    assert "group-hover:block" in cls and "group-focus-within:block" in cls, (
        "the delete button no longer reveals on hover AND on focus-within; "
        f"without focus-within it is invisible to a keyboard user: {cls!r}"
    )
    assert re.search(r'class="group [^"]*', chat), (
        "the row wrapper lost its `group` class, so the reveal never fires"
    )
