#!/usr/bin/env bash
# ADR-019 cloud eval — runs on a spot T4 VM, then deletes itself.
#
# Lifecycle (all unattended):
#   1. wait for the GPU driver (the Deep Learning VM image auto-installs it on boot)
#   2. pull the run bundle (repo code + fixtures + prebuilt indexes) from GCS
#   3. build a venv with a CUDA torch + the repo's pinned stack (pip install -e .)
#   4. run tools/real_repo_eval.py on the GPU over ALL repos in one process
#      -> this prints verdict()'s own authoritative pooled C-B line
#   5. upload result.jsonl + logs to GCS
#   6. self-delete (a hard --max-run-duration DELETE, set by the launcher, is the
#      backstop in case anything below dies before the delete call)
#
# Config comes from instance metadata (eval-bucket) and an optional bucket file
# inputs/eval-args.txt (defaults to "--arms B,C" = the authoritative reranker run).
set -uo pipefail

LOG=/var/log/eval-run.log
touch "$LOG"
exec > >(tee -a "$LOG") 2>&1   # console + serial + uploadable logfile

md() { curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/$1"; }
BUCKET="$(md attributes/eval-bucket)"
ZONE="$(md zone | awk -F/ '{print $NF}')"
NAME="$(md name)"
echo "[startup] $(date -u) bucket=$BUCKET zone=$ZONE name=$NAME"

# --- self-delete + log upload on ANY exit ---------------------------------------
cleanup() {
  echo "[startup] $(date -u) uploading logs + self-deleting"
  gcloud storage cp "$LOG" "gs://$BUCKET/outputs/eval-run.log"            || true
  gcloud storage cp /var/log/eval_progress.log \
                    "gs://$BUCKET/outputs/eval_progress.log"              || true
  gcloud compute instances delete "$NAME" --zone="$ZONE" --quiet         || true
}
trap cleanup EXIT

# --- wait for the GPU driver ----------------------------------------------------
for i in $(seq 1 60); do
  if nvidia-smi >/dev/null 2>&1; then echo "[startup] GPU ready:"; nvidia-smi; break; fi
  echo "[startup] waiting for GPU driver ($i/60)..."; sleep 10
done

# --- stage the bundle -----------------------------------------------------------
WORK=/opt/eval; mkdir -p "$WORK"; cd "$WORK"
gcloud storage cp "gs://$BUCKET/inputs/bundle.tar.gz" .
tar -xzf bundle.tar.gz
echo "[startup] bundle extracted into $WORK"

# --- python env: guard 3.11+, CUDA torch first, then the pinned stack -----------
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "[startup] system python3 = $PYV (repo requires >=3.11)"
case "$PYV" in 3.11|3.12|3.13) : ;; *)
  echo "[startup] FATAL: python $PYV < 3.11 — use a debian-12 DLVM image"; exit 1 ;;
esac
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
# CUDA torch BEFORE the repo, so pip install -e . (sentence-transformers) sees torch
# already satisfied and does not pull a CPU build.
pip install -q torch --index-url https://download.pytorch.org/whl/cu121
pip install -q -e .
python -c 'import torch; print("[startup] torch", torch.__version__, "cuda?", torch.cuda.is_available())'

# --- run the eval on the GPU ----------------------------------------------------
export EVAL_PROGRESS_LOG=/var/log/eval_progress.log
export HF_HOME=/opt/hf
mkdir -p out
if gcloud storage cp "gs://$BUCKET/inputs/eval-args.txt" ./eval-args.txt 2>/dev/null; then
  EVAL_ARGS="$(cat eval-args.txt)"
else
  EVAL_ARGS="--arms B,C"     # default: authoritative reranker run over all repos
fi
echo "[startup] $(date -u) running: real_repo_eval.py $EVAL_ARGS"
python -u tools/real_repo_eval.py $EVAL_ARGS --baseline out/result.jsonl
echo "[startup] $(date -u) eval finished"

# --- upload results (logs handled by the EXIT trap) -----------------------------
gcloud storage cp out/result.jsonl "gs://$BUCKET/outputs/result.jsonl" || true
echo "[startup] $(date -u) results uploaded — exiting (trap self-deletes the VM)"
