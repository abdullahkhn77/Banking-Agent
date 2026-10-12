"""Metric deltas through the API, and the label-cardinality guard (no user_id / session_id / free-text labels)."""

from __future__ import annotations

from agent import telemetry
from tests.conftest_api import auth, build, freeze_llm

FORBIDDEN_LABELS = {"user_id", "session_id", "user", "session", "text", "request_id", "sub", "token", "message"}
ALLOWED_LABELS = {
    "direction",
    "tool",
    "decision",
    "layer",
    "code",
    "outcome",
    "status",
    "service",
    "route",
    "method",
    "le",
}


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    return telemetry.REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_freeze_flow_produces_exact_counter_deltas() -> None:
    before_calls = _value("agent_tool_calls_total", {"tool": "freeze_card", "decision": "confirm"})
    before_appr = _value("agent_confirmations_total", {"outcome": "approved"})
    before_exec = _value("agent_tool_executions_total", {"tool": "freeze_card", "status": "ok"})
    before_list = _value("agent_tool_calls_total", {"tool": "list_cards", "decision": "allow"})

    client, store, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    client.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=auth("u_1001"))

    assert _value("agent_tool_calls_total", {"tool": "freeze_card", "decision": "confirm"}) == before_calls + 1
    assert _value("agent_tool_calls_total", {"tool": "list_cards", "decision": "allow"}) == before_list + 1
    assert _value("agent_confirmations_total", {"outcome": "approved"}) == before_appr + 1
    assert _value("agent_tool_executions_total", {"tool": "freeze_card", "status": "ok"}) == before_exec + 1


def test_declined_confirmation_counts_as_declined() -> None:
    before = _value("agent_confirmations_total", {"outcome": "declined"})
    client, _, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    client.post(f"/v1/sessions/{sid}/confirm", json={"approved_ids": []}, headers=auth("u_1001"))
    assert _value("agent_confirmations_total", {"outcome": "declined"}) == before + 1


def test_metrics_endpoint_exposes_families() -> None:
    client, _, _ = build()
    text = client.get("/metrics").text
    for family in (
        "agent_turn_latency_seconds",
        "agent_tool_calls_total",
        "api_http_requests_total",
        "agent_active_sessions",
        "agent_guardrail_blocks_total",
    ):
        assert family in text


def test_no_metric_uses_a_high_cardinality_or_identity_label() -> None:
    """Cardinality guard. Checks DECLARED label names (catches a bad label even before any sample is emitted),
    and also any emitted sample labels."""
    for name, labelnames in telemetry.metric_label_names().items():
        labels = set(labelnames)
        assert not (labels & FORBIDDEN_LABELS), f"{name} declares a forbidden label: {labels & FORBIDDEN_LABELS}"
        assert labels <= ALLOWED_LABELS, f"{name} declares an unexpected label: {labels - ALLOWED_LABELS}"
    for metric in telemetry.REGISTRY.collect():
        for sample in metric.samples:
            labels = set(sample.labels)
            bad = labels & FORBIDDEN_LABELS
            assert not bad, f"{metric.name} emitted a forbidden label: {bad}"
            assert labels <= ALLOWED_LABELS, f"{metric.name} emitted an unexpected label: {labels - ALLOWED_LABELS}"


def test_http_route_label_is_a_template_not_the_concrete_path() -> None:
    client, _, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.get(f"/v1/sessions/{sid}", headers=auth("u_1001"))
    routes = {
        s.labels["route"]
        for m in telemetry.REGISTRY.collect()
        if m.name == "api_http_requests"
        for s in m.samples
        if "route" in s.labels
    }
    # the templated route is present; the concrete session id is NOT a label value
    assert "/v1/sessions/{session_id}" in routes
    assert not any(sid in r for r in routes)
