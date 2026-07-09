"""Verbose eval — run each question through the live agent, print full answer."""

import asyncio
import json
from pathlib import Path

from app.agent import build_agent
from app.db import close_pool

EVAL_FILE = Path(__file__).parent / "eval_set.json"


def has_burmese(t):
    return any("က" <= ch <= "႟" for ch in t)


def norm(s):
    # numbers/codes are correct whether written 14,963 or 14963; case-insensitive
    return s.lower().replace(",", "").replace(" ", "")


async def main():
    cases = json.loads(EVAL_FILE.read_text(encoding="utf-8"))["cases"]
    agent = build_agent()
    passed = 0
    for i, c in enumerate(cases, 1):
        out = await agent.arun(c["question"])
        ans = getattr(out, "content", str(out))
        na = norm(ans)
        probs = [f"missing '{n}'" for n in c.get("expect_contains", []) if norm(n) not in na]
        if c.get("expect_script") == "my" and not has_burmese(ans):
            probs.append("not Burmese")
        ok = not probs
        passed += ok
        print("=" * 70)
        print(f"[{i}/{len(cases)}] {c['id']}   {'✅ PASS' if ok else '❌ FAIL'}")
        print(f"Q: {c['question']}")
        print(f"expect: {c.get('expect_contains', [])}"
              + (f" + Burmese" if c.get('expect_script') == 'my' else ""))
        print(f"A: {ans.strip()}")
        if probs:
            print(f"!! {probs}")
    print("=" * 70)
    print(f"SCORE: {passed}/{len(cases)}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
