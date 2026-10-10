.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV ?= .venv
PY_VERSION ?= 3.12
BIN := $(VENV)/bin
PYTHON := $(BIN)/python

# Placeholder for targets owned by a later phase: $(call todo,<phase>)
todo = @echo "$@: not implemented yet (phase $(1))"

.PHONY: help install lint format test serve-bank \
	eval redteam serve-fp16 serve-awq bench api monitoring-up docker-build compose-up report

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

test: ## Run the test suite
	$(PYTHON) -m pytest

# ---- Phase 1 -----------------------------------------------------------------

serve-bank: ## Run the simulated bank on :9000 (all 5 services; SERVICE=<name> runs one)
	$(BIN)/uvicorn bank_services.app:app --port 9000

# ---- Later phases: placeholders ----------------------------------------------

eval: ## Run the evaluation suite -> results/<run_name>/ (phase 4)
	$(call todo,4)

redteam: ## Run the guardrail red-team suite (phase 5)
	$(call todo,5)

serve-fp16: ## Serve the LLM with vLLM, FP16 (phase 6)
	$(call todo,6)

serve-awq: ## Serve the LLM with vLLM, AWQ 4-bit (phase 6)
	$(call todo,6)

bench: ## Load-benchmark vLLM on the recorded agent workload (phase 6)
	$(call todo,6)

api: ## Run the agent HTTP API (phase 7)
	$(call todo,7)

monitoring-up: ## Start Prometheus + Grafana (phase 7)
	$(call todo,7)

docker-build: ## Build container images (phase 8)
	$(call todo,8)

compose-up: ## Start the full stack with docker compose (phase 8)
	$(call todo,8)

report: ## Build comparison report + RESUME_METRICS.md from results/ (phase 9)
	$(call todo,9)
