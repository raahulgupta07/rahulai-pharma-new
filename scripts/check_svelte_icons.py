#!/usr/bin/env python3
"""Fail if a .svelte file references a component/icon it never brought into scope.

Why this exists: an icon used in a nav entry (`icon: KeyRound`) or in markup
(`<KeyRound/>`) but never imported is NOT a Vite build error — the bundle builds
clean, then the SPA throws `ReferenceError` at startup and every page renders
BLANK. `svelte-check` does not flag it either (a plain-JS `<script>` is not
checked for undefined identifiers). This scanner does.

It resolves names the file legitimately defines — imports, top-level
declarations, `$props()` destructuring, `{#each ... as x}`, `{@const x}`,
`{#snippet x()}` — and flags any PascalCase component reference that is left.

Exit 0 = clean. Exit 1 = at least one unresolved reference (prints them).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "admin" / "src"

# PascalCase names that are DOM/Svelte built-ins or otherwise always in scope.
BUILTINS = {"DOCTYPE"}


def _defined_names(head: str, body: str) -> set[str]:
    names: set[str] = set(BUILTINS)

    # import Default, { A, B as C }, * as NS  from '...'
    for m in re.finditer(r"import\s+([^;]+?)\s+from\s*['\"][^'\"]+['\"]", head, re.S):
        clause = m.group(1)
        for nm in re.finditer(r"\b([A-Za-z_$]\w*)\b(?:\s+as\s+([A-Za-z_$]\w*))?", clause):
            names.add(nm.group(2) or nm.group(1))

    # top-level const/let/var + function/class declarations (incl. simple destructuring)
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$]\w*)", head):
        names.add(m.group(1))
    for m in re.finditer(r"\b(?:function|class)\s+([A-Za-z_$]\w*)", head):
        names.add(m.group(1))

    # destructured names from `let { a, icon: Icon } = $props()` (and any {...} =)
    for m in re.finditer(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", head):
        for part in m.group(1).split(","):
            part = part.strip()
            if ":" in part:
                part = part.split(":", 1)[1].strip()   # alias target
            nm = re.match(r"[A-Za-z_$]\w*", part)
            if nm:
                names.add(nm.group(0))

    # template-scoped bindings. `as` may destructure: `as [k, v, Icon]`,
    # `as {a, b}`, `as x, i` — pull every identifier out of the pattern.
    for m in re.finditer(r"\{#each\b.+?\bas\s+([^}]+?)\s*\}", body, re.S):
        for nm in re.findall(r"[A-Za-z_$]\w*", m.group(1)):
            names.add(nm)
    for m in re.finditer(r"\{@const\s+([A-Za-z_$]\w*)", body):
        names.add(m.group(1))
    for m in re.finditer(r"\{#snippet\s+([A-Za-z_$]\w*)", body):
        names.add(m.group(1))
    return names


def check_file(path: Path) -> list[str]:
    txt = path.read_text()
    if "</script>" not in txt:
        return []
    head, body = txt.split("</script>", 1)

    defined = _defined_names(head, body)

    used: set[str] = set()
    used |= set(re.findall(r"<([A-Z]\w+)", body))                    # <Component
    used |= set(re.findall(r"\bicon:\s*([A-Z]\w+)", head + body))    # icon: Component

    return sorted(u for u in used if u not in defined)


def main() -> int:
    bad = {}
    for f in sorted(ROOT.rglob("*.svelte")):
        missing = check_file(f)
        if missing:
            bad[f.relative_to(ROOT)] = missing
    for f, missing in bad.items():
        print(f"{f}: references not in scope -> {missing}")
    if bad:
        print(f"\n{len(bad)} file(s) with unresolved component references.")
        return 1
    print("ok: every component/icon reference is in scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
