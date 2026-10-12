# Phase 6 Serving & Benchmark Plan (pre-registered)

Committed **before** any Phase 6 code. Everything here is fixed in advance; later deviations go in a deviation log
in `docs/PHASE6_SUMMARY.md` with a reason.

## 0. Scope

Phase 6 builds and *verifies locally* the vLLM serving scripts, the inference benchmark, and the comparison report.
**This machine has no GPU**, so the real numbers are produced in Phase 9 on a GPU (see `docs/GPU_RUNBOOK.md`). Phase
6 does not change the agent, prompts, guardrails or scenarios.

**Any Ollama / Mac number produced in this phase is a smoke test to prove the plumbing works, never a reported
result.** Every smoke artifact is labelled as such in its `meta` and report.

## 1. Models (pre-registered)

| Variant | Model id | dtype | Notes |
|---|---|---|---|
| `fp16` | `Qwen/Qwen2.5-7B-Instruct` | bf16 on Ampere/Ada, fp16 on T4 | the quality + latency baseline |
| `awq` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | — (AWQ 4-bit, official) | the 4-bit point |
| `fp8` (optional) | `Qwen/Qwen2.5-7B-Instruct` | fp8 | **Ada/Hopper only**; skipped elsewhere |
| `t4-fp16` (fallback) | `Qwen/Qwen2.5-3B-Instruct` | half | if only a T4 (16 GB) is available |
| `t4-awq` (fallback) | `Qwen/Qwen2.5-3B-Instruct-AWQ` | — | T4 fallback 4-bit |

The 7B FP16 model needs ~15 GB just for weights, which does not fit a 16 GB T4 with an 8192 context; the 3B
fallbacks are pre-registered so the Phase 9 comparison is still 7B-vs-7B *or* 3B-vs-3B, never mixed. Which pair ran
is recorded in every `summary.json`.

## 2. Fixed vLLM settings

```
--enable-auto-tool-choice --tool-call-parser hermes
--max-model-len 8192
--gpu-memory-utilization 0.90
(prefix caching ON by default; the ablation run adds --no-enable-prefix-caching)
--served-model-name bank-agent-llm
```

Env overrides allowed (recorded, not changing the defaults above): `BASE_MODEL`, `DTYPE`, `MAX_MODEL_LEN`,
`GPU_MEM_UTIL`, `TP` (tensor parallel), `EXTRA_ARGS`.

**vLLM version:** `requirements-gpu.txt` pins the current stable release **vLLM 0.31.0** (PyPI, checked 2026-10-12).
The serve script prints the installed vLLM version so Phase 9 records what actually ran.

## 3. Workload (identical across variants)

- The workload is the **exact chat requests recorded from one eval run**: system prompt + tool schemas + the
  multi-step message histories, exactly as the agent sent them (`evals/run.py --record-llm` →
  `llm_requests.jsonl`, OpenAI wire format).
- It is recorded **once**, from the **FP16 TEST run in Phase 9 with prompt v1′ (production)**, and then replayed
  **byte-identically** against every variant.
- **Why once, from FP16:** FP16 and AWQ must see byte-identical requests, or latency/throughput differences would be
  confounded by different prompts. Recording per-variant would let the model's own outputs change the histories.
- The benchmark shuffles the request list with a **fixed seed (20261012)** and replays the **same order** for every
  variant. A `workload_hash` (sha256 of the ordered request bodies) is written to `meta`; the comparison refuses to
  compare runs whose hashes differ.

## 4. Benchmark design

- Concurrency levels **1, 4, 8, 16**.
- Requests per level: **max(64, 4 × concurrency)**.
- **1 warm-up level** (concurrency 1) is run and **discarded** before the measured levels.
- **Streaming** responses (`stream=True`, `stream_options={"include_usage": true}`), so TTFT is measured.
- `max_tokens` 256, `temperature` 0, `tools` included in every request.
- The async client is OpenAI-compatible; requests are dispatched to hold exactly `concurrency` in flight.

### Metrics and formulas

Per concurrency level:
- **TTFT** (time to first token) p50 / p95: wall time from send to the first streamed delta that carries content
  **or** a tool-call fragment.
- **TPOT** (time per output token) p50: `(end_to_end − TTFT) / max(1, output_tokens − 1)`, averaged as a p50 over
  requests.
- **end-to-end** p50 / p95: send to final chunk.
- **requests/s**: completed requests / wall time of the level.
- **mean output tokens** per request (reported next to throughput): identical requests can still yield different
  output lengths across variants, which affects tok/s and cost, so throughput is always read together with this.
- **output tok/s**: Σ output tokens / wall time; **total tok/s**: Σ (input+output) tokens / wall time.
- **error rate**: failed requests / attempted; plus the first 3 error messages.
- **GPU memory**: peak `memory.used` from `nvidia-smi` during the level (MiB), if present.

### Cost

- **GPU price** ($/hour) is **passed as an argument**; its **source and date** are recorded in `meta`. Never
  hard-coded.
- **USD per 1M output tokens** = price_per_hour / 3600 × (wall seconds for the level) / (output tokens for the
  level) × 1e6, reported at the headline level.
- **USD per 1K agent tasks** = USD-per-LLM-call × (LLM calls per task) × 1000, where USD-per-LLM-call uses the
  headline level's per-request cost (price_per_hour / 3600 × mean end-to-end seconds / effective concurrency), and
  **LLM calls per task comes from the same variant's eval run** (`llm_calls_per_task` in its `summary.json`). The
  comparison states both source paths.

### Headline

- **Concurrency 8** is the headline throughput/cost point; **concurrency 1** is the single-user latency point.

## 5. Quality runs for Phase 9 (pre-registered)

- TEST split, **3 trials**, **temperature 0.3**, guardrails on, same digest/protocol family as Phase 4.5/5.
- Prompts **v1′** (production) and **v5** (best candidate), each served on **FP16** and on **AWQ** → **4 eval runs**.
- These reuse `evals/run.py` unchanged against the vLLM endpoint (`--base-url`, `--model bank-agent-llm`).
- **Quality retention** = AWQ task success / FP16 task success, for each prompt, with both Wilson 95% CIs.
- **Significance (primary): a paired scenario-level (cluster) bootstrap.** Resample scenarios with replacement
  (**10,000 resamples, fixed seed 20261012**); for each resample compute the task-success difference
  **AWQ − FP16** using that scenario's trials under both variants; report the **95% CI of the difference**. The
  variants are **"not significantly different"** only if that CI **contains 0**. This treats the 3 trials of a
  scenario as a cluster, which is the correct unit, since trials of one scenario are correlated.
- **Significance (secondary):** a two-proportion z-test over all trials; the z-statistic and p-value are reported,
  but the bootstrap CI is the decision rule.
- `paired_cluster_bootstrap` is implemented and unit-tested in `evals/metrics.py`.

## 6. Prefix-caching ablation

One **AWQ** benchmark repeat with `--no-enable-prefix-caching`, same workload and levels, to measure prefix
caching's effect on this agent's workload (the system prompt + tool schemas are a large shared prefix). Reported as
% change in TTFT and throughput at the headline level.

## 6a. GPU environment check (in GPU_RUNBOOK, pre-registered)

Before serving, the runbook checks the host's `nvidia-smi` driver and CUDA version against what the pinned vLLM
wheel (0.31.0) requires. If they are incompatible, the documented fallback is to install an **older vLLM pin** that
matches the host's CUDA, and that actual version is recorded in every run's `meta` (the serve script already prints
it). AWQ serving on the T4 is proven by the 10-request pilot, not assumed.

## 7. Outputs

- `serving/serve.sh`, `serving/benchmark.py`, `serving/compare.py`, `requirements-gpu.txt`.
- `results/bench-<name>/{summary.json, report.md}` per benchmark run.
- `results/SERVING_COMPARISON.md` built by `compare.py` from the bench + Phase 9 eval summaries; **sections with
  missing inputs are skipped, never estimated**; every number cites a `summary.json` path.
- `docs/GPU_RUNBOOK.md`: exact Phase 9 command sequence, T4 fallback, expected time per step. It **begins with a
  10-request vLLM pilot** that verifies streamed tool-call parsing (TTFT fires on a tool-call delta, not only on
  content) before any full benchmark, and **verifies AWQ actually loads and serves on the T4** rather than assuming
  it — the fallback to the 3B pair is taken only if the pilot fails.
- Make targets: `serve-fp16`, `serve-awq`, `bench`, `compare-serving`.

## 8. Local verification (this Mac, no GPU)

- Unit tests: percentile and cost math; workload hashing and fixed ordering; TTFT measured from the first content OR
  tool-call delta; `compare.py` skips a section whose input summary is missing.
- Plumbing against the existing mock OpenAI server (`tests/`): streaming + tool calls + usage parsed; server-down →
  errors counted, no crash.
- Smoke benchmark against Ollama `qwen2.5:7b`, concurrency 1 and 2, 8 requests, on a small recorded workload (record
  3 dev scenarios with `evals/run.py --record-llm` first). Name `bench-smoke-ollama`, labelled
  "smoke test, not a result".
- Mutation check: a second variant sent in a different request order → the `workload_hash` check must fail.

## 8a. Known statistical limitation

The Wilson 95% CIs on task success treat the 3 trials of each scenario as independent Bernoulli samples. Trials of
one scenario are correlated (same request, same expected outcome), so these CIs are likely **slightly too narrow**.
The **paired cluster bootstrap** (§5) resamples at the scenario level and is the more conservative, preferred
measure for the FP16-vs-AWQ decision. This limitation is recorded in `CLAUDE.md` and `docs/PHASE6_SUMMARY.md`.

## 9. What these results can and cannot be

The serve scripts, benchmark and comparison are verified here; **no latency, throughput, cost or quality number from
this machine is a reported result.** The reported numbers come only from the Phase 9 GPU runs, recorded with GPU
name, vLLM version, workload hash and GPU price in `meta`.
