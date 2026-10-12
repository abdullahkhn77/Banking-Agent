"""Build results/SERVING_COMPARISON.md from the benchmark and Phase 9 eval summaries. See docs/SERVING_PLAN.md.

    python -m serving.compare --fp16-bench results/bench-fp16 --awq-bench results/bench-awq \
        --fp16-eval results/eval-fp16-v1p --awq-eval results/eval-awq-v1p \
        --awq-nocache-bench results/bench-awq-nocache

Every section whose inputs are missing is skipped (never estimated). Every number cites a summary.json path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "results" / "SERVING_COMPARISON.md"


def _load(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    f = path / "summary.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _pct_change(new: float | None, base: float | None) -> str:
    if new is None or base in (None, 0):
        return "n/a"
    return f"{(new - base) / base * 100:+.1f}%"


def _hdr(bench: dict[str, Any], level: int) -> dict[str, Any]:
    return bench["by_level"].get(str(level)) or bench["by_level"].get(level) or {}


def _skip(name: str) -> list[str]:
    return [f"## {name}", "", "_Skipped: required input summary not present._", ""]


def latency_section(fp16: dict | None, awq: dict | None) -> list[str]:
    if not (fp16 and awq):
        return _skip("Latency & throughput (FP16 vs AWQ)")
    out = [
        "## Latency & throughput (FP16 vs AWQ)",
        "",
        f"Sources: `results/{fp16['name']}/summary.json`, `results/{awq['name']}/summary.json`. "
        f"Workload hashes: FP16 `{fp16['meta']['workload_hash']}`, AWQ `{awq['meta']['workload_hash']}`"
        + ("" if fp16["meta"]["workload_hash"] == awq["meta"]["workload_hash"] else " — **DIFFER: not comparable**"),
        "",
    ]
    header = ["Metric (conc. 8 headline; 1 single-user)", "FP16", "AWQ", "Δ"]
    rows = []
    for level, tag in ((8, "c8"), (1, "c1")):
        f, a = _hdr(fp16, level), _hdr(awq, level)
        for key, lbl in (
            ("ttft_p50_s", "TTFT p50 (s)"),
            ("e2e_p50_s", "e2e p50 (s)"),
            ("requests_per_s", "req/s"),
            ("output_tokens_per_s", "out tok/s"),
            ("mean_output_tokens", "mean out tok"),
        ):
            rows.append([f"{lbl} [{tag}]", str(f.get(key)), str(a.get(key)), _pct_change(a.get(key), f.get(key))])
    out += _table(header, rows)
    out += [
        "",
        "Mean output tokens is shown because identical requests can yield different output lengths, which "
        "moves tok/s and cost.",
        "",
    ]
    return out


def cost_section(fp16: dict | None, awq: dict | None) -> list[str]:
    if not (fp16 and awq):
        return _skip("Cost")
    rows = []
    for b in (fp16, awq):
        h = _hdr(b, b["headline_level"])
        rows.append(
            [
                b["name"],
                str(h.get("usd_per_1m_output_tokens")),
                str(b.get("cost_per_1k_tasks")),
                str(b["meta"].get("gpu_price_per_hour")),
                str(b["meta"].get("llm_calls_per_task")),
            ]
        )
    return (
        [
            "## Cost",
            "",
            f"GPU price source: FP16 {fp16['meta'].get('gpu_price_source')} ({fp16['meta'].get('gpu_price_date')}).",
            "",
        ]
        + _table(["Run", "$/1M output tok", "$/1K tasks", "GPU $/h", "LLM calls/task"], rows)
        + [""]
    )


def quality_section(runs: dict[str, dict | None]) -> list[str]:
    from evals.metrics import paired_cluster_bootstrap, two_proportion_z

    out = ["## Quality retention (FP16 vs AWQ)", ""]
    any_done = False
    for prompt in ("v1p", "v5"):
        fp16, awq = runs.get(f"fp16-{prompt}"), runs.get(f"awq-{prompt}")
        if not (fp16 and awq):
            out += [f"### Prompt {prompt}", "", "_Skipped: eval summary missing._", ""]
            continue
        any_done = True
        fh, ah = fp16["metrics"]["headline"], awq["metrics"]["headline"]
        clusters = _clusters(fp16, awq)
        boot = paired_cluster_bootstrap(clusters)
        z, p = two_proportion_z(ah["successes"], ah["trials"], fh["successes"], fh["trials"])
        retention = ah["task_success_rate"] / fh["task_success_rate"] if fh["task_success_rate"] else None
        verdict = (
            "not significantly different (bootstrap CI contains 0)"
            if boot["contains_zero"]
            else "**different** (bootstrap CI excludes 0)"
        )
        out += [
            f"### Prompt {prompt}",
            "",
            f"Sources: `results/{fp16['name']}/summary.json`, `results/{awq['name']}/summary.json`.",
            "",
            *_table(
                ["Variant", "Task success", "95% CI", "n trials"],
                [
                    ["FP16", _pct(fh["task_success_rate"]), _ci(fh), str(fh["trials"])],
                    ["AWQ", _pct(ah["task_success_rate"]), _ci(ah), str(ah["trials"])],
                ],
            ),
            "",
            f"- Quality retention (AWQ / FP16): **{_pct(retention)}**",
            f"- Primary — paired scenario-level bootstrap of AWQ − FP16: difference {_pct(boot['difference'])}, "
            f"95% CI [{_pct(boot['ci_low'])}, {_pct(boot['ci_high'])}] over {boot['n_scenarios']} scenarios, "
            f"{boot['resamples']} resamples → {verdict}.",
            f"- Secondary — two-proportion z-test (all trials independent): z = {z:.2f}, p = {p:.3f}.",
            "",
        ]
    if not any_done:
        return _skip("Quality retention (FP16 vs AWQ)")
    return out


def prefix_cache_section(awq: dict | None, nocache: dict | None) -> list[str]:
    if not (awq and nocache):
        return _skip("Prefix-caching ablation (AWQ)")
    on, off = _hdr(awq, awq["headline_level"]), _hdr(nocache, nocache["headline_level"])
    return [
        "## Prefix-caching ablation (AWQ, headline level)",
        "",
        f"Sources: `results/{awq['name']}/summary.json` (on), `results/{nocache['name']}/summary.json` (off).",
        "",
        *_table(
            ["Metric", "prefix cache ON", "OFF", "Δ (on vs off)"],
            [
                [
                    "TTFT p50 (s)",
                    str(on.get("ttft_p50_s")),
                    str(off.get("ttft_p50_s")),
                    _pct_change(on.get("ttft_p50_s"), off.get("ttft_p50_s")),
                ],
                [
                    "req/s",
                    str(on.get("requests_per_s")),
                    str(off.get("requests_per_s")),
                    _pct_change(on.get("requests_per_s"), off.get("requests_per_s")),
                ],
            ],
        ),
        "",
    ]


def _clusters(fp16: dict, awq: dict) -> dict[str, tuple[list[bool], list[bool]]]:
    """Per-scenario (success-boolean) trials under each variant, from the committed trials.jsonl of each run.

    The bootstrap needs per-trial outcomes grouped by scenario. If a run's trials.jsonl is absent (git-ignored),
    the overlap is empty and the bootstrap reports no scenarios, which the report shows plainly.
    """
    fp16_rows, awq_rows = _trials(fp16), _trials(awq)
    # Bootstrap computes A - B; the pre-registered difference is AWQ - FP16, so A = AWQ, B = FP16.
    return {sid: (awq_rows[sid], fp16_rows[sid]) for sid in set(fp16_rows) & set(awq_rows)}


def _trials(summary: dict) -> dict[str, list[bool]]:
    path = REPO_ROOT / "results" / summary["name"] / "trials.jsonl"
    rows: dict[str, list[bool]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows.setdefault(r["scenario_id"], []).append(bool(r["success"]))
    return rows


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ci(h: dict) -> str:
    return f"{h['task_success_ci95_low'] * 100:.1f}–{h['task_success_ci95_high'] * 100:.1f}%"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"] + [
        "| " + " | ".join(r) + " |" for r in rows
    ]


def build(args: argparse.Namespace) -> str:
    fp16_b, awq_b = _load(args.fp16_bench), _load(args.awq_bench)
    nocache_b = _load(args.awq_nocache_bench)
    eval_runs = {
        "fp16-v1p": _load(args.fp16_eval),
        "awq-v1p": _load(args.awq_eval),
        "fp16-v5": _load(args.fp16_eval_v5),
        "awq-v5": _load(args.awq_eval_v5),
    }
    out = [
        "# Serving comparison (FP16 vs AWQ)",
        "",
        "Built by `serving/compare.py` from committed summaries. Sections with missing inputs are skipped. "
        "GPU numbers come only from Phase 9 runs (see `docs/SERVING_PLAN.md`).",
        "",
    ]
    out += latency_section(fp16_b, awq_b)
    out += cost_section(fp16_b, awq_b)
    out += quality_section(eval_runs)
    out += prefix_cache_section(awq_b, nocache_b)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in (
        "fp16-bench",
        "awq-bench",
        "awq-nocache-bench",
        "fp16-eval",
        "awq-eval",
        "fp16-eval-v5",
        "awq-eval-v5",
    ):
        p.add_argument(f"--{flag}", type=Path, default=None)
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(args), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
