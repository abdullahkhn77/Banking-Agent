"""Unit + plumbing tests for the serving benchmark and the FP16/AWQ statistics. No GPU, no real server."""

import json
from typing import Any

import httpx
import pytest

from serving.benchmark import (
    LevelResult,
    RequestResult,
    order_workload,
    stream_one,
    summarize_level,
    workload_hash,
)


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def _chunk(content: str | None = None, tool: bool = False, usage: dict | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool:
        delta["tool_calls"] = [{"index": 0, "function": {"name": "list_cards", "arguments": ""}}]
    chunk: dict[str, Any] = {"choices": [{"index": 0, "delta": delta}]}
    if usage:
        chunk["usage"] = usage
        chunk["choices"] = []
    return chunk


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


# ---- workload hashing and ordering --------------------------------------------------------------------------------


def test_order_is_deterministic_and_hash_matches() -> None:
    reqs = [{"messages": [{"role": "user", "content": f"q{i}"}], "tools": None} for i in range(20)]
    a, b = order_workload(reqs), order_workload(reqs)
    assert [r["messages"][0]["content"] for r in a] == [r["messages"][0]["content"] for r in b]
    assert workload_hash(a) == workload_hash(b)
    # a different order gives a different hash (mutation-check analogue)
    shuffled = order_workload(reqs, seed=999)
    assert workload_hash(shuffled) != workload_hash(a)


# ---- streaming: TTFT from content OR tool-call --------------------------------------------------------------------


@pytest.mark.anyio
async def test_ttft_from_first_content_delta() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                [
                    _chunk(content="Hello"),
                    _chunk(content=" there"),
                    _chunk(usage={"prompt_tokens": 100, "completion_tokens": 2}),
                ]
            ),
        )

    async with _client(handler) as client:
        r = await stream_one(client, "http://x/v1", "m", {"messages": [], "tools": None}, 256)
    assert r.ok and r.ttft_s is not None and r.output_tokens == 2 and r.input_tokens == 100


@pytest.mark.anyio
async def test_ttft_fires_on_a_tool_call_delta_with_no_content() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse([_chunk(tool=True), _chunk(usage={"prompt_tokens": 50, "completion_tokens": 5})])
        )

    async with _client(handler) as client:
        r = await stream_one(client, "http://x/v1", "m", {"messages": [], "tools": [{"x": 1}]}, 256)
    assert r.ok and r.ttft_s is not None  # TTFT measured even though no content was streamed


@pytest.mark.anyio
async def test_error_path_counts_not_crashes() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="server down")

    async with _client(handler) as client:
        r = await stream_one(client, "http://x/v1", "m", {"messages": [], "tools": None}, 256)
    assert r.ok is False and "503" in r.error


@pytest.mark.anyio
async def test_tools_are_sent_in_the_request() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, content=_sse([_chunk(content="hi"), _chunk(usage={"prompt_tokens": 1, "completion_tokens": 1})])
        )

    async with _client(handler) as client:
        await stream_one(client, "http://x/v1", "m", {"messages": [], "tools": [{"type": "function"}]}, 128)
    assert seen[0]["stream"] is True and seen[0]["stream_options"]["include_usage"] is True
    assert seen[0]["tools"] == [{"type": "function"}] and seen[0]["tool_choice"] == "auto"
    assert seen[0]["max_tokens"] == 128 and seen[0]["temperature"] == 0.0


# ---- metrics and cost math ----------------------------------------------------------------------------------------


def test_summarize_level_metrics_and_cost() -> None:
    results = [
        RequestResult(ok=True, ttft_s=0.1, end_to_end_s=1.1, output_tokens=101, input_tokens=100),
        RequestResult(ok=True, ttft_s=0.2, end_to_end_s=2.1, output_tokens=201, input_tokens=100),
        RequestResult(ok=False, error="boom"),
    ]
    level = LevelResult(concurrency=8, wall_s=2.0, results=results)
    s = summarize_level(level, price_per_hour=3600.0)  # $3600/h => $1 per wall-second
    assert s["ok"] == 2 and s["errors"] == 1 and s["error_rate"] == round(1 / 3, 4)
    assert s["ttft_p50_s"] == pytest.approx(0.15)
    # TPOT: req1 (1.1-0.1)/100=0.01, req2 (2.1-0.2)/200=0.0095 -> p50 ~0.00975
    assert s["tpot_p50_s"] == pytest.approx(0.00975, abs=1e-4)  # stored rounded to 4 dp (0.0098)
    assert s["mean_output_tokens"] == pytest.approx(151.0)
    out_tokens = 302
    assert s["usd_per_1m_output_tokens"] == pytest.approx(1.0 * 2.0 / out_tokens * 1e6, rel=1e-3)
    assert s["usd_per_llm_call"] == pytest.approx(1.0 * 2.0 / 2, rel=1e-3)


def test_cost_metrics_absent_without_price() -> None:
    level = LevelResult(
        concurrency=1, wall_s=1.0, results=[RequestResult(ok=True, ttft_s=0.1, end_to_end_s=0.5, output_tokens=10)]
    )
    s = summarize_level(level, price_per_hour=None)
    assert s["usd_per_1m_output_tokens"] is None and s["usd_per_llm_call"] is None


# ---- FP16/AWQ statistics ------------------------------------------------------------------------------------------


def test_two_proportion_z_known() -> None:
    from evals.metrics import two_proportion_z

    z, p = two_proportion_z(90, 100, 80, 100)
    assert round(z, 2) == 1.98 and 0.04 < p < 0.06
    z0, p0 = two_proportion_z(50, 100, 50, 100)
    assert z0 == 0.0 and p0 == 1.0


def test_paired_cluster_bootstrap_detects_a_clear_difference() -> None:
    from evals.metrics import paired_cluster_bootstrap

    # difference = A - B. A (first) all fail, B (second) all pass -> -1.0 (as compare.py passes AWQ as A).
    clusters = {f"s{i}": ([False, False, False], [True, True, True]) for i in range(20)}
    b = paired_cluster_bootstrap(clusters, resamples=2000)
    assert b["difference"] == -1.0 and b["ci_high"] < 0 and b["contains_zero"] is False


def test_paired_cluster_bootstrap_contains_zero_when_equal() -> None:
    from evals.metrics import paired_cluster_bootstrap

    clusters = {f"s{i}": ([True, False, True], [True, False, True]) for i in range(20)}
    b = paired_cluster_bootstrap(clusters, resamples=2000)
    assert b["difference"] == 0.0 and b["ci_low"] <= 0 <= b["ci_high"] and b["contains_zero"] is True


def test_paired_cluster_bootstrap_is_seed_stable() -> None:
    from evals.metrics import paired_cluster_bootstrap

    clusters = {f"s{i}": ([i % 2 == 0, True], [i % 3 == 0, False]) for i in range(30)}
    assert paired_cluster_bootstrap(clusters, resamples=1000) == paired_cluster_bootstrap(clusters, resamples=1000)


def test_paired_cluster_bootstrap_empty() -> None:
    from evals.metrics import paired_cluster_bootstrap

    b = paired_cluster_bootstrap({}, resamples=100)
    assert b["n_scenarios"] == 0 and b["contains_zero"] is True and b["difference"] is None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_compare_skips_sections_with_missing_inputs(tmp_path: Any) -> None:
    import argparse

    from serving import compare

    args = argparse.Namespace(
        fp16_bench=None,
        awq_bench=None,
        awq_nocache_bench=None,
        fp16_eval=None,
        awq_eval=None,
        fp16_eval_v5=None,
        awq_eval_v5=None,
    )
    md = compare.build(args)
    assert md.count("_Skipped: required input") >= 3  # latency, cost, prefix-cache all skipped
    assert "estimated" not in md.lower()


def test_workload_hash_guard_flags_a_different_order(tmp_path: Any) -> None:
    """Mutation check: if a second variant ran a different request order, the comparison must mark it not comparable."""
    from serving import compare

    def bench(name: str, whash: str) -> dict[str, Any]:
        level = {
            "ttft_p50_s": 0.1,
            "e2e_p50_s": 1.0,
            "requests_per_s": 5.0,
            "output_tokens_per_s": 50.0,
            "mean_output_tokens": 30.0,
        }
        return {
            "name": name,
            "headline_level": 8,
            "by_level": {"8": level, "1": level},
            "meta": {
                "workload_hash": whash,
                "gpu_price_source": "x",
                "gpu_price_date": "d",
                "gpu_price_per_hour": None,
                "llm_calls_per_task": None,
            },
            "cost_per_1k_tasks": None,
        }

    same = compare.latency_section(bench("fp16", "AAAA"), bench("awq", "AAAA"))
    diff = compare.latency_section(bench("fp16", "AAAA"), bench("awq", "BBBB"))
    assert not any("DIFFER: not comparable" in line for line in same)
    assert any("DIFFER: not comparable" in line for line in diff)
