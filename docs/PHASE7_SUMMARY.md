# Phase 7 — Implementation Summary

**Status:** complete · **Date:** 2026-10-12

Phase 7 adds the agent HTTP API, Prometheus metrics, a structured JSON audit log, and a Grafana dashboard with alert
rules. The agent's decisions, prompts, guardrails and scenarios are unchanged. Only the monitoring stack
(Prometheus + Grafana) runs in Docker; containerising the app and CI/CD are Phase 8.

## 1. What was built

| File | Role |
|---|---|
| `api/server.py` | `create_api()`: JWT auth, sessions, the six endpoints, rate limiting, per-session locks, metrics + audit wiring |
| `api/app.py` | Host entrypoint (`uvicorn api.app:app`): builds the app from env, in-process bank, readiness pings the LLM |
| `api/audit.py` | `AuditLog`: one PAN-redacted JSON line per event; message length, never text |
| `agent/telemetry.py` | 17 Prometheus metric families; `record_trace_event` driven from the graph's trace events |
| `agent/client.py` | `retry_scope` contextvar → per-request retry accounting (Phase 3 fix); `agent/graph.py` uses it |
| `scripts/mint_token.py`, `scripts/demo.py` | dev token minting; the 5-step live demo (`make demo`) |
| `monitoring/` | `prometheus.yml`, `alerts.yml` (7 rules), `compose.yml`, Grafana provisioning, `dashboards/banking-agent.json` |

Make targets: `api`, `monitoring-up`, `monitoring-down`, `demo`.

## 2. API contract

- **Identity: HS256 JWT only.** `jwt.decode(..., algorithms=["HS256"], options={"require": ["exp", "sub"]})`;
  `user_id = sub`, never from the body or the model. `ApiConfig.from_env` refuses to start without `AUTH_SECRET`
  unless `DEV_MODE=1`.
- **Endpoints:** create session, post message, confirm, get session (turns + pending, no history), `/healthz`,
  `/readyz`, `/metrics`.
- **Errors:** 401 (missing/bad/expired/`alg=none`/wrong-secret/missing-exp/missing-sub); **404 identical** for a
  missing session and one owned by another user; 409 (pending confirmation, nothing-pending confirm, or a concurrent
  request on the same session); 422 (validation); 429 (per-user in-memory rate limit).
- **LLM outage during a turn → HTTP 200, `degraded: true`, fallback reply** (the turn completed; a retry could
  resend). `/readyz` → 503 while the LLM is unreachable; the message endpoint is never 503.
- **Concurrency:** a per-session lock; a second simultaneous request on one session gets 409.
- **Sessions:** in-memory, idle TTL (default 30 min) + max count; expiry → `agent_sessions_expired_total`.
  Persistence is Phase 10.
- **Per-request retry accounting:** a contextvar (`retry_scope`) isolates each request's retries even when sessions
  share a client, fixing the Phase 3 cross-session limitation. Verified with two concurrent sessions.

## 3. Metrics and audit

- 17 families (listed in `CLAUDE.md`), **bounded labels only** — tool names, layers/codes, decisions, services, HTTP
  route templates (never the concrete path), fixed enums. **Never user_id/session_id/free text.** Driven by
  `record_trace_event` at the same points the graph emits trace events, so trace and metrics agree.
- Audit log: session_start, user_message (length only), tool_call (validated args), confirmation, execution, reply
  (length only), session_end — each with `ts, request_id, session_id, user_id`, PAN-redacted, never full tool
  results or other customers' data.

## 4. Monitoring

- `monitoring/compose.yml` runs Prometheus (v2.54.1) + Grafana (11.2.0) only; they scrape the host API on `:8080`
  and vLLM on `:8000` (Phase 9).
- `alerts.yml`: 7 rules — turn p95 > 15s/10m; guardrail block rate > 3× its 6h baseline; repeated NOT_OWNER in 15m;
  tool error ratio > 5%; LLM error rate > 2%; high downstream retry rate; vLLM queue backlog.
- Dashboard: an Agent row (latency, tool calls, blocks, confirmations, errors, tokens, bank calls/retries, sessions,
  API requests) and a vLLM row (TTFT, request latency, running/waiting, throughput, KV-cache). **vLLM panel metric
  names are marked "verify against vLLM 0.31.0 /metrics in Phase 9"** (GPU_RUNBOOK §0a).

## 5. Verification (this Mac, Docker working)

| Check | Result |
|---|---|
| API tests (`tests/test_api.py`, 23) | auth (alg=none, wrong secret, missing exp/sub, expired), session flow, 404 same-body, 409 pending/concurrent, 429, 200-degraded on LLM outage, readyz 503, validation, per-request retry isolation across two concurrent sessions |
| Telemetry (`tests/test_telemetry.py`, 5) | exact counter deltas (freeze → `tool_calls{freeze_card,confirm}` +1, `confirmations{approved}` +1, `tool_executions{freeze_card,ok}` +1); route label is a template not the path; **label-cardinality guard on declared label names** |
| Audit (`tests/test_audit.py`, 5) | PAN redaction; no message text (length only); identity on every line; one tool_call+confirmation+execution triple per confirmed write; no tool results logged |
| Monitoring (`tests/test_monitoring.py`, 4) | every agent metric in the dashboard/alerts PromQL is registered; vLLM metrics namespaced/external; **`promtool check rules`/`check config` pass via Docker** |
| promtool (direct) | `alerts.yml`: 7 rules found, SUCCESS; `prometheus.yml`: valid |
| **Live Grafana** | `docker compose -f monitoring/compose.yml up`; API on host with Ollama `qwen2.5:7b`; Prometheus target `api → up`; drove 10 conversations; Prometheus showed 9 tool calls, 4 guardrail blocks, 28 HTTP requests, 10 active sessions; dashboard rendered with data → `docs/img/grafana.png` |

## 6. Mutation checks (each restored)

| Mutation | Expected | Result |
|---|---|---|
| Remove the session-ownership check | an API test fails | `test_other_users_session_is_404_same_as_missing` failed |
| Add `user_id` as a metric label | the cardinality test fails | `test_no_metric_uses_a_high_cardinality_or_identity_label` failed (after strengthening it to check **declared** label names, so it catches the bad label even before a sample is emitted) |

## 7. Deviation / notes

- The two changes requested mid-phase are implemented: (1) LLM outage → 200 + `degraded`, not 503; `/readyz` 503.
  (2) JWT decode requires `exp`+`sub`, rejects `alg=none`/wrong-secret/missing-exp/expired (all tested).
- The cardinality mutation initially did **not** fail because a label added to a Counter with no emitted samples
  shows no sample labels; the test was strengthened to inspect declared label names (`metric_label_names()`), which
  catches it. Noted for honesty.
- `PyJWT>=2.15,<3` added to `requirements.txt`.

## 8. Definition of done (from `docs/PLAN.md`)

- [x] Agent HTTP API: token-only identity, sessions, confirmation endpoint, health/ready/metrics, full error matrix,
      per-session lock, per-request retry accounting
- [x] Prometheus metrics (bounded labels) driven from the graph's trace events; exact-delta test
- [x] Structured PAN-redacted JSON audit log; redaction + no-text + one-triple tests
- [x] Grafana dashboard JSON + 7 alert rules under `monitoring/`; PromQL validated; promtool passes; live dashboard
      with data captured to `docs/img/grafana.png`
- [x] `make api`, `make monitoring-up`, `make demo`; both mutation checks

**Next:** Phase 8 — Docker, docker-compose, CI/CD (lint, tests, red-team gate, eval gate, image build, deploy).
