"""The index survives a failed save or a failed full rebuild (#49, #50), and a
reindex resets every retriever that holds the old index (#51).

A real build runs through run_incremental with a stubbed embedder and the
summarizer off, so no model loads (as test_heal_missing_vectors does).
"""
from __future__ import annotations

import os

import faiss
import numpy as np
import pytest

import core
import incremental_indexer as ii
import MCPServer
from core import MultiIndexManager, embed_dimension
from db import CodeDB


def _fake_embed(texts, batch_size=32):
    rng = np.random.default_rng(len(texts))
    return rng.standard_normal((len(texts), embed_dimension())).astype(np.float32)


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A repo of three files, fully indexed into tmp_path/index."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for name, body in (("a", "return 1"), ("b", "return 2"), ("c", "return 3")):
        (repo / "src" / f"{name}.py").write_text(f"def {name}():\n    {body}\n", encoding="utf-8")
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    ii.run_incremental(str(repo), interactive=False)
    return repo, str(index)


def _state(index_dir):
    """(file rows, chunk rows, {faiss file: sorted ids})."""
    with CodeDB(os.path.join(index_dir, "graph.db")) as db:
        n_files = db._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_chunks = db._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    ids = {}
    for name in sorted(os.listdir(index_dir)):
        if name.endswith(".faiss"):
            idx = faiss.read_index(os.path.join(index_dir, name))
            ids[name] = sorted(faiss.vector_to_array(idx.id_map).tolist())
    return n_files, n_chunks, ids


def test_a_save_that_dies_mid_write_leaves_the_previous_file_loadable(built, monkeypatch):
    _, index_dir = built
    before = _state(index_dir)
    assert before[2], "the build wrote no FAISS files"

    im = MultiIndexManager(base_dir=index_dir)
    for name, _, _ in ii.TIER_CONFIGS:
        im.load_or_create(name)

    real_write = faiss.write_index

    def dies_mid_write(index, path):
        with open(path, "wb") as fh:          # a truncated file, then the kill
            fh.write(b"\x00" * 16)
        raise KeyboardInterrupt

    monkeypatch.setattr(faiss, "write_index", dies_mid_write)
    with pytest.raises(KeyboardInterrupt):
        im.save_all()
    monkeypatch.setattr(faiss, "write_index", real_write)

    assert _state(index_dir) == before


@pytest.fixture
def reindex_env(built, monkeypatch):
    repo, index_dir = built
    real_run = ii.run_incremental
    monkeypatch.setattr(ii, "run_incremental",
                        lambda **kw: real_run(str(repo), **kw))
    monkeypatch.setattr(MCPServer, "_ensure_indexes", lambda: None)
    reloads = []
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: reloads.append(1))
    return repo, index_dir, reloads


def test_a_full_rebuild_that_raises_restores_the_previous_index(reindex_env, monkeypatch):
    _, index_dir, reloads = reindex_env
    before = _state(index_dir)

    def oom(**kw):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(ii, "run_incremental", oom)
    with pytest.raises(RuntimeError, match="out of memory"):
        MCPServer._reindex(changed_files_only=False)

    assert _state(index_dir) == before
    assert not os.path.exists(os.path.join(index_dir, ".pre-full-reindex"))
    assert reloads, "the restored index was not reloaded into the server"


def test_a_full_rebuild_that_succeeds_removes_its_backup(reindex_env):
    _, index_dir, reloads = reindex_env
    before = _state(index_dir)

    MCPServer._reindex(changed_files_only=False)

    assert _state(index_dir) == before       # same repo, same stable ids
    assert not os.path.exists(os.path.join(index_dir, ".pre-full-reindex"))
    assert reloads


def test_reindex_resets_both_retrievers(reindex_env, monkeypatch):
    monkeypatch.undo()   # use the real _reload_indexes, with stub index objects
    _, index_dir, _ = reindex_env

    class _StubIM:
        def load_or_create(self, name):
            return None

    monkeypatch.setattr(ii, "run_incremental", lambda **kw: None)
    monkeypatch.setattr(ii, "INDEX_DIR", index_dir)
    monkeypatch.setattr(MCPServer, "_ensure_indexes", lambda: None)
    monkeypatch.setattr(MCPServer, "MultiIndexManager", _StubIM)
    monkeypatch.setattr(MCPServer, "DocumentStore", lambda: None)
    monkeypatch.setattr(MCPServer, "_hybrid_retriever", object())
    monkeypatch.setattr(MCPServer, "_iterative_retriever", object())

    MCPServer._reindex(changed_files_only=False)

    assert MCPServer._hybrid_retriever is None
    assert MCPServer._iterative_retriever is None
