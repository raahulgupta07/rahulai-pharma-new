"""Agent-layer tests (P3).

Structural tests always run (no network). The live bilingual test runs only
when a real OPENROUTER_API_KEY is configured — otherwise it is skipped, so CI
without a key stays green.
"""

import asyncio

import pytest

from app.agent import TOOLS, build_agent
from app.config import get_settings


def test_agent_builds_with_seven_tools():
    agent = build_agent()
    assert type(agent).__name__ == "Agent"
    assert len(TOOLS) == 12
    assert agent.model.id  # model id set


def _has_real_key() -> bool:
    import os

    key = get_settings().openrouter_api_key or ""
    # Require explicit opt-in: live LLM tests are slow + cost money, so they run
    # only with RUN_LIVE=1, never on a plain `pytest`.
    return (
        os.getenv("RUN_LIVE") == "1"
        and key.startswith("sk-or-")
        and "REPLACE" not in key
    )


@pytest.mark.skipif(not _has_real_key(), reason="no real OPENROUTER_API_KEY set")
def test_live_english_stock_query():
    agent = build_agent()
    out = asyncio.run(
        agent.arun("What is the stock of article 1000000015837 at site 20052-CCTLKK?")
    )
    text = getattr(out, "content", str(out))
    assert "4154" in text  # real anchor, sourced from tool


@pytest.mark.skipif(not _has_real_key(), reason="no real OPENROUTER_API_KEY set")
def test_live_burmese_reply_language():
    agent = build_agent()
    out = asyncio.run(
        agent.arun("article 1000000015837 ရဲ့ stock ဘယ်လောက်ရှိလဲ")
    )
    text = getattr(out, "content", str(out))
    # reply should contain Burmese script
    assert any("က" <= ch <= "႟" for ch in text)
