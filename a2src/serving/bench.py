"""Q4: serving/scale measurements -- real, not estimated.

Reuses the teammate's Kaggle-notebook design for the measurement primitives
(``measure_build_memory``, ``measure_latency``) almost verbatim -- that part
of their notebook was well-designed and its review found nothing wrong with
it: RSS memory delta via psutil, and a genuine p50/p90/p99 over repeated
end-to-end requests with an explicit warmup phase. What changes here is what
gets measured: the full two-stage pipeline (BM25 candidate generation + Q1
feature building + Q2 re-ranker scoring) for ONE dataset's ONE impression,
callable for both MIND and EB-NeRD, instead of the notebook's EB-NeRD-only
wiring.
"""

from __future__ import annotations

import time
from typing import Callable

import psutil
import os


def measure_build_memory(build_fn: Callable[[], object]) -> tuple[object, float]:
    """RSS memory delta (MB) caused by building one component (an index, a
    feature store load, a trained model)."""
    proc = psutil.Process(os.getpid())
    before = proc.memory_info().rss
    result = build_fn()
    after = proc.memory_info().rss
    return result, (after - before) / (1024**2)


def measure_latency(
    score_one_fn: Callable[[object], object], sample_requests: list, warmup: int = 10
) -> dict:
    """Wall-clock latency (ms) of ONE full end-to-end request, over many
    sample requests for a real p50/p90/p99 -- not a single timed call."""
    for req in sample_requests[:warmup]:
        score_one_fn(req)  # warm up caches / lazy loads, don't time this

    latencies = []
    for req in sample_requests:
        t0 = time.perf_counter()
        score_one_fn(req)
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    n = len(latencies)
    return {
        "n": n,
        "p50_ms": latencies[n // 2],
        "p90_ms": latencies[int(n * 0.90)],
        "p99_ms": latencies[min(int(n * 0.99), n - 1)],
        "max_ms": latencies[-1],
        "mean_ms": sum(latencies) / n,
    }


def cost_per_1000_queries(
    p99_ms: float, target_qps: float = 50.0, hourly_cost_per_core: float = 0.05
) -> dict:
    """Back-of-envelope Q4.3: cost/QPS at a target SLA.

    Single stated assumption, flagged as such per the brief's own allowance
    ("A measured local benchmark plus a scaling argument suffices") -- the
    per-core cloud price. Everything else derives from the MEASURED p99.
    """
    max_qps_per_core = 1000.0 / p99_ms if p99_ms > 0 else float("inf")
    cores_needed = target_qps / max_qps_per_core if max_qps_per_core else float("inf")
    cost = (1000.0 / target_qps) / 3600.0 * cores_needed * hourly_cost_per_core
    return {
        "p99_ms": p99_ms,
        "max_qps_per_core": max_qps_per_core,
        "target_qps": target_qps,
        "cores_needed": cores_needed,
        "hourly_cost_per_core_usd": hourly_cost_per_core,
        "cost_per_1000_queries_usd": cost,
    }
