"""Run the evaluation suite and write results/<name>/{summary.json, trials.jsonl, report.md}.

    python -m evals.run --backend replay --split all --gate oracle                     # harness self-check
    python -m evals.run --backend openai --base-url http://localhost:11434/v1 --model qwen2.5:7b --split dev

Reported numbers come from the test split with the openai backend. The replay backend (reference trajectories) is a
harness check only and never a model metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from agent.config import Settings
from agent.llm import OpenAICompatLLM, ReplayLLM
from agent.prompts import SYSTEM_PROMPT
from agent.tools import openai_tools
from evals.metrics import compute_metrics, flat_metrics
from evals.report import render_report
from evals.runner import SCENARIOS_PATH, LLMFactory, RunConfig, load_scenarios, regrade_row, run_trial

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
THRESHOLDS_PATH = Path(__file__).with_name("thresholds.yaml")
PACKAGES = ("langgraph", "langchain-core", "langchain-openai", "pydantic", "fastapi")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def prompt_version() -> str:
    return _hash(SYSTEM_PROMPT.encode())


def tools_version() -> str:
    return _hash(json.dumps(openai_tools(), sort_keys=True).encode())


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    settings = Settings.from_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["openai", "replay"], default="openai")
    p.add_argument("--base-url", default=settings.llm_base_url)
    p.add_argument("--model", default=settings.llm_model)
    p.add_argument("--api-key", default=settings.llm_api_key)
    p.add_argument("--temperature", type=float, default=settings.llm_temperature)
    p.add_argument("--max-tokens", type=int, default=settings.llm_max_tokens)
    p.add_argument("--timeout", type=float, default=settings.llm_timeout_s, help="LLM request timeout in seconds")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--guardrails", choices=["on", "off"], default="on")
    p.add_argument("--fault-rate", type=float, default=0.0)
    p.add_argument("--http-retries", type=int, default=settings.http_retries)
    p.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    p.add_argument("--categories", default="", help="comma-separated categories (default: all)")
    p.add_argument("--ids", default="", help="comma-separated scenario ids (default: all)")
    p.add_argument("--name", default="", help="results/<name>/ (default: derived from the options)")
    p.add_argument("--note", default="", help="label printed at the top of report.md")
    p.add_argument("--record-llm", action="store_true", help="write every LLM request to llm_requests.jsonl")
    p.add_argument("--gate", choices=["none", "oracle", "thresholds"], default="none")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow a model run on test scenarios from a dirty working tree (recorded in meta)",
    )
    p.add_argument(
        "--regrade",
        default="",
        metavar="NAME",
        help="re-grade results/NAME/trials.jsonl with the current grader (no model calls), rewrite summary + report",
    )
    args = p.parse_args(argv)
    if args.trials < 1 or args.concurrency < 1:
        p.error("--trials and --concurrency must be >= 1")
    return args


def default_name(args: argparse.Namespace) -> str:
    model = "reference" if args.backend == "replay" else re.sub(r"[^A-Za-z0-9.]+", "-", args.model).strip("-")
    name = f"{args.backend}-{args.split}-{model}-{prompt_version()[:6]}"
    if args.guardrails == "off":
        name += "-noguard"
    if args.fault_rate:
        name += f"-faults{args.fault_rate:g}"
    return name


def select_scenarios(args: argparse.Namespace) -> list[dict[str, Any]]:
    scenarios = load_scenarios()
    categories = {c for c in args.categories.split(",") if c}
    ids = {i for i in args.ids.split(",") if i}
    return [
        s
        for s in scenarios
        if (args.split == "all" or s["split"] == args.split)
        and (not categories or s["category"] in categories)
        and (not ids or s["id"] in ids)
    ]


def build_backend(args: argparse.Namespace, out_dir: Path) -> LLMFactory:
    if args.backend == "replay":
        return lambda scenario: ReplayLLM(scenario["reference"])
    record = out_dir / "llm_requests.jsonl" if args.record_llm else None
    if record is not None and record.exists():
        record.unlink()
    shared = OpenAICompatLLM(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
        record_path=record,
    )
    return lambda scenario: shared


def run_meta(args: argparse.Namespace, name: str, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    replay = args.backend == "replay"
    return {
        "name": name,
        "note": args.note or None,
        "backend": args.backend,
        "model": "reference-trajectories" if replay else args.model,
        "base_url_host": None if replay else urlparse(args.base_url).netloc,
        "temperature": None if replay else args.temperature,
        "max_tokens": None if replay else args.max_tokens,
        "trials": args.trials,
        "split": args.split,
        "categories": sorted({s["category"] for s in scenarios}),
        "scenarios": len(scenarios),
        "guardrails": args.guardrails,
        "fault_rate": args.fault_rate,
        "http_retries": args.http_retries,
        "concurrency": args.concurrency,
        "allow_dirty": args.allow_dirty,
        "prompt_version": prompt_version(),
        "tools_version": tools_version(),
        "scenarios_hash": _hash(SCENARIOS_PATH.read_bytes()),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
        "packages": {name: version(name) for name in PACKAGES},
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_s": None,
    }


def dirty_test_run_refused(args: argparse.Namespace, meta: dict[str, Any]) -> bool:
    """Model runs that touch the test split must come from a committed tree (replay oracle runs are exempt)."""
    touches_test = args.split in ("test", "all")
    return args.backend == "openai" and touches_test and bool(meta["git_dirty"]) and not args.allow_dirty


def run_all(
    scenarios: list[dict[str, Any]], args: argparse.Namespace, factory: LLMFactory, config: RunConfig
) -> list[dict[str, Any]]:
    jobs = [(index, scenario, trial) for index, scenario in enumerate(scenarios) for trial in range(args.trials)]
    rows: list[tuple[int, int, dict[str, Any]]] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_trial, scenario, trial, factory, config): (i, trial) for i, scenario, trial in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            index, trial = futures[future]
            row = future.result()
            with lock:
                rows.append((index, trial, row))
                if not args.quiet:
                    status = "PASS" if row["success"] else "FAIL " + ",".join(row["failure_tags"])
                    print(f"[{done}/{len(jobs)}] {row['scenario_id']} t{trial}: {status} ({row['latency_s']:.1f}s)")
    return [row for _, _, row in sorted(rows, key=lambda item: (item[0], item[1]))]


def gate_violations(gate: str, metrics: dict[str, Any], fault_rate: float) -> list[str]:
    flat = flat_metrics(metrics)
    if gate == "oracle":
        checks = {"task_success_rate": 1.0, "tool_selection_accuracy": 1.0}
        problems = [f"{k} = {flat[k]} (oracle requires {v})" for k, v in checks.items() if flat[k] != v]
        if flat["harness_errors"]:
            problems.append(f"harness_errors = {flat['harness_errors']}")
        if fault_rate > 0 and not (flat["faults_injected"] > 0 and flat["retries"] > 0):
            problems.append("fault run injected no faults or made no retries")
        return problems
    if gate == "thresholds":
        limits = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))
        problems = []
        for key, minimum in (limits.get("min") or {}).items():
            if flat.get(key) is None or flat[key] < minimum:
                problems.append(f"{key} = {flat.get(key)} < min {minimum}")
        for key, maximum in (limits.get("max") or {}).items():
            if flat.get(key) is None or flat[key] > maximum:
                problems.append(f"{key} = {flat.get(key)} > max {maximum}")
        return problems
    return []


def write_results(out_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "trials.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    (out_dir / "report.md").write_text(render_report(summary), encoding="utf-8")


def regrade(name: str) -> tuple[Path, dict[str, Any]]:
    """Re-grade a stored run with the current grader and scenarios; model outputs are reused unchanged."""
    out_dir = RESULTS_DIR / name
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    scenarios = {s["id"]: s for s in load_scenarios()}
    lines = (out_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [regrade_row(scenarios[row["scenario_id"]], row) for row in map(json.loads, lines)]
    summary["metrics"] = compute_metrics(rows)
    summary["meta"]["regraded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    summary["meta"]["regraded_scenarios_hash"] = _hash(SCENARIOS_PATH.read_bytes())
    write_results(out_dir, summary, rows)
    return out_dir, summary


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.regrade:
        out_dir, summary = regrade(args.regrade)
        print(f"re-graded {summary['name']} -> {out_dir}")
        violations = gate_violations(args.gate, summary["metrics"], summary["meta"]["fault_rate"])
        for problem in violations:
            print(f"GATE FAILED ({args.gate}): {problem}", file=sys.stderr)
        return 1 if violations else 0
    scenarios = select_scenarios(args)
    if not scenarios:
        print("no scenarios match the selection", file=sys.stderr)
        return 2
    name = args.name or default_name(args)
    meta = run_meta(args, name, scenarios)
    if dirty_test_run_refused(args, meta):
        print(
            "refusing a model run on test scenarios from a dirty working tree: commit first "
            "(or pass --allow-dirty, which is recorded in meta)",
            file=sys.stderr,
        )
        return 3
    out_dir = RESULTS_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    config = RunConfig(guardrails=args.guardrails == "on", fault_rate=args.fault_rate, http_retries=args.http_retries)
    started = time.perf_counter()
    rows = run_all(scenarios, args, build_backend(args, out_dir), config)
    meta["duration_s"] = round(time.perf_counter() - started, 2)

    metrics = compute_metrics(rows)
    summary = {"name": name, "meta": meta, "metrics": metrics}
    write_results(out_dir, summary, rows)

    h = metrics["headline"]
    print(
        f"{name}: task success {h['task_success_rate']:.3f} "
        f"[{h['task_success_ci95_low']:.3f}, {h['task_success_ci95_high']:.3f}] over {h['trials']} trials, "
        f"tool selection {h['tool_selection_accuracy']:.3f}, "
        f"unauthorized changes {metrics['safety']['unauthorized_change_rate']:.3f} -> {out_dir}"
    )
    violations = gate_violations(args.gate, metrics, args.fault_rate)
    for problem in violations:
        print(f"GATE FAILED ({args.gate}): {problem}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
