"""Golden tests for the shared eval helpers (tools/eval_common.py, ADR-019).

These lock the metric math that both retrieval scorecards depend on, so the
ADR-007 → ADR-019 extraction can't silently drift MRR/NDCG/CI or the baseline
dedupe. eval_common lives in tools/ (not src/, which pyproject puts on the path),
so add tools/ explicitly.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import eval_common as ec  # noqa: E402


def test_score_query_hit_at_rank_2():
    out = ec.score_query(["a", "b", "c"], {"b": 1})
    assert out["mrr@10"] == 0.5              # b is rank 2 -> 1/2
    assert out["map"] == 0.5
    assert math.isclose(out["ndcg@10"], 1.0 / math.log2(3), rel_tol=1e-9)
    assert out["recall@1"] == 0.0
    assert out["success@1"] == 0.0
    assert out["recall@10"] == 1.0
    assert out["success@10"] == 1.0


def test_score_query_perfect_hit_at_rank_1():
    out = ec.score_query(["b", "a"], {"b": 1})
    assert out["mrr@10"] == 1.0
    assert out["recall@1"] == 1.0
    assert out["success@1"] == 1.0
    assert out["ndcg@10"] == 1.0             # gold at rank 1 -> ideal ordering


def test_score_query_miss():
    out = ec.score_query(["a", "c"], {"b": 1})
    assert out["mrr@10"] == 0.0
    assert out["map"] == 0.0
    assert out["ndcg@10"] == 0.0
    assert out["success@10"] == 0.0


def test_score_query_empty_rel_is_safe():
    out = ec.score_query(["a", "b"], {})
    assert out["mrr@10"] == 0.0
    assert out["recall@10"] == 0.0


def test_ci95_zero_variance_and_singleton():
    assert ec.ci95([1.0, 1.0, 1.0, 1.0]) == 0.0
    assert ec.ci95([0.42]) == 0.0            # < 2 samples -> no interval
    assert ec.ci95([]) == 0.0


def test_ci95_known_value():
    # std(ddof=1) of [0,1] = 0.7071...; 1.96 * s / sqrt(2) = 0.98
    assert math.isclose(ec.ci95([0.0, 1.0]), 0.98, rel_tol=1e-6)


def test_append_baseline_dedupes_on_key_and_sorts(tmp_path):
    path = str(tmp_path / "b.jsonl")
    ec.append_baseline({"subtask": "s2", "config": "dense", "v": 1}, path)
    ec.append_baseline({"subtask": "s1", "config": "dense", "v": 2}, path)
    # same (subtask, config) as the first row -> replaces it, does not duplicate
    ec.append_baseline({"subtask": "s2", "config": "dense", "v": 99}, path)

    rows = list(ec.read_jsonl(path))
    assert len(rows) == 2                                  # deduped
    assert [r["subtask"] for r in rows] == ["s1", "s2"]    # sorted by key
    s2 = next(r for r in rows if r["subtask"] == "s2")
    assert s2["v"] == 99                                   # latest write won


def test_append_baseline_custom_key_fields(tmp_path):
    path = str(tmp_path / "rr.jsonl")
    key = ("repo", "arm", "language")
    ec.append_baseline({"repo": "click", "arm": "B", "language": "py", "mrr": 0.1}, path, key)
    ec.append_baseline({"repo": "click", "arm": "B", "language": "py", "mrr": 0.9}, path, key)
    ec.append_baseline({"repo": "click", "arm": "C", "language": "py", "mrr": 0.5}, path, key)
    rows = list(ec.read_jsonl(path))
    assert len(rows) == 2                                  # (B) replaced, (C) appended
    b = next(r for r in rows if r["arm"] == "B")
    assert b["mrr"] == 0.9
