#!/usr/bin/env bash
# ============================================================
#  CoIR Wave-0 benchmark launcher (ADR-007) - dense, core set.
#  For Git Bash. Run:  bash scripts/run_wave0.sh
#  Shows live progress AND writes benchmarks/wave0.log (tee).
#  Keep the window open; plug in and close the lid.
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 1

export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export PYTHONIOENCODING=utf-8

PY="/c/Users/edb/Documents/Development Dependencies/VectorEnv/Scripts/python.exe"

echo "CoIR Wave-0 (dense, core set) starting. Log: benchmarks/wave0.log"
"$PY" tools/coir_eval.py 2>&1 | tee benchmarks/wave0.log
echo "DONE. Results: benchmarks/baseline.jsonl"
