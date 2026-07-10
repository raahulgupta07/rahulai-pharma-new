"""Catch the blank-page class of bug before it ships.

An icon referenced in a .svelte file but never brought into scope (e.g.
`icon: KeyRound` in a nav entry with no matching import) is NOT a build error:
Vite treats it as an undefined global, the bundle builds clean, and then the SPA
throws `ReferenceError` at startup and every page renders BLANK. `svelte-check`
does not flag it either — a plain-JS `<script>` is not checked for undefined
identifiers (verified: it reported 0 errors with the bug present).

`scripts/check_svelte_icons.py` does catch it. This test runs it. It needs no
node/JS runtime — it is pure Python static analysis over the .svelte sources.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_svelte_icons.py"


def test_every_svelte_component_reference_is_in_scope():
    """Fails (exit 1) if any <Component>/icon: reference is never imported/defined."""

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
