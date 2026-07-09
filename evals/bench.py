"""Latency benchmark harness for the CityCare pharmacy agent.

Companion to :mod:`evals.run_eval` (which scores accuracy). This one measures
SPEED against the running HTTP API, so a before/after change (e.g. a caching or
prompt tweak) can be diffed with a machine-readable JSON blob.

What it does
------------
* A fixed set of ~20 questions covering the two hot intents ("do I have X" /
  "where else has X"), catalog info, substitutes, semantic ("medicine for
  fever"), and site-scoped questions. Half English, half Burmese (မြန်မာ).
  Article codes / product names / site codes are REAL values queried from the
  loaded data (see the module docstring on _QUESTIONS).
* Talks to the running API at ``$BENCH_BASE_URL`` (default
  ``http://localhost:8088``) using the embed contract: ``/api/embed/session/
  create`` then ``/api/embed/chat`` (see ``app/api.py`` / ``INTEGRATION.md``).
* Runs each question TWICE — once COLD, once WARM — and reports both. The warm
  pass exercises the Redis answer cache (``app/cache.py``); the whole warm pass
  should collapse to cache hits.
* Measures per-question wall-clock latency and reports p50 / p95 / mean for each
  pass, plus the cache-hit rate computed from ``/metrics`` deltas (snapshotted
  before and after each pass).
* Prints a compact table plus a machine-readable JSON blob, written to the path
  given as argv[1] (default: the scratchpad dir).

Cost guard — matches ``evals.run_eval``
---------------------------------------
Live chat calls hit the LLM (on a cache miss) and cost money, so the live path
is gated behind ``RUN_LIVE=1`` AND a real ``OPENROUTER_API_KEY`` — the exact
guard ``tests/test_eval.py`` uses. Without that, ``--dry-run`` still works: it
prints the plan and verifies connectivity + session minting (no LLM spend).

    python -m evals.bench --dry-run                 # no cost, prints the plan
    RUN_LIVE=1 python -m evals.bench [out.json]      # live, costs $
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings

BASE_URL = os.getenv("BENCH_BASE_URL", "http://localhost:8088").rstrip("/")

DEFAULT_OUT_DIR = (
    "/private/tmp/claude-501/-Users-rahulgupta/"
    "d43c093a-f5e1-4155-8539-be8654e0e7b0/scratchpad"
)


# ---------------------------------------------------------------------------
# Fixed question set (~20).
#
# Real values pulled from the loaded data (v33, 5292 catalog / 111654 inventory):
#   article 1000000024029 = PARACAP PARACETAMOL 10`S            (Paracetamol)
#   article 1000000008752 = SILOXOGENE 10`S (ALUMINA/MG/...)    (Antacids)
#   article 1000000002229 = FLEMEX SYRUP 30ML                   (Carbocisteine)
#   article 1000000000450 = SIMILAC NEOSURE INFANT MILK 850G    (Baby Milk Powder)
#   sites: 20060-CCBHSC, 20063-CCBRBKMY, 20065-CCSKSC (real site_codes)
# The session is UNSCOPED (public mode), so site-scoped questions name the exact
# site_code in the message; the agent scopes via the tool call.
#
# Tags: hot_have | hot_where | catalog | substitute | semantic | site
# ---------------------------------------------------------------------------
_QUESTIONS: List[Dict[str, str]] = [
    # --- English (10) ---
    {"lang": "EN", "tag": "hot_have", "q": "Do we have article 1000000024029 in stock right now?"},
    {"lang": "EN", "tag": "hot_have", "q": "Is SILOXOGENE (article 1000000008752) available?"},
    {"lang": "EN", "tag": "hot_where", "q": "Which other branches have article 1000000024029?"},
    {"lang": "EN", "tag": "hot_where", "q": "Where else can I find FLEMEX SYRUP, article 1000000002229?"},
    {"lang": "EN", "tag": "catalog", "q": "Show full information for article code 1000000024029."},
    {"lang": "EN", "tag": "catalog", "q": "What is the total stock of article 1000000024029 across all sites?"},
    {"lang": "EN", "tag": "substitute", "q": "What are the substitutes for article 1000000024029?"},
    {"lang": "EN", "tag": "semantic", "q": "I need a medicine for fever."},
    {"lang": "EN", "tag": "semantic", "q": "Do you have something for a cough?"},
    {"lang": "EN", "tag": "site", "q": "Top 5 by stock at site 20060-CCBHSC."},
    # --- Burmese (10) ---
    {"lang": "MY", "tag": "hot_have", "q": "ဆေးကုဒ် 1000000024029 လက်ကျန် ရှိလား။"},
    {"lang": "MY", "tag": "hot_have", "q": "ဆေးကုဒ် 1000000008752 (SILOXOGENE) ရှိသေးလား။"},
    {"lang": "MY", "tag": "hot_where", "q": "ဆေးကုဒ် 1000000024029 ကို တခြားဆိုင်ခွဲတွေမှာ ရှိလား။"},
    {"lang": "MY", "tag": "hot_where", "q": "FLEMEX SYRUP (ဆေးကုဒ် 1000000002229) ကို ဘယ်ဆိုင်ခွဲမှာ ရနိုင်လဲ။"},
    {"lang": "MY", "tag": "catalog", "q": "ဆေးကုဒ် 1000000024029 ရဲ့ အချက်အလက် အပြည့်အစုံ ပြပါ။"},
    {"lang": "MY", "tag": "catalog", "q": "ဆေးကုဒ် 1000000024029 ရဲ့ ဆိုင်အားလုံးက စုစုပေါင်း လက်ကျန် ဘယ်လောက်လဲ။"},
    {"lang": "MY", "tag": "substitute", "q": "ဆေးကုဒ် 1000000024029 အတွက် အစားထိုးဆေးတွေ ဘာတွေ ရှိလဲ။"},
    {"lang": "MY", "tag": "semantic", "q": "ဖျားနာအတွက် ဆေး လိုချင်ပါတယ်။"},
    {"lang": "MY", "tag": "semantic", "q": "ချောင်းဆိုးအတွက် ဆေး ရှိလား။"},
    {"lang": "MY", "tag": "site", "q": "20060-CCBHSC ဆိုင်မှာ လက်ကျန်အများဆုံး ဆေး ၅ မျိုး ပြပါ။"},
]


# ---------------------------------------------------------------------------
# Tiny stdlib HTTP helpers (no third-party dep needed for a benchmark).
# ---------------------------------------------------------------------------


def _post(path: str, payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str, timeout: float = 15.0) -> Dict[str, Any]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _create_session() -> str:
    """Mint a public (unscoped) session token — no LLM, no cost."""

    out = _post("/api/embed/session/create", {"embed_id": "web", "public_key": "web"})
    return out["session_token"]


# ---------------------------------------------------------------------------
# Stats.
# ---------------------------------------------------------------------------


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[k], 1)


def _summ(latencies: List[float]) -> Dict[str, float]:
    s = sorted(latencies)
    n = len(s)
    return {
        "count": n,
        "p50": _pct(s, 50),
        "p95": _pct(s, 95),
        "mean": round(sum(s) / n, 1) if n else 0.0,
        "max": round(s[-1], 1) if s else 0.0,
    }


def _cache_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    dh = after.get("cache_hits", 0) - before.get("cache_hits", 0)
    dm = after.get("cache_misses", 0) - before.get("cache_misses", 0)
    total = dh + dm
    return {
        "hits": dh,
        "misses": dm,
        "hit_rate": round(dh / total, 3) if total else None,
    }


# ---------------------------------------------------------------------------
# Bench pass.
# ---------------------------------------------------------------------------


def _run_pass(token: str, label: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run every question once, timing each. Returns (rows, cache_delta)."""

    before = _get("/metrics")
    rows: List[Dict[str, Any]] = []
    for item in _QUESTIONS:
        t0 = time.perf_counter()
        out = _post("/api/embed/chat", {"session_token": token, "message": item["q"]})
        ms = round((time.perf_counter() - t0) * 1000, 1)
        content = out.get("content", "")
        rows.append({
            "lang": item["lang"], "tag": item["tag"], "q": item["q"],
            "ms": ms, "chars": len(content),
        })
        print(f"  [{label:4}] {item['lang']} {item['tag']:10} {ms:8.1f}ms  {item['q'][:42]}")
    after = _get("/metrics")
    return rows, _cache_delta(before, after)


def _print_table(cold: List[Dict[str, Any]], warm: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 78)
    print(f"{'lang':4} {'tag':10} {'cold ms':>9} {'warm ms':>9}  question")
    print("-" * 78)
    for c, w in zip(cold, warm):
        print(f"{c['lang']:4} {c['tag']:10} {c['ms']:>9.1f} {w['ms']:>9.1f}  {c['q'][:36]}")
    print("=" * 78)


def _resolve_out_path(argv: List[str]) -> Path:
    args = [a for a in argv if not a.startswith("-")]
    if args:
        p = Path(args[0])
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = Path(DEFAULT_OUT_DIR) / f"bench-{ts}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Cost guard (mirrors tests/test_eval.py).
# ---------------------------------------------------------------------------


def _has_real_key() -> bool:
    key = get_settings().openrouter_api_key or ""
    return (
        os.getenv("RUN_LIVE") == "1"
        and key.startswith("sk-or-")
        and "REPLACE" not in key
    )


def _dry_run(out_path: Path) -> int:
    """No LLM spend: print the plan, verify connectivity + session minting."""

    print(f"DRY RUN — no LLM calls. base_url={BASE_URL}")
    print(f"Question set: {len(_QUESTIONS)} questions "
          f"({sum(1 for x in _QUESTIONS if x['lang'] == 'EN')} EN / "
          f"{sum(1 for x in _QUESTIONS if x['lang'] == 'MY')} MY)")
    tags: Dict[str, int] = {}
    for x in _QUESTIONS:
        tags[x["tag"]] = tags.get(x["tag"], 0) + 1
    print(f"Coverage by intent: {tags}")

    reachable, ready, token = False, None, None
    try:
        ready = _get("/ready")
        reachable = True
        token = _create_session()  # session minting is free (no LLM)
        print(f"Connectivity OK — /ready={ready.get('status')} "
              f"catalog={ready.get('catalog_rows')} session_minted={bool(token)}")
    except (urllib.error.URLError, OSError, KeyError) as exc:
        print(f"Connectivity check failed (API not up?): {exc}")

    for x in _QUESTIONS:
        print(f"  {x['lang']} {x['tag']:10} {x['q']}")

    blob = {
        "mode": "dry-run", "base_url": BASE_URL,
        "num_questions": len(_QUESTIONS),
        "lang_split": {"EN": sum(1 for x in _QUESTIONS if x["lang"] == "EN"),
                       "MY": sum(1 for x in _QUESTIONS if x["lang"] == "MY")},
        "coverage": tags,
        "reachable": reachable, "ready": ready, "session_minted": bool(token),
        "questions": _QUESTIONS,
    }
    out_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDry-run plan written to {out_path}")
    print("To run for real (costs $): RUN_LIVE=1 python -m evals.bench")
    return 0


def _live_run(out_path: Path) -> int:
    print(f"LIVE benchmark — base_url={BASE_URL} — {len(_QUESTIONS)} questions x2 passes")
    try:
        ready = _get("/ready")
    except (urllib.error.URLError, OSError) as exc:
        print(f"API not reachable at {BASE_URL}: {exc}")
        return 2

    metrics_before = _get("/metrics")
    token = _create_session()

    print("\nCOLD pass (first time each question is asked):")
    cold_rows, cold_cache = _run_pass(token, "cold")
    print("\nWARM pass (same questions again — should hit the answer cache):")
    warm_rows, warm_cache = _run_pass(token, "warm")

    metrics_after = _get("/metrics")
    _print_table(cold_rows, warm_rows)

    cold_summ = _summ([r["ms"] for r in cold_rows])
    warm_summ = _summ([r["ms"] for r in warm_rows])
    overall_cache = _cache_delta(metrics_before, metrics_after)

    print(f"\nCOLD  p50={cold_summ['p50']}ms  p95={cold_summ['p95']}ms  "
          f"mean={cold_summ['mean']}ms  max={cold_summ['max']}ms")
    print(f"WARM  p50={warm_summ['p50']}ms  p95={warm_summ['p95']}ms  "
          f"mean={warm_summ['mean']}ms  max={warm_summ['max']}ms")
    speedup = round(cold_summ["mean"] / warm_summ["mean"], 1) if warm_summ["mean"] else None
    print(f"warm speedup (mean cold/warm): {speedup}x")
    print(f"cache hit rate — cold pass: {cold_cache['hit_rate']}  "
          f"warm pass: {warm_cache['hit_rate']}  overall: {overall_cache['hit_rate']}")

    blob = {
        "mode": "live", "base_url": BASE_URL,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "data_version": ready.get("data_version"),
        "num_questions": len(_QUESTIONS),
        "model_default": get_settings().openrouter_model,
        "cold": {"summary": cold_summ, "cache": cold_cache, "rows": cold_rows},
        "warm": {"summary": warm_summ, "cache": warm_cache, "rows": warm_rows},
        "warm_speedup_x": speedup,
        "overall_cache": overall_cache,
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
    }
    out_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON results written to {out_path}")
    print("\n--- machine-readable summary ---")
    print(json.dumps({
        "cold": cold_summ, "warm": warm_summ,
        "warm_speedup_x": speedup, "overall_cache": overall_cache,
    }, ensure_ascii=False))
    return 0


def main() -> None:
    out_path = _resolve_out_path(sys.argv[1:])
    dry = "--dry-run" in sys.argv[1:]

    if dry:
        sys.exit(_dry_run(out_path))

    if not _has_real_key():
        print("SKIP: live benchmark needs RUN_LIVE=1 and a real OPENROUTER_API_KEY "
              "(it drives the LLM through the API and costs $).")
        print("Plan-only (no cost): python -m evals.bench --dry-run")
        sys.exit(0)

    sys.exit(_live_run(out_path))


if __name__ == "__main__":
    main()
