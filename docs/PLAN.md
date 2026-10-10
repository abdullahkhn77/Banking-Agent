# Project Plan

Guardrailed Multi-Step Banking Support Agent. Architecture rules live in `CLAUDE.md`; every phase must follow them.

**Global definition of done (applies to every phase):**
- [ ] `make lint` and `make test` pass locally and in CI
- [ ] Any number mentioned in docs comes from a file under `results/` written by a script
- [ ] `CLAUDE.md` "Current phase" line and this checklist are updated
- [ ] One commit (or PR) titled `Phase N: <summary>`

---

## Phase 0 — Foundation
- [x] Environment checked (Python 3.11+, git, make, docker)
- [x] Repo layout with empty packages (`__init__.py`)
- [x] `pyproject.toml` (metadata, ruff, pytest), `requirements*.txt` with pinned major versions, `.gitignore`
- [x] `Makefile` with working `install`, `lint`, `format`, `test`; placeholder targets for later phases
- [x] `CLAUDE.md` (architecture rules, phase plan, commands) and this plan
- [x] Smoke test imports every package; `make lint` and `make test` pass
- [x] GitHub Actions CI (ruff + pytest on push/PR, Python 3.12)
- [x] README stub; `git init` + first commit

## Phase 1 — Simulated microservices + seed data + anomaly detector
- [x] 5 FastAPI services (accounts, cards, disputes, risk, payments) under `/<name>/v1`
- [x] Runnable in one process or one-per-process via `SERVICE` env var
- [x] Deterministic seed (fixed RNG seed, `NOW = 2026-10-01T12:00:00Z`): ~6 customers, ~11 cards, ~250 transactions;
      re-running the seed yields byte-identical data (test)
- [x] All 9 planted cases present and asserted by tests
- [x] Domain rules enforced in services; `Idempotency-Key` honored on write endpoints (test: replay returns same result,
      no double write)
- [x] Fault injection (fail-before / fail-after apply) with tests
- [x] Payments transfers and admin ops (close account, change email) exist in the backend
- [x] State snapshot endpoint/helper usable by the grader
- [x] Rule-based anomaly detector with ground-truth tests: output must equal `EXPECTED_ANOMALIES` exactly for every
      account (checked by a test, not asserted from memory)
- [x] `make serve-bank` implemented (replaces the `seed`/`services` placeholders; the seed is built in memory)

## Phase 2 — Bank client + tool registry + guardrail engine
- [x] `BankClient`: prefix routing, 4xx never retried, 5xx/transport retried with backoff, one Idempotency-Key per
      write reused on every retry (test: "after" fault + retry applies exactly once)
- [x] Tool registry: agent allow-list excludes payments and admin ops (test)
- [x] Customer identity injected from session, never a tool argument (test: schema has no customer field)
- [x] L1 allow-list, L2 strict pydantic (`extra="forbid"`, ID regexes, enums, ranges), L3 ownership, L4 business policy
      (dispute window, stolen card, duplicate dispute, amount ≤ transaction), L5 action budget — each with unit tests
      for allow and block paths
- [x] Guard decision object records which layer blocked and why
- [x] Blocked calls produce a model-facing error message (`GuardResult.message`); phase 3 wraps it in a ToolMessage
- [x] Validated arguments are captured at proposal time for later execution
- [x] Engine never raises (fuzz test) and fails closed on lookup errors; ablation mode (`enabled=False`) available
- [x] Mutation check: disabling the L3 ownership comparison makes tests fail

## Phase 3 — LangGraph agent
- [ ] Graph: agent → guard → confirm (`interrupt()`) → execute loop
- [ ] L6: every state-changing action requires human confirmation; approval executes the stored validated args
- [ ] Rejected confirmation and blocked calls return to the model as error ToolMessages
- [ ] Retries for transient service errors, global step limit
- [ ] PAN redaction in all model-visible text and logs
- [ ] Tool output treated as untrusted data (system prompt + structure; test with the prompt-injection transaction)
- [ ] LLM reached only through an OpenAI-compatible endpoint (env-configured); scripted/replay LLM used only in tests
- [ ] End-to-end tests with the scripted LLM for dispute, freeze/unfreeze, and anomaly-flag flows

## Phase 4 — Evaluation suite
- [ ] Scenario generator: ~70 scenarios across 11 categories, deterministic, written to disk
- [ ] Simulated customer (answers follow-ups, approves/denies confirmations per scenario)
- [ ] Grader compares backend end-state snapshot diff with the expected diff (plus transcript checks)
- [ ] Metrics: task success with 95% CI, pass^k, tool-selection accuracy, argument accuracy, unsafe-change rate,
      unauthorized-change rate, latency, tokens
- [ ] Oracle self-check: reference trajectories = 100%, do-nothing agent = 0% (enforced by a test)
- [ ] `make eval` writes `results/<run_name>/summary.json` and `report.md`

## Phase 5 — Guardrail red team
- [ ] Generated attack corpus (unauthorized ownership, disallowed tools, schema smuggling, policy evasion,
      budget exhaustion, prompt injection via tool output)
- [ ] Hijacked-model end-to-end test: a model that always proposes attacks causes zero unauthorized state changes
- [ ] Guardrails on vs off comparison, results per layer
- [ ] `make redteam` writes results under `results/`

## Phase 6 — vLLM serving + benchmark
- [ ] Serving scripts for FP16 and AWQ 4-bit (same base model) behind an OpenAI-compatible endpoint
- [ ] Recorded agent workload (from eval LLM requests) replayed as the load benchmark
- [ ] Metrics: throughput, TTFT, latency p50/p95/p99, GPU memory, cost per 1K tasks (cost inputs from config)
- [ ] `make serve-fp16`, `make serve-awq`, `make bench` implemented; results under `results/`

## Phase 7 — Agent API + observability
- [ ] Agent HTTP API (session auth → customer identity, confirmation endpoint for interrupts)
- [ ] Prometheus metrics (requests, tool calls, guardrail blocks by layer, latency, tokens)
- [ ] Structured JSON audit log of every proposed/blocked/approved/executed action
- [ ] Grafana dashboard JSON + alert rules committed under `monitoring/`
- [ ] `make api` and `make monitoring-up` implemented

## Phase 8 — Docker + CI/CD
- [ ] Dockerfiles for services and agent API; `docker-compose.yml` for the full stack (+ Prometheus/Grafana)
- [ ] CI pipeline: lint, tests, red-team gate, eval gate (scripted thresholds), image build, deploy
- [ ] `make docker-build` and `make compose-up` implemented

## Phase 9 — GPU run + report
- [ ] Colab notebook runs vLLM FP16 and AWQ, the eval suite, red team, and benchmark end-to-end
- [ ] Comparison report generated from `results/`
- [ ] `RESUME_METRICS.md` generated by a script from `results/` (no hand-typed numbers)
- [ ] `make report` implemented
