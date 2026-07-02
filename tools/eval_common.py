#!/usr/bin/env python3
"""Shared eval helpers — the single implementation behind both retrieval scorecards.

Extracted from ``tools/coir_eval.py`` (ADR-007) so the CoIR harness and the
real-repo harness (ADR-019) grade, score, and persist results identically. Keeping
one copy is the same single-implementation discipline ``src/reranker.py`` /
``src/fusion.py`` applied to ADR-009 — "MRR@10" and "paired lift" must mean the same
thing in every scorecard or the numbers are not comparable (ADR-007 §6).

Nothing here is CoIR- or repo-specific: metric math, the 95%% CI half-width, the
git-SHA stamp, and a parameterized append-baseline (dedupe key + output path passed
by the caller, since CoIR keys on subtask×config and the real-repo eval keys on
repo×arm×language).
"""
import json
import os
import subprocess

import numpy as np

K = 10  # metric depth (MRR@K / NDCG@K); shared so both scorecards report the same @K


def read_jsonl(path):
    """Yield parsed JSON objects from a JSONL file, skipping blank lines."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def git_sha(cwd=None):
    """Short HEAD SHA for stamping a baseline record; ``"unknown"`` if unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=cwd, text=True
        ).strip()
    except Exception:
        return "unknown"


def dcg(gains):
    return sum(g / np.log2(i + 2) for i, g in enumerate(gains))


def score_query(ranked_ids, rel):
    """Grade one query's ranking.

    ranked_ids: top-K doc ids (best first). rel: {doc_id: gain}.
    Returns MRR@10, NDCG@10, MAP, and Recall/Success@{1,5,10}.
    """
    relset = set(rel)

    mrr = 0.0
    for i, d in enumerate(ranked_ids):
        if d in relset:
            mrr = 1.0 / (i + 1)
            break

    # MAP
    n_rel = len(relset)
    num_correct, ap = 0, 0.0
    for i, d in enumerate(ranked_ids):
        if d in relset:
            num_correct += 1
            ap += num_correct / (i + 1)
    ap = ap / n_rel if n_rel else 0.0

    # NDCG@K
    gains = [rel.get(d, 0) for d in ranked_ids]
    ideal = sorted(rel.values(), reverse=True)[:len(ranked_ids)]
    idcg = dcg(ideal)
    ndcg = (dcg(gains) / idcg) if idcg else 0.0

    out = {"mrr@10": mrr, "ndcg@10": ndcg, "map": ap}
    for k in (1, 5, 10):
        topk = set(ranked_ids[:k])
        out[f"recall@{k}"] = len(topk & relset) / n_rel if n_rel else 0.0
        out[f"success@{k}"] = 1.0 if (topk & relset) else 0.0
    return out


def ci95(values):
    """Half-width of the 95%% confidence interval for the mean (normal approx).

    Reported alongside each sampled metric so a subsampled score is an interval,
    never false exactness. For a paired lift, pass the per-query differences — the CI
    then accounts for the cancelled sampling noise automatically.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size < 2:
        return 0.0
    return 1.96 * float(arr.std(ddof=1)) / (arr.size ** 0.5)


def append_baseline(record, path, key_fields=("subtask", "config")):
    """Append ``record`` to a JSONL baseline, replacing any row with the same key.

    ``key_fields`` are the record keys that identify a unique run (CoIR:
    subtask×config; real-repo: repo×arm×language). The file is rewritten sorted by
    the key so diffs stay stable and git-friendly (§6 comparability). Returns ``path``.
    """
    existing = []
    if os.path.exists(path):
        existing = list(read_jsonl(path))
    key = tuple(record[f] for f in key_fields)
    existing = [r for r in existing if tuple(r.get(f) for f in key_fields) != key]
    existing.append(record)
    existing.sort(key=lambda r: tuple("" if r.get(f) is None else r.get(f) for f in key_fields))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path
