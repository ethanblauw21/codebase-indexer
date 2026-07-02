#!/usr/bin/env python3
"""ADR-019 §7 — real-repo retrieval CI tripwire.

The fast regression guard for the retrieval path: one small repo, its committed
fixtures, **arm B only** (today's shipped default — graph on, reranker off, RRF),
graded against a single committed MRR@10 floor. It is the analogue of ADR-007's
``--limit-queries`` smoke: not a publishable number, just a dead-man's switch that
fails the build when a refactor silently breaks retrieval.

Why p-queue: it is the smallest pinned repo (5 TS/JS source files), so a full
clone + production index + 8-query grade runs in well under a minute once the
embedder is cached — cheap enough to gate every retrieval-path change on.

Why arm B / MRR@10 / a floor (not a paired lift):
    A tripwire answers one yes/no question — "did retrieval collapse?" — about the
    config that actually ships (arm B). Lifts (C−B, D−B) drive the ADR-009 enable
    decisions and live in the full scorecard (real_repo_eval.py); they are the wrong
    tool for a regression gate. MRR@10 is the ADR-007 headline metric, so the floor
    is comparable across scorecards.

The floor is deliberately LOOSE. Embeddings are deterministic for a fixed model +
input, so run-to-run MRR is stable; the only expected drift is a HuggingFace model
version bump. The floor therefore sits ~one query's worth of MRR below the measured
arm-B baseline (p-queue B mrr@10 = 0.5875, n=8; one hit@1→miss ≈ −0.12) so ordinary
noise never trips it, while any real break — several queries losing their gold, or a
total collapse toward 0 — clears the margin and fails hard. Tighten it only after a
deliberate re-baseline; the full eval (not this gate) is where small regressions are
caught.

Usage:
    python tools/real_repo_tripwire.py                 # prepare if needed, then check
    python tools/real_repo_tripwire.py --no-prepare    # fail if the index is missing
    python tools/real_repo_tripwire.py --floor 0.50    # override the committed floor
    python tools/real_repo_tripwire.py --repo click    # gate on a different repo

Exit code: 0 if mrr@10 >= floor, 1 otherwise (or on a missing/unprepared repo).
"""
import argparse
import os
import sys

import numpy as np

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)  # sibling tools/ modules (real_repo_eval / real_repo_prepare)

from real_repo_eval import (  # noqa: E402
    load_manifest, load_fixtures, run_arm, index_dir_for, _mean,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# The committed tripwire repo + floor (see module docstring for the derivation).
# p-queue arm-B mrr@10 baseline = 0.5875 (n=8); floor set ~one query below it.
_DEFAULT_REPO = "p-queue"
_DEFAULT_FLOOR = 0.45
_ARM = "B"  # today's shipped default: graph on, reranker off, RRF fusion.


def _repo_from_manifest(name):
    repo = next((r for r in load_manifest() if r["name"] == name), None)
    if repo is None:
        raise SystemExit(f"tripwire: '{name}' is not in the pinned manifest (repos.toml)")
    return repo


def _ensure_prepared(repo, allow_prepare):
    """Return the index dir for ``repo``, cloning + indexing it if it is missing."""
    idx = index_dir_for(repo["name"])
    if os.path.exists(os.path.join(idx, "graph.db")):
        return idx
    if not allow_prepare:
        raise SystemExit(
            f"tripwire: {repo['name']} is not prepared (no index at {idx}) "
            f"and --no-prepare was passed. Run tools/real_repo_prepare.py --only {repo['name']}."
        )
    # Self-prepare: clone at the pinned SHA + build the production index (ADR-021
    # call resolution runs at finalization). Same code path as real_repo_prepare.
    from real_repo_prepare import ensure_clone, build_index
    print(f"tripwire: preparing {repo['name']} (clone + index)…")
    status, corpus_dir = ensure_clone(repo)
    build_index(repo, corpus_dir)
    print(f"tripwire: {repo['name']} prepared ({status}).")
    return idx


def run_tripwire(repo_name=_DEFAULT_REPO, floor=_DEFAULT_FLOOR, allow_prepare=True,
                 verbose=False):
    """Grade arm B on one repo's fixtures; return (passed, mrr10, n)."""
    repo = _repo_from_manifest(repo_name)
    _ensure_prepared(repo, allow_prepare)

    fixtures = load_fixtures(repo_name)
    if not fixtures:
        raise SystemExit(f"tripwire: no fixtures for {repo_name} — nothing to check")

    _, rows = run_arm(repo_name, _ARM, fixtures, verbose=verbose)
    mrr10 = _mean(rows, "mrr@10")
    passed = mrr10 >= floor

    status = "PASS" if passed else "FAIL"
    print(
        f"\n[tripwire] {repo_name} arm {_ARM}: mrr@10 = {mrr10:.4f} "
        f"(floor {floor:.4f}, n={len(rows)})  →  {status}"
    )
    if not passed:
        # Surface the per-query misses so a real break is diagnosable from the log.
        misses = [r for r in rows if r["metrics"]["mrr@10"] == 0.0]
        if misses:
            print(f"  {len(misses)}/{len(rows)} queries returned no gold in top-10:")
            for r in misses:
                print(f"    miss: {r.get('feature', '?')}")
        print(
            "  Retrieval regressed below the committed floor. Investigate before merge; "
            "re-baseline the floor only if the drop is intentional (ADR-019 §7)."
        )
    return passed, mrr10, len(rows)


def main():
    ap = argparse.ArgumentParser(description="ADR-019 §7 real-repo retrieval tripwire (arm B, one repo).")
    ap.add_argument("--repo", default=_DEFAULT_REPO, help=f"repo to gate on (default: {_DEFAULT_REPO})")
    ap.add_argument("--floor", type=float, default=_DEFAULT_FLOOR,
                    help=f"minimum arm-B mrr@10 (default: {_DEFAULT_FLOOR})")
    ap.add_argument("--no-prepare", action="store_true",
                    help="fail instead of cloning+indexing when the repo is not prepared")
    ap.add_argument("--verbose", action="store_true", help="per-query hit/miss lines")
    args = ap.parse_args()

    passed, _, _ = run_tripwire(
        repo_name=args.repo, floor=args.floor,
        allow_prepare=not args.no_prepare, verbose=args.verbose,
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
