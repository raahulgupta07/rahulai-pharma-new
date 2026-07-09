"""Eval-harness tests (P7).

Structure checks always run (no LLM). The full accuracy eval runs only with a
real OPENROUTER_API_KEY.
"""

import json
from pathlib import Path

import pytest

from app.config import get_settings

EVAL_FILE = Path(__file__).parent.parent / "evals" / "eval_set.json"


def test_eval_set_well_formed():
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 10
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for c in cases:
        assert c["question"].strip()
        assert isinstance(c.get("expect_contains", []), list)
        if "expect_script" in c:
            assert c["expect_script"] in ("my", "en")
    # must cover both languages
    assert any(c.get("expect_script") == "my" for c in cases)


def _has_real_key() -> bool:
    import os

    key = get_settings().openrouter_api_key or ""
    return (
        os.getenv("RUN_LIVE") == "1"
        and key.startswith("sk-or-")
        and "REPLACE" not in key
    )


@pytest.mark.skipif(not _has_real_key(), reason="no real OPENROUTER_API_KEY set")
def test_accuracy_eval_passes():
    import asyncio

    from evals.run_eval import run

    assert asyncio.run(run()) == 0  # all cases pass
