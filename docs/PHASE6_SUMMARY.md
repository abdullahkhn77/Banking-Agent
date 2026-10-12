# Phase 6 — Implementation Summary

**Status:** complete (local build + verification) · **Date:** 2026-10-12 · **Pre-registration:**
`docs/SERVING_PLAN.md` (committed `4258fd9`, before any code).

Phase 6 builds and verifies the vLLM serving scripts, the streaming inference benchmark, and the FP16-vs-AWQ
comparison. **This machine has no GPU**, so every latency/throughput/cost/quality number here is a smoke test; the
reported numbers come from the Phase 9 GPU run (`docs/GPU_RUNBOOK.md`). The agent, prompts, guardrails and scenarios
are unchanged.

## 1. What was built

| File | Role |
|---|---|
| `serving/serve.sh` | vLLM OpenAI server for variants `fp16`, `awq`, `fp8`, `t4-fp16`, `t4-awq`; fixed settings; prints the exact command, the vLLM version and the GPU |
| `requirements-gpu.txt` | `vllm==0.31.0` (current PyPI stable, checked 2026-10-12); GPU host only |
| `serving/benchmark.py` | Async streaming benchmark: TTFT, TPOT, latency, throughput, mean output tokens, errors, GPU memory, cost; fixed-seed workload order + `workload_hash` |
| `serving/benchmark_report.py` | `report.md` from `summary.json` only |
| `serving/compare.py` | `results/SERVING_COMPARISON.md`; sections with missing inputs are skipped, never estimated |
| `evals/metrics.py` | `paired_cluster_bootstrap`, `two_proportion_z` (quality statistics) |
| `docs/GPU_RUNBOOK.md` | exact Phase 9 command sequence, pilot, env/CUDA check, T4 fallback, timings |

Make targets: `serve-fp16`, `serve-awq`, `bench`, `compare-serving`.

## 2. Fixed settings and models (from the plan)

- FP16 = `Qwen/Qwen2.5-7B-Instruct`, AWQ = `Qwen/Qwen2.5-7B-Instruct-AWQ`, optional `fp8` (Ada/Hopper), T4 fallback
  = the 3B pair. Every comparison is same-size; the pair that ran is in each `summary.json`.
- vLLM flags: `--enable-auto-tool-choice --tool-call-parser hermes --max-model-len 8192
  --gpu-memory-utilization 0.90`, prefix caching on, `--served-model-name bank-agent-llm`; env overrides recorded.
- Workload: recorded **once** from the FP16 TEST run with prompt **v1′**, replayed **byte-identically** (fixed shuffle
  seed 20261012, `workload_hash` in meta; the comparison refuses to compare differing hashes).
- Benchmark: concurrency 1/4/8/16, `max(64, 4×concurrency)` requests, 1 discarded warm-up, streaming, max_tokens 256,
  temp 0. Headline concurrency 8; concurrency 1 for single-user latency.
- Cost: GPU $/hour passed as an argument with source+date in meta; USD/1M output tokens and USD/1K tasks
  (calls-per-task from the same variant's eval summary).

## 3. Quality statistics (for Phase 9)

- **Primary: paired scenario-level (cluster) bootstrap** of AWQ − FP16 task success (10,000 resamples, seed
  20261012); "not significantly different" only if the 95% CI contains 0. Implemented and unit-tested in
  `evals/metrics.py` (`paired_cluster_bootstrap`).
- **Secondary:** a two-proportion z-test over all trials; reported, but the bootstrap CI is the decision rule.
- Quality retention = AWQ task success / FP16 task success with both Wilson CIs.

## 4. Local verification (this Mac, no GPU)

| Check | Result |
|---|---|
| Unit tests (`tests/test_benchmark.py`, 14) | percentile/cost math; TTFT from content **or** tool-call delta; server-down error path (counted, no crash); tools+stream+usage in every request; two-proportion z (known value z=1.98); bootstrap detects a clear difference, contains 0 when equal, seed-stable, empty-safe; compare skips missing sections; **workload-hash guard flags a different order (mutation check)** |
| Full suite | `make test` all green |
| Smoke benchmark vs Ollama `qwen2.5:7b` | `results/bench-smoke-ollama/`: concurrency 1 and 2, 8 requests, **0 errors**, TTFT measured (0.35 s c1, 1.34 s c2), usage parsed (mean 34 output tokens). Labelled "smoke test, not a result"; `vllm_version`/`gpu_name` null (Ollama) |

The smoke recording used `evals/run.py --record-llm` on 3 dev scenarios (7 requests). The smoke benchmark is the only
serving artifact committed this phase and is explicitly not a result.

## 5. Mutation check

The benchmark fingerprints the ordered workload (`workload_hash`). `serving/compare.py` marks two benchmark runs
**"DIFFER: not comparable"** when their hashes differ. The test `test_workload_hash_guard_flags_a_different_order`
confirms a changed request order trips this guard, and `test_order_is_deterministic_and_hash_matches` confirms the
same seed reproduces the order and hash.

## 6. Known statistical limitation (also in `CLAUDE.md`)

The Wilson 95% CIs on task success treat the 3 trials of each scenario as independent, but trials of one scenario are
correlated (same request, same expected outcome), so those CIs are likely **slightly too narrow**. The **paired
cluster bootstrap** resamples at the scenario level and is the preferred, more conservative measure for the
FP16-vs-AWQ decision.

## 7. Deviation log

None. All work follows `docs/SERVING_PLAN.md` as pre-registered (including the six additions committed in the plan).

## 8. Definition of done (from `docs/PLAN.md`)

- [x] Serving scripts for FP16 and AWQ (same base model) behind an OpenAI-compatible endpoint, plus FP8 and T4
      fallbacks
- [x] Recorded agent workload replayed identically as the load benchmark (fixed order + hash guard)
- [x] Metrics: TTFT/TPOT/latency p50/p95, throughput, mean output tokens, GPU memory, error rate, cost per 1K tasks
      (inputs from arguments + eval summary, never hard-coded)
- [x] `make serve-fp16`, `make serve-awq`, `make bench`, `make compare-serving`; results under `results/`
- [x] Quality statistics (cluster bootstrap primary, z-test secondary) implemented and unit-tested
- [x] Verified locally (unit + mock-server plumbing + Ollama smoke + mutation check); GPU runbook written

**Next:** Phase 7 — agent HTTP API + Prometheus + audit log + Grafana. The real serving/quality numbers are produced
in Phase 9 per `docs/GPU_RUNBOOK.md`.
