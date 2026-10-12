"""Inference load benchmark against an OpenAI-compatible endpoint (vLLM in Phase 9). See docs/SERVING_PLAN.md.

    python -m serving.benchmark --name bench-fp16 --base-url http://localhost:8000/v1 \
        --workload results/test-ollama-qwen7b-v1p/llm_requests.jsonl --gpu-price 1.10 \
        --gpu-price-source "cloud X on-demand L4" --gpu-price-date 2026-10-12 --llm-calls-per-task 2.4

Replays a recorded workload (chat requests with tools) at several concurrency levels, streaming, measuring TTFT,
TPOT, latency, throughput, mean output tokens, errors, GPU memory and cost. The workload order is fixed by a seed
and fingerprinted (workload_hash) so every variant sees byte-identical requests in the same order.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import random
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
LEVELS = (1, 4, 8, 16)
WARMUP_CONCURRENCY = 1
WORKLOAD_SEED = 20261012
HEADLINE_LEVEL = 8
SINGLE_USER_LEVEL = 1


@dataclass
class RequestResult:
    ok: bool
    ttft_s: float | None = None
    end_to_end_s: float | None = None
    output_tokens: int = 0
    input_tokens: int = 0
    error: str | None = None


@dataclass
class LevelResult:
    concurrency: int
    wall_s: float
    results: list[RequestResult] = field(default_factory=list)


# ---- workload -----------------------------------------------------------------------------------------------------


def load_workload(path: Path) -> list[dict[str, Any]]:
    """Recorded requests: each line is {"messages": [...], "tools": [...]}. Returns chat-completion bodies."""
    requests = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        requests.append({"messages": record["messages"], "tools": record.get("tools") or None})
    return requests


def synthetic_workload() -> list[dict[str, Any]]:
    """First-turn requests built from the scenarios, for smoke tests only (no recorded workload needed)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.llm import to_openai_messages
    from agent.prompts import system_prompt
    from agent.tools import openai_tools
    from evals.runner import load_scenarios

    tools = openai_tools()
    out = []
    for scenario in load_scenarios():
        messages = [SystemMessage(content=system_prompt("the customer")), HumanMessage(content=scenario["turns"][0])]
        out.append({"messages": to_openai_messages(messages), "tools": tools})
    return out


def order_workload(requests: list[dict[str, Any]], seed: int = WORKLOAD_SEED) -> list[dict[str, Any]]:
    ordered = list(requests)
    random.Random(seed).shuffle(ordered)
    return ordered


def workload_hash(ordered: list[dict[str, Any]]) -> str:
    payload = json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# ---- one streamed request -----------------------------------------------------------------------------------------


async def stream_one(
    client: httpx.AsyncClient, base_url: str, model: str, body: dict[str, Any], max_tokens: int
) -> RequestResult:
    payload = {
        "model": model,
        "messages": body["messages"],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if body.get("tools"):
        payload["tools"] = body["tools"]
        payload["tool_choice"] = "auto"
    started = time.perf_counter()
    ttft: float | None = None
    input_tokens = output_tokens = 0
    try:
        async with client.stream("POST", f"{base_url}/chat/completions", json=payload) as response:
            if response.status_code != 200:
                text = (await response.aread()).decode()[:200]
                return RequestResult(ok=False, error=f"HTTP {response.status_code}: {text}")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                choices = chunk.get("choices") or []
                if choices and ttft is None:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content") or delta.get("tool_calls"):  # first content OR tool-call delta
                        ttft = time.perf_counter() - started
                usage = chunk.get("usage")
                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return RequestResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return RequestResult(
        ok=True,
        ttft_s=ttft,
        end_to_end_s=time.perf_counter() - started,
        output_tokens=output_tokens,
        input_tokens=input_tokens,
    )


async def run_level(
    base_url: str, model: str, requests: list[dict[str, Any]], concurrency: int, max_tokens: int, timeout: float
) -> LevelResult:
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 2, max_keepalive_connections=concurrency + 2)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:

        async def worker(body: dict[str, Any]) -> RequestResult:
            async with semaphore:
                return await stream_one(client, base_url, model, body, max_tokens)

        started = time.perf_counter()
        results = await asyncio.gather(*(worker(b) for b in requests))
        wall = time.perf_counter() - started
    return LevelResult(concurrency=concurrency, wall_s=wall, results=list(results))


# ---- metrics ------------------------------------------------------------------------------------------------------


def _pctile(values: list[float], q: float) -> float | None:
    from evals.metrics import percentile

    return percentile(values, q)


def summarize_level(level: LevelResult, price_per_hour: float | None) -> dict[str, Any]:
    ok = [r for r in level.results if r.ok]
    failed = [r for r in level.results if not r.ok]
    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    e2es = [r.end_to_end_s for r in ok if r.end_to_end_s is not None]
    tpots = [
        (r.end_to_end_s - (r.ttft_s or 0)) / max(1, r.output_tokens - 1)
        for r in ok
        if r.end_to_end_s is not None and r.output_tokens > 1
    ]
    out_tokens = sum(r.output_tokens for r in ok)
    in_tokens = sum(r.input_tokens for r in ok)
    wall = level.wall_s or 1e-9
    per_hour = price_per_hour or 0.0
    usd_per_1m_out = (per_hour / 3600 * wall / out_tokens * 1e6) if out_tokens and price_per_hour else None
    usd_per_call = (per_hour / 3600 * wall / len(ok)) if ok and price_per_hour else None
    return {
        "concurrency": level.concurrency,
        "attempted": len(level.results),
        "ok": len(ok),
        "errors": len(failed),
        "error_rate": round(len(failed) / len(level.results), 4) if level.results else None,
        "error_messages": [r.error for r in failed[:3]],
        "wall_s": round(wall, 3),
        "ttft_p50_s": _round(_pctile(ttfts, 50)),
        "ttft_p95_s": _round(_pctile(ttfts, 95)),
        "tpot_p50_s": _round(_pctile(tpots, 50)),
        "e2e_p50_s": _round(_pctile(e2es, 50)),
        "e2e_p95_s": _round(_pctile(e2es, 95)),
        "requests_per_s": round(len(ok) / wall, 3),
        "output_tokens_per_s": round(out_tokens / wall, 2),
        "total_tokens_per_s": round((in_tokens + out_tokens) / wall, 2),
        "mean_output_tokens": round(out_tokens / len(ok), 2) if ok else None,
        "usd_per_1m_output_tokens": _round(usd_per_1m_out),
        "usd_per_llm_call": _round(usd_per_call, 6),
        "gpu_memory_used_mib": gpu_memory_mib(),
    }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def gpu_memory_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return max(int(x) for x in out.stdout.split())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def gpu_name() -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def served_model(base_url: str) -> str | None:
    try:
        data = httpx.get(f"{base_url}/models", timeout=5).json()
        return data["data"][0]["id"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None


def vllm_version(base_url: str) -> str | None:
    host = base_url.rsplit("/v1", 1)[0]
    try:
        return httpx.get(f"{host}/version", timeout=5).json().get("version")
    except (httpx.HTTPError, ValueError):
        return None


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


# ---- driver -------------------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True)
    p.add_argument("--base-url", default="http://localhost:8000/v1")
    p.add_argument("--model", default="bank-agent-llm")
    p.add_argument("--workload", type=Path, help="recorded llm_requests.jsonl; omit for the synthetic smoke workload")
    p.add_argument("--levels", default=",".join(map(str, LEVELS)), help="comma-separated concurrency levels")
    p.add_argument("--requests", type=int, default=0, help="override requests/level (default max(64, 4×concurrency))")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--gpu-price", type=float, default=None, help="GPU price USD/hour (cost metrics need it)")
    p.add_argument("--gpu-price-source", default=None)
    p.add_argument("--gpu-price-date", default=None)
    p.add_argument("--llm-calls-per-task", type=float, default=None, help="from the same variant's eval summary")
    p.add_argument("--label", default="", help="free-text label, e.g. 'smoke test, not a result'")
    p.add_argument("--no-prefix-caching", action="store_true", help="record that the server ran without prefix caching")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return p.parse_args(argv)


def level_requests(ordered: list[dict[str, Any]], concurrency: int, override: int) -> list[dict[str, Any]]:
    count = override if override else max(64, 4 * concurrency)
    return [ordered[i % len(ordered)] for i in range(count)]


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    raw = load_workload(args.workload) if args.workload else synthetic_workload()
    ordered = order_workload(raw)
    whash = workload_hash(ordered)
    levels = [int(x) for x in args.levels.split(",")]

    # Warm-up (discarded).
    await run_level(
        args.base_url,
        args.model,
        level_requests(ordered, WARMUP_CONCURRENCY, args.requests),
        WARMUP_CONCURRENCY,
        args.max_tokens,
        args.timeout,
    )

    level_summaries = []
    for concurrency in levels:
        reqs = level_requests(ordered, concurrency, args.requests)
        result = await run_level(args.base_url, args.model, reqs, concurrency, args.max_tokens, args.timeout)
        level_summaries.append(summarize_level(result, args.gpu_price))

    by_level = {s["concurrency"]: s for s in level_summaries}
    headline = by_level.get(HEADLINE_LEVEL)
    cost_per_1k_tasks = None
    if headline and headline.get("usd_per_llm_call") is not None and args.llm_calls_per_task:
        cost_per_1k_tasks = round(headline["usd_per_llm_call"] * args.llm_calls_per_task * 1000, 4)

    meta = {
        "name": args.name,
        "label": args.label or None,
        "base_url_host": urlparse(args.base_url).netloc,
        "served_model": served_model(args.base_url),
        "requested_model": args.model,
        "vllm_version": vllm_version(args.base_url),
        "gpu_name": gpu_name(),
        "workload_hash": whash,
        "workload_size": len(ordered),
        "workload_source": str(args.workload) if args.workload else "synthetic (smoke)",
        "levels": levels,
        "requests_override": args.requests or None,
        "max_tokens": args.max_tokens,
        "warmup_concurrency": WARMUP_CONCURRENCY,
        "prefix_caching": not args.no_prefix_caching,
        "gpu_price_per_hour": args.gpu_price,
        "gpu_price_source": args.gpu_price_source,
        "gpu_price_date": args.gpu_price_date,
        "llm_calls_per_task": args.llm_calls_per_task,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return {
        "name": args.name,
        "meta": meta,
        "headline_level": HEADLINE_LEVEL,
        "single_user_level": SINGLE_USER_LEVEL,
        "by_level": by_level,
        "cost_per_1k_tasks": cost_per_1k_tasks,
    }


def main(argv: list[str] | None = None) -> int:
    from serving.benchmark_report import render_benchmark_report

    args = parse_args(argv)
    summary = asyncio.run(run_benchmark(args))
    out_dir = args.results_dir / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_benchmark_report(summary), encoding="utf-8")
    h = summary["by_level"].get(HEADLINE_LEVEL) or next(iter(summary["by_level"].values()), {})
    print(
        f"{args.name}: workload {summary['meta']['workload_hash']} ({summary['meta']['workload_size']} reqs) | "
        f"headline c{HEADLINE_LEVEL} req/s {h.get('requests_per_s')} ttft_p50 {h.get('ttft_p50_s')} "
        f"errors {sum(v['errors'] for v in summary['by_level'].values())} -> {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
