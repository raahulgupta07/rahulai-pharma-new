"""Operator-selectable answer length (crisp / standard / detailed).

The style is Redis-backed and re-tunes the agent's system prompt plus the fast
path's phrasing. Pinned here:
  * get/set round-trip + validation + fallback to 'standard';
  * the system prompt actually carries the length override for a style;
  * the fast-path phrasing input carries its directive;
  * an unknown style degrades to standard, never raises to the caller.
"""

import asyncio

import pytest

from app import cache
from app import agent as agentmod
from app import fastpath


def run(coro):
    cache._client = None
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        cache._client = None


# ---- redis round-trip --------------------------------------------------------


def test_style_round_trip_and_default():
    async def go():
        await cache.get_client().delete("pharmacy:answer_style")
        d = await cache.get_answer_style()          # unset -> default
        await cache.set_answer_style("crisp")
        c = await cache.get_answer_style()
        await cache.close_client()
        return d, c

    default, crisp = run(go())
    assert default == "standard"
    assert crisp == "crisp"


def test_bad_style_rejected():
    async def go():
        with pytest.raises(ValueError):
            await cache.set_answer_style("verbose")
        await cache.close_client()

    run(go())


# ---- prompt wiring -----------------------------------------------------------


def test_system_prompt_carries_the_length_override():
    base = agentmod.BILINGUAL_SYSTEM_PROMPT
    assert agentmod.prompt_for_style("standard") == base          # standard adds nothing
    assert "CRISP" in agentmod.prompt_for_style("crisp")
    assert "DETAILED" in agentmod.prompt_for_style("detailed")
    # unknown style never raises and never mutates the base
    assert agentmod.prompt_for_style("nonsense") == base


def test_fastpath_phrasing_carries_directive():
    facts = {"tool": "get_stock", "rows": []}
    crisp = fastpath.build_phrasing_input("[en] q", facts, "crisp")
    detailed = fastpath.build_phrasing_input("[en] q", facts, "detailed")
    standard = fastpath.build_phrasing_input("[en] q", facts, "standard")
    assert "ONE short line" in crisp
    assert "context line" in detailed
    # standard is the historical shape — no injected directive
    assert "ONE short line" not in standard and "context line" not in standard
    # facts still present in every case
    assert "FACTS" in crisp and "FACTS" in standard


def test_get_agent_accepts_style_and_ignores_junk():
    # Should not raise for any of these; junk falls back to standard internally.
    for s in ("crisp", "standard", "detailed", "junk"):
        a = agentmod.get_agent(style=s)
        assert a is not None
