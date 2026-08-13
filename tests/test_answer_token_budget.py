"""The full agent needs room to finish a long answer.

Same defect as `tests/test_fastpath_phrasing_budget.py`, on the other path.
`max_tokens` caps reasoning AND content together and reasoning cannot be
disabled, so at 2048 the tool loop spent ~1,450 tokens deliberating and cut
real answers mid-table::

    List every substitute for BIOGESIC with prices
    -> "| **BIOGESIC PARA 250MG SUSPENSION 60ML (S`BERRY)** | 100000001000"

    Show me all medicines under 500 MMK
    -> "| **SILOXOGENE 10`S (ALUMINA /MG/SIMETHICONE)** | 10000000087"

Both are the answer shape CMHL's form 10 asked to be shown in full, and both
came back as a Markdown table with the last row severed — which renders as a
broken table, not as a short answer.

`ANSWER_REASONING_EFFORT = "low"` is what frees the budget, and it also halves
latency (46.4s -> 18.6s on the substitute list; 16.7s -> 5.9s on the price
filter). Tool selection was unaffected: the field-feedback eval still scores
22/22 with it applied.

Configuration assertions only — no network — so they run on a plain `pytest`.
"""

from __future__ import annotations

import pytest

from app import agent as agentmod

# The value that shipped broken was 2048; the longest genuine answer measured
# ~1,300 content tokens on top of reasoning. Below this there is no headroom.
MIN_SAFE_BUDGET = 4096


def test_the_answer_budget_is_large_enough_for_a_long_table():
    assert agentmod.ANSWER_MAX_TOKENS >= MIN_SAFE_BUDGET


def test_reasoning_effort_is_held_down():
    """Pinned to the exact value: "none" is rejected by the endpoint with a 400.

    A tightening past "low" would take the whole agent offline rather than
    making it cheaper.
    """

    assert agentmod.ANSWER_REASONING_EFFORT == "low"


@pytest.mark.parametrize(
    "builder", ["build_agent", "build_history_agent"]
)
def test_every_agent_builder_applies_the_budget(builder):
    """The constants must reach each builder, not just the one that was tested.

    `build_agent` and `build_history_agent` construct their models separately,
    so a fix applied to one leaves the other truncating — and which one runs
    depends on whether the caller sent a session_id, i.e. on the client, not on
    anything visible in a test of the other.
    """

    agent = getattr(agentmod, builder)()

    assert agent.model.max_tokens == agentmod.ANSWER_MAX_TOKENS
    assert agent.model.reasoning_effort == agentmod.ANSWER_REASONING_EFFORT


def test_no_answer_model_is_left_at_the_old_budget():
    """Guards the whole module against a re-introduced literal.

    The defect was five separate `max_tokens=2048` literals; a sixth added
    later would regress silently, because nothing about a truncated answer
    raises. The learning extraction model is deliberately excluded — it is a
    cheap non-answer path with its own budget.
    """

    import inspect
    import re

    source = inspect.getsource(agentmod)
    literals = re.findall(r"max_tokens=(\d+)", source)

    offenders = [int(x) for x in literals if int(x) < MIN_SAFE_BUDGET and int(x) != 1500]
    assert not offenders, (
        f"answer-side max_tokens literals below {MIN_SAFE_BUDGET}: {offenders}. "
        f"Use ANSWER_MAX_TOKENS — reasoning shares this budget and will starve "
        f"the answer."
    )
