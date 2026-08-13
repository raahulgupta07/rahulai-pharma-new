"""The fast path's phrasing call must have room to finish a sentence.

`max_tokens` on these models caps reasoning AND content together, and reasoning
cannot be switched off — `reasoning.enabled: false` is answered with a 400,
"Reasoning is mandatory for this endpoint and cannot be disabled". At the
original 1024 the model spent 979 tokens thinking and had roughly 41 left to
answer with, so every HOT_HAVE / HOT_WHERE reply was cut mid-sentence::

    Do you have BIOGESIC?
    -> "BIOGESIC 500MG 10`S (article code: 1000000131948) has 395 on the"

    BIOGESIC ဘယ်ဆိုင်မှာရှိလဲ
    -> "BIOGESIC 500MG 10`S (Article Code: 1000000131948) ကို အခြားဆိုင်ခွဲများဖြစ်"

Measured live in both languages and on both hot intents — which between them
are most of real traffic. It presents as a broken widget, and a stock answer
that stops before the number is worse than no answer in a pharmacy.

Two settings fix it and each is load-bearing:

* `reasoning_effort="low"` takes reasoning from 979–1,453 tokens to ~0. It is
  also most of the latency — the same call goes 8.5s to 3.6s.
* a 4096 budget leaves room for the longest genuine reply (a 53-branch stock
  list is ~1,300 output tokens) even if a future model reasons more.

The tests below are configuration assertions and make no network call, so they
run on a plain `pytest`. The live end-to-end check is gated on RUN_LIVE like
the rest of the suite.
"""

from __future__ import annotations

import os

import pytest

from app import fastpath

# Below this, a long branch list plus any reasoning at all cannot fit. Chosen
# from the measured worst case (~1,300 content tokens) with headroom, not
# guessed.
MIN_SAFE_BUDGET = 4096


def _has_real_key() -> bool:
    from app.config import get_settings

    key = get_settings().openrouter_api_key or ""
    return os.getenv("RUN_LIVE") == "1" and key.startswith("sk-")


def test_the_phrasing_budget_is_large_enough_to_finish_an_answer():
    """The constant itself. 1024 is the value that shipped broken."""

    assert fastpath.PHRASING_MAX_TOKENS >= MIN_SAFE_BUDGET


def test_the_phrasing_agent_actually_uses_that_budget():
    """The constant must reach the model, not just exist beside it."""

    agent = fastpath.get_phrasing_agent()

    assert agent.model.max_tokens == fastpath.PHRASING_MAX_TOKENS
    assert agent.model.max_tokens >= MIN_SAFE_BUDGET


def test_reasoning_effort_is_held_down():
    """Unset, reasoning alone consumed more than the whole original budget.

    Pinned as an explicit value rather than "not None": `"none"` is rejected by
    the endpoint with a 400, so a well-meaning tightening would take the fast
    path offline entirely rather than making it cheaper.
    """

    agent = fastpath.get_phrasing_agent()

    assert agent.model.reasoning_effort == "low"


@pytest.mark.parametrize("model_id", ["google/gemini-2.5-flash-lite", "google/gemini-3.5-flash"])
def test_the_budget_holds_for_every_selectable_model(model_id):
    """The chat UI lets a user pick the model per message; each gets the fix.

    `get_phrasing_agent` is lru_cached per model id, so a per-model regression
    would only show on the variant nobody tested by hand.
    """

    from app.agent import ALLOWED_MODEL_IDS

    assert model_id in ALLOWED_MODEL_IDS  # guard against a renamed model

    agent = fastpath.get_phrasing_agent(model_id)

    assert agent.model.id == model_id
    assert agent.model.max_tokens >= MIN_SAFE_BUDGET
    assert agent.model.reasoning_effort == "low"


def test_the_phrasing_agent_still_has_no_tools():
    """The budget change must not have widened what this agent can do.

    Tool-less is the reason the fast path cannot invent a quantity — it can
    only restate the FACTS block it was handed.
    """

    assert fastpath.get_phrasing_agent().tools == []


@pytest.mark.skipif(not _has_real_key(), reason="no real OPENROUTER_API_KEY set")
@pytest.mark.asyncio
async def test_live_a_hot_answer_is_not_truncated():
    """End-to-end: the actual bug, against the real model.

    Truncation is detected by output token count rather than by punctuation —
    a stock answer legitimately ends on a bullet with no full stop, so
    "ends with a period" would report false failures.
    """

    facts = await fastpath.answer("Do you have BIOGESIC?", None)
    assert facts, "BIOGESIC should resolve; check the catalog is loaded"

    agent = fastpath.get_phrasing_agent()
    prompt = fastpath.build_phrasing_input(
        "[Reply in English] Do you have BIOGESIC?", facts, "standard"
    )
    run = await agent.arun(prompt)

    assert run.content
    used = getattr(getattr(run, "metrics", None), "output_tokens", 0) or 0
    assert used < fastpath.PHRASING_MAX_TOKENS, (
        f"the model used its entire {fastpath.PHRASING_MAX_TOKENS}-token budget "
        f"({used}) — the answer is truncated, not finished"
    )
