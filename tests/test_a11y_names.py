"""Accessible names: every control, every image, every landmark.

**Read this before changing the scanner.** A line regex cannot audit this
codebase. `<button[^>]*>` terminates at the first `>`, and every second control
here is written `onclick={() => remove(u)}` — the `=>` contains a `>`, so the
match ends mid-attribute and everything after it is invisible. An earlier pass
with that regex reported four unnamed buttons, two images with no `alt` and four
unlabelled inputs. Checked properly: no control is nameless, **both** `<img>`
hits were text inside JavaScript comments, and two of the four inputs were
labelled by a wrapping `<label>` and by an expression-valued `for`/`id` pair.
Three of ten reported defects were real. So the scanner below is brace- and
quote-aware and skips comments, and the tests assert on what it finds.

What each test guards:

* **`title` is not a name.** It is computed last in the accessible-name
  algorithm, never appears on touch, and never appears on keyboard focus. Three
  destructive row actions ("Delete user", "Delete credential", "Remove origin")
  were named by nothing else.
* **A placeholder is not a label.** It disappears the moment the field has
  content, which is exactly when somebody re-reads the form.
* **`alt` absent is not the same as `alt=""`.** The empty one is a decision
  that the image is decorative; the missing one is silence.
* **Two navigation landmarks with one name between them** leave the landmark
  list reading "navigation" / "Breadcrumb", with no way to tell which is the
  product's map.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"

FORM_TAGS = {"input", "select", "textarea"}
NAMED_BY = ("aria-label", "aria-labelledby")


def _blank_js_comments(text: str) -> str:
    """Replace the contents of `//` and `/* */` comments inside <script> with
    spaces, keeping every offset and newline so line numbers stay true.

    This is not fussiness. Both "image with no alt" findings in the first pass
    were the text `<img>` written inside a `//` comment explaining why that
    image is a beacon risk. A scanner that skips `<!-- -->` but not `//` reports
    prose as markup. String-awareness is required too: `'https://…'` inside a
    script block is not a comment.
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


def _scan(raw: str):
    """Yield (offset, end_of_open_tag, tag, attrs_text, self_closing, is_close_tag).

    Brace- and quote-aware, so `{() => f(x)}` does not end the tag. `<!-- -->`
    is skipped whole — both "missing alt" reports in the first pass were `<img>`
    written inside a comment explaining why that image is dangerous.
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
        m = re.match(r"<(/?)([A-Za-z][\w.\-]*)", text[i:])
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
    """The raw source of one attribute value, or None if it is absent.

    Returns the text as written — `{'f-' + f.key}` comes back as that string —
    because a `for`/`id` pair written as the same expression renders to the same
    string at runtime, and comparing the expressions is how that association is
    recognised without evaluating Svelte.
    """
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
def files() -> list[Path]:
    # `.bak` copies are not shipped and their line numbers are meaningless.
    out = sorted(p for p in SRC.rglob("*.svelte") if ".bak" not in p.name)
    assert len(out) > 50, f"only {len(out)} .svelte files found — the glob is wrong"
    return out


def test_every_form_control_has_a_name(files):
    """`aria-label`, `aria-labelledby`, `<label for>`, or a wrapping `<label>`."""
    bad = []
    for path in files:
        text = path.read_text()
        # Every `for` in the file. Svelte renders `for={'f-' + k}` and
        # `id={'f-' + k}` to the same string, so the expressions are compared.
        fors = {
            _attr(a, "for")
            for _, _, t, a, _, close in _scan(text)
            if t == "label" and not close and _attr(a, "for") is not None
        }
        depth = 0  # how deep we are inside a <label>
        for off, _end, tag, attrs, selfclose, close in _scan(text):
            if tag == "label":
                depth += -1 if close else (0 if selfclose else 1)
                continue
            if close or tag not in FORM_TAGS:
                continue
            if any(_attr(attrs, k) is not None for k in NAMED_BY):
                continue
            if _attr(attrs, "id") in fors:
                continue
            if depth > 0:  # wrapped by a <label>, implicit association
                continue
            cls = _attr(attrs, "class") or ""
            if "hidden" in cls:
                # display:none — out of the accessibility tree entirely. The
                # file picker is like this and is clicked by a labelled button.
                continue
            if (_attr(attrs, "type") or "") in ("hidden", "submit", "button"):
                continue
            ph = _attr(attrs, "placeholder")
            bad.append(
                f"  {path.relative_to(ROOT)}:{_line(text, off)} <{tag}>"
                + (f"  placeholder={ph!r}" if ph else "  (no placeholder either)")
            )
    assert not bad, (
        "form control(s) with no accessible name — a placeholder is not a "
        "label, it vanishes as soon as the field has content:\n" + "\n".join(bad)
    )


def test_every_image_declares_an_alternative(files):
    """`alt=""` is a decision. No `alt` at all is silence."""
    bad = []
    for path in files:
        text = path.read_text()
        for off, _end, tag, attrs, _, close in _scan(text):
            if tag == "img" and not close and _attr(attrs, "alt") is None:
                bad.append(f"  {path.relative_to(ROOT)}:{_line(text, off)}")
    assert not bad, (
        "<img> with no alt. If it is decorative say so with alt=\"\" — the "
        "absent attribute makes a screen reader read the filename:\n"
        + "\n".join(bad)
    )


def test_no_control_is_named_only_by_title(files):
    """`title` is last in the name algorithm and invisible to touch and keyboard.

    Only controls whose content is a single self-closing icon component are
    checked: a `title` on a control that also has visible text is a tooltip,
    which is a different and legitimate thing.
    """
    bad = []
    for path in files:
        text = path.read_text()
        for off, end, tag, attrs, selfclose, close in _scan(text):
            if close or selfclose or tag not in ("button", "a"):
                continue
            if _attr(attrs, "title") is None:
                continue
            if any(_attr(attrs, k) is not None for k in NAMED_BY):
                continue
            # Content is taken from the scanner's own end-of-open-tag. Slicing
            # at the first ">" instead cuts inside `onclick={() => f(x)}` and
            # leaves the rest of the attributes looking like body text, which
            # made an earlier version of THIS test pass over the three controls
            # it was written to catch.
            stop = text.find(f"</{tag}", end)
            body = re.sub(r"<[^>]*>", "", text[end : stop if stop != -1 else end])
            if body.strip():
                continue  # has visible text; the title is a tooltip
            bad.append(
                f"  {path.relative_to(ROOT)}:{_line(text, off)} <{tag} "
                f"title={_attr(attrs, 'title')!r}>"
            )
    assert not bad, (
        "icon-only control(s) named only by `title`. Add aria-label and keep "
        "title for the tooltip:\n" + "\n".join(bad)
    )


def test_every_navigation_landmark_is_named(files):
    """More than one `nav` on a page and only one name is worse than none."""
    bad = []
    for path in files:
        text = path.read_text()
        for off, _end, tag, attrs, _, close in _scan(text):
            if tag != "nav" or close:
                continue
            if not any(_attr(attrs, k) for k in NAMED_BY):
                bad.append(f"  {path.relative_to(ROOT)}:{_line(text, off)} <nav>")
    assert not bad, (
        "unlabelled <nav>. The console renders two or three navigation "
        "landmarks per page, so an unnamed one is just 'navigation' in the "
        "landmark list:\n" + "\n".join(bad)
    )


def test_the_primary_navigation_is_the_one_called_main(files):
    """Naming the breadcrumb but not the rail is the failure this catches."""
    layout = (SRC / "routes" / "+layout.svelte").read_text()
    names = [
        _attr(a, "aria-label")
        for _, _, t, a, _, close in _scan(layout)
        if t == "nav" and not close
    ]
    assert "Main" in names, (
        f"the rail's <nav> is not labelled 'Main' (found {names!r}); it is the "
        f"product's map and it sits beside a nav that IS named"
    )
