"""B-055: pass 2 embeds one window on a background thread while the next window's
files are chunked and parsed.

Same fake embedder as test_cross_file_embed_batching: deterministic per text, so the
overlapped and back-to-back builds must write byte-identical indexes. No model loads.
"""
from __future__ import annotations

import os
import threading

import faiss
import numpy as np
import pytest

import core
import incremental_indexer as ii
from db import CodeDB
from stable_id import TIER_CONFIGS
from core import embed_dimension


def _fake_embed(texts, batch_size=32):
    """Deterministic per text, never per call (see test_cross_file_embed_batching)."""
    dim = embed_dimension()
    return np.array([[float((hash(t) >> (8 * i)) & 0xFF) for i in range(dim)] for t in texts],
                    dtype=np.float32)


def _make_repo(tmp_path, name, n_files):
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    for i in range(n_files):
        (repo / "src" / f"m{i}.py").write_text(
            f"def f{i}(x):\n    return x + {i}\n\n\ndef g{i}(y):\n    return y * {i}\n",
            encoding="utf-8")
    return repo


def _chunk_rows(db_path: str) -> list[tuple]:
    with CodeDB(db_path) as db:
        return sorted(tuple(r) for r in db._conn.execute(
            "SELECT f.path, c.tier, c.scope FROM chunks c JOIN files f ON f.id = c.file_id"))


def _build(tmp_path, monkeypatch, repo, name: str, overlap: bool) -> str:
    index = tmp_path / name
    index.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    monkeypatch.setattr(ii, "embed_overlap", lambda: overlap)
    ii.run_incremental(str(repo), interactive=False)
    return str(index)


def _vectors(index_dir: str) -> dict[str, dict[int, bytes]]:
    out: dict[str, dict[int, bytes]] = {}
    for name, _, _ in TIER_CONFIGS:
        path = os.path.join(index_dir, f"{name}.faiss")
        if not os.path.exists(path):
            continue
        idx = faiss.read_index(path)
        ids = faiss.vector_to_array(idx.id_map)
        flat = idx.index.reconstruct_n(0, idx.ntotal)
        out[name] = {int(i): v.tobytes() for i, v in zip(ids, flat)}
    return out


@pytest.fixture
def small_windows(monkeypatch):
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "_EMBED_WINDOW_TEXTS", 6)     # several windows from a small repo


def test_overlap_writes_exactly_what_back_to_back_writes(tmp_path, monkeypatch, small_windows):
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    repo = _make_repo(tmp_path, "repo", n_files=10)
    serial = _build(tmp_path, monkeypatch, repo, "serial", overlap=False)
    overlapped = _build(tmp_path, monkeypatch, repo, "overlapped", overlap=True)
    assert _chunk_rows(os.path.join(overlapped, "graph.db")) == \
        _chunk_rows(os.path.join(serial, "graph.db"))
    assert _vectors(overlapped) == _vectors(serial)
    assert _vectors(serial)                                   # not vacuous


def test_the_next_window_is_prepared_while_one_is_embedding(tmp_path, monkeypatch, small_windows):
    """The first embed call holds until a later file has started preparing. Back to
    back, that never happens (the loop is waiting on the call), so it times out."""
    prepared_after_first_submit = threading.Event()
    first_call = threading.Event()
    seen = {"threads": set(), "overlapped": None}

    def embed(texts, batch_size=32):
        seen["threads"].add(threading.current_thread().name)
        if not first_call.is_set():
            first_call.set()
            seen["overlapped"] = prepared_after_first_submit.wait(timeout=5)
        return _fake_embed(texts, batch_size)

    real_prepare = ii._prepare_file

    def prepare(**kw):
        if first_call.is_set():
            prepared_after_first_submit.set()
        return real_prepare(**kw)

    monkeypatch.setattr(core, "embed_batch", embed)
    monkeypatch.setattr(ii, "_prepare_file", prepare)
    repo = _make_repo(tmp_path, "repo", n_files=10)
    _build(tmp_path, monkeypatch, repo, "index", overlap=True)
    assert seen["overlapped"] is True
    assert seen["threads"] == {"embed_0"}                  # model calls off the main thread


def test_files_are_written_in_queue_order_across_windows(tmp_path, monkeypatch, small_windows):
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    written: list[str] = []
    real_write = ii._write_plan

    def write(plan, *a, **kw):
        written.append(plan.rel_path)
        return real_write(plan, *a, **kw)

    monkeypatch.setattr(ii, "_write_plan", write)
    repo = _make_repo(tmp_path, "repo", n_files=10)
    _build(tmp_path, monkeypatch, repo, "index", overlap=True)
    assert written == sorted(written) and len(written) == 10


def test_an_embed_failure_still_fails_the_run(tmp_path, monkeypatch, small_windows):
    calls = {"n": 0}

    def embed(texts, batch_size=32):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("host went away")
        return _fake_embed(texts, batch_size)

    monkeypatch.setattr(core, "embed_batch", embed)
    repo = _make_repo(tmp_path, "repo", n_files=10)
    with pytest.raises(RuntimeError, match="host went away"):
        _build(tmp_path, monkeypatch, repo, "index", overlap=True)
    assert not any(t.name.startswith("embed_") for t in threading.enumerate())


def test_pass2_reports_its_timing(tmp_path, monkeypatch, small_windows, capsys):
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    repo = _make_repo(tmp_path, "repo", n_files=4)
    _build(tmp_path, monkeypatch, repo, "index", overlap=True)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "Pass 2 timing" in ln)
    assert "embed call(s)" in line and "waited" in line and "(overlap on)" in line
    with CodeDB(os.path.join(str(tmp_path / "index"), "graph.db")) as db:
        n_chunks = db._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert f"{n_chunks} text(s)" in line
    assert np.isfinite(float(line.split("wall ")[1].split("s")[0]))
