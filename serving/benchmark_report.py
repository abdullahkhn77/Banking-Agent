"""Render results/bench-<name>/report.md from its summary.json only."""

from __future__ import annotations

from typing import Any


def _n(value: Any) -> str:
    return "n/a" if value is None else (f"{value:,.4g}" if isinstance(value, float) else str(value))


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"] + [
        "| " + " | ".join(r) + " |" for r in rows
    ]


def render_benchmark_report(summary: dict[str, Any]) -> str:
    m = summary["meta"]
    dirty = " (dirty)" if m["git_dirty"] else ""
    out = [f"# Inference benchmark: `{summary['name']}`", ""]
    if m.get("label"):
        out += [f"> **{m['label']}**", ""]
    out += [
        f"served model `{_n(m['served_model'])}` (requested `{m['requested_model']}`) · "
        f"vLLM `{_n(m['vllm_version'])}` · GPU `{_n(m['gpu_name'])}` · host `{m['base_url_host']}` · "
        f"commit `{(m['git_commit'] or '?')[:12]}`{dirty}",
        "",
        f"workload `{m['workload_hash']}` ({m['workload_size']} requests, source `{m['workload_source']}`) · "
        f"prefix caching {'on' if m['prefix_caching'] else 'OFF'} · max_tokens {m['max_tokens']} · "
        f"headline concurrency {summary['headline_level']} · started {m['started_at']}. Plan: `docs/SERVING_PLAN.md`.",
        "",
    ]
    if m.get("gpu_price_per_hour") is not None:
        out += [
            f"GPU price ${m['gpu_price_per_hour']}/h (source: {_n(m['gpu_price_source'])}, {_n(m['gpu_price_date'])}); "
            f"LLM calls/task {_n(m['llm_calls_per_task'])}.",
            "",
        ]
    out += ["## Per concurrency level", ""]
    out += _table(
        [
            "conc.",
            "ok/att",
            "err",
            "TTFT p50 / p95 (s)",
            "TPOT p50",
            "e2e p50 / p95 (s)",
            "req/s",
            "out tok/s",
            "mean out tok",
            "$/1M out",
        ],
        [
            [
                str(v["concurrency"]),
                f"{v['ok']}/{v['attempted']}",
                str(v["errors"]),
                f"{_n(v['ttft_p50_s'])} / {_n(v['ttft_p95_s'])}",
                _n(v["tpot_p50_s"]),
                f"{_n(v['e2e_p50_s'])} / {_n(v['e2e_p95_s'])}",
                _n(v["requests_per_s"]),
                _n(v["output_tokens_per_s"]),
                _n(v["mean_output_tokens"]),
                _n(v["usd_per_1m_output_tokens"]),
            ]
            for v in summary["by_level"].values()
        ],
    )
    if summary.get("cost_per_1k_tasks") is not None:
        out += [
            "",
            f"**Cost per 1K agent tasks (headline level): ${summary['cost_per_1k_tasks']}** "
            f"(= $/LLM-call × {_n(m['llm_calls_per_task'])} calls/task × 1000).",
        ]
    errs = [e for v in summary["by_level"].values() for e in v["error_messages"] if e]
    if errs:
        out += ["", "### First errors", ""] + [f"- {e}" for e in errs[:3]]
    gpu_mem = next((v["gpu_memory_used_mib"] for v in summary["by_level"].values() if v["gpu_memory_used_mib"]), None)
    if gpu_mem:
        out += ["", f"Peak GPU memory used: {gpu_mem} MiB."]
    return "\n".join(out) + "\n"
