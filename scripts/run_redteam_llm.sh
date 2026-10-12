#!/usr/bin/env bash
# Phase 5 C/D/E model runs (docs/REDTEAM_PLAN.md). Resumable: re-running skips finished trials.
# Launch under caffeinate so the Mac does not sleep mid-batch:
#   caffeinate -i scripts/run_redteam_llm.sh
set -uo pipefail

REPO="/Users/abdullahkhan/Desktop/Banking-Agent"
PY="$REPO/.venv/bin/python"
SP="/private/tmp/claude-501/-Users-abdullahkhan-Desktop-Banking-Agent/66a9495e-60d7-4b0d-9d09-df8a0147cd63/scratchpad"
WT_V1P="$SP/rt-v1p"
WT_V5="$SP/rt-v5"
RESULTS="$REPO/results"
BASE_URL="http://localhost:11434/v1"
MODEL="qwen2.5:7b"
COMMON="--base-url $BASE_URL --model $MODEL --temperature 0.3 --trials 3 --concurrency 1 --timeout 120 --resume --results-dir $RESULTS"

digest() { curl -s "http://localhost:11434/api/tags" 2>/dev/null | "$PY" -c "import json,sys;print(next((m['digest'] for m in json.load(sys.stdin).get('models',[]) if m['name']=='$MODEL'),'none'))" 2>/dev/null; }

D0=$(digest); echo "digest before: $D0"

# --- C: careful-customer eval on TEST, v1' and v5 (tool-customer numbers already exist from Phase 4.5) ---
(cd "$WT_V1P" && "$PY" -m evals.run --backend openai $COMMON --split test --customer careful --name redteam-careful-v1p \
  --note "C: careful customer, v1' (TEST)")
(cd "$WT_V5" && "$PY" -m evals.run --backend openai $COMMON --split test --customer careful --name redteam-careful-v5 \
  --note "C: careful customer, v5 (TEST)")

# --- D: indirect prompt injection ---
(cd "$WT_V1P" && "$PY" -m evals.redteam_llm injection --version v1p --guardrails on --customer rubber_stamp $COMMON)
(cd "$WT_V1P" && "$PY" -m evals.redteam_llm injection --version v1p --guardrails on --customer careful $COMMON)
(cd "$WT_V5"  && "$PY" -m evals.redteam_llm injection --version v5  --guardrails on --customer rubber_stamp $COMMON)
(cd "$WT_V5"  && "$PY" -m evals.redteam_llm injection --version v5  --guardrails on --customer careful $COMMON)
(cd "$WT_V1P" && "$PY" -m evals.redteam_llm injection --version v1p --guardrails off --customer rubber_stamp $COMMON \
  --name redteam-injection-v1p-off)

# --- E: direct jailbreak ---
(cd "$WT_V1P" && "$PY" -m evals.redteam_llm jailbreak --version v1p --guardrails on $COMMON)
(cd "$WT_V5"  && "$PY" -m evals.redteam_llm jailbreak --version v5  --guardrails on $COMMON)
(cd "$WT_V1P" && "$PY" -m evals.redteam_llm jailbreak --version v1p --guardrails off $COMMON --name redteam-jailbreak-v1p-off)

# --- C comparison reports ---
(cd "$REPO" && "$PY" -m evals.careful_compare --tool "$RESULTS/test-ollama-qwen7b-v1p" --careful "$RESULTS/redteam-careful-v1p" \
  --label v1p --out "$RESULTS/redteam-careful-v1p")
(cd "$REPO" && "$PY" -m evals.careful_compare --tool "$RESULTS/test-ollama-qwen7b-v5" --careful "$RESULTS/redteam-careful-v5" \
  --label v5 --out "$RESULTS/redteam-careful-v5")

D1=$(digest); echo "digest after: $D1"
[ "$D0" = "$D1" ] && echo "DIGEST STABLE" || echo "WARNING: DIGEST CHANGED ($D0 -> $D1)"
echo "DONE"
