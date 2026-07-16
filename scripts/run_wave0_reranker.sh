#!/usr/bin/env bash
# ============================================================
#  CoIR Wave-0 reranker run (ADR-007 §8). For Git Bash.
#  dense+reranker, full core set. RUN CALIBRATION FIRST so the
#  reranker is already cached (this script is offline).
#
#  Samples [eval].rerank_sample_queries (default 500) gradable
#  queries/subtask, seeded + reproducible. At depth 100 ~= 21 h
#  (vs ~12 days for all queries). Scores carry 95% CIs; the
#  reranker lift is measured paired. Progress prints every 25 q.
#  Faster: [eval].rerank_depth = 50. Full precision: set
#  [eval].rerank_sample_queries = 0 (multi-day).
#  Run:  bash scripts/run_wave0_reranker.sh   (best over a weekend)
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 1

export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export PYTHONIOENCODING=utf-8

PY="/c/Users/edb/Documents/Development Dependencies/VectorEnv/Scripts/python.exe"

echo "CoIR Wave-0 reranker (dense+reranker, core set). Log: benchmarks/wave0_reranker.log"
"$PY" tools/coir_eval.py --config dense+reranker 2>&1 | tee benchmarks/wave0_reranker.log
echo "DONE. Results: benchmarks/baseline.jsonl"
