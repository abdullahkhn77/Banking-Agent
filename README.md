# Guardrailed Multi-Step Banking Support Agent

A LangGraph agent that carries out multi-step banking support tasks (disputing a transaction, freezing or unfreezing a
card, detecting and flagging anomalous spending) across five simulated FastAPI microservices. Layered guardrails
(allow-list, strict validation, ownership checks, business policy, action budget, and human confirmation) stop
unauthorized tool calls. An evaluation suite grades the agent on backend end state to measure tool-selection and
task-completion reliability. The LLM is served through vLLM (FP16 vs AWQ 4-bit) with CI/CD and Prometheus/Grafana
monitoring. Every reported metric is produced by a script and saved under `results/`.

> **Status: phase 4 complete.** Simulated bank (5 FastAPI microservices) with deterministic seed data and a rule-based
> anomaly detector; a retrying, idempotent bank client; the tool registry and layered guardrails (L1–L5); a
> LangGraph agent that pauses every state-changing action for human confirmation (L6); and an end-state evaluation
> suite (69 scenarios, oracle + negative controls). Reported model numbers come later from the vLLM test-split runs;
> see [docs/PLAN.md](docs/PLAN.md) and `results/`.

## Setup

Requires Python 3.11+ (3.12 recommended) and `make`. Uses [uv](https://github.com/astral-sh/uv) if installed,
otherwise `venv` + `pip`.

```bash
make install   # create .venv and install requirements-dev.txt
make lint      # ruff check + format check
make test      # pytest
make help      # list all targets
```

Run the simulated bank (all five services on http://localhost:9000, docs at `/docs`):

```bash
make serve-bank
SERVICE=cards .venv/bin/uvicorn bank_services.app:app --port 9001   # one service per process
```

Chat with the agent against a local model (e.g. [Ollama](https://ollama.com) with `qwen2.5:7b`):

```bash
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b make chat USER=u_1001
```

Evaluate (every number lands in `results/<name>/summary.json` and `report.md`):

```bash
make oracle                                                              # harness self-check (must be 100%)
make eval-dev BASE_URL=http://localhost:11434/v1 MODEL=qwen2.5:7b TRIALS=1 EVAL_CONC=1
```
