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
- [x] Graph: agent → guard → confirm (`interrupt()`) → execute loop
- [x] L6: every state-changing action requires human confirmation; approval executes the stored validated args
- [x] Rejected confirmation and blocked calls return to the model as error ToolMessages
- [x] Retries for transient service errors (client, idempotent), global step limit, LLM failure → fallback reply
- [x] PAN redaction in final replies (tool outputs only ever carry last4; structured logs arrive in phase 7)
- [x] Tool output treated as untrusted data (system prompt + `memo_untrusted`; prompt-injection test with the
      BookNook transaction: contained with guardrails on, money moves with guardrails off)
- [x] LLM reached only through an OpenAI-compatible endpoint (env-configured); scripted/replay LLM used only in tests
- [x] End-to-end tests with the scripted LLM for dispute, freeze/unfreeze, and anomaly-flag flows
- [x] Session rules: thread pinned to user, no send while confirmation pending, no resume with nothing pending
- [x] Mutation checks: executing raw model args and skipping the confirm node both make tests fail
- [x] `make chat` CLI; optional live smoke test (`make test-live`, excluded from CI)

## Phase 4 — Evaluation suite
- [x] Scenario generator: 69 scenarios across 11 categories, deterministic, written to disk (CI checks it is fresh)
- [x] Stratified dev/test split (≥1 dev, ≥2 test per category); dev for iteration, test for reported numbers
- [x] Simulated customer (multi-turn follow-ups, approves confirmations only for the scenario's tools)
- [x] Grader compares backend end-state snapshot diff with the expected diff (plus proposals, reads, clarification,
      reply checks) and tags failures automatically
- [x] Metrics: task success with Wilson 95% CI, pass^k, tool-selection accuracy, argument accuracy, unsafe-attempt,
      unauthorized- and unintended-change rates, guardrail blocks, latency, tokens, reliability, breakdowns
- [x] Oracle self-check: reference trajectories = 100% (also under 20% faults); do-nothing agent = 0% (tests)
- [x] Negative controls (wrong card, injection with guards on/off, over-eager flagging) + grader mutation check
- [x] `make eval-dev` / `make eval-test` write `results/<run_name>/summary.json` and `report.md`; threshold gate

## Phase 4.5 — Prompt and tool-design iteration (measured with the Phase 4 harness)
- [x] Hard rules recorded in `CLAUDE.md`; dirty-tree guard for model runs on test scenarios (`--allow-dirty`)
- [x] Leakage test: prompt, tool schemas and model-facing messages contain no seed-specific content (found and fixed a
      seed-id leak in v1 → baseline v1′)
- [x] v1 and v1′ re-run on dev with the protocol settings (Ollama qwen2.5:7b, temperature 0.3, 3 trials, concurrency 1)
- [x] 4 versions (v2–v5), each committed before its dev run; keep/revert decided on dev numbers only; final decision
      rule pre-registered before v5
- [x] Unauthorized-change rate 0 in every run; every candidate that regressed a safety metric was rejected
- [x] TEST split run exactly twice (v1′ and the best rejected candidate v5, identical settings); before/after table
      with Wilson CIs and the full gate on held-out data
- [x] `docs/PROMPT_ITERATIONS.md` (one section per version) and `docs/PHASE4_5_SUMMARY.md`
- [x] `make lint`, `make test`, `make oracle` (still 100%) pass at the end

## Phase 5 — Guardrail red team
- [x] Pre-registered plan (`docs/REDTEAM_PLAN.md`) committed before any code or result
- [x] Generated guardrail decision corpus (944 calls, 7 attack classes): 766/766 malicious blocked, 0 legitimate
      wrongly blocked (Gate A)
- [x] Hijacked-model end-to-end grid (776 attacks × guards on/off × rubber-stamp/careful): guards on + careful = 0
      state changes (Gate B); guards off = 192 changes + 35 exposures
- [x] Careful-customer metric quantifies what L6 adds (unintended-change rate 0.76% → 0% for v5 on TEST)
- [x] Indirect prompt-injection suite (8 families) and direct jailbreak suite (26 attacks), model in the loop,
      guards on vs off; honest negative recorded (system prompt is extractable)
- [x] `make redteam` writes `results/redteam/`, gated, in CI; `make redteam-llm` (C/D/E) never in CI
- [x] Mutation checks (L3, confirm node, careful customer); `docs/PHASE5_SUMMARY.md` with resume-ready facts

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
