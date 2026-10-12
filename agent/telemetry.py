"""Prometheus metrics for the agent and API, driven by the same trace events the graph already emits.

Metrics use bounded labels only: tool names, guardrail layers/codes, decisions, service names, HTTP route templates
and fixed enums. Never user_id, session_id or any free text (a test enforces this). ``record_trace_event`` is
called at the same points the Phase 3 trace is appended, so the trace and the metrics cannot disagree.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)
CHECK_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5)

# A dedicated registry so tests can reset it and the app can expose exactly these metrics.
REGISTRY = CollectorRegistry()


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
    return Counter(name, doc, labels, registry=REGISTRY)


def _histogram(name: str, doc: str, buckets: tuple[float, ...]) -> Histogram:
    return Histogram(name, doc, buckets=buckets, registry=REGISTRY)


turn_latency = _histogram("agent_turn_latency_seconds", "End-to-end agent turn latency.", LATENCY_BUCKETS)
llm_latency = _histogram("agent_llm_call_latency_seconds", "Latency of one LLM call.", LATENCY_BUCKETS)
llm_tokens = _counter("agent_llm_tokens_total", "LLM tokens.", ("direction",))
llm_errors = _counter("agent_llm_errors_total", "LLM call errors.")
tool_calls = _counter("agent_tool_calls_total", "Proposed tool calls by guard decision.", ("tool", "decision"))
guardrail_blocks = _counter("agent_guardrail_blocks_total", "Guardrail blocks.", ("layer", "code"))
guardrail_check = _histogram("agent_guardrail_check_seconds", "Guardrail check duration.", CHECK_BUCKETS)
confirmations = _counter("agent_confirmations_total", "Confirmation outcomes.", ("outcome",))
tool_executions = _counter("agent_tool_executions_total", "Executed tool calls.", ("tool", "status"))
bank_calls = _counter("bank_service_calls_total", "Bank service calls.", ("service", "status"))
bank_retries = _counter("bank_service_retries_total", "Bank service retries.", ("service",))
step_limit_hits = _counter("agent_step_limit_hits_total", "Turns that hit the step limit.")
active_sessions = Gauge("agent_active_sessions", "Live sessions.", registry=REGISTRY)
sessions_expired = _counter("agent_sessions_expired_total", "Sessions evicted by TTL or capacity.")
http_requests = _counter("api_http_requests_total", "API HTTP requests.", ("route", "method", "status"))
http_request_seconds = _histogram("api_http_request_seconds", "API HTTP request latency.", LATENCY_BUCKETS)
rate_limited = _counter("api_rate_limited_total", "Requests rejected by the per-user rate limit.")

_TOOL_PLACEHOLDER = "<non-tool>"


def record_trace_event(event: dict[str, Any]) -> None:
    """Update metrics from one Phase 3 trace event. Mirrors graph.py's trace emission exactly."""
    kind = event.get("type")
    if kind == "llm":
        llm_latency.observe(event.get("latency_s", 0.0))
        llm_tokens.labels(direction="input").inc(event.get("input_tokens", 0))
        llm_tokens.labels(direction="output").inc(event.get("output_tokens", 0))
        if event.get("error"):
            llm_errors.inc()
    elif kind == "tool_call":
        tool = _safe_tool(event.get("name"))
        decision = str(event.get("decision", "unknown"))
        tool_calls.labels(tool=tool, decision=decision).inc()
        if decision == "block":
            guardrail_blocks.labels(layer=event.get("layer") or "none", code=event.get("code") or "none").inc()
    elif kind == "confirmation":
        approved = set(event.get("approved", []))
        for action_id in event.get("requested", []):
            confirmations.labels(outcome="approved" if action_id in approved else "declined").inc()
    elif kind == "execution":
        tool = _safe_tool(event.get("name"))
        tool_executions.labels(tool=tool, status="ok" if event.get("ok") else "error").inc()
    elif kind == "step_limit":
        step_limit_hits.inc()


def record_guardrail_check(seconds: float) -> None:
    guardrail_check.observe(seconds)


def record_bank_call(service: str, status: str, retries: int = 0) -> None:
    bank_calls.labels(service=_safe_service(service), status=status).inc()
    if retries:
        bank_retries.labels(service=_safe_service(service)).inc(retries)


def _safe_tool(name: Any) -> str:
    from agent.tools import REGISTRY

    return name if isinstance(name, str) and name in REGISTRY else _TOOL_PLACEHOLDER


def _safe_service(name: Any) -> str:
    known = {"accounts", "cards", "disputes", "risk", "payments"}
    return name if name in known else "unknown"


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def registered_metric_names() -> set[str]:
    """Base names of every metric family in the app registry (for the dashboard/alert validation test)."""
    names: set[str] = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
    return names


def metric_label_names() -> dict[str, tuple[str, ...]]:
    """Declared label names per metric family, so a cardinality test catches a bad label even with no samples yet."""
    out: dict[str, tuple[str, ...]] = {}
    for collector in list(REGISTRY._collector_to_names):  # noqa: SLF001 (stable prometheus_client internal)
        name = getattr(collector, "_name", None)
        if name:
            out[name] = tuple(getattr(collector, "_labelnames", ()))
    return out
