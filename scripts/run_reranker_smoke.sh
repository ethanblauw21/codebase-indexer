#!/usr/bin/env bash
# ============================================================
#  CoIR reranker SMOKE test (ADR-007 §8). For Git Bash.
#  cosqa, dense+reranker, only 5 queries (~500 rerank pairs).
#  Purpose: validate the Qwen3-Reranker path end-to-end and
#  trigger the one-time model download BEFORE committing to the
#  ~30-45 min calibration. HF_HUB_OFFLINE=0 so the reranker
#  downloads on first run (~1.2 GB, Apache-2.0, ungated).
#  Run:  bash scripts/run_reranker_smoke.sh
#  Expect: a cosqa dense+reranker scorecard row, no errors.
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 1

export HF_HUB_OFFLINE=0
export TOKENIZERS_PARALLELISM=false
export PYTHONIOENCODING=utf-8

PY="/c/Users/edb/Documents/Development Dependencies/VectorEnv/Scripts/python.exe"

echo "CoIR reranker SMOKE (cosqa, dense+reranker, 5 queries). Log: benchmarks/reranker_smoke.log"
echo "First run also downloads Qwen/Qwen3-Reranker-0.6B (~1.2 GB, one time)."
"$PY" tools/coir_eval.py --config dense+reranker --subtasks cosqa --limit-queries 5 \
    2>&1 | tee benchmarks/reranker_smoke.log
echo "DONE. If a cosqa scorecard row printed with no errors, the Qwen3 path works ->"
echo "      next run the full calibration: bash scripts/run_reranker_calibrate.sh"
