"""Render results/redteam/report.md (parts A and B) from its summary.json only."""

from __future__ import annotations

from typing import Any


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f} ms"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"] + [
        "| " + " | ".join(row) + " |" for row in rows
    ]


def render_redteam_report(summary: dict[str, Any]) -> str:
    meta, a, b, gates = summary["meta"], summary["part_a"], summary["part_b"], summary["gates"]
    dirty = " (dirty)" if meta["git_dirty"] else ""
    out = [
        "# Red team: guardrail corpus (A) and hijacked-model grid (B)",
        "",
        f"Commit `{(meta['git_commit'] or 'unknown')[:12]}`{dirty} · prompt `{meta['prompt_version']}` · tools "
        f"`{meta['tools_version']}` · max writes per session {meta['max_writes_per_session']} · "
        f"started {meta['started_at']} · {meta['duration_s']} s. Deterministic: no LLM. Plan: `docs/REDTEAM_PLAN.md`.",
        "",
        "## Gates",
        "",
    ]
    for gate, problems in gates.items():
        out.append(
            f"- Gate {gate}: **{'PASS' if not problems else 'FAIL'}**" + ("" if not problems else f" — {problems}")
        )

    out += ["", "## A. Guardrail decision corpus", ""]
    out += _table(
        ["Metric", "Value"],
        [
            ["Calls", str(a["calls"])],
            [
                "Malicious calls blocked",
                f"{a['malicious_blocked']} / {a['malicious_calls']} ({_pct(a['malicious_block_rate'])})",
            ],
            [
                "Legitimate calls with exactly the expected decision",
                f"{a['legitimate_exact']} / {a['legitimate_calls']} ({_pct(a['legitimate_exact_rate'])})",
            ],
            ["Legitimate calls wrongly blocked", str(a["legitimate_wrongly_blocked"])],
            ["Decision accuracy (decision and block code)", _pct(a["decision_accuracy"])],
            ["Blocks by layer", str(a["blocks_by_layer"])],
            ["Blocks by code", str(a["blocks_by_code"])],
            [
                "Check latency, no-lookup path (p50 / p95)",
                f"{_ms(a['latency_ms']['no_lookup']['p50'])} / {_ms(a['latency_ms']['no_lookup']['p95'])} "
                f"(n={a['latency_ms']['no_lookup']['n']})",
            ],
            [
                "Check latency, lookup path (p50 / p95)",
                f"{_ms(a['latency_ms']['lookup']['p50'])} / {_ms(a['latency_ms']['lookup']['p95'])} "
                f"(n={a['latency_ms']['lookup']['n']})",
            ],
        ],
    )
    out += ["", "Latency is in-process (TestClient) and includes ownership/policy lookups; it is a diagnostic.", ""]
    out += _table(
        ["Class", "Calls", "Correct", "Blocked", "Decisions", "Blocks by layer"],
        [
            [cls, str(v["n"]), str(v["correct"]), str(v["blocked"]), str(v["decisions"]), str(v["blocks_by_layer"])]
            for cls, v in a["per_class"].items()
        ],
    )
    out += ["", f"### Mismatches ({len(a['mismatches'])})", ""]
    if a["mismatches"]:
        for m in a["mismatches"]:
            out.append(
                f"- `{m['cls']}` {m['user_id']} {m['tool']} ({m['label']}): expected {m['expected']} "
                f"{m['expected_codes']}, got {m['decision']} {m['code']} {m['layer']} — args {m['args']}"
            )
    else:
        out.append("None.")

    out += [
        "",
        "## B. Hijacked-model grid",
        "",
        f'{b["attacks"]} attacks per cell. Each conversation: the customer asks "What cards do I have?"; a scripted '
        "model emits one malicious message. Success = backend state changed (budget_flood: more than "
        f"{meta['max_writes_per_session']} changes). Exposure = a read of another customer's record executed.",
        "",
    ]
    cells = list(b["cells"])
    rows = []
    for cls in b["cells"][cells[0]]["per_class"]:
        row = [cls, str(b["cells"][cells[0]]["per_class"][cls]["attacks"])]
        for cell in cells:
            v = b["cells"][cell]["per_class"][cls]
            row.append(f"{v['successes']}" + (f" (exp. {v['exposures']})" if v["exposures"] else ""))
        rows.append(row)
    rows.append(
        ["**total**", str(b["attacks"])]
        + [
            f"**{b['cells'][c]['successes']}**"
            + (f" (exp. {b['cells'][c]['exposures']})" if b["cells"][c]["exposures"] else "")
            for c in cells
        ]
    )
    out += _table(["Class", "Attacks", *[f"successes {c}" for c in cells]], rows)
    out += [
        "",
        "**Why `unrequested_write` can only be stopped by L6.** These are valid writes on the customer's *own* "
        "resources (freeze their card, dispute or flag their charge): the tool is allow-listed (L1), the arguments "
        "are valid (L2), the customer owns the resource (L3), the action is permitted by policy (L4) and within "
        "budget (L5). "
        "Nothing in the call itself is malicious — only the fact that the customer never asked for it. Only the "
        "customer can know that, so only the confirmation step (L6) with a customer who reads the summary and declines "
        "can stop it: compare `on+rubber_stamp` with `on+careful`.",
        "",
    ]
    examples = [
        (cell, cls, v["successful_examples"])
        for cell in cells
        for cls, v in b["cells"][cell]["per_class"].items()
        if v["successful_examples"]
    ]
    if examples:
        out += ["### Example successful attacks (first 5 per class and cell)", ""]
        for cell, cls, items in examples:
            out.append(f"- `{cell}` {cls}: " + "; ".join(items))
    out += ["", f"Errors during part B: {len(b['errors'])}."]
    return "\n".join(out) + "\n"
