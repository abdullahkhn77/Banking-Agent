"""Validate the Grafana dashboard and Prometheus alert PromQL against the metrics the app actually registers.

Agent metrics referenced in panels/alerts must be registered by the app. vLLM metrics (names with a colon) are
external and only listed, with a reminder to verify them against vLLM 0.31.0 in Phase 9.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent import telemetry

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "dashboards" / "banking-agent.json"
ALERTS = ROOT / "monitoring" / "alerts.yml"
PROM_CONFIG = ROOT / "monitoring" / "prometheus.yml"

SUFFIXES = ("_bucket", "_count", "_sum", "_created", "_total")
# Prometheus built-ins / functions that look like identifiers but are not metrics.
NOT_METRICS = {"le", "by", "rate", "sum", "increase", "histogram_quantile", "clamp_min", "offset", "and", "or"}
METRIC_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def _base(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _metric_names(expr: str) -> set[str]:
    names = set()
    for token in METRIC_RE.findall(expr):
        if token in NOT_METRICS or "(" in token:
            continue
        # skip label-value fragments and bare numbers handled by the regex anyway
        names.add(token)
    return names


def _dashboard_exprs() -> list[str]:
    data = json.loads(DASHBOARD.read_text())
    return [t["expr"] for panel in data["panels"] for t in panel.get("targets", []) if "expr" in t]


def _alert_exprs() -> list[str]:
    data = yaml.safe_load(ALERTS.read_text())
    return [rule["expr"] for group in data["groups"] for rule in group["rules"]]


def _all_referenced_metrics() -> set[str]:
    metrics: set[str] = set()
    for expr in _dashboard_exprs() + _alert_exprs():
        cleaned = re.sub(r"\{[^}]*\}", "", expr)  # drop {label matchers}
        cleaned = re.sub(r"by\s*\([^)]*\)", "", cleaned)  # drop grouping clauses: by (le), by (decision)
        cleaned = re.sub(r"\[[0-9]+[smhdwy]\]", "", cleaned)  # drop ranges: [5m], [6h]
        # a metric name always contains "_" or ":"; bare words (le, status, rate, ...) are labels/functions
        metrics |= {t for t in _metric_names(cleaned) if "_" in t or ":" in t}
    return metrics


def test_dashboard_is_valid_json_with_targets() -> None:
    data = json.loads(DASHBOARD.read_text())
    assert data["uid"] == "banking-agent"
    assert any(p.get("targets") for p in data["panels"])


def test_every_agent_metric_referenced_is_registered() -> None:
    registered = telemetry.registered_metric_names()
    external, missing = [], []
    for name in _all_referenced_metrics():
        if ":" in name:  # vLLM namespaced metric, external
            external.append(name)
            continue
        if _base(name) not in registered:
            # histogram_quantile buckets and api_* histograms map to a *_seconds base
            missing.append(name)
    assert missing == [], f"PromQL references metrics the app does not register: {sorted(set(missing))}"
    assert external, "expected some external vLLM metrics to be present (they are verified in Phase 9)"


def test_external_vllm_metrics_are_namespaced() -> None:
    external = {n for n in _all_referenced_metrics() if ":" in n}
    assert external and all(n.startswith("vllm:") for n in external)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available for promtool")
def test_promtool_checks_rules_and_config() -> None:
    def promtool(*args: str, mount: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{mount}:/work",
                "-w",
                "/work",
                "--entrypoint",
                "promtool",
                "prom/prometheus:v2.54.1",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        rules = promtool("check", "rules", "alerts.yml", mount=ROOT / "monitoring")
        config = promtool("check", "config", "prometheus.yml", mount=ROOT / "monitoring")
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"docker/promtool unavailable: {exc}")
    if rules.returncode != 0 and ("Cannot connect" in rules.stderr or "daemon" in rules.stderr):
        pytest.skip("docker daemon not running")
    assert rules.returncode == 0, rules.stderr + rules.stdout
    assert config.returncode == 0, config.stderr + config.stdout
