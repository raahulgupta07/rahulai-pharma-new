"""Lightweight in-process metrics (no external deps).

Counters + a bounded latency window, exposed at GET /metrics. Enough to watch
the things that matter for this agent: request volume, errors, cache hit rate,
LLM call count, and p50/p95 latency. For multi-instance prod, scrape /metrics
per instance or swap this for prometheus_client later.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Deque, Dict, List

_lock = threading.Lock()
_counters: Dict[str, int] = collections.defaultdict(int)
_latencies: Deque[float] = collections.deque(maxlen=1000)  # recent request ms
_buckets: "collections.OrderedDict[int, Dict[str, int]]" = collections.OrderedDict()  # minute -> counts


def incr(name: str, by: int = 1) -> None:
    with _lock:
        _counters[name] += by


def _minute() -> int:
    return int(time.time() // 60)


def record_request(is_llm: bool = False) -> None:
    """Bucket a request by minute for the live requests sparkline."""

    with _lock:
        m = _minute()
        b = _buckets.setdefault(m, {"requests": 0, "llm": 0})
        b["requests"] += 1
        if is_llm:
            b["llm"] += 1
        while len(_buckets) > 180:
            _buckets.popitem(last=False)


def record_llm() -> None:
    with _lock:
        b = _buckets.setdefault(_minute(), {"requests": 0, "llm": 0})
        b["llm"] += 1


def history(n: int = 12) -> List[Dict]:
    """Last ``n`` minutes of {minute, requests, llm} (oldest first)."""

    with _lock:
        m = _minute()
        return [
            {"minute": m - i, **_buckets.get(m - i, {"requests": 0, "llm": 0})}
            for i in range(n - 1, -1, -1)
        ]


def observe_latency(ms: float) -> None:
    with _lock:
        _latencies.append(ms)


def _pct(sorted_vals, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[k], 1)


def snapshot() -> Dict:
    with _lock:
        counters = dict(_counters)
        lat = sorted(_latencies)
    hits = counters.get("cache_hits", 0)
    misses = counters.get("cache_misses", 0)
    total_cache = hits + misses
    return {
        "requests_total": counters.get("requests_total", 0),
        "errors_total": counters.get("errors_total", 0),
        "llm_calls": counters.get("llm_calls", 0),
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_rate": round(hits / total_cache, 3) if total_cache else None,
        "latency_ms": {
            "count": len(lat),
            "p50": _pct(lat, 50),
            "p95": _pct(lat, 95),
            "p99": _pct(lat, 99),
            "max": round(lat[-1], 1) if lat else 0.0,
        },
    }


__all__ = ["incr", "observe_latency", "snapshot", "record_request", "record_llm", "history"]
