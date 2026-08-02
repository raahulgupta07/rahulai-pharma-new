"""Accuracy eval runner.

Runs every case in eval_set.json through the real agent and scores it:
  * every string in ``expect_contains`` must appear in the answer (no
    hallucinated numbers — the value has to come from a tool result);
  * if ``expect_script == 'my'`` the reply must contain Burmese characters
    (proves bilingual: Burmese question -> Burmese answer).

Needs a real OPENROUTER_API_KEY (it calls the LLM). Exit code is non-zero if
any case fails, so it doubles as a CI gate.

    python -m evals.run_eval
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.agent import build_agent
from app.answer_filter import filter_answer
from app.tools import reset_store_scope, set_store_scope
from app.config import get_settings
from app.db import close_pool

import os

# Override with EVAL_SET=eval_set_v2.json (path relative to this dir, or absolute).
EVAL_FILE = Path(os.getenv("EVAL_SET") or (Path(__file__).parent / "eval_set.json"))
if not EVAL_FILE.is_absolute():
    EVAL_FILE = Path(__file__).parent / EVAL_FILE


def has_burmese(text: str) -> bool:
    return any("က" <= ch <= "႟" for ch in text)


def has_real_key() -> bool:
    key = get_settings().openrouter_api_key or ""
    return key.startswith("sk-or-") and "REPLACE" not in key


async def run() -> int:
    spec = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    cases = spec["cases"]
    leak_markers = spec.get("_leak_markers", [])
    agent = build_agent()

    # Scope the run to one store, exactly as every production request does.
    # Unscoped, the agent answers across all 50+ sites and returns page-long
    # per-branch tables — a configuration no customer ever hits, and one that
    # pushed the model into narrating mid-table. Grading that measured the
    # harness, not the product.
    store = os.getenv("EVAL_STORE", "20015-CCGV")
    scope_token = set_store_scope(store)
    print(f"scoped to store {store}\n")

    passed = 0
    failed = []
    # Cases where the RAW model leaked reasoning and the filter caught it.
    # A high count here means the filter is load-bearing, not decorative.
    leaked_raw: list = []
    for c in cases:
        out = await agent.arun(c["question"])
        raw = getattr(out, "content", str(out))
        # Grade what a customer actually receives. The API applies the same
        # filter to every route (app/api.py), so scoring the unfiltered text
        # would measure a string nobody is ever shown.
        answer = filter_answer(raw)
        if answer != raw.strip():
            leaked_raw.append(c["id"])

        # Normalise: a number is correct whether written 14,963 or 14963;
        # match case-insensitively, ignore commas/spaces, and fold Burmese
        # numerals (၀-၉) to Arabic digits.
        na = answer.translate(str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789"))
        na = na.lower().replace(",", "").replace(" ", "")
        problems = []
        for needle in c.get("expect_contains", []):
            if needle.lower().replace(",", "").replace(" ", "") not in na:
                problems.append(f"missing '{needle}'")
        if c.get("expect_script") == "my" and not has_burmese(answer):
            problems.append("reply not in Burmese")

        # Substrings that must NOT appear. Two kinds: per-case phrases (the
        # refusal or the wrong answer the ticket actually reported), and the
        # shared leak markers. A correct fact wrapped in leaked reasoning is
        # still a failed answer — that is what customers saw, and grading only
        # on expect_contains would score those screenshots as passes.
        for needle in c.get("expect_absent", []) + leak_markers:
            if needle.lower() in answer.lower():
                problems.append(f"must not contain '{needle}'")

        if problems:
            failed.append((c["id"], problems, answer[:120]))
            print(f"FAIL {c['id']}: {'; '.join(problems)}")
        else:
            passed += 1
            print(f"PASS {c['id']}")

    total = len(cases)
    print(f"\nScore: {passed}/{total} ({100*passed//total}%)")
    if leaked_raw:
        print(f"Reasoning leaked from the raw model in {len(leaked_raw)}/{total} "
              f"cases and was filtered before display: {leaked_raw}")
    if failed:
        print("\n--- failures ---")
        for cid, probs, snippet in failed:
            print(f"  {cid}: {probs}\n    answer: {snippet!r}")
    reset_store_scope(scope_token)
    await close_pool()
    return 0 if not failed else 1


def main() -> None:
    if not has_real_key():
        print("SKIP: no real OPENROUTER_API_KEY set — accuracy eval needs the LLM.")
        print("Set OPENROUTER_API_KEY in .env, then: python -m evals.run_eval")
        sys.exit(0)
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
