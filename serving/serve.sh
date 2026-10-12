#!/usr/bin/env bash
# Serve the bank agent's LLM with vLLM behind an OpenAI-compatible endpoint. GPU host only (Phase 9).
# See docs/SERVING_PLAN.md (fixed settings) and docs/GPU_RUNBOOK.md (step-by-step).
#
#   serving/serve.sh <variant> [port]
#     variant: fp16 | awq | fp8 | t4-fp16 | t4-awq
#     port:    default 8000
#
# Env overrides (recorded in the benchmark meta, do not change the pre-registered defaults):
#   BASE_MODEL DTYPE MAX_MODEL_LEN GPU_MEM_UTIL TP EXTRA_ARGS
set -euo pipefail

VARIANT="${1:-fp16}"
PORT="${2:-8000}"
SERVED_NAME="bank-agent-llm"

case "$VARIANT" in
  fp16)    MODEL="Qwen/Qwen2.5-7B-Instruct";      DT="bfloat16" ;;   # fp16 on T4-class cards; see t4-fp16
  awq)     MODEL="Qwen/Qwen2.5-7B-Instruct-AWQ";  DT="float16"  ;;
  fp8)     MODEL="Qwen/Qwen2.5-7B-Instruct";      DT="auto"     ; FP8="--quantization fp8" ;;  # Ada/Hopper only
  t4-fp16) MODEL="Qwen/Qwen2.5-3B-Instruct";      DT="float16"  ;;
  t4-awq)  MODEL="Qwen/Qwen2.5-3B-Instruct-AWQ";  DT="float16"  ;;
  *) echo "unknown variant: $VARIANT (fp16|awq|fp8|t4-fp16|t4-awq)" >&2; exit 2 ;;
esac

MODEL="${BASE_MODEL:-$MODEL}"
DT="${DTYPE:-$DT}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
TP="${TP:-1}"

echo "# vLLM version:"; python -c "import vllm; print(vllm.__version__)" 2>/dev/null || echo "  (vLLM not importable here)"
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; else echo "# no nvidia-smi"; fi

CMD=(python -m vllm.entrypoints.openai.api_server
  --model "$MODEL"
  --served-model-name "$SERVED_NAME"
  --dtype "$DT"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_MEM_UTIL"
  --tensor-parallel-size "$TP"
  --enable-auto-tool-choice
  --tool-call-parser hermes
  --port "$PORT")
[ -n "${FP8:-}" ] && CMD+=($FP8)
[ -n "${EXTRA_ARGS:-}" ] && CMD+=($EXTRA_ARGS)

echo "# exact command:"
printf '%q ' "${CMD[@]}"; echo
exec "${CMD[@]}"
