@echo off
REM ============================================================
REM  CoIR Wave-0 benchmark launcher (ADR-007) - dense, core set.
REM  Cylance-friendly: plain cmd + installed Python on a .py file.
REM  Run from Command Prompt, or just double-click this file.
REM  Keep the window open; plug in and close the lid.
REM ============================================================
setlocal
cd /d "C:\Users\edb\Documents\indexer"
set HF_HUB_OFFLINE=1
set TOKENIZERS_PARALLELISM=false
set PYTHONIOENCODING=utf-8

set PY="C:\Users\edb\Documents\Development Dependencies\VectorEnv\Scripts\python.exe"

echo ============================================================
echo  CoIR Wave-0 benchmark (dense, core set) starting...
echo.
echo  Live progress + results are written to:
echo    benchmarks\wave0.log
echo.
echo  Keep this window open. Plug in and close the lid.
echo  Final deliverable: benchmarks\baseline.jsonl
echo ============================================================
echo.

%PY% tools\coir_eval.py > benchmarks\wave0.log 2>&1

echo.
echo ============================================================
echo  DONE. Exit code %ERRORLEVEL%.
echo  Results: benchmarks\baseline.jsonl
echo ============================================================
pause
