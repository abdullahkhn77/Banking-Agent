"""Render report.md from summary.json. Every number in the report comes from the summary."""

from __future__ import annotations

from typing import Any


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.{digits}f}"


def _ci(low: float, high: float) -> str:
    return f"{low * 100:.1f}–{high * 100:.1f}%"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_report(summary: dict[str, Any]) -> str:
    meta, m = summary["meta"], summary["metrics"]
    h, safety, latency, tokens, rel = m["headline"], m["safety"], m["latency"], m["tokens"], m["reliability"]
    out = [f"# Evaluation report: `{summary['name']}`", ""]
    if meta.get("note"):
        out += [f"> **{meta['note']}**", ""]
    dirty = " (dirty)" if meta["git_dirty"] else ""
    out += _table(
        ["Setting", "Value"],
        [
            ["backend / model", f"{meta['backend']} / `{meta['model']}`"],
            ["endpoint host", str(meta["base_url_host"])],
            ["temperature / max tokens", f"{meta['temperature']} / {meta['max_tokens']}"],
            ["split / scenarios / trials", f"{meta['split']} / {meta['scenarios']} / {meta['trials']}"],
            [
                "guardrails / fault rate / HTTP retries",
                f"{meta['guardrails']} / {meta['fault_rate']} / {meta['http_retries']}",
            ],
            [
                "prompt / tools / scenarios version",
                f"`{meta['prompt_version']}` / `{meta['tools_version']}` / `{meta['scenarios_hash']}`",
            ],
            ["git commit", f"`{(meta['git_commit'] or 'unknown')[:12]}`{dirty}"],
            ["started / duration", f"{meta['started_at']} / {_num(meta['duration_s'], 1)} s"],
            *([["re-graded (same model outputs)", str(meta["regraded_at"])]] if meta.get("regraded_at") else []),
        ],
    )
    pass_k = ", ".join(f"pass^{k} {_pct(v)}" for k, v in m["pass_hat_k"].items())
    ci = _ci(h["task_success_ci95_low"], h["task_success_ci95_high"])
    out += ["", "## Headline", ""]
    out += _table(
        ["Metric", "Value"],
        [
            ["Task success", f"**{_pct(h['task_success_rate'])}** (95% CI {ci}, n={h['trials']})"],
            ["pass^k", pass_k],
            ["Tool-selection accuracy", _pct(h["tool_selection_accuracy"])],
            ["Argument accuracy", _pct(h["argument_accuracy"])],
            ["Required-lookup rate", _pct(h["required_lookup_rate"])],
            ["Clarification rate", _pct(h["clarification_rate"])],
            ["Malformed tool-call rate", _pct(h["malformed_tool_call_rate"])],
        ],
    )
    out += ["", "## Safety", ""]
    out += _table(
        ["Metric", "Value"],
        [
            ["Unauthorized-change rate", f"**{_pct(safety['unauthorized_change_rate'])}**"],
            ["Unintended-change rate", _pct(safety["unintended_change_rate"])],
            ["Trials with an unsafe attempt", _pct(safety["unsafe_attempt_rate"])],
            [
                "Unsafe calls blocked",
                f"{safety['unsafe_calls_blocked']} / {safety['unsafe_calls']} ({_pct(safety['unsafe_block_rate'])})",
            ],
            ["Guardrail blocks", f"{safety['guardrail_blocks']} — by layer {safety['blocks_by_layer']}"],
            ["Blocks by code", str(safety["blocks_by_code"])],
            [
                "Confirmations approved / requested",
                f"{safety['confirmations_approved']} / {safety['confirmations_requested']}",
            ],
        ],
    )
    out += ["", "## Latency, tokens, reliability", ""]
    out += _table(
        ["Metric", "Value"],
        [
            ["Task latency p50 / p95", f"{_num(latency['task_p50_s'])} s / {_num(latency['task_p95_s'])} s"],
            [
                "LLM call latency p50 / p95",
                f"{_num(latency['llm_call_p50_s'])} s / {_num(latency['llm_call_p95_s'])} s",
            ],
            ["LLM calls per task", _num(latency["llm_calls_per_task"])],
            [
                "Tokens in / out per task",
                f"{_num(tokens['input_tokens_per_task'], 0)} / {_num(tokens['output_tokens_per_task'], 0)}",
            ],
            ["Faults injected / client retries", f"{rel['faults_injected']} / {rel['retries']}"],
            [
                "Step-limit hits / LLM errors / harness errors",
                f"{rel['step_limit_hits']} / {rel['llm_errors']} / {rel['harness_errors']}",
            ],
        ],
    )
    for title, key in (("By category", "by_category"), ("By difficulty", "by_difficulty"), ("By split", "by_split")):
        out += ["", f"## {title}", ""]
        out += _table(
            ["Group", "n", "Success", "95% CI"],
            [[name, str(b["n"]), _pct(b["success_rate"]), _ci(*b["ci95"])] for name, b in m[key].items()],
        )
    out += ["", "## Failure tags", ""]
    if m["failure_tags"]:
        out += _table(["Tag", "Trials"], [[tag, str(count)] for tag, count in m["failure_tags"].items()])
    else:
        out.append("None.")
    out += ["", f"## Failed trials ({len(m['failed_trials'])})", ""]
    if m["failed_trials"]:
        for f in m["failed_trials"]:
            tags = ", ".join(f["failure_tags"]) or "untagged"
            out.append(
                f"- `{f['scenario_id']}` trial {f['trial']} ({f['category']}; {tags}): {'; '.join(f['reasons'])}"
            )
    else:
        out.append("None.")
    return "\n".join(out) + "\n"
