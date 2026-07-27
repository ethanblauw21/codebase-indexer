@echo off
REM ============================================================
REM  CoIR Wave-0 reranker run (ADR-007 §8) - dense+reranker,
REM  full core set. RUN THE CALIBRATION FIRST so the reranker
REM  is already downloaded (this script is offline).
REM
REM  Samples [eval].rerank_sample_queries (default 500) gradable
REM  queries per subtask, seeded + reproducible. At depth 100 this
REM  is ~21 h (vs ~12 days for all queries) - a weekend run.
REM  Scores carry 95% CIs; the reranker lift is measured paired.
REM  Faster: set [eval].rerank_depth = 50 (~halves it).
REM  Full precision: set [eval].rerank_sample_queries = 0 (multi-day).
REM  Progress prints every 25 queries. Plug in, close the lid.
REM ============================================================
setlocal
cd /d "C:\Users\edb\Documents\indexer"
set HF_HUB_OFFLINE=1
set TOKENIZERS_PARALLELISM=false
set PYTHONIOENCODING=utf-8

set PY="C:\Users\edb\Documents\Development Dependencies\VectorEnv\Scripts\python.exe"

echo ============================================================
echo  CoIR Wave-0 reranker (dense+reranker, core set) starting...
echo.
echo  Live progress + results: benchmarks\wave0_reranker.log
echo  Result rows appended to: benchmarks\baseline.jsonl
echo  Keep this window open. Plug in and close the lid.
echo ============================================================
echo.

%PY% tools\coir_eval.py --config dense+reranker > benchmarks\wave0_reranker.log 2>&1

echo.
echo ============================================================
echo  DONE. Exit code %ERRORLEVEL%.
echo  Results: benchmarks\baseline.jsonl
echo ============================================================
pause
