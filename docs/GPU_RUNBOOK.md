# GPU Runbook (Phase 9)

Exact command sequence for the GPU host that produces the **reported** serving, benchmark and FP16-vs-AWQ quality
numbers. Nothing here runs on the Mac; the Mac only verified the plumbing (Phase 6). Plan and fixed settings:
`docs/SERVING_PLAN.md`.

All commands run from the repo root with the project installed. Replace `GPU_PRICE`, its source and date with the
actual values for the instance; the price is an argument, never hard-coded.

## 0. Environment check (before anything)

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
nvcc --version 2>/dev/null | tail -1 || echo "no nvcc; driver CUDA from nvidia-smi above"
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements-gpu.txt    # vllm==0.31.0
python -c "import vllm; print('vllm', vllm.__version__)"
```

**Driver/CUDA vs the pinned wheel.** vLLM 0.31.0 needs a recent CUDA runtime (CUDA 12.x driver). If
`pip install -r requirements-gpu.txt` or `import vllm` fails on a CUDA/driver mismatch (common on older T4 images):

```bash
# Fallback: install the newest vLLM that matches the host's CUDA. Record the version you land on.
pip install "vllm<0.31"      # try the next-older minor; repeat until import succeeds
python -c "import vllm; print('vllm fallback', vllm.__version__)"
```

The serve script prints the installed vLLM version and the benchmark records it in `meta.vllm_version`, so whatever
version actually ran is captured. Do not edit `requirements-gpu.txt` to match; note the fallback version in the run
log.

**Pick the variant pair by GPU memory:**
- ≥ 24 GB (L4, A10, A100, H100): `fp16` + `awq` (7B). On Ada/Hopper you may add `fp8`.
- 16 GB (T4): the 7B FP16 weights (~15 GB) do not fit with an 8192 context → use the T4 fallback pair
  `t4-fp16` + `t4-awq` (3B). Every comparison stays same-size; `summary.json` records which pair ran.

## 0a. Verify vLLM metric names for the Grafana dashboard (Phase 7 panels)

The Grafana dashboard's vLLM row uses metric names that change between vLLM versions. Once the server is up, check
them against the real endpoint and fix `monitoring/dashboards/banking-agent.json` if they differ:

```bash
curl -s localhost:8000/metrics | grep -E "vllm:(num_requests_(running|waiting)|time_to_first_token_seconds|e2e_request_latency_seconds|generation_tokens_total|gpu_cache_usage_perc)" | head
```

Expected in vLLM 0.31.0 (verify, do not assume): `vllm:num_requests_running`, `vllm:num_requests_waiting`,
`vllm:time_to_first_token_seconds_bucket`, `vllm:e2e_request_latency_seconds_bucket`,
`vllm:generation_tokens_total`, `vllm:gpu_cache_usage_perc`. The agent-side panels are already validated by
`tests/test_monitoring.py`; only the vLLM names need this manual check.

## 1. Pilot: 10 requests, verify streamed tool-call parsing (both variants)

Before any full benchmark, prove the server streams tool calls and the benchmark measures TTFT on a tool-call delta,
and — on a T4 — that AWQ actually loads and serves.

```bash
# terminal A: serve the variant you will pilot (fp16, or t4-awq on a T4)
serving/serve.sh fp16 8000        # or: serving/serve.sh t4-awq 8000

# terminal B: once "Uvicorn running" appears, a 10-request synthetic pilot
curl -s localhost:8000/v1/models | python -m json.tool | grep '"id"'
python -m serving.benchmark --name bench-pilot --base-url http://localhost:8000/v1 \
    --levels 1 --requests 10 --max-tokens 64 --label "pilot, not a result"
```

Check `results/bench-pilot/summary.json`: `errors` is 0 and `ttft_p50_s` is not null. If TTFT is null, the model
is not streaming tool-call deltas with `--tool-call-parser hermes` — stop and fix serving before continuing. On a T4,
if `serving/serve.sh t4-awq` fails to load, fall back is already the 3B pair; if even that OOMs, lower
`--max-model-len` via `MAX_MODEL_LEN=4096` and re-record it in meta.

## 2. FP16: serve → eval TEST (record workload) → benchmark

```bash
# terminal A
serving/serve.sh fp16 8000        # t4-fp16 on a T4

# terminal B — quality eval, v1' (production), records the shared workload ONCE from FP16
python -m evals.run --backend openai --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --split test --trials 3 --temperature 0.3 --concurrency 8 \
    --record-llm --name eval-fp16-v1p --note "Phase 9 FP16 v1' TEST"
# quality eval, v5 (serve stays up; v5 prompt comes from the redteam-v5 branch checkout — see note)
python -m evals.run --backend openai --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --split test --trials 3 --temperature 0.3 --concurrency 8 --name eval-fp16-v5 --note "Phase 9 FP16 v5 TEST"

# benchmark FP16 on the recorded workload
python -m serving.benchmark --name bench-fp16 --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --workload results/eval-fp16-v1p/llm_requests.jsonl \
    --gpu-price GPU_PRICE --gpu-price-source "SOURCE" --gpu-price-date YYYY-MM-DD \
    --llm-calls-per-task $(python -c "import json;print(json.load(open('results/eval-fp16-v1p/summary.json'))['metrics']['latency']['llm_calls_per_task'])")
```

Stop the FP16 server (Ctrl-C in terminal A) before starting AWQ.

## 3. AWQ: serve → eval TEST → benchmark → prefix-cache ablation

```bash
# terminal A
serving/serve.sh awq 8000         # t4-awq on a T4

# terminal B — same two evals against AWQ (do NOT re-record; reuse the FP16 workload for the benchmark)
python -m evals.run --backend openai --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --split test --trials 3 --temperature 0.3 --concurrency 8 --name eval-awq-v1p --note "Phase 9 AWQ v1' TEST"
python -m evals.run --backend openai --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --split test --trials 3 --temperature 0.3 --concurrency 8 --name eval-awq-v5 --note "Phase 9 AWQ v5 TEST"

# benchmark AWQ on the SAME recorded FP16 workload (byte-identical requests)
python -m serving.benchmark --name bench-awq --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --workload results/eval-fp16-v1p/llm_requests.jsonl \
    --gpu-price GPU_PRICE --gpu-price-source "SOURCE" --gpu-price-date YYYY-MM-DD \
    --llm-calls-per-task $(python -c "import json;print(json.load(open('results/eval-awq-v1p/summary.json'))['metrics']['latency']['llm_calls_per_task'])")

# stop the server, restart AWQ WITHOUT prefix caching for the ablation
# terminal A: EXTRA_ARGS="--no-enable-prefix-caching" serving/serve.sh awq 8000
python -m serving.benchmark --name bench-awq-nocache --base-url http://localhost:8000/v1 --model bank-agent-llm \
    --workload results/eval-fp16-v1p/llm_requests.jsonl --no-prefix-caching \
    --gpu-price GPU_PRICE --gpu-price-source "SOURCE" --gpu-price-date YYYY-MM-DD
```

**v5 prompt:** the v5 evals use the `redteam-v5` branch's prompt. Run them from a checkout of that branch (or a
worktree), exactly as in Phase 5; each `summary.json` records `prompt_version` `47c197da48b7` so the source is
verifiable. FP16/AWQ is chosen by which server is up, independent of the prompt.

## 4. Compare

```bash
python -m serving.compare \
    --fp16-bench results/bench-fp16 --awq-bench results/bench-awq --awq-nocache-bench results/bench-awq-nocache \
    --fp16-eval results/eval-fp16-v1p --awq-eval results/eval-awq-v1p \
    --fp16-eval-v5 results/eval-fp16-v5 --awq-eval-v5 results/eval-awq-v5
cat results/SERVING_COMPARISON.md
```

The quality section needs each eval run's `trials.jsonl` (git-ignored) present in `results/<name>/` for the paired
cluster bootstrap; run `compare` on the GPU host right after the evals, before cleaning up.

## 5. Expected time per step (L4-class GPU, 7B)

| Step | Time |
|---|---|
| Install + env check | 5–10 min (model downloads: 7B ~15 GB FP16, ~5 GB AWQ) |
| Pilot (10 requests) | < 1 min once the server is up (serve load ~1–3 min) |
| Each TEST eval (132 trials, concurrency 8) | ~8–20 min |
| Each benchmark (4 levels) | ~3–6 min |
| Prefix-cache ablation benchmark | ~3–6 min |
| Compare | seconds |
| **Total (FP16 + AWQ + ablation + 4 evals + compare)** | **~1.5–2.5 h** |

On a T4 with the 3B pair, roughly halve the eval/benchmark times but add server-load time per switch.

## 6. Artifacts to commit from the GPU run

`results/bench-fp16/`, `results/bench-awq/`, `results/bench-awq-nocache/`, the four `results/eval-*-v{1p,5}/`
(summary.json + report.md; `trials.jsonl` and `llm_requests.jsonl` stay git-ignored), and
`results/SERVING_COMPARISON.md`. Note the actual GPU name, vLLM version and price source in the Phase 9 writeup;
they are already in each `summary.json` meta.
