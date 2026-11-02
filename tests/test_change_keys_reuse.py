"""ADR-051 §5 (B-034, tier 1): an edited file's unchanged tier-1 chunks keep their vectors.

The fake embedder counts what it is asked to embed and is deterministic per text, so
the overlapped and back-to-back builds can be compared byte for byte (ADR-040). The
summarizer is off. No model loads.
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

SAMPLE = (
    "def alpha(x):\n    return x + 1\n\n\n"
    "def beta(y):\n    return y * 2\n\n\n"
    "def gamma(z):\n    return z - 3\n"
)


@pytest.fixture(autouse=True)
def _offline_tokens(monkeypatch):
    """Token counts by characters, so chunking needs no tokenizer download."""
    monkeypatch.setattr(core.jina_tokenizer, "count_tokens", lambda s: max(1, len(s) // 4))


def _fake_embed(texts, batch_size=32):
    dim = embed_dimension()
    return np.array([[float((hash(t) >> (8 * i)) & 0xFF) + 1.0 for i in range(dim)] for t in texts],
                    dtype=np.float32)


@pytest.fixture
def env(tmp_path, monkeypatch):
    embedded: list[str] = []

    def counting_embed(texts, batch_size=32):
        embedded.extend(texts)
        return _fake_embed(texts)

    monkeypatch.setattr(core, "embed_batch", counting_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    return embedded


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text(SAMPLE, encoding="utf-8")
    (repo / "src" / "other.py").write_text("def other():\n    return 0\n", encoding="utf-8")
    return repo


def _run(tmp_path, monkeypatch, repo, name="index", overlap=True):
    index = tmp_path / name
    index.mkdir(exist_ok=True)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    monkeypatch.setattr(ii, "embed_overlap", lambda: overlap)
    ii.run_incremental(str(repo), interactive=False)
    return str(index)


def _tier1(texts):
    return [t for t in texts if "\nEntity: " in t]


def _vectors(index_dir: str) -> dict[str, dict[int, bytes]]:
    out: dict[str, dict[int, bytes]] = {}
    for name, _, _ in TIER_CONFIGS:
        idx = faiss.read_index(os.path.join(index_dir, f"{name}.faiss"))
        ids = faiss.vector_to_array(idx.id_map)
        flat = idx.index.reconstruct_n(0, idx.ntotal)
        out[name] = {int(i): v.tobytes() for i, v in zip(ids, flat)}
    return out


def _vectors_match_rows(index_dir: str) -> None:
    db_path = os.path.join(index_dir, "graph.db")
    mgr = core.MultiIndexManager(index_dir)
    indexes = {name: mgr.load_or_create(name) for name, _, _ in TIER_CONFIGS}
    with CodeDB(db_path) as db:
        missing, surplus = ii.reconcile_vectors(db, indexes, DocumentStore(db_path))
        n_rows = db._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert (missing, surplus) == ([], 0)
    assert sum(idx.ntotal for idx in indexes.values()) == n_rows


def test_a_one_function_edit_embeds_only_that_functions_tier1_chunk(tmp_path, monkeypatch, env):
    embedded = env
    repo = _repo(tmp_path)
    index = _run(tmp_path, monkeypatch, repo)
    before = _vectors(index)
    embedded.clear()

    (repo / "src" / "m.py").write_text(SAMPLE.replace("y * 2", "y * 7"), encoding="utf-8")
    _run(tmp_path, monkeypatch, repo)

    t1 = _tier1(embedded)
    assert len(t1) == 1 and "beta" in t1[0] and "y * 7" in t1[0]
    assert not any("other" in t for t in embedded)                     # other.py untouched
    after = _vectors(index)
    unchanged = [i for i in before["tier1_surgical"] if i in after["tier1_surgical"]
                 and before["tier1_surgical"][i] == after["tier1_surgical"][i]]
    assert len(unchanged) == len(after["tier1_surgical"]) - 1          # alpha, gamma, other
    _vectors_match_rows(index)


def test_a_moved_function_keeps_its_vector_and_gets_its_new_lines(tmp_path, monkeypatch, env):
    embedded = env
    repo = _repo(tmp_path)
    index = _run(tmp_path, monkeypatch, repo)
    before = _vectors(index)["tier1_surgical"]
    embedded.clear()

    (repo / "src" / "m.py").write_text("import os\n\n\n" + SAMPLE, encoding="utf-8")
    _run(tmp_path, monkeypatch, repo)

    assert not any("alpha" in t or "gamma" in t for t in _tier1(embedded))
    after = _vectors(index)["tier1_surgical"]
    assert all(after[k] == v for k, v in before.items())               # same ids, same bytes
    with CodeDB(os.path.join(index, "graph.db")) as db:
        lines = db._conn.execute(
            "SELECT c.start_line FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.path = 'src/m.py' AND c.scope LIKE '%alpha' AND c.tier = 1").fetchone()
    assert tuple(lines) == (4,)
    _vectors_match_rows(index)


def test_reuse_writes_the_same_bytes_with_the_overlap_on_or_off(tmp_path, monkeypatch, env):
    monkeypatch.setattr(ii, "_EMBED_WINDOW_TEXTS", 3)                   # several windows
    built = {}
    for overlap in (False, True):
        repo = _repo(tmp_path, f"repo-{overlap}")
        (repo / "src" / "n.py").write_text(SAMPLE.replace("alpha", "delta"), encoding="utf-8")
        _run(tmp_path, monkeypatch, repo, f"index-{overlap}", overlap)
        (repo / "src" / "m.py").write_text(SAMPLE.replace("y * 2", "y * 7"), encoding="utf-8")
        (repo / "src" / "n.py").write_text("\n" + SAMPLE.replace("alpha", "delta"), encoding="utf-8")
        built[overlap] = _run(tmp_path, monkeypatch, repo, f"index-{overlap}", overlap)
    assert _vectors(built[False]) == _vectors(built[True])
    for index in built.values():
        _vectors_match_rows(index)


def test_an_all_reused_file_still_writes_its_rows(tmp_path, monkeypatch, env):
    """A file whose pending texts are all tier 2/3 still gets its reused tier-1 vectors
    added: `scatter` runs even when nothing of the file was embedded in tier 1."""
    embedded = env
    repo = _repo(tmp_path)
    index = _run(tmp_path, monkeypatch, repo)
    embedded.clear()
    (repo / "src" / "m.py").write_text(SAMPLE + "\n\n", encoding="utf-8")
    _run(tmp_path, monkeypatch, repo)
    assert _tier1(embedded) == []
    _vectors_match_rows(index)


def test_ingest_file_with_nothing_pending_writes_reused_vectors(tmp_path, monkeypatch, env):
    """The single-file path: every vector reused, so no embed call at all."""
    repo = _repo(tmp_path)
    index = _run(tmp_path, monkeypatch, repo)
    mgr = core.MultiIndexManager(index)
    indexes = {name: mgr.load_or_create(name) for name, _, _ in TIER_CONFIGS}
    db_path = os.path.join(index, "graph.db")
    with CodeDB(db_path) as db:
        reuse = ii.tier1_vectors_to_reuse(db, indexes["tier1_surgical"], ["src/other.py"])
        assert len(reuse) == 1
        plan = ii._prepare_file("src/other.py", "def other():\n    return 0\n", "h",
                                DocumentStore(db_path), db, reuse=reuse)
    assert plan.pending_texts() == [t for tier in ("tier2_component", "tier3_architectural")
                                    for t in plan.tier_texts[tier]]
    plan.tier_texts = {"tier1_surgical": plan.tier_texts["tier1_surgical"]}
    plan.tier_ids = {"tier1_surgical": plan.tier_ids["tier1_surgical"]}
    assert plan.pending_texts() == []
    plan.scatter(None)
    assert plan.tier_vectors["tier1_surgical"].shape == (1, embed_dimension())
