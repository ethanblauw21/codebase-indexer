"""B-029 — an index records which chunker built it, and says so when that is stale.

Drives run_incremental over a two-file repo with a stubbed embedder and the
summarizer off, so no model loads.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import core
import incremental_indexer as ii
from core import embed_dimension
from db import CodeDB


def _fake_embed(texts, batch_size=32):
    rng = np.random.default_rng(len(texts))
    return rng.standard_normal((len(texts), embed_dimension())).astype(np.float32)


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    return str(repo), str(index / "graph.db")


def _marker(db_path):
    with CodeDB(db_path) as db:
        return db.meta_get(ii.CHUNKER_VERSION_KEY), ii.chunker_version_warning(db)


def test_a_fresh_build_records_the_version(env):
    repo, db_path = env
    ii.run_incremental(repo, interactive=False)
    recorded, warning = _marker(db_path)
    assert recorded == str(ii.CHUNKER_VERSION)
    assert warning is None


def test_a_build_killed_midway_leaves_no_marker_and_warns(env, monkeypatch):
    repo, db_path = env
    real_ingest = ii.ingest_file
    calls = []

    def ingest_then_die(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt  # not caught by the per-file handler, like a kill
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(ii, "ingest_file", ingest_then_die)
    with pytest.raises(KeyboardInterrupt):
        ii.run_incremental(repo, interactive=False)

    recorded, warning = _marker(db_path)
    assert recorded is None
    assert warning is not None

    # The next ordinary run finishes the files but is not a fresh build, so the
    # index stays marked as unknown until a full rebuild.
    monkeypatch.setattr(ii, "ingest_file", real_ingest)
    ii.run_incremental(repo, interactive=False)
    assert _marker(db_path)[0] is None


def test_an_older_version_warns_and_is_not_overwritten(env):
    repo, db_path = env
    ii.run_incremental(repo, interactive=False)
    with CodeDB(db_path) as db:
        db.meta_set(ii.CHUNKER_VERSION_KEY, "1")
    (Path(repo) / "src" / "a.py").write_text("def a():\n    return 3\n", encoding="utf-8")
    ii.run_incremental(repo, interactive=False)
    recorded, warning = _marker(db_path)
    assert recorded == "1"
    assert "chunker v1" in warning and f"v{ii.CHUNKER_VERSION}" in warning
