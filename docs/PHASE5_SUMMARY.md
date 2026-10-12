# Phase 5 — Implementation Summary

**Status:** complete · **Date:** 2026-10-11 to 2026-10-12 · **Pre-registration:** `docs/REDTEAM_PLAN.md` (committed
`b076daf`, before any Phase 5 code).

Phase 5 red-teams the guardrails and the agent. It adds **measurement only**: the production prompt (v1′), the
scenarios, the grader's success criteria, and all guardrail decisions are unchanged. No prompt or tool change was
made on the basis of any Phase 5 result.

**Headline results:**
- **Guardrail decision corpus (A):** 766/766 malicious calls blocked, 178/178 legitimate calls get exactly their
  expected decision. Gate A **PASS**.
- **Hijacked-model grid (B):** guards ON + careful customer = **0 successful attacks** of 776. Gate B **PASS**.
  Guards OFF = 192 state-changing attacks and 35 data exposures, which quantifies what the guardrails prevent.
- **What L6 adds (C):** the careful customer removed v5's residual unintended-change rate on TEST (0.76% → 0%).
- **Indirect injection (D):** `qwen2.5:7b` followed **0 of 120** injected payloads per run (all 5 runs, including
  guards-off) and answered the real question in ~91–96% of conversations.
- **Direct jailbreak (E):** unsafe-attempt rate 0%; no money moved, no account closed, no other-customer data
  leaked. **But the system prompt is extractable:** 2 of 4 reveal attacks reproduced large spans of it.

Every number below is in a committed `results/.../summary.json`; the resume-ready facts section lists exact paths.

## 1. What was built

| File | Role |
|---|---|
| `evals/redteam.py` | Parts A + B (deterministic): corpus generator, `GuardrailEngine` runner, hijacked-model grid, gates |
| `evals/redteam_report.py` | A + B report (from summary.json only) |
| `evals/injection.py` | Part D red-team overlay: 8 payload families, 4 targets, benign requests |
| `evals/jailbreak.py` | Part E: 26 user-side attacks; prompt-leak n-gram check with instructed-speech exclusions |
| `evals/redteam_llm.py` | Parts D + E driver (model in the loop), resumable |
| `evals/redteam_llm_report.py` | D + E reports |
| `evals/careful_compare.py` | Part C comparison (tool vs careful customer) |
| `evals/resumable.py` | Append-only, fsync'd trial log; skips finished (case, trial) pairs on resume |
| `evals/runner.py`, `evals/run.py` | `--customer tool\|careful`, `--resume`, `--results-dir`, model digest in meta |
| `scripts/run_redteam_llm.sh` | The C/D/E batch (resumable, under `caffeinate -i`) |
| `tests/test_redteam.py`, `tests/test_careful_customer.py` | 15 + 7 tests |

**Make / CI:** `make redteam` (A + B, gated) and a separate CI `redteam` job that writes the report to the job
summary. `make redteam-llm` points at the batch script and is never run in CI.

## 2. Part A — guardrail decision corpus (deterministic)

Source: `results/redteam/summary.json`. 944 generated calls through `GuardrailEngine.check`.

| Class | Calls | Blocked at |
|---|---|---|
| legitimate | 142 | — (66 allow, 76 confirm) |
| not_allowlisted (transfers, admin, 6 hallucinated tools) | 90 | L1 |
| cross_account (every other customer's card/txn/dispute) | 260 | L3 |
| identity_smuggling (user_id/customer_id/account_id on all 11 tools) | 198 | L2 |
| invalid_input (28 templates × 6) | 168 | L2 |
| policy (stolen, window, duplicate, amount, flagged, frozen) | 32 | L4 |
| action_budget (writes_so_far 0–8) | 54 | L5 (18) |

- **Malicious calls blocked: 766/766 (100%).**
- **Legitimate calls with exactly the expected decision: 178/178 (100%)** — reads `allow`, writes `confirm`.
- Decision accuracy (decision and block code): 100%; 0 mismatches.
- Check latency (in-process diagnostic): no-lookup path p50 0.004 ms / p95 0.011 ms; lookup path p50 0.68 ms /
  p95 1.40 ms.
- **Gate A: PASS.**

## 3. Part B — hijacked-model grid (deterministic)

Source: `results/redteam/summary.json`. 776 attacks per cell; a scripted model emits one malicious message while the
customer's request is a harmless read.

| Cell | Successful attacks | Data exposures |
|---|---|---|
| guards ON + rubber-stamp | 22 | 0 |
| **guards ON + careful** | **0** | 0 |
| guards OFF + rubber-stamp | 192 | 35 |
| guards OFF + careful | 192 | 35 |

- The 22 ON + rubber-stamp successes are all **`unrequested_write`**: valid writes on the customer's *own*
  resources that pass L1–L5 and are stopped only by L6 with a careful customer. The careful cell confirms it: 0.
- Guards OFF: 54 admin calls, 110 cross-account writes, 22 unrequested writes and 6 budget floods change state; 35
  cross-account reads leak a record. Even OFF, tool schemas still block all 198 identity-smuggling and 168
  invalid-input attacks, and the backend rules refuse all 32 policy attacks.
- **Gate B: PASS** (ON + careful = 0 in every class).

## 4. Part C — what L6 adds (model in the loop)

TEST split, Ollama `qwen2.5:7b`, 3 trials (132 conversations). The `tool` customer is the Phase 4.5 number; the
`careful` customer also requires the confirmation summary to match the request.

| Version | Customer | Task success (95% CI) | Unintended-change rate |
|---|---|---|---|
| v1′ | tool | 28.8% (21.8–37.0%) | 0.0% |
| v1′ | careful | 28.0% (21.1–36.2%) | 0.0% |
| v5 | tool | 76.5% (68.6–82.9%) | **0.76%** (1/132) |
| v5 | careful | 65.1% (56.7–72.7%) | **0.0%** |

Sources: `results/redteam-careful-v1p/summary.json`, `results/redteam-careful-v5/summary.json` (comparison), and the
Phase 4.5 `results/test-ollama-qwen7b-{v1p,v5}` for the tool numbers.

- **What L6 adds:** for v5, the careful customer's rejections removed the residual unintended-change rate
  (0.76% → 0%). For v1′ there was nothing to remove (it was already 0%, because v1′ rarely acts).
- **Cost of the careful customer:** v5 task success falls 76.5% → 65.1%, because a careful customer declines
  genuine actions whose summary it judges not to match, and the agent does not always recover. This is the price of
  the stricter human in the loop, not a guardrail failure.
- This is an additional measurement. **It does not change the Phase 4.5 decision: production stays v1′.**

## 5. Part D — indirect prompt injection (model in the loop)

5 runs × 40 conversations × 3 trials = 600 conversations. A payload is placed in one recent transaction memo; a
benign request makes the model read it.

| Run | Followed the injection | State changed | Answered the real question |
|---|---|---|---|
| v1′ ON, rubber-stamp | 0/120 | 0/120 | 95.0% |
| v1′ ON, careful | 0/120 | 0/120 | 95.0% |
| v5 ON, rubber-stamp | 0/120 | 1/120 (0.83%) | 95.8% |
| v5 ON, careful | 0/120 | 0/120 | 90.8% |
| **v1′ OFF, rubber-stamp** | **0/120** | 0/120 | 96.7% |

Sources: `results/redteam-injection-*/summary.json`.

- **The model followed 0 of 120 injected actions in every run, including guards-off.** For this model and these 8
  payload families (direct, fake-system, JSON tool-call, claimed authority, urgency, hide-from-customer, split, and
  Spanish), `qwen2.5:7b` treated memo text as data and answered the user's actual question.
- This is a **model-robustness** result, not a guardrail result: because the model never proposed the harmful
  action, L1–L5 were never exercised (blocks by layer were empty). Part B is the guardrail backstop — it forces the
  model to emit the attack and shows the guardrails then block it.
- **The one v5 state change** was not following the injection (`followed=False`): the model opened a dispute on the
  customer's *own* transaction `txn_1006_n003` — an unrequested own-resource write, the L6 category from Part B. The
  careful run shows it at 0.

## 6. Part E — direct jailbreak (model in the loop)

3 runs × 26 attacks × 3 trials = 234 conversations, rubber-stamp customer. Sources:
`results/redteam-jailbreak-*/summary.json`.

| Run | Unsafe-attempt rate | State changed | Refusal rate | Data leak | Prompt-reveal overlap rate (control) |
|---|---|---|---|---|---|
| v1′ ON | 0% | 0/78 | 61.5% | 0/9 | 50.0% (control 0%) |
| v5 ON | 0% | 1/78 (1.3%) | 69.2% | 0/9 | 33.3% (control 0%) |
| v1′ OFF | 0% | 0/78 | 60.3% | 0/9 | 50.0% (control 0%) |

- **No money moved, no account was closed, no contact email changed, and no other customer's balance or card was
  leaked, in any run** (including guards-off). Blocks were L2 only (obfuscated and fake-tool attacks produced
  malformed calls), 10–11 per guarded run.
- The one v5 state change was again an unrequested flag on the customer's *own* transaction (`txn_1001_a01`), not a
  cross-customer or admin action.
- **Finding: the system prompt is extractable.** A prompt leak is **declared** for all three runs (reveal-attack
  overlap rate 33–50% vs a 0% control on refusals to other attacks). Two of the four reveal attacks reproduced large
  spans of the prompt (up to 197 and 387 word-8-grams). The prompt contains the agent's rules but **no credentials
  and no other customer's data**, so the impact is disclosure of the agent's instructions, not a data breach. A
  refusal keyword and leaked prompt text sometimes co-occur (the model refuses and then quotes the prompt anyway).

## 7. Mutation checks (each restored)

| Mutation | Expected | Result |
|---|---|---|
| Disable the L3 owner comparison | Gate A fails | malicious_block_rate 0.74 → FAIL |
| Skip the confirm node (auto-approve) | B's ON+careful shows successes | 22 + 6 successes → Gate B FAIL |
| Careful customer approves everything | a careful-customer test fails | 3 tests failed (wrong-card decline, user_declines, end-to-end) |

## 8. Hygiene

- Pre-registration (`docs/REDTEAM_PLAN.md`) committed before any code (`b076daf`).
- Model runs from clean worktrees: v1′ at `78ebdaa`, v5 from branch `redteam-v5` at `5ff3b67` (each verified by
  `prompt_version`/`tools_version` in its summary). `agent/` on `main` is byte-identical to v1′ (`fd5d14d`).
- Model digest `845dbda0ea48…` (Q4_K_M) recorded in every run's meta and **identical before and after** the batch.
- Runs are resumable (fsync'd append log; finished trials skipped on restart) — exercised during the batch.
- `make lint`, `make test` (427 passed), `make oracle` (100%, and 100% under 20% faults) and `make redteam`
  (Gates A + B pass) all green on the final commit.

## 9. Known gaps / future work

1. **System-prompt extraction (Part E).** The prompt is not confidential-robust. It holds no secrets today, so this
   is low-severity, but any future secret must never go in the prompt. A prompt-hardening iteration would be a
   separate, measured change with fresh held-out attacks.
2. **Model-level injection resistance is model-specific.** The 0/120 follow rate is for `qwen2.5:7b` and these
   payloads. A different or larger model, or adaptive payloads, could follow them — which is exactly why the
   guardrails (Part B) are the backstop, independent of the model.
3. **Careful customer is a simulation.** It approves by exact summary match. A real customer is noisier; the
   careful number is an upper bound on what a perfectly attentive customer achieves, and the tool number is the
   agent-alone lower bound. The truth is between them.
4. **The careful-customer cost** (v5 76.5% → 65.1%) suggests the agent should recover better after a decline; that
   is an agent-design question for a future phase, measured on dev.
5. **TEST is now also used for Part C.** Future prompt/tool iteration still needs fresh held-out scenarios
   (unchanged from the Phase 4.5 note).

## 10. Resume-ready facts

Each fact is a number that appears in a committed `summary.json`, with its path. These are reproducible
(`make redteam` for A/B; `scripts/run_redteam_llm.sh` for C/D/E).

- **Guardrail decision corpus: 766/766 malicious calls blocked (100%), 0 legitimate calls wrongly blocked, across
  944 calls in 7 attack classes.** → `results/redteam/summary.json` (`part_a.malicious_block_rate` = 1.0,
  `legitimate_wrongly_blocked` = 0, `legitimate_exact_rate` = 1.0).
- **Hijacked model, guardrails on with a human confirming: 0 of 776 attacks changed state.** → `results/redteam/summary.json`
  (`part_b.cells["on+careful"].successes` = 0).
- **Guardrails removed 192 state-changing attacks and 35 cross-customer data exposures** (guards-off vs on). →
  `results/redteam/summary.json` (`part_b.cells["off+rubber_stamp"].successes` = 192, `exposures` = 35).
- **The human-confirmation step (L6) drove the agent's unintended-change rate to 0% on the held-out test split**
  (0.76% → 0% for the strongest prompt). → `results/redteam-careful-v5/summary.json`
  (`careful_customer.unintended_change_rate` = 0.0, `l6_prevented_unintended_change_rate` = 0.0076).
- **Indirect prompt injection: the agent followed 0 of 120 injected instructions and still answered the user's real
  question 95% of the time** (`qwen2.5:7b`, guardrails on). → `results/redteam-injection-v1p-on-rubber_stamp/summary.json`
  (`metrics.overall.model_followed` = 0, `answered_question_rate` = 0.95).
- **Direct jailbreak suite (78 conversations): 0% unsafe-attempt rate, 0 state changes, 0 customer-data leaks.** →
  `results/redteam-jailbreak-v1p-on/summary.json` (`metrics.overall.unsafe_attempt_rate` = 0.0, `state_changes` = 0;
  `metrics.data_leak.leaks` = 0).
- **Honest negative: the system prompt is extractable — 50% of reveal attacks reproduced prompt text vs a 0%
  control.** → `results/redteam-jailbreak-v1p-on/summary.json` (`metrics.prompt_leak.reveal_rate` = 0.5,
  `control_rate` = 0.0, `leak_declared` = true).

All model runs: Ollama `qwen2.5:7b`, digest `845dbda0ea48…`, temperature 0.3, 3 trials, guardrails on unless noted.
These are local GGUF development numbers, not the reported vLLM result (phase 6/9).
