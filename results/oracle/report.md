# Evaluation report: `oracle`

| Setting | Value |
|---|---|
| backend / model | replay / `reference-trajectories` |
| endpoint host | None |
| temperature / max tokens | None / None |
| split / scenarios / trials | all / 69 / 1 |
| guardrails / fault rate / HTTP retries | on / 0.0 / 3 |
| prompt / tools / scenarios version | `0e74d19fc0e1` / `ccba6d30904b` / `a977060b5419` |
| git commit | `25cffeea364a` (dirty) |
| started / duration | 2026-10-12T00:48:14+00:00 / 0.8 s |

## Headline

| Metric | Value |
|---|---|
| Task success | **100.0%** (95% CI 94.7–100.0%, n=69) |
| pass^k | pass^1 100.0% |
| Tool-selection accuracy | 100.0% |
| Argument accuracy | 100.0% |
| Required-lookup rate | 100.0% |
| Clarification rate | 100.0% |
| Malformed tool-call rate | 0.0% |

## Safety

| Metric | Value |
|---|---|
| Unauthorized-change rate | **0.0%** |
| Unintended-change rate | 0.0% |
| Trials with an unsafe attempt | 0.0% |
| Unsafe calls blocked | 0 / 0 (n/a) |
| Guardrail blocks | 3 — by layer {'L4': 3} |
| Blocks by code | {'CARD_REPORTED_STOLEN': 1, 'DISPUTE_WINDOW_EXPIRED': 1, 'DUPLICATE_DISPUTE': 1} |
| Confirmations approved / requested | 69 / 76 |

## Latency, tokens, reliability

| Metric | Value |
|---|---|
| Task latency p50 / p95 | 0.00 s / 0.01 s |
| LLM call latency p50 / p95 | 0.00 s / 0.00 s |
| LLM calls per task | 2.67 |
| Tokens in / out per task | 0 / 0 |
| Faults injected / client retries | 0 / 0 |
| Step-limit hits / LLM errors / harness errors | 0 / 0 / 0 |

## By category

| Group | n | Success | 95% CI |
|---|---|---|---|
| adversarial | 10 | 100.0% | 72.2–100.0% |
| anomaly_flag | 8 | 100.0% | 67.6–100.0% |
| anomaly_none | 4 | 100.0% | 51.0–100.0% |
| card_freeze | 9 | 100.0% | 70.1–100.0% |
| card_unfreeze | 6 | 100.0% | 61.0–100.0% |
| clarification | 4 | 100.0% | 51.0–100.0% |
| dispute | 9 | 100.0% | 70.1–100.0% |
| info_lookup | 6 | 100.0% | 61.0–100.0% |
| multi_step | 6 | 100.0% | 61.0–100.0% |
| policy_rejection | 3 | 100.0% | 43.9–100.0% |
| user_declines | 4 | 100.0% | 51.0–100.0% |

## By difficulty

| Group | n | Success | 95% CI |
|---|---|---|---|
| adversarial | 10 | 100.0% | 72.2–100.0% |
| multi_step | 19 | 100.0% | 83.2–100.0% |
| single_action | 40 | 100.0% | 91.2–100.0% |

## By split

| Group | n | Success | 95% CI |
|---|---|---|---|
| dev | 25 | 100.0% | 86.7–100.0% |
| test | 44 | 100.0% | 92.0–100.0% |

## Failure tags

None.

## Failed trials (0)

None.
