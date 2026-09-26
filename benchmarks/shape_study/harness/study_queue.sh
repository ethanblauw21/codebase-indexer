#!/usr/bin/env bash
# Runs shape_study.py arms one after another (build, then eval), from the master worktree.
# One process per step, so each arm starts with a clean GPU. A failed arm is logged and skipped.
#   bash study_queue.sh <arm> [<arm> ...]
P="python"
KIT="<kit>"
LOGS="$KIT/telemetry/study_logs"
mkdir -p "$LOGS"
cd <master checkout> || exit 1
for arm in "$@"; do
  for step in build eval; do
    echo "$(date +%H:%M:%S) $arm $step start" >> "$LOGS/queue.log"
    env -u CUDA_VISIBLE_DEVICES PYTHONIOENCODING=utf-8 OMP_NUM_THREADS=2 \
      "$P" "$KIT/shape_study.py" $step "$arm" >> "$LOGS/$arm.log" 2>&1
    rc=$?
    echo "$(date +%H:%M:%S) $arm $step exit $rc" >> "$LOGS/queue.log"
    [ $rc -eq 0 ] || break
  done
done
echo "$(date +%H:%M:%S) queue done" >> "$LOGS/queue.log"
