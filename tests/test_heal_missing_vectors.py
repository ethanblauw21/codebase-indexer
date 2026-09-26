"""ADR-037 (B-035) — a run killed before its FAISS save is repaired by the next run.

SQLite rows are committed per file during a run, and FAISS is saved once at the end.
These tests kill a run in between, the way a closed MCP server or a crash would, and
check that the next ordinary run leaves exactly one vector per chunk row in every tier.
They drive run_incremental with a stubbed embedder and the summarizer off, so no model
loads (as test_chunker_version does).
"""
from __future__ import annotations

import os

import faiss
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
    for name, body in (("a", "return 1"), ("b", "return 2"), ("c", "return 3")):
        (repo / "src" / f"{name}.py").write_text(f"def {name}():\n    {body}\n", encoding="utf-8")
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    return repo, str(index)


def _kill_on(monkeypatch, file_name):
    """Make ingest_file die, like a kill, when it reaches file_name."""
    real = ii.ingest_file

    def ingest(rel_path, *args, **kwargs):
        if rel_path.endswith(file_name):
            raise KeyboardInterrupt      # not caught by the per-file handler
        return real(rel_path, *args, **kwargs)

    monkeypatch.setattr(ii, "ingest_file", ingest)
    return real


def _tier_state(index_dir):
    """Per tier: (vectors, distinct ids, chunk rows)."""
    with CodeDB(os.path.join(index_dir, "graph.db")) as db:
        rows = dict(db._conn.execute("SELECT tier, COUNT(*) FROM chunks GROUP BY tier").fetchall())
    out = {}
    for name, _, _ in ii.TIER_CONFIGS:
        path = os.path.join(index_dir, f"{name}.faiss")
        idx = faiss.read_index(path) if os.path.exists(path) else None
        ids = faiss.vector_to_array(idx.id_map) if idx is not None else np.empty(0)
        out[name] = (len(ids), len(set(ids.tolist())), rows.get(ii.TIER_NUM[name], 0))
    return out


def _assert_whole(index_dir):
    for name, (n, distinct, rows) in _tier_state(index_dir).items():
        assert n == distinct == rows, f"{name}: {n} vectors, {distinct} distinct ids, {rows} rows"


def test_a_build_killed_before_its_save_is_healed_by_the_next_run(env, monkeypatch, capsys):
    repo, index_dir = env
    real = _kill_on(monkeypatch, "c.py")
    with pytest.raises(KeyboardInterrupt):
        ii.run_incremental(str(repo), interactive=False)
    # a.py and b.py have rows and an MD5; nothing reached FAISS.
    with CodeDB(os.path.join(index_dir, "graph.db")) as db:
        assert db._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0

    monkeypatch.setattr(ii, "ingest_file", real)
    capsys.readouterr()
    ii.run_incremental(str(repo), interactive=False)
    out = capsys.readouterr().out
    _assert_whole(index_dir)
    assert "[reconcile] 2 file(s) had chunks with no vector" in out


def test_a_modify_killed_before_its_save_leaves_no_duplicate_vectors(env, monkeypatch, capsys):
    repo, index_dir = env
    ii.run_incremental(str(repo), interactive=False)
    _assert_whole(index_dir)

    # b.py changes. The run deletes its rows (committed) and removes its vectors in
    # memory only, then dies before saving, so the old vectors stay on disk.
    (repo / "src" / "b.py").write_text("def b():\n    return 20\n", encoding="utf-8")
    real = _kill_on(monkeypatch, "b.py")
    with pytest.raises(KeyboardInterrupt):
        ii.run_incremental(str(repo), interactive=False)

    monkeypatch.setattr(ii, "ingest_file", real)
    capsys.readouterr()
    ii.run_incremental(str(repo), interactive=False)
    out = capsys.readouterr().out
    _assert_whole(index_dir)
    assert "vector(s) with no chunk row removed" in out


def test_an_intact_index_reconciles_to_nothing(env, capsys):
    """Summaries are off here, so the summary index is empty. That must not flag files."""
    repo, index_dir = env
    ii.run_incremental(str(repo), interactive=False)
    capsys.readouterr()
    ii.run_incremental(str(repo), interactive=False)
    out = capsys.readouterr().out
    assert "[reconcile]" not in out
    assert "Nothing changed" in out


def test_surplus_alone_is_removed_and_saved_on_an_unchanged_repo(env, capsys):
    repo, index_dir = env
    ii.run_incremental(str(repo), interactive=False)
    path = os.path.join(index_dir, "tier1_surgical.faiss")
    idx = faiss.read_index(path)
    idx.add_with_ids(np.ones((1, idx.d), dtype=np.float32), np.array([12345], dtype=np.int64))
    faiss.write_index(idx, path)

    capsys.readouterr()
    ii.run_incremental(str(repo), interactive=False)
    out = capsys.readouterr().out
    _assert_whole(index_dir)
    assert "1 vector(s) with no chunk row removed" in out
    assert "Nothing changed" in out


def test_save_all_writes_the_summary_index_first(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(core.faiss, "write_index", lambda _idx, path: written.append(os.path.basename(path)))
    mgr = core.MultiIndexManager(str(tmp_path))
    for name in ("tier1_surgical", "tier2_component", "tier3_architectural", "summary"):
        mgr.load_or_create(name, dimension=4)
    mgr.save_all()
    assert written[0] == "summary.faiss"
    assert sorted(written[1:]) == ["tier1_surgical.faiss", "tier2_component.faiss",
                                   "tier3_architectural.faiss"]
