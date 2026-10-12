# Inference benchmark: `bench-smoke-ollama`

> **smoke test, not a result**

served model `qwen2.5:7b` (requested `qwen2.5:7b`) · vLLM `n/a` · GPU `n/a` · host `localhost:11434` · commit `4258fd93b34d` (dirty)

workload `2355958587407077` (7 requests, source `/private/tmp/claude-501/-Users-abdullahkhan-Desktop-Banking-Agent/66a9495e-60d7-4b0d-9d09-df8a0147cd63/scratchpad/smoke-record/llm_requests.jsonl`) · prefix caching on · max_tokens 128 · headline concurrency 8 · started 2026-10-12T15:33:02+00:00. Plan: `docs/SERVING_PLAN.md`.

## Per concurrency level

| conc. | ok/att | err | TTFT p50 / p95 (s) | TPOT p50 | e2e p50 / p95 (s) | req/s | out tok/s | mean out tok | $/1M out |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 8/8 | 0 | 0.3476 / 1.142 | 0.0382 | 1.102 / 2.837 | 0.695 | 23.63 | 34 | n/a |
| 2 | 8/8 | 0 | 1.339 / 3.874 | 0.0372 | 2.714 / 4.207 | 0.701 | 23.83 | 34 | n/a |
