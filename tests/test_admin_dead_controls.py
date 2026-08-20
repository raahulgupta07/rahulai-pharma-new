"""No control on this console may look interactive and do nothing.

The Catalog & stock page carried two of them for months: `Upload` and `Export`
sat in its header, hovered, took a click, and fired no request, showed no error
and raised nothing. Nobody reported it, because a button that does nothing
looks exactly like a button whose job you misunderstood.

A dead control is worse than an absent one — it teaches the reader that a click
on this console might mean nothing, and that doubt then applies to every other
button on the page.

So: every `<button>` either does something, or is `disabled` and says why.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.svelte_source import blank_comments

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"

#: Ways a button can be wired up. `use:` covers actions, `bind:this` covers a
#: button driven imperatively from script.
WIRED = re.compile(r"onclick|onmousedown|onpointerdown|use:|bind:this|type=\"submit\"")


def _tags(src: str, name: str):
    """Each `<name ...>` opening tag, with the line it starts on.

    Comments are blanked first. This test started failing the day four comments
    were written that mention `<button>` while explaining why a scrim must NOT
    be one — it was reporting the documentation of the fix as the defect.
    """
    src = blank_comments(src)
    for m in re.finditer(rf"<{name}\b", src):
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
        yield src[m.start() : i], src[: m.start()].count("\n") + 1


def test_no_button_is_decorative():
    dead: list[str] = []
    total = 0
    for f in sorted(SRC.rglob("*.svelte")):
        if ".bak" in f.name:
            continue
        src = f.read_text()
        for tag, line in _tags(src, "button"):
            total += 1
            if WIRED.search(tag):
                continue
            # A control that is off ON PURPOSE is honest, and the reader can
            # see it is off. Those are allowed; silent ones are not.
            if re.search(r"\bdisabled\b", tag):
                continue
            dead.append(f"{f.relative_to(ROOT)}:{line}")
    assert total > 50, "the scan found almost no buttons — the parser is wrong, not the console"
    assert not dead, (
        f"{len(dead)} button(s) have no handler and are not disabled, so they "
        f"take a click and do nothing:\n  " + "\n  ".join(dead)
    )


def test_the_upload_control_goes_where_the_work_happens():
    """Catalog & stock does not ingest files; Data pipeline does. The control
    is a link there rather than a second door onto the same endpoint."""
    src = (SRC / "routes" / "data" / "+page.svelte").read_text()
    m = re.search(r"<a\b[^>]*?href=\{appBase \+ '(/[\w-]+)'\}[^>]*>\s*<Upload", src, re.S)
    assert m, "the Upload control on Catalog & stock is gone or no longer a link"
    assert m.group(1) == "/ftp", (
        f"Upload points at {m.group(1)!r} rather than the page that ingests files"
    )


def test_the_export_button_is_not_back_without_an_endpoint():
    """It was removed because nothing served it. Putting it back before the
    endpoint exists recreates the exact control this file is named after."""
    src = (SRC / "routes" / "data" / "+page.svelte").read_text()
    # The word survives in the comment that records why it went. What matters
    # is whether a CONTROL offers it again.
    markup = src[src.index("</script>") :]
    if not re.search(r"<(?:button|a)\b[^>]*>[^<]*Export", markup, re.S):
        return
    api = (ROOT / "app" / "admin.py").read_text()
    assert re.search(r'@router\.get\("/(catalog|articles|inventory)/export', api), (
        "Catalog & stock offers an Export control and no endpoint serves it"
    )
