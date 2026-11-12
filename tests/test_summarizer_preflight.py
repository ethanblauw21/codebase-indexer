"""ADR-058 (B-079): a summarizer that can't load says so, before pass 1.

A fresh install without `accelerate` built 659 files and ended "Done with warnings":
4236 texts without a summary "(oom = the GPU was full)" and 4670 pass-2 cache misses
blamed on "the two passes disagree about chunking". The GPU was empty and the passes
agreed; transformers refused `device_map` inside the worker, so nothing was summarized.
No model loads here.
"""
from __future__ import annotations

import importlib.util

import numpy as np
import pytest

import core
import incremental_indexer as ii
import summarizer as sm
from core import embed_dimension

SAMPLE = '''\
def alpha(x):
    """First function."""
    return x + 1


def delta():
    total = 0
    for i in range(10):
        total += i
    return total
'''


def _hide(monkeypatch, *names):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name in names else real(name, *a, **k))


# --- fix 2: refuse before pass 1 -------------------------------------------------

def test_reason_names_accelerate_when_it_is_missing(monkeypatch):
    _hide(monkeypatch, "accelerate")
    reason = sm.summarizer_unavailable_reason()
    assert "`accelerate` is not installed" in reason
    assert "pip install accelerate" in reason
    assert "[summarization].enabled = false" in reason


def test_no_reason_when_everything_imports(monkeypatch):
    _hide(monkeypatch)
    assert sm.summarizer_unavailable_reason() is None


def _index_paths(tmp_path, monkeypatch):
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))


def test_run_refuses_before_pass_one_when_the_summarizer_cannot_load(
        tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(SAMPLE, encoding="utf-8")
    _index_paths(tmp_path, monkeypatch)
    _hide(monkeypatch, "accelerate")
    monkeypatch.setattr(ii, "summarization_enabled", lambda: True)
    made = []
    monkeypatch.setattr("model_client.make_summarizer", lambda: made.append(1))

    with pytest.raises(sm.SummarizerUnavailable, match="accelerate"):
        ii.run_incremental(str(repo), interactive=False)
    assert made == []                                   # no worker was ever started
    assert "Pass 1 of 2" not in capsys.readouterr().out


def test_disabled_summarization_never_checks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _index_paths(tmp_path, monkeypatch)
    _hide(monkeypatch, "accelerate", "torch", "transformers")
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    ii.run_incremental(str(repo), interactive=False)    # no raise


# --- fix 3: pass 2 doesn't blame chunking for pass 1's failures ------------------

def test_cache_only_summarizer_separates_pass_one_failures():
    stand_in = ii._CacheOnlySummarizer({"failed one", "failed two"})
    assert stand_in.summarize_batch(["failed one", "failed two", "never seen"]) == ["", "", ""]
    assert stand_in.pass1_failed == 2
    assert stand_in.misses == 1


class _BrokenSummarizer:
    """Every text comes back empty, as when the worker can't load the model."""

    def summarize_batch(self, codes):
        return [""] * len(codes)

    def stats_line(self):
        return "0 summarized (oom 0, error 9, blank 0)"

    def shutdown(self):
        pass


def test_pass_one_says_when_it_produced_nothing(tmp_path, capsys):
    from db import CodeDB
    (tmp_path / "mod.py").write_text(SAMPLE, encoding="utf-8")
    failed: set[str] = set()
    with CodeDB(str(tmp_path / "graph.db")) as db:
        missing = ii.run_summarization_pass(["mod.py"], str(tmp_path), db, _BrokenSummarizer(),
                                            failed_out=failed)
    assert missing > 0 and len(failed) == missing
    assert "produced no summaries at all" in capsys.readouterr().out


def test_a_failed_pass_one_is_not_reported_as_a_chunking_disagreement(
        tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(SAMPLE, encoding="utf-8")
    _index_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "embed_batch", lambda texts, batch_size=32:
                        np.ones((len(texts), embed_dimension()), dtype=np.float32))
    monkeypatch.setattr(ii, "summarization_enabled", lambda: True)
    monkeypatch.setattr(ii, "summarizer_model_id", lambda: "fake")
    monkeypatch.setattr(sm, "summarizer_unavailable_reason", lambda: None)
    monkeypatch.setattr("model_client.make_summarizer", _BrokenSummarizer)

    ii.run_incremental(str(repo), interactive=False)
    out = capsys.readouterr().out
    assert "disagree about chunking" not in out
    assert "because pass 1 could not summarize them" in out
    assert "Done with warnings" in out
