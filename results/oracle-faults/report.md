# Evaluation report: `oracle-faults`

| Setting | Value |
|---|---|
| backend / model | replay / `reference-trajectories` |
| endpoint host | None |
| temperature / max tokens | None / None |
| split / scenarios / trials | all / 69 / 3 |
| guardrails / fault rate / HTTP retries | on / 0.2 / 5 |
| prompt / tools / scenarios version | `0e74d19fc0e1` / `3417d449ae9e` / `a977060b5419` |
| git commit | `242edb0e12e1` (dirty) |
| started / duration | 2026-10-11T00:09:55+00:00 / 2.5 s |

## Headline

| Metric | Value |
|---|---|
| Task success | **100.0%** (95% CI 98.2–100.0%, n=207) |
| pass^k | pass^1 100.0%, pass^2 100.0%, pass^3 100.0% |
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
| Guardrail blocks | 9 — by layer {'L4': 9} |
| Blocks by code | {'CARD_REPORTED_STOLEN': 3, 'DISPUTE_WINDOW_EXPIRED': 3, 'DUPLICATE_DISPUTE': 3} |
| Confirmations approved / requested | 207 / 228 |

## Latency, tokens, reliability

| Metric | Value |
|---|---|
| Task latency p50 / p95 | 0.01 s / 0.01 s |
| LLM call latency p50 / p95 | 0.00 s / 0.00 s |
| LLM calls per task | 2.67 |
| Tokens in / out per task | 0 / 0 |
| Faults injected / client retries | 266 / 266 |
| Step-limit hits / LLM errors / harness errors | 0 / 0 / 0 |

## By category

| Group | n | Success | 95% CI |
|---|---|---|---|
| adversarial | 30 | 100.0% | 88.6–100.0% |
| anomaly_flag | 24 | 100.0% | 86.2–100.0% |
| anomaly_none | 12 | 100.0% | 75.8–100.0% |
| card_freeze | 27 | 100.0% | 87.5–100.0% |
| card_unfreeze | 18 | 100.0% | 82.4–100.0% |
| clarification | 12 | 100.0% | 75.8–100.0% |
| dispute | 27 | 100.0% | 87.5–100.0% |
| info_lookup | 18 | 100.0% | 82.4–100.0% |
| multi_step | 18 | 100.0% | 82.4–100.0% |
| policy_rejection | 9 | 100.0% | 70.1–100.0% |
| user_declines | 12 | 100.0% | 75.8–100.0% |

## By difficulty

| Group | n | Success | 95% CI |
|---|---|---|---|
| adversarial | 30 | 100.0% | 88.6–100.0% |
| multi_step | 57 | 100.0% | 93.7–100.0% |
| single_action | 120 | 100.0% | 96.9–100.0% |

## By split

| Group | n | Success | 95% CI |
|---|---|---|---|
| dev | 75 | 100.0% | 95.1–100.0% |
| test | 132 | 100.0% | 97.2–100.0% |

## Failure tags

None.

## Failed trials (0)

None.
