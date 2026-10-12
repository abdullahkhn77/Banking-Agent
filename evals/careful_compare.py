"""Part C: compare the agent under the tool customer (Phase 4.5 runs) and the careful customer (new runs).

    python -m evals.careful_compare --tool results/test-ollama-qwen7b-v1p \
        --careful results/redteam-careful-v1p --label v1p --out results/redteam-careful-v1p

Writes a comparison summary.json + report.md. The headline is "what L6 adds on top of the agent alone": the drop in
unintended-change rate from the tool customer to the careful customer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads((path / "summary.json").read_text(encoding="utf-8"))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def build(tool: dict[str, Any], careful: dict[str, Any], label: str) -> dict[str, Any]:
    th, ch = tool["metrics"]["headline"], careful["metrics"]["headline"]
    ts, cs = tool["metrics"]["safety"], careful["metrics"]["safety"]
    return {
        "label": label,
        "tool_run": tool["name"],
        "careful_run": careful["name"],
        "tool_customer": {
            "customer": tool["meta"].get("customer", "tool"),
            "task_success_rate": th["task_success_rate"],
            "task_success_ci95": [th["task_success_ci95_low"], th["task_success_ci95_high"]],
            "unintended_change_rate": ts["unintended_change_rate"],
            "unauthorized_change_rate": ts["unauthorized_change_rate"],
            "confirmations_requested": ts["confirmations_requested"],
            "confirmations_approved": ts["confirmations_approved"],
        },
        "careful_customer": {
            "customer": careful["meta"].get("customer", "careful"),
            "task_success_rate": ch["task_success_rate"],
            "task_success_ci95": [ch["task_success_ci95_low"], ch["task_success_ci95_high"]],
            "unintended_change_rate": cs["unintended_change_rate"],
            "unauthorized_change_rate": cs["unauthorized_change_rate"],
            "confirmations_requested": cs["confirmations_requested"],
            "confirmations_approved": cs["confirmations_approved"],
        },
        "l6_prevented_unintended_change_rate": round(
            (ts["unintended_change_rate"] or 0) - (cs["unintended_change_rate"] or 0), 4
        ),
        "meta": {
            "tool_commit": tool["meta"]["git_commit"],
            "careful_commit": careful["meta"]["git_commit"],
            "prompt_version": careful["meta"]["prompt_version"],
            "tools_version": careful["meta"]["tools_version"],
            "model": careful["meta"].get("model"),
            "model_digest": careful["meta"].get("model_digest"),
            "trials": careful["meta"]["trials"],
            "split": careful["meta"]["split"],
        },
    }


def render(summary: dict[str, Any]) -> str:
    t, c = summary["tool_customer"], summary["careful_customer"]
    m = summary["meta"]

    def ci(entry: dict[str, Any]) -> str:
        lo, hi = entry["task_success_ci95"]
        return f"{_pct(entry['task_success_rate'])} ({lo * 100:.1f}–{hi * 100:.1f}%)"

    conf_t = f"{t['confirmations_approved']}/{t['confirmations_requested']}"
    conf_c = f"{c['confirmations_approved']}/{c['confirmations_requested']}"
    out = [
        f"# Careful-customer comparison (C): {summary['label']}",
        "",
        f"prompt `{m['prompt_version']}` · tools `{m['tools_version']}` · model `{m['model']}` digest "
        f"`{(m['model_digest'] or '?')[:16]}` · split {m['split']} · {m['trials']} trials. "
        f"tool run `{summary['tool_run']}`, careful run `{summary['careful_run']}`. Plan: `docs/REDTEAM_PLAN.md`.",
        "",
        "The **tool** customer approves any action whose tool the customer wanted (the official agent-only number). "
        "The **careful** customer also requires the confirmation summary to match the request. The difference is what "
        "the human confirmation step (L6) adds on top of the agent alone.",
        "",
        "| Metric | tool customer | careful customer |",
        "|---|---|---|",
        f"| Task success (95% CI) | {ci(t)} | {ci(c)} |",
        f"| Unintended-change rate | {_pct(t['unintended_change_rate'])} | {_pct(c['unintended_change_rate'])} |",
        f"| Unauthorized-change rate | {_pct(t['unauthorized_change_rate'])} | {_pct(c['unauthorized_change_rate'])} |",
        f"| Confirmations approved / requested | {conf_t} | {conf_c} |",
        "",
        f"**What L6 adds:** the careful customer's rejections reduce the unintended-change rate by "
        f"**{_pct(summary['l6_prevented_unintended_change_rate'])}** (from {_pct(t['unintended_change_rate'])} to "
        f"{_pct(c['unintended_change_rate'])}).",
        "",
        "This is an additional measurement. It does not change the Phase 4.5 decision: production stays v1′.",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tool", type=Path, required=True)
    p.add_argument("--careful", type=Path, required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    summary = build(_load(args.tool), _load(args.careful), args.label)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (args.out / "report.md").write_text(render(summary), encoding="utf-8")
    prevented = summary["l6_prevented_unintended_change_rate"]
    print(f"{args.label}: L6 prevented unintended-change rate {prevented} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
