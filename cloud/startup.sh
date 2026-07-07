#!/usr/bin/env bash
# ADR-019 cloud eval — runs on a spot T4 VM, then deletes itself.
#
# Lifecycle (all unattended):
#   1. verify the GPU driver (the Deep Learning VM image ships it pre-baked)
#   2. pull + VERIFY the run bundle (retry on truncated download) from GCS
#   3. build a venv with a CUDA torch + the repo's pinned stack (pip install -e .)
#   4. run tools/real_repo_eval.py on the GPU over ALL repos in one process
#      -> this prints verdict()'s own authoritative pooled C-B line
#   5. upload result.jsonl + logs to GCS
#   6. self-delete (a hard --max-run-duration DELETE is the backstop if this fails)
#
# Every step fails LOUD (FATAL + exit) rather than silently continuing, so a broken
# run is obvious in the log instead of masquerading as "finished".
set -uo pipefail

LOG=/var/log/eval-run.log
touch "$LOG"
exec > >(tee -a "$LOG") 2>&1   # console + serial + uploadable logfile

# Pin gcloud's interpreter to the system python. Without this, `source .venv/bin/activate`
# (below) shadows python3 and breaks gcloud's credential detection — which silently
# stops the VM from being able to delete itself.
export CLOUDSDK_PYTHON=/usr/bin/python3

md() { curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/$1"; }
BUCKET="$(md attributes/eval-bucket)"
ZONE="$(md zone | awk -F/ '{print $NF}')"
NAME="$(md name)"
SA_EMAIL="$(md service-accounts/default/email)"
echo "[startup] $(date -u) bucket=$BUCKET zone=$ZONE name=$NAME sa=$SA_EMAIL"

# --- self-delete + log upload on ANY exit ---------------------------------------
cleanup() {
  echo "[startup] $(date -u) cleanup: uploading logs + self-deleting"
  # Explicitly activate the metadata service account so `gcloud compute` (which needs
  # an active account, unlike `gcloud storage`) can delete the instance.
  gcloud config set account "$SA_EMAIL" 2>/dev/null || true
  gcloud storage cp "$LOG" "gs://$BUCKET/outputs/eval-run.log"        || true
  gcloud storage cp /var/log/eval_progress.log \
                    "gs://$BUCKET/outputs/eval_progress.log"          || true
  gcloud compute instances delete "$NAME" --zone="$ZONE" --quiet     || true
}
trap cleanup EXIT
fatal() { echo "[startup] FATAL: $*"; exit 1; }

# --- verify the GPU driver ------------------------------------------------------
for i in $(seq 1 30); do
  if nvidia-smi >/dev/null 2>&1; then echo "[startup] GPU ready:"; nvidia-smi; break; fi
  echo "[startup] waiting for GPU driver ($i/30)..."; sleep 10
done
nvidia-smi >/dev/null 2>&1 || fatal "no GPU driver after 5 min"

# --- stage the bundle WITH verification + retry (guards truncated downloads) -----
WORK=/opt/eval; mkdir -p "$WORK"; cd "$WORK"
ok=0
for attempt in 1 2 3; do
  rm -rf "$WORK"/* ; rm -f bundle.tar.gz
  echo "[startup] downloading bundle (attempt $attempt)..."
  gcloud storage cp "gs://$BUCKET/inputs/bundle.tar.gz" ./bundle.tar.gz || { sleep 5; continue; }
  if tar -xzf bundle.tar.gz \
      && [ -f pyproject.toml ] && [ -d tools ] && [ -d src ] \
      && [ -d benchmarks/real_repo/index ]; then
    ok=1; echo "[startup] bundle extracted + verified (attempt $attempt)"; break
  fi
  echo "[startup] bundle incomplete/corrupt (attempt $attempt) — retrying"; sleep 5
done
[ "$ok" = 1 ] || fatal "bundle never verified after 3 attempts"

# --- python env: guard 3.11+, CUDA torch first, then the pinned stack -----------
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "[startup] system python3 = $PYV (repo requires >=3.11)"
case "$PYV" in 3.11|3.12|3.13) : ;; *) fatal "python $PYV < 3.11 — use an ubuntu-2404 image" ;; esac
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
python3 -m venv .venv || fatal "venv creation failed"
source .venv/bin/activate
pip install -q --upgrade pip || fatal "pip upgrade failed"
# CUDA torch BEFORE the repo, so pip install -e . (sentence-transformers) sees torch
# already satisfied and does not pull a CPU build.
pip install -q torch --index-url https://download.pytorch.org/whl/cu121 || fatal "torch install failed"
pip install -q -e . || fatal "pip install -e . failed (deps)"
python -c 'import torch; print("[startup] torch", torch.__version__, "cuda?", torch.cuda.is_available())'

# --- run the eval on the GPU ----------------------------------------------------
export EVAL_PROGRESS_LOG=/var/log/eval_progress.log
export HF_HOME=/opt/hf
mkdir -p out

# --- optional reindex (embedder swap / ADR-009 §P1) -----------------------------
# If a prepare-args.txt is staged, rebuild the FAISS indexes from the BUNDLED
# indexer.toml [embeddings] BEFORE the eval. Required when swapping the embedder: the
# bundled indexes were built with a different model/dimension, so eval over them would
# (correctly) trip the dimension guard. Absent for the default reranker run, which
# reuses the bundled indexes untouched. real_repo_prepare clones the pinned repos +
# re-embeds on the GPU (HF_HOME already set); summarization is off for eval builds.
# NOTE: reindex the SAME repo set the eval runs, or a mixed-embedder index dir will
# fail the dimension guard mid-run.
if gcloud storage cp "gs://$BUCKET/inputs/prepare-args.txt" ./prepare-args.txt 2>/dev/null; then
  PREP_ARGS="$(cat prepare-args.txt)"
  echo "[startup] $(date -u) reindexing (embedder swap): real_repo_prepare.py $PREP_ARGS"
  python -u tools/real_repo_prepare.py $PREP_ARGS || fatal "prepare/reindex failed"
  echo "[startup] $(date -u) reindex done"
else
  echo "[startup] no prepare-args.txt — skipping reindex (reusing bundled indexes)"
fi

if gcloud storage cp "gs://$BUCKET/inputs/eval-args.txt" ./eval-args.txt 2>/dev/null; then
  EVAL_ARGS="$(cat eval-args.txt)"
else
  EVAL_ARGS="--arms B,C"     # default: authoritative reranker run over all repos
fi
echo "[startup] $(date -u) running: real_repo_eval.py $EVAL_ARGS"
python -u tools/real_repo_eval.py $EVAL_ARGS --baseline out/result.jsonl
rc=$?
echo "[startup] $(date -u) eval exit code: $rc"
[ "$rc" = 0 ] || fatal "eval exited non-zero ($rc)"

# --- upload results (logs handled by the EXIT trap) -----------------------------
gcloud storage cp out/result.jsonl "gs://$BUCKET/outputs/result.jsonl" || echo "[startup] WARN: result upload failed"
echo "[startup] $(date -u) done — exiting (trap self-deletes the VM)"
