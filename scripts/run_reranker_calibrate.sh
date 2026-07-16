#!/usr/bin/env bash
# ============================================================
#  CoIR reranker RATE CHECK (ADR-007 §8). For Git Bash.
#  cosqa only, dense+reranker, a 60-query seeded sample
#  (~6K cross-encoder pairs) - finishes in minutes, not hours.
#  Purpose: measure real CPU q/s (printed every 25 queries) so
#  the full reranker run can be sized firmly before committing.
#  HF_HUB_OFFLINE=0 so the reranker downloads if not yet cached
#  (~1.2 GB, one time); reused offline by the long run after.
#  Run:  bash scripts/run_reranker_calibrate.sh
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 1

export HF_HUB_OFFLINE=0
export TOKENIZERS_PARALLELISM=false
export PYTHONIOENCODING=utf-8

PY="/c/Users/edb/Documents/Development Dependencies/VectorEnv/Scripts/python.exe"

echo "CoIR reranker RATE CHECK (cosqa, dense+reranker, 60 queries). Log: benchmarks/reranker_calibrate.log"
echo "Watch the q/s + eta lines (every 25 queries) for the throughput."
"$PY" tools/coir_eval.py --config dense+reranker --subtasks cosqa --limit-queries 60 \
    2>&1 | tee benchmarks/reranker_calibrate.log
echo "DONE. The q/s rate + latency in the log sizes the full run."
