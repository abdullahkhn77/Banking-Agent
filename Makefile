.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV ?= .venv
PY_VERSION ?= 3.12
BIN := $(VENV)/bin
PYTHON := $(BIN)/python

# Placeholder for targets owned by a later phase: $(call todo,<phase>)
todo = @echo "$@: not implemented yet (phase $(1))"

.PHONY: help install lint format test test-live serve-bank chat scenarios oracle eval-dev eval-test redteam redteam-llm api monitoring-up monitoring-down demo \
	 serve-fp16 serve-awq bench compare-serving api monitoring-up docker-build compose-up report

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-15s %s\n", $$1, $$2}'

# ---- Phase 0: working targets ------------------------------------------------

install: ## Create .venv (if missing) and install dev requirements (uses uv when available)
	@if command -v uv >/dev/null 2>&1; then \
		[ -x $(PYTHON) ] || uv venv --python $(PY_VERSION) $(VENV); \
		uv pip install --python $(PYTHON) -r requirements-dev.txt; \
	else \
		[ -x $(PYTHON) ] || python$(PY_VERSION) -m venv $(VENV); \
		$(PYTHON) -m pip install --upgrade pip; \
		$(PYTHON) -m pip install -r requirements-dev.txt; \
	fi

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-fix lint issues and format code
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

test: ## Run the test suite (live LLM tests excluded)
	$(PYTHON) -m pytest

test-live: ## Live LLM smoke tests; set LLM_BASE_URL / LLM_MODEL (e.g. Ollama) first
	$(PYTHON) -m pytest -m live -s tests/test_live_llm.py

# ---- Phase 1 -----------------------------------------------------------------

serve-bank: ## Run the simulated bank on :9000 (all 5 services; SERVICE=<name> runs one)
	$(BIN)/uvicorn bank_services.app:app --port 9000

# ---- Phase 3 -----------------------------------------------------------------

# USER is also the shell's login name, so only values like u_1001 are used; anything else falls back to u_1001.
CHAT_USER = $(if $(filter u_%,$(USER)),$(USER),u_1001)

chat: ## Chat with the agent (USER=u_1001; LLM_BASE_URL / LLM_MODEL pick the model)
	$(PYTHON) -m scripts.chat --user $(CHAT_USER) --show-trace

# ---- Phase 4 -----------------------------------------------------------------

# Model under test for eval-dev / eval-test (override on the command line).
BASE_URL ?= http://localhost:8000/v1
PORT ?= 8000
COMPARE_ARGS ?=
MODEL ?= bank-agent-llm
TRIALS ?= 3
TEMP ?= 0
EVAL_CONC ?= 4
EVAL_ARGS = --backend openai --base-url $(BASE_URL) --model $(MODEL) --trials $(TRIALS) --temperature $(TEMP) --concurrency $(EVAL_CONC)

scenarios: ## Regenerate evals/scenarios.json from the seed
	$(PYTHON) -m evals.build_scenarios

oracle: ## Harness self-check: replay reference trajectories (normal + 20% faults); gate requires 100%
	$(PYTHON) -m evals.run --backend replay --split all --name oracle --gate oracle --quiet
	$(PYTHON) -m evals.run --backend replay --split all --trials 3 --fault-rate 0.2 --http-retries 5 \
		--name oracle-faults --gate oracle --quiet

eval-dev: ## Evaluate a model on the dev split (BASE_URL, MODEL, TRIALS, TEMP, EVAL_CONC)
	$(PYTHON) -m evals.run $(EVAL_ARGS) --split dev

eval-test: ## Evaluate a model on the test split with the threshold gate (reported numbers)
	$(PYTHON) -m evals.run $(EVAL_ARGS) --split test --gate thresholds

# ---- Phase 5 -----------------------------------------------------------------

redteam: ## Red team A+B: guardrail corpus + hijacked-model grid (deterministic, gated) -> results/redteam
	$(PYTHON) -m evals.redteam

redteam-llm: ## Red team C+D+E (model in the loop); set BASE_URL / MODEL; never run in CI
	@echo "C/D/E are launched by scripts/run_redteam_llm.sh (long-running, resumable). See docs/REDTEAM_PLAN.md."

# ---- Phase 6: vLLM serving + benchmark (GPU host; see docs/GPU_RUNBOOK.md) ----

# BENCH_* configure the benchmark; GPU_PRICE/SOURCE/DATE and WORKLOAD are set per run on the GPU host.
BENCH_NAME ?= bench
WORKLOAD ?=
GPU_PRICE ?=
BENCH_ARGS = --name $(BENCH_NAME) --base-url $(BASE_URL) $(if $(WORKLOAD),--workload $(WORKLOAD),) \
	$(if $(GPU_PRICE),--gpu-price $(GPU_PRICE),)

serve-fp16: ## Serve Qwen2.5-7B-Instruct (FP16) with vLLM (GPU host)
	serving/serve.sh fp16 $(PORT)

serve-awq: ## Serve Qwen2.5-7B-Instruct-AWQ (4-bit) with vLLM (GPU host)
	serving/serve.sh awq $(PORT)

bench: ## Load-benchmark a running vLLM endpoint (BENCH_NAME, WORKLOAD, GPU_PRICE, BASE_URL)
	$(PYTHON) -m serving.benchmark $(BENCH_ARGS)

compare-serving: ## Build results/SERVING_COMPARISON.md from the bench + eval summaries
	$(PYTHON) -m serving.compare $(COMPARE_ARGS)

# ---- Phase 7: API + observability --------------------------------------------

API_PORT ?= 8080

api: ## Run the agent HTTP API (AUTH_SECRET required; LLM_BASE_URL/LLM_MODEL pick the model)
	$(BIN)/uvicorn api.app:app --host 0.0.0.0 --port $(API_PORT)

monitoring-up: ## Start Prometheus + Grafana (scrape the host API on :8080); Grafana at :3000 (admin/admin)
	docker compose -f monitoring/compose.yml up -d

monitoring-down: ## Stop the monitoring stack
	docker compose -f monitoring/compose.yml down

demo: ## Scripted live demo against the API (LLM_BASE_URL/LLM_MODEL pick the model)
	$(PYTHON) -m scripts.demo

# ---- Later phases: placeholders ----------------------------------------------

docker-build: ## Build container images (phase 8)
	$(call todo,8)

compose-up: ## Start the full stack with docker compose (phase 8)
	$(call todo,8)

report: ## Build comparison report + RESUME_METRICS.md from results/ (phase 9)
	$(call todo,9)
