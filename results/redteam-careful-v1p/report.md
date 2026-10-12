# Careful-customer comparison (C): v1p

prompt `0e74d19fc0e1` · tools `ccba6d30904b` · model `qwen2.5:7b` digest `845dbda0ea48ed74` · split test · 3 trials. tool run `test-ollama-qwen7b-v1p`, careful run `redteam-careful-v1p`. Plan: `docs/REDTEAM_PLAN.md`.

The **tool** customer approves any action whose tool the customer wanted (the official agent-only number). The **careful** customer also requires the confirmation summary to match the request. The difference is what the human confirmation step (L6) adds on top of the agent alone.

| Metric | tool customer | careful customer |
|---|---|---|
| Task success (95% CI) | 28.8% (21.8–37.0%) | 28.0% (21.1–36.2%) |
| Unintended-change rate | 0.0% | 0.0% |
| Unauthorized-change rate | 0.0% | 0.0% |
| Confirmations approved / requested | 29/34 | 27/34 |

**What L6 adds:** the careful customer's rejections reduce the unintended-change rate by **0.0%** (from 0.0% to 0.0%).

This is an additional measurement. It does not change the Phase 4.5 decision: production stays v1′.
