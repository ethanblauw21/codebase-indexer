@echo off
REM ============================================================
REM  CoIR reranker RATE CHECK (ADR-007 §8).
REM  cosqa only, dense+reranker, a 60-query seeded sample
REM  (~6K cross-encoder pairs) - finishes in minutes, not hours.
REM  Purpose: measure real CPU q/s (printed every 25 queries) so
REM  the full reranker run can be sized firmly before you commit.
REM  HF_HUB_OFFLINE=0 so the reranker downloads if not yet cached
REM  (~1.2 GB, one time); reused offline by the long run after.
REM  Cylance-friendly: plain cmd + VectorEnv Python on a .py.
REM ============================================================
setlocal
cd /d "C:\Users\edb\Documents\indexer"
set HF_HUB_OFFLINE=0
set TOKENIZERS_PARALLELISM=false
set PYTHONIOENCODING=utf-8

set PY="C:\Users\edb\Documents\Development Dependencies\VectorEnv\Scripts\python.exe"

echo ============================================================
echo  CoIR reranker RATE CHECK (cosqa, dense+reranker, 60 queries)
echo.
echo  Watch the "q/s" + "eta" lines (every 25 queries) for the rate.
echo  Live progress + results: benchmarks\reranker_calibrate.log
echo ============================================================
echo.

%PY% tools\coir_eval.py --config dense+reranker --subtasks cosqa --limit-queries 60 > benchmarks\reranker_calibrate.log 2>&1

echo.
echo ============================================================
echo  DONE. Exit code %ERRORLEVEL%.
echo  benchmarks\reranker_calibrate.log shows the q/s rate +
echo  latency (mean/p50/p95) -> use it to size the full run.
echo ============================================================
pause
