"""`GET /admin/architecture/question-path` — the three routes a question takes.

One question does not have one path, and which of the three it takes is the
single biggest thing about how the product feels. Two ways of reporting that go
wrong, both guarded here.

**One median over all of them.** A 5 s median across 1 ms cache hits and 15 s
agent runs describes no question anybody asked, and it improves when the cache
warms rather than when anything gets faster. Each route carries its own.

**A route the console has no description for, folded into one it does.** A new
branch in `_answer` would then silently inflate an existing row instead of
showing up as something nobody has described yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "app" / "admin.py"
API = ROOT / "app" / "api.py"
AGENT = ROOT / "app" / "agent.py"
PAGE = ROOT / "admin" / "src" / "routes" / "architecture" / "+page.svelte"


@pytest.fixture(scope="module")
def handler() -> str:
    src = ADMIN.read_text()
    i = src.index("# ---- architecture: the three routes")
    j = src.index('@router.get("/architecture/health")')
    return src[i:j]


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_each_route_carries_its_own_median(handler):
    assert "GROUP BY path" in handler, (
        "the latency is no longer grouped by route, so one number now stands "
        "for a 1 ms cache hit and a 15 s agent run alike"
    )


def test_a_route_nobody_took_is_null_and_not_zero(handler, page):
    assert "turns=None" in handler, (
        "a route no question took is being reported as a count rather than as "
        "nothing — 0 questions and no measurement are different claims"
    )
    assert "no question took this route" in page, (
        "the page no longer distinguishes an unused route from a measured zero"
    )


def test_an_undescribed_route_is_named_not_absorbed(handler, page):
    assert "unknown_paths" in handler, "the unknown-route list is gone"
    m = re.search(r"if key not in routes:(.*?)continue", handler, re.S)
    assert m, "an unrecognised path is no longer handled separately"
    assert "seen_unknown.append" in m.group(1), (
        "an unrecognised path is being folded into a described route, so a new "
        "branch in `_answer` would silently inflate an existing row"
    )
    assert "no description for" in page, "the page never surfaces an unknown route"


def test_the_route_ids_are_the_ones_the_code_actually_records(handler):
    """`chat_logs.path` is written by `_answer`. A rename there without one here
    turns a described route into an unknown one — which is at least visible, but
    the point is that these two lists are the same list."""
    described = set(re.findall(r'"id": "(\w+)"', handler))
    api = API.read_text()
    for route in described:
        assert re.search(rf'''path=["']{route}["']''', api) or f"'{route}'" in api, (
            f"route {route!r} is described here but `app/api.py` never records "
            f"it, so this row can only ever read 'no question took this route'"
        )


def test_the_tool_count_in_the_copy_matches_the_agent(handler):
    """"twelve read-only tools" is a claim about `TOOLS`, not a round number."""
    words = {12: "twelve", 11: "eleven", 13: "thirteen", 10: "ten", 14: "fourteen"}
    m = re.search(r"TOOLS = \[(.*?)\n\]", AGENT.read_text(), re.S)
    assert m, "TOOLS is gone from app/agent.py"
    n = len({t for t in re.findall(r"\b(\w+)\b", m.group(1))})
    assert words.get(n, str(n)) in handler, (
        f"the agent now has {n} tools and the route description still says "
        f"otherwise"
    )


def test_the_model_call_floor_is_measured_not_asserted(handler, page):
    """"about five seconds" was the design's number. This stack's own median is
    the only one worth printing, and it is read from `llm_calls`."""
    assert "llm_calls" in handler and "duration_ms" in handler, (
        "the per-call floor is no longer measured from the call log"
    )
    assert "model_call_p50_ms" in page, "the page no longer states the floor"
    assert "has to delete a call" in page, (
        "the conclusion the floor exists to support is gone: on this stack a "
        "round trip cannot be made meaningfully quicker, only removed"
    )


def test_a_failed_read_does_not_imply_the_slow_route(page):
    assert "not that they all took the slow one" in page, (
        "when the turn log cannot be read the page must say the routes are "
        "unknown rather than implying anything about them"
    )
