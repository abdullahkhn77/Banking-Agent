# Careful-customer comparison (C): v5

prompt `47c197da48b7` · tools `ccba6d30904b` · model `qwen2.5:7b` digest `845dbda0ea48ed74` · split test · 3 trials. tool run `test-ollama-qwen7b-v5`, careful run `redteam-careful-v5`. Plan: `docs/REDTEAM_PLAN.md`.

The **tool** customer approves any action whose tool the customer wanted (the official agent-only number). The **careful** customer also requires the confirmation summary to match the request. The difference is what the human confirmation step (L6) adds on top of the agent alone.

| Metric | tool customer | careful customer |
|---|---|---|
| Task success (95% CI) | 76.5% (68.6–82.9%) | 65.1% (56.7–72.8%) |
| Unintended-change rate | 0.8% | 0.0% |
| Unauthorized-change rate | 0.0% | 0.0% |
| Confirmations approved / requested | 106/124 | 86/107 |

**What L6 adds:** the careful customer's rejections reduce the unintended-change rate by **0.8%** (from 0.8% to 0.0%).

This is an additional measurement. It does not change the Phase 4.5 decision: production stays v1′.
