"""B-050 (ADR-040) — pass 2 embeds a window of files in one call instead of one
call per (file, tier).

The embedder is a fake that (a) records how many texts each call it received
held, and (b) is a pure function of a text's own content, never of which call it
arrived in or what else rode along with it. That second property is what lets
`test_batched_build_matches_the_per_file_path` assert the batched build and the
one-file-at-a-time reference path (`ingest_file`, called directly) produce
byte-identical FAISS ids and SQLite rows: grouping cannot change what a chunk
embeds to. No model is loaded.
"""
from __future__ import annotations

import os

import faiss
import numpy as np
import pytest

import core
import incremental_indexer as ii
from core import DocumentStore, embed_dimension
from db import CodeDB
from stable_id import TIER_CONFIGS


def _fake_embed(texts, batch_size=32):
    """Deterministic per TEXT, not per call, so batching texts differently can
    never change the vector a given chunk gets."""
    dim = embed_dimension()
    return np.array(
        [[float((hash(t) >> (8 * i)) & 0xFF) for i in range(dim)] for t in texts],
        dtype=np.float32,
    )


def _make_repo(tmp_path, name, n_files):
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    for i in range(n_files):
        (repo / "src" / f"m{i}.py").write_text(
            f"def f{i}(x):\n    return x + {i}\n\n\ndef g{i}(y):\n    return y * {i}\n",
            encoding="utf-8",
        )
    return repo


@pytest.fixture
def recorder(monkeypatch):
    """Records the size of every embed_batch call, underneath the real
    (deterministic-per-text) fake."""
    calls: list[int] = []

    def recording(texts, batch_size=32):
        calls.append(len(texts))
        return _fake_embed(texts, batch_size=batch_size)

    monkeypatch.setattr(core, "embed_batch", recording)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    return calls


def _tier_ids(index_dir: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for name, _, _ in TIER_CONFIGS:
        path = os.path.join(index_dir, f"{name}.faiss")
        if not os.path.exists(path):
            out[name] = []
            continue
        idx = faiss.read_index(path)
        out[name] = sorted(faiss.vector_to_array(idx.id_map).tolist())
    return out


def _chunk_rows(db_path: str) -> list[tuple]:
    with CodeDB(db_path) as db:
        rows = db._conn.execute(
            "SELECT f.path, c.tier, c.scope FROM chunks c "
            "JOIN files f ON f.id = c.file_id"
        ).fetchall()
        return sorted(tuple(row) for row in rows)


def test_a_multi_file_build_embeds_in_one_large_batch(tmp_path, recorder, monkeypatch):
    n_files = 8
    repo = _make_repo(tmp_path, "repo", n_files)
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))

    ii.run_incremental(str(repo), interactive=False)

    total_chunks = sum(len(ids) for ids in _tier_ids(str(index)).values())
    assert total_chunks > 0

    # Before B-050: one embed_batch call per (file, tier) with a chunk, so 8 tiny
    # files would have made up to 24 calls, most of them 1-4 texts. All of them
    # fit comfortably under one window here.
    assert len(recorder) == 1, f"expected one batched call, got {len(recorder)}: {recorder}"
    assert recorder[0] == total_chunks
    assert total_chunks > n_files, "the single batch should span more than one file's chunks"


def test_a_window_boundary_produces_more_than_one_batch(tmp_path, recorder, monkeypatch):
    """A repo bigger than one window still flushes more than once — the point of
    _EMBED_WINDOW_TEXTS is to bound memory, not to force a single call regardless
    of size."""
    monkeypatch.setattr(ii, "_EMBED_WINDOW_TEXTS", 6)
    repo = _make_repo(tmp_path, "repo", n_files=8)
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))

    ii.run_incremental(str(repo), interactive=False)

    assert len(recorder) > 1, "a small window should force more than one flush"
    assert sum(recorder) == sum(len(ids) for ids in _tier_ids(str(index)).values())
    # Every completed batch met the window before flushing; only the final one
    # (whatever was left) may be smaller.
    assert all(n >= 6 for n in recorder[:-1]), recorder


def test_batched_build_matches_the_per_file_path(tmp_path, recorder, monkeypatch):
    """The whole point: batching when and how texts are embedded must not change
    what gets written. Compares a normal (batched) run_incremental build against
    the same files run one at a time through ingest_file, its reference path."""
    n_files = 5
    repo = _make_repo(tmp_path, "repo", n_files)

    batched_dir = tmp_path / "batched"
    batched_dir.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(batched_dir))
    monkeypatch.setattr(ii, "DB_PATH", str(batched_dir / "graph.db"))
    ii.run_incremental(str(repo), interactive=False)

    # Reference path: ingest_file, once per file, no cross-file batching at all.
    per_file_dir = tmp_path / "per_file"
    per_file_dir.mkdir()
    ref_db_path = str(per_file_dir / "graph.db")
    ref_db = CodeDB(ref_db_path)
    ref_store = DocumentStore(ref_db_path)
    ref_indexes = {
        name: faiss.IndexIDMap(faiss.IndexFlatIP(embed_dimension()))
        for name, _, _ in TIER_CONFIGS
    }
    for i in range(n_files):
        rel = f"src/m{i}.py"
        content = (repo / rel).read_text(encoding="utf-8")
        ii.ingest_file(rel, content, "h", ref_indexes, ref_store, ref_db)
    ref_db.close()
    for name, idx in ref_indexes.items():
        faiss.write_index(idx, os.path.join(str(per_file_dir), f"{name}.faiss"))

    # Cross-file batching changed the number and size of embed_batch calls...
    assert len(recorder) < n_files * len(TIER_CONFIGS)
    # ...but every FAISS id and every chunk row is identical either way.
    assert _tier_ids(str(batched_dir)) == _tier_ids(str(per_file_dir))
    assert _chunk_rows(str(batched_dir / "graph.db")) == _chunk_rows(ref_db_path)
