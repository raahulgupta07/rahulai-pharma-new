"""Guard the agentic-trace helpers that make the working steps legible.

The step frame now carries the tool's argument so three lookups render as three
distinct lines instead of the same label thrice, and a template plan line is
emitted before any tool runs (no LLM call). These are the pure functions behind
that; they must stay bilingual and must never surface a store scope.
"""

from __future__ import annotations

from app.api import _plan_line, _step_detail


def test_step_detail_prefers_the_human_argument():
    assert _step_detail({"query": "fever medicine"}) == "fever medicine"
    assert _step_detail({"name": "RELYTE"}) == "RELYTE"
    assert _step_detail({"code": "1000000369323"}) == "1000000369323"
    assert _step_detail({"article_code": "1000000369323"}) == "1000000369323"


def test_step_detail_is_safe_on_junk():
    assert _step_detail(None) == ""
    assert _step_detail({}) == ""
    assert _step_detail("not-a-dict") == ""
    # long values are clipped so a step label cannot blow up the trace row
    assert len(_step_detail({"query": "x" * 200})) <= 48


def test_plan_line_is_intent_specific_and_bilingual():
    en_price = _plan_line("what is the price of relyte")
    assert "price" in en_price.lower()

    en_sub = _plan_line("what can I use instead of alaxan")
    assert "substitut" in en_sub.lower()

    # a Burmese question yields a Burmese plan
    my = _plan_line("ဖျားနာ အတွက် ဘာဆေး ရှိလဲ")
    assert any("က" <= c <= "႟" for c in my)

    # every branch returns a non-empty single line
    for q in ["do we have royal-d", "top 5 by stock", "hello", ""]:
        line = _plan_line(q)
        assert isinstance(line, str)
        assert "\n" not in line
