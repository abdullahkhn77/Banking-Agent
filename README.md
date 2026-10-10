# Guardrailed Multi-Step Banking Support Agent

A LangGraph agent that carries out multi-step banking support tasks (disputing a transaction, freezing or unfreezing a
card, detecting and flagging anomalous spending) across five simulated FastAPI microservices. Layered guardrails
(allow-list, strict validation, ownership checks, business policy, action budget, and human confirmation) stop
unauthorized tool calls. An evaluation suite grades the agent on backend end state to measure tool-selection and
task-completion reliability. The LLM is served through vLLM (FP16 vs AWQ 4-bit) with CI/CD and Prometheus/Grafana
monitoring. Every reported metric is produced by a script and saved under `results/`.

> **Status: phase 0 (foundation).** Repo layout, tooling, and CI only — no features yet. See [docs/PLAN.md](docs/PLAN.md).

## Setup

Requires Python 3.11+ (3.12 recommended) and `make`. Uses [uv](https://github.com/astral-sh/uv) if installed,
otherwise `venv` + `pip`.

```bash
make install   # create .venv and install requirements-dev.txt
make lint      # ruff check + format check
make test      # pytest
make help      # list all targets
```
