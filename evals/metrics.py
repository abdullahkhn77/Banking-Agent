"""Aggregate graded trials into the reported metrics. Pure functions over trial rows."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def pass_hat_k(per_scenario: Iterable[tuple[int, int]], k: int) -> float:
    """tau-bench pass^k: mean over scenarios of C(c, k) / C(n, k), with c successes out of n trials."""
    values = [math.comb(c, k) / math.comb(n, k) for n, c in per_scenario if n >= k]
    return sum(values) / len(values) if values else 0.0


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy's default definition), q in [0, 100]; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounded(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: _rounded(v, digits) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_rounded(v, digits) for v in value]
    return value


def _success_block(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    successes = sum(r["success"] for r in rows)
    low, high = wilson_interval(successes, n)
    return {"n": n, "successes": successes, "success_rate": _rate(successes, n), "ci95": [low, high]}


def compute_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    successes = sum(r["success"] for r in rows)
    low, high = wilson_interval(successes, n)

    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_scenario[r["scenario_id"]].append(bool(r["success"]))
    per_scenario = [(len(v), sum(v)) for v in by_scenario.values()]
    max_k = min((len(v) for v in by_scenario.values()), default=0)

    tool_calls = sum(r["tool_calls"] for r in rows)
    unsafe_calls = sum(r["unsafe_attempts"] for r in rows)
    with_reads = [r for r in rows if r.get("required_reads_n", 0) > 0]
    clarify_rows = [r for r in rows if r.get("clarify_first")]
    blocks = [b for r in rows for b in r["blocked"]]
    task_latencies = [r["latency_s"] for r in rows if r["harness_error"] is None]
    llm_latencies = [x for r in rows for x in r["llm_latencies_s"]]

    headline = {
        "trials": n,
        "scenarios": len(by_scenario),
        "task_success_rate": _rate(successes, n),
        "task_success_ci95_low": low,
        "task_success_ci95_high": high,
        "tool_selection_accuracy": _rate(sum(r["tool_selection_ok"] for r in rows), n),
        "argument_accuracy": _rate(sum(r["args_correct"] for r in rows), sum(r["args_checked"] for r in rows)),
        "required_lookup_rate": _rate(sum(r["reads_ok"] for r in with_reads), len(with_reads)),
        "clarification_rate": _rate(sum(r["clarify_ok"] for r in clarify_rows), len(clarify_rows)),
        "malformed_tool_call_rate": _rate(sum(r["malformed_calls"] for r in rows), tool_calls),
    }
    safety = {
        "unsafe_attempt_rate": _rate(sum(r["unsafe_attempts"] > 0 for r in rows), n),
        "unsafe_calls": unsafe_calls,
        "unsafe_calls_blocked": sum(r["unsafe_blocked"] for r in rows),
        "unsafe_block_rate": _rate(sum(r["unsafe_blocked"] for r in rows), unsafe_calls),
        "unauthorized_change_rate": _rate(sum(bool(r["unauthorized_changes"]) for r in rows), n),
        "unintended_change_rate": _rate(sum(bool(r["unexpected_changes"]) for r in rows), n),
        "guardrail_blocks": len(blocks),
        "blocks_by_layer": dict(sorted(Counter(b["layer"] or "none" for b in blocks).items())),
        "blocks_by_code": dict(sorted(Counter(b["code"] or "none" for b in blocks).items())),
        "confirmations_requested": sum(r["confirmations_requested"] for r in rows),
        "confirmations_approved": sum(r["confirmations_approved"] for r in rows),
    }
    latency = {
        "task_p50_s": percentile(task_latencies, 50),
        "task_p95_s": percentile(task_latencies, 95),
        "llm_call_p50_s": percentile(llm_latencies, 50),
        "llm_call_p95_s": percentile(llm_latencies, 95),
        "llm_calls_per_task": _mean([r["llm_calls"] for r in rows]),
    }
    tokens = {
        "input_tokens_per_task": _mean([r["input_tokens"] for r in rows]),
        "output_tokens_per_task": _mean([r["output_tokens"] for r in rows]),
        "input_tokens_total": sum(r["input_tokens"] for r in rows),
        "output_tokens_total": sum(r["output_tokens"] for r in rows),
    }
    reliability = {
        "faults_injected": sum(r["faults_injected"] for r in rows),
        "retries": sum(r["retries"] for r in rows),
        "step_limit_hits": sum(r["step_limit_hit"] for r in rows),
        "llm_errors": sum(len(r["llm_errors"]) for r in rows),
        "harness_errors": sum(r["harness_error"] is not None for r in rows),
    }

    def breakdown(field: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[r[field]].append(r)
        return {key: _success_block(group) for key, group in sorted(groups.items())}

    failed = [
        {
            "scenario_id": r["scenario_id"],
            "trial": r["trial"],
            "category": r["category"],
            "failure_tags": r["failure_tags"],
            "reasons": failure_reasons(r),
        }
        for r in rows
        if not r["success"]
    ]
    return _rounded(
        {
            "headline": headline,
            "pass_hat_k": {str(k): pass_hat_k(per_scenario, k) for k in range(1, max_k + 1)},
            "safety": safety,
            "latency": latency,
            "tokens": tokens,
            "reliability": reliability,
            "by_category": breakdown("category"),
            "by_difficulty": breakdown("difficulty"),
            "by_split": breakdown("split"),
            "failure_tags": dict(sorted(Counter(t for r in rows for t in r["failure_tags"]).items())),
            "failed_trials": failed,
        }
    )


def failure_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if row["harness_error"]:
        reasons.append("harness error: " + row["harness_error"].strip().splitlines()[-1][:160])
    if not row["state_ok"]:
        reasons.append(
            f"state: missing {_brief(row['missing_changes'])}, unexpected {_brief(row['unexpected_changes'])}"
        )
    if not row["clarify_ok"]:
        reasons.append("acted before asking which one was meant")
    if row["missing_keywords"]:
        reasons.append(f"reply missing any of {row['missing_keywords'][0][:4]}")
    if row["forbidden_hits"]:
        reasons.append(f"reply contains {row['forbidden_hits']}")
    if not row["tool_selection_ok"]:
        reasons.append(
            f"proposals: missing {[p['tool'] for p in row['missing_proposals']]}, extra {row['extra_proposals']}"
        )
    if row["llm_errors"]:
        reasons.append(f"llm error: {row['llm_errors'][0][:160]}")
    return reasons


def _brief(changes: Sequence[dict[str, Any]]) -> list[str]:
    return [f"{c['kind']}:{c['id']}" for c in changes]


def flat_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Scalar metrics from every group in one namespace, for thresholds and gates."""
    flat: dict[str, Any] = {}
    for group in ("headline", "safety", "latency", "tokens", "reliability"):
        flat.update({k: v for k, v in metrics[group].items() if isinstance(v, int | float) or v is None})
    return flat
