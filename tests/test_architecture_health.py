"""`GET /admin/architecture/health` and the board it draws.

A dependency board fails in a way nothing else does: when it is wrong it looks
exactly like a working system. Three specific ways, all guarded here.

**A green tick for something never checked.** Every part carries `how` —
`probed` (we called it just now and timed the reply), `observed` (we read a
record it left) or `not_checked`. A part that was not established is `unknown`
and is drawn grey; it must never take the colour of `ok`.

**Probing something that costs money.** The model provider is never called. A
health check that spends a fraction of a cent and adds five seconds to every
page view is worse than not knowing, so its row is read from `llm_calls`.

**Claiming to see a process we cannot see.** The ingest worker is a separate
container. Nothing in this process can tell whether it is alive; its row is the
trail it leaves in `ingest_events`, and it says "last seen", never "healthy".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "app" / "admin.py"
PAGE = ROOT / "admin" / "src" / "routes" / "architecture" / "+page.svelte"

STATES = {"ok", "watch", "down", "unknown"}
HOWS = {"probed", "observed", "not_checked"}


@pytest.fixture(scope="module")
def handler() -> str:
    """The health block's helpers and its route, and nothing else.

    The two sibling architecture sections (the question path, the
    observability board) sit between them in the file and emit their own
    `state` vocabularies. Slicing across all three made this file assert that
    the health board draws `in_place` and `none`, which belong to a different
    board."""
    src = ADMIN.read_text()
    helpers = src[
        src.index("# ---- architecture: what this is made of")
        : src.index("# ---- architecture: what we can see")
    ]
    route = src[
        src.index('@router.get("/architecture/health")')
        : src.index('@router.get("/analytics/branch-stock")')
    ]
    return helpers + route


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_the_model_provider_is_never_called(handler):
    """A page view must not cost money or five seconds."""
    m = re.search(r"async def _arch_observe_model\(\).*?\n\n\n", handler, re.S)
    assert m, "the model provider row is gone"
    body = m.group(0)
    assert "llm_calls" in body, "the provider row no longer reads the call log"
    for banned in ("httpx", "aiohttp", "openrouter", "chat/completions", "build_agent", "arun("):
        assert banned not in body.lower(), (
            f"{banned!r} suggests the provider is being probed; a health check "
            f"there spends money and adds a five-second leg to every page view"
        )


def test_the_worker_row_never_claims_to_see_the_process(handler):
    """It is another container. All we have is what it wrote down."""
    m = re.search(r"async def _arch_observe_ingest\(\).*?\n\n\n", handler, re.S)
    assert m, "the ingest row is gone"
    body = m.group(0)
    assert "ingest_events" in body, "the worker row no longer reads the pipeline log"
    assert '"how": "observed"' in body or "'how': 'observed'" in body, (
        "the worker row claims to have probed something; nothing in this "
        "process can reach that container"
    )
    # Silence must not read as death.
    assert "stopped worker both look like" in body, (
        "the no-events case no longer distinguishes idle from stopped, which "
        "are the same thing from here"
    )


def test_an_unreadable_source_is_unknown_and_not_healthy(handler):
    """Every `except` in here must degrade to `unknown`, never to `ok`."""
    # Only an `except` whose very next statement is a return of a state dict.
    # A handler that degrades one FIELD to None (a row count it could not read)
    # is a different thing and is not a verdict about the dependency.
    # No re.S: `except ...:` must be one line, and the return must be the very
    # next statement. With DOTALL this pattern walked from one function's
    # except to another function's success return and failed on it.
    for m in re.finditer(r"except [^\n]*:[^\n]*\n\s+return \{([^}]*)", handler):
        block = m.group(1)
        assert '"state": "unknown"' in block or '"state": "down"' in block, (
            "a failed check is returning something other than unknown/down:\n"
            + block.strip()[:200]
        )


def test_every_state_and_how_the_api_can_emit_is_drawn(handler, page):
    """A state the UI has no entry for falls through to a default, and the
    default is the one that must not be green."""
    emitted_states = set(re.findall(r'"state": "(\w+)"', handler))
    assert emitted_states <= STATES, f"unexpected state(s): {emitted_states - STATES}"
    for st in emitted_states:
        assert re.search(rf"\b{st}: \{{", page), f"the page has no styling for state {st!r}"

    emitted_hows = set(re.findall(r'"how": "(\w+)"', handler))
    assert emitted_hows <= HOWS, f"unexpected how(s): {emitted_hows - HOWS}"
    for h in emitted_hows:
        assert re.search(rf"\b{h}: \{{", page), f"the page has no label for how {h!r}"


def test_unknown_is_never_drawn_as_working(page):
    m = re.search(r"const TONE = \{(.*?)\n  \};", page, re.S)
    assert m, "the tone map is gone"
    tones = m.group(1)
    unknown = re.search(r"unknown: \{([^}]*)\}", tones)
    assert unknown, "there is no `unknown` tone"
    assert "success" not in unknown.group(1), (
        "`unknown` is being painted with the working palette — a part nobody "
        "checked would then be indistinguishable from a part that answered"
    )
    assert "Not known" in unknown.group(1), "`unknown` no longer says so in words"

    fallback = re.search(r"const tone = \(s\) => TONE\[s\] \?\? TONE\.(\w+);", page)
    assert fallback and fallback.group(1) == "unknown", (
        "an unrecognised state falls back to something other than `unknown`"
    )


def test_the_evidence_is_shown_next_to_the_verdict(page):
    """"Working · not checked" must be a contradiction the reader can see."""
    assert "how(p.how).label" in page, (
        "the board no longer says how each row was established, so a probed "
        "row and an unprobed one look identical"
    )


def test_a_failed_board_does_not_report_on_the_parts(page):
    assert "none of them was asked" in page, (
        "when the health endpoint itself fails the page must say the parts "
        "were not checked, rather than implying anything about them"
    )
