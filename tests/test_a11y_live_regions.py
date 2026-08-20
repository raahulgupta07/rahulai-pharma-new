"""Toasts: the icon says what happened, and somebody is told it happened.

`toast()` is not decoration. For privileged and destructive actions it is the
ONLY confirmation the console gives — `toast(\`Deleted ${u.email}\`)` on success
and `toast(reason(e, 'delete this user'), 'alert-triangle')` on failure, with no
banner, no navigation and no other trace on screen. Eight files call it about
thirty-four times.

Two defects lived in that one path.

**Failures rendered a green tick.** `ToastHost` looks the icon name up in a map
and falls back to `Check` when it misses. Callers were passing `alert-triangle`
and `shield-alert`; neither was a key. So "could not save branding" and "backend
offline — nothing was saved" appeared with a success glyph. Nothing errored, and
no test could have noticed, because the fallback is a legitimate branch.

**Nobody was told at all.** The container carried no `role` and no `aria-live`,
so a screen-reader user deleted a user and heard nothing — success or failure.

The mounting rule is what makes the fix real rather than nominal: a live region
must be in the accessibility tree BEFORE its content changes. A region created
in the same tick as its text is read as initial content and never announced. So
the `{#each}` has to be INSIDE the region, and the region outside any `{#if}`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "admin" / "src"
HOST = SRC / "lib" / "aurora" / "ToastHost.svelte"
STORE = SRC / "lib" / "aurora" / "toast.js"


@pytest.fixture(scope="module")
def host() -> str:
    return HOST.read_text()


@pytest.fixture(scope="module")
def store() -> str:
    return STORE.read_text()


@pytest.fixture(scope="module")
def icon_map(host: str) -> set[str]:
    m = re.search(r"const icons = \{(.*?)\n  \};", host, re.S)
    assert m, "the icon map is gone"
    return set(re.findall(r"['\"]?([\w-]+)['\"]?\s*:", m.group(1)))


@pytest.fixture(scope="module")
def called_icons() -> dict[str, list[str]]:
    """Every literal icon name passed as toast()'s second argument.

    Only the global `toast()` is in scope. `routes/ftp/+page.svelte` declares
    its own local `toast(message, bad = false)` whose second argument is a
    boolean, so its call sites are excluded rather than mis-read as icon names.
    """
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.svelte")):
        if ".bak" in path.name:
            continue
        text = path.read_text()
        if re.search(r"function toast\(", text):
            continue  # declares its own; not this store's API
        if "aurora/toast" not in text and "aurora/toast.js" not in text:
            continue
        for m in re.finditer(r"\btoast\((?:[^()]|\([^()]*\))*?,\s*'([\w-]+)'\s*\)", text):
            out.setdefault(m.group(1), []).append(
                f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}"
            )
    assert out, "no icon-carrying toast() calls found — the scan is wrong, not the code"
    return out


def test_every_icon_a_caller_passes_is_in_the_map(icon_map, called_icons):
    """The fallback is a tick, so a typo turns a failure into a success."""
    missing = {k: v for k, v in called_icons.items() if k not in icon_map}
    assert not missing, (
        "toast() is called with icon name(s) the host does not know. The host "
        "falls back to a checkmark, so each of these renders a FAILURE with a "
        "SUCCESS glyph:\n"
        + "\n".join(f"  {k!r} at {', '.join(v)}" for k, v in sorted(missing.items()))
    )


def test_every_failure_icon_is_classified_as_bad(store, called_icons):
    """A name that looks like a failure must route to the assertive region."""
    m = re.search(r"BAD_ICONS = new Set\(\[(.*?)\]\)", store, re.S)
    assert m, "BAD_ICONS is gone; nothing distinguishes a failure from a save"
    bad = set(re.findall(r"'([\w-]+)'", m.group(1)))
    looks_bad = {k for k in called_icons if re.search(r"alert|error|danger|fail|shield", k)}
    assert looks_bad <= bad, (
        f"icon(s) {sorted(looks_bad - bad)} are passed on failure paths but are "
        f"not in BAD_ICONS, so those messages are announced politely — they "
        f"queue behind whatever is being read, and arrive after the user has "
        f"moved on"
    )


def test_both_regions_are_mounted_before_their_content(host):
    """The `{#each}` must be INSIDE the region, and the region outside any `{#if}`."""
    for role, attrs in (("status", 'role="status" aria-live="polite"'), ("alert", 'role="alert"')):
        i = host.find(attrs)
        assert i != -1, f"there is no permanently-mounted {role} region"
        after = host[i : i + 400]
        assert "{#each" in after, (
            f"the {role} region does not contain the loop; if the region is "
            f"created alongside its text the insertion counts as initial "
            f"content and is never announced"
        )
    assert "{#if" not in host, (
        "a conditional appeared in the toast host — a live region behind an "
        "{#if} is mounted at the same moment as its first message, which is "
        "exactly the case screen readers do not announce"
    )


def test_failures_are_assertive_and_successes_are_not(host):
    assert re.search(r'role="alert"(?![^>]*aria-live="polite")', host), (
        "the failure region is polite; a failure the user is not told about "
        "until later is one they act on too late"
    )
    assert 'role="status" aria-live="polite"' in host, (
        "the success region is no longer polite — routine confirmations "
        "interrupting speech is its own accessibility problem"
    )


def test_a_failure_does_not_look_like_a_success(host):
    """Sighted users had the same defect: a tick beside 'could not save'."""
    m = re.search(r"\{#each bad as t \(t\.id\)\}(.*?)\{/each\}", host, re.S)
    assert m, "the failure branch is gone"
    body = m.group(1)
    assert "AlertTriangle" in body, "the failure fallback icon is not a warning glyph"
    assert "danger" in body, (
        "nothing visually distinguishes a failed toast from a saved one; they "
        "share a background and sit in the same corner"
    )


def test_the_shared_error_state_still_announces():
    """21 files render load failures through it; it is the other half of this."""
    src = (SRC / "lib" / "ErrorState.svelte").read_text()
    assert 'role="alert"' in src, (
        "ErrorState no longer announces. Every data-load failure in the "
        "console renders through it"
    )
