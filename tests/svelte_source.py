"""Reading `.svelte` source in tests, without the two traps that keep firing.

Not a test module. Shared helpers, because both mistakes below have now been
made five times in this repo — three times by tests written in the same day as
this file, twice by tests written months apart.

**Trap 1: `>` inside an attribute expression.** A regex like `<button[^>]*>`
terminates at the first `>`, and half the controls here are written
`onclick={() => remove(u)}` — the `=>` contains a `>`. The match ends
mid-attribute, everything after it is invisible, and the test quietly reports
either nothing or nonsense. One audit using that pattern reported four unnamed
buttons where none was nameless.

**Trap 2: the prose describing the defect.** Every guard in this suite is
documented, and good documentation names the thing it forbids. A test for
`translate-x-full` matched the comment explaining why `translate-x-full` is
wrong. A test for `<button>` scrims matched a comment saying the scrim "used to
be a `<button>`". A test for Escape handling passed on a file with no handler,
because the file's comments said "Escape". A test is not allowed to read its own
documentation and call it evidence.

So: blank comments first, then scan brace-aware.
"""

from __future__ import annotations

import re

__all__ = ["blank_comments", "scan_tags", "attr", "line_of"]


def blank_comments(text: str) -> str:
    """Replace comment BODIES with spaces, preserving every offset and newline.

    Both kinds: `<!-- -->` anywhere, and `//` / `/* */` inside `<script>`.
    Offsets are preserved so reported line numbers still point at real source.

    String-awareness inside script matters: `'https://example.com'` is not a
    comment, and blanking from `//` there would eat the rest of the line.
    """
    out = list(text)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    for m in re.finditer(r"<!--.*?-->", text, re.S):
        blank(m.start(), m.end())

    for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.S):
        body, base = m.group(1), m.start(1)
        i, n = 0, len(body)
        while i < n:
            c = body[i]
            if c in "\"'`":
                quote, i = c, i + 1
                while i < n:
                    if body[i] == "\\":
                        i += 2
                        continue
                    if body[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if c == "/" and i + 1 < n and body[i + 1] == "/":
                start = i
                while i < n and body[i] != "\n":
                    i += 1
                blank(base + start, base + i)
                continue
            if c == "/" and i + 1 < n and body[i + 1] == "*":
                j = body.find("*/", i + 2)
                j = n if j == -1 else j + 2
                blank(base + i, base + j)
                i = j
                continue
            i += 1
    return "".join(out)


def scan_tags(text: str, *, strip_comments: bool = True):
    """Yield `(start, end, tag, attrs, self_closing, is_closing)` for every tag.

    Brace- and quote-aware, so `{() => f(x)}` does not end the tag. `end` is the
    offset just past the opening tag's `>` — slice content from there, never
    from the first `>` you can find.

    The tag-name pattern includes `:` on purpose. Without it `<svelte:window>`
    scans as a tag called `svelte`, and a test looking for `<svelte:window>`
    silently finds nothing — that produced an Escape-handler test that passed
    against a page with no handler at all.
    """
    src = blank_comments(text) if strip_comments else text
    i, n = 0, len(src)
    while i < n:
        if src[i] != "<":
            i += 1
            continue
        m = re.match(r"<(/?)([A-Za-z][\w.:\-]*)", src[i:])
        if not m:
            i += 1
            continue
        j, depth, quote = i + m.end(), 0, None
        while j < n:
            ch = src[j]
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
        attrs = src[i + m.end() : j]
        yield i, j + 1, m.group(2), attrs, attrs.rstrip().endswith("/"), m.group(1) == "/"
        i = j + 1


def attr(attrs: str, name: str) -> str | None:
    """One attribute's value as WRITTEN, or None if absent; `""` if bare.

    Expressions come back as their source text — `{'f-' + f.key}` stays that
    string. That is deliberate: a `for`/`id` pair written as the same expression
    renders to the same string at runtime, and comparing the expressions is how
    that association is recognised without evaluating Svelte.
    """
    m = re.search(
        rf"(?<![\w-]){re.escape(name)}\s*=\s*(\{{.*?\}}|\"[^\"]*\"|'[^']*')", attrs, re.S
    )
    if m:
        return m.group(1).strip("\"'")
    return "" if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", attrs) else None


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
