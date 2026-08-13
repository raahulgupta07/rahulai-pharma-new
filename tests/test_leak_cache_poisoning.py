"""A leaked answer must never be cached — and this leak shape must be caught.

Measured live on 2026-08-13 through the embed API, store-scoped session, CRISP
answer style. The model answered a stock question by reciting its own prompt::

    And ANSWER LENGTH - CRISP rules:
    "Answer in ONE short line. Give only the product name, article code, and
     the single figure asked for (stock or price). Nothing else."
    Wait, if it's a scope limit:
    "I can't see other branches' stock from this session. At our store, ..."
    Wait, let's keep

Two independent failures, and the second is what turns a glitch into an outage:

1. `LeakFilter` did not recognise it. No tool name, no matching opener — the
   deliberation markers were "Wait, if" and "Wait, let's", and the quoted text
   was a prompt SECTION HEADING rather than one of the tracked sentences.
2. It was then CACHED. Re-asking returned the identical leak in 0.00s, three
   times running, and would have for the whole TTL — so one unlucky sample
   became the permanent answer to that question for every customer.

Fix (1) is more patterns, which is inherently best-effort — the model can
always invent a new shape. Fix (2) is the durable one: an answer that tripped
the filter is disposable. Recomputing costs one LLM call; caching it costs
every future customer the same broken reply.

No LLM and no network here, so these cannot flake.
"""

from __future__ import annotations

import pytest

from app.answer_filter import contains_reasoning, filter_answer, looks_like_reasoning

# The exact text captured from the live API, verbatim.
LIVE_LEAK = (
    "And ANSWER LENGTH - CRISP rules:\n"
    '"Answer in ONE short line. Give only the product name, article code, and '
    'the single figure asked for (stock or price). Nothing else."\n'
    "Wait, if it's a scope limit:\n"
    "\"I can't see other branches' stock from this session. At our store, "
    "BIOGESIC 500MG 10`S 1000000131948 has 149 left.\"\n"
    "Wait, let's keep"
)


def test_the_live_leak_is_recognised():
    assert looks_like_reasoning(LIVE_LEAK)
    assert contains_reasoning(LIVE_LEAK)


def test_the_live_leak_never_reaches_the_customer():
    """Filtered to the honest fallback, not to the recited prompt."""

    out = filter_answer(LIVE_LEAK)

    assert "ANSWER LENGTH" not in out
    assert "Answer in ONE short line" not in out
    assert "Wait," not in out
    assert out.strip()  # an empty bubble reads as a broken widget


@pytest.mark.parametrize(
    "text",
    [
        "Wait, if it's a scope limit: I should check.",
        "Wait, let's keep the branch name out of it.",
        "Wait, but the tool returned nothing.",
        "Wait, maybe I should search again.",
        "Wait, should I use the other tool?",
    ],
)
def test_widened_wait_forms_are_caught(text):
    """The original pattern only matched "Wait, the/this/that/i/we/it"."""

    assert looks_like_reasoning(text)


@pytest.mark.parametrize(
    "heading",
    ["ANSWER LENGTH", "RESPONSE STYLE", "SEARCH STRATEGY", "Answer in ONE short line"],
)
def test_quoted_prompt_headings_are_caught(heading):
    """Reciting a section of the system prompt is never a pharmacy answer."""

    assert looks_like_reasoning(f"Following the {heading} rules: give one line.")


# ---- the answers that must SURVIVE -----------------------------------------
#
# Precision matters more than recall in both directions: suppressing a real
# answer is as bad as leaking one, and several field tickets were complaints
# about answers that had been wrongly withheld.

@pytest.mark.parametrize(
    "answer",
    [
        "**BIOGESIC 500MG 10`S** 1000000131948 — 149 left at 20005-CCYK.",
        # The legitimate scope refusal — nearly the same words as the leak.
        "I can't see other branches' stock from this session, but BIOGESIC "
        "500MG 10`S has 149 left at this store.",
        # A real dosage instruction containing the word "wait".
        "Take 1 tablet 3 times a day. Wait 30 minutes before eating. "
        "Please consult a licensed pharmacist before use.",
        "**PARAGEN PARACETAMOL DROPS 15ML (CHERRY)** 1000000348102 — 215 in stock.",
        "ROYAL-D 25G (ကုဒ်: 1000000015837) - 4154 ခု ရှိပါသည်။",
        "I searched our catalog but could not find that product.",
    ],
)
def test_real_answers_are_not_suppressed(answer):
    assert not contains_reasoning(answer), f"false positive on: {answer[:60]}"
    assert filter_answer(answer) == answer


def test_contains_reasoning_is_false_for_empty_input():
    """Called on every answer; must not throw or flag on nothing."""

    assert contains_reasoning("") is False
    assert contains_reasoning(None) is False


def test_a_clean_answer_is_still_cacheable():
    """The guard must not disable caching wholesale — that would cost every
    repeat question a full LLM call and undo the 0.1s cache hit."""

    assert not contains_reasoning(
        "**RELYTE ORAL REHYDRATION SALTS 20.5G** 1000000369323 — 6533 on the shelf."
    )
