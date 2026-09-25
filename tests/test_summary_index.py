"""ADR-030: summaries get their own FAISS index under their chunk's id.

No model is loaded: the embedder is a stub that hashes text into a vector and
records what it was given, and the summarizer is a stub.
"""
import os
import sys

import faiss
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config                               # noqa: E402
import core                                 # noqa: E402
import incremental_indexer as ii            # noqa: E402
from core import DocumentStore              # noqa: E402
from db import CodeDB                       # noqa: E402
from stable_id import TIER_CONFIGS, TIER_NUM, stable_id, to_faiss_ids  # noqa: E402

DIM = 8
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


class StubSummarizer:
    def summarize_batch(self, codes):
        return [f"SUMMARY {len(c)}" for c in codes]


@pytest.fixture
def embedded(monkeypatch):
    seen: list[str] = []

    def fake_embed_batch(texts, batch_size=32):
        seen.extend(texts)
        return np.array([[float((hash(t) >> s) & 0xFF) + 1.0 for s in range(DIM)] for t in texts],
                        dtype=np.float32)

    monkeypatch.setattr(core, "embed_batch", fake_embed_batch)
    return seen


def _indexes():
    out = {name: faiss.IndexIDMap(faiss.IndexFlatIP(DIM)) for name, _, _ in TIER_CONFIGS}
    out[ii.SUMMARY_INDEX] = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
    return out


def _ingest(tmp_path, indexes, tiers=None, monkeypatch=None):
    if tiers is not None:
        monkeypatch.setattr(ii, "summarizer_tiers", lambda: set(tiers))
    with CodeDB(str(tmp_path / "graph.db")) as db:
        store = DocumentStore(str(tmp_path / "graph.db"))
        ii.ingest_file("mod.py", SAMPLE, "h1", indexes, store, db, summarizer=StubSummarizer())
        chunks = ii.chunk_all_tiers("mod.py", SAMPLE)
    return chunks


def test_code_vectors_are_code_only(tmp_path, embedded):
    _ingest(tmp_path, _indexes())
    code_texts = [t for t in embedded if not t.startswith("SUMMARY")]
    assert code_texts and all("# Summary" not in t for t in code_texts)


def test_each_summary_is_stored_under_its_chunks_id(tmp_path, embedded):
    idx = _indexes()
    chunks = _ingest(tmp_path, idx)
    want = {int(i) for tier, cs in chunks.items() for i in to_faiss_ids([stable_id(tier, "mod.py", c.scope) for c in cs])}
    got = {int(i) for i in faiss.vector_to_array(idx[ii.SUMMARY_INDEX].id_map)}
    assert got == want
    assert sum(1 for t in embedded if t.startswith("SUMMARY")) == len(want)


def test_only_configured_tiers_are_summarized(tmp_path, embedded, monkeypatch):
    idx = _indexes()
    chunks = _ingest(tmp_path, idx, tiers=[1], monkeypatch=monkeypatch)
    tier1 = [n for n in chunks if TIER_NUM[n] == 1][0]
    want = {int(i) for i in to_faiss_ids([stable_id(tier1, "mod.py", c.scope) for c in chunks[tier1]])}
    assert {int(i) for i in faiss.vector_to_array(idx[ii.SUMMARY_INDEX].id_map)} == want


def test_stale_removal_clears_the_summary_index_too(tmp_path, embedded):
    idx = _indexes()
    chunks = _ingest(tmp_path, idx)
    ids = to_faiss_ids([stable_id(t, "mod.py", c.scope) for t, cs in chunks.items() for c in cs])
    store = DocumentStore(str(tmp_path / "graph.db"))
    ii.purge_stale_vectors(idx, store, ids)
    assert idx[ii.SUMMARY_INDEX].ntotal == 0
    assert all(i.ntotal == 0 for i in idx.values())


def test_no_summary_index_in_the_dict_still_ingests(tmp_path, embedded):
    idx = _indexes()
    del idx[ii.SUMMARY_INDEX]
    _ingest(tmp_path, idx)
    assert sum(i.ntotal for i in idx.values()) > 0


def test_summarizer_tiers_config(tmp_path, monkeypatch):
    (tmp_path / "indexer.toml").write_text("[summarization]\ntiers = [1]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config.reset_config_cache()
    try:
        assert config.summarizer_tiers() == {1}
    finally:
        config.reset_config_cache()


# ── search ──────────────────────────────────────────────────────────────────

def test_a_summary_hit_lifts_its_chunk(tmp_path, embedded, monkeypatch):
    """A chunk whose summary matches the query outranks one whose code matches slightly better."""
    import hybrid_retriever as hr

    q = np.zeros(DIM, dtype=np.float32); q[0] = 1.0
    near_code = np.array([0.9, 0.44, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    far_code = np.array([0.8, 0.6, 0, 0, 0, 0, 0, 0], dtype=np.float32)

    r = hr.HybridRetriever.__new__(hr.HybridRetriever)
    t1 = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
    t1.add_with_ids(np.stack([near_code, far_code]), np.array([1, 2], dtype=np.int64))
    empty = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
    r._tier1, r._tier2, r._tier3 = t1, empty, empty
    r._fusion_mode, r._bm25 = "rrf", None
    r._summary = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
    r._summary.add_with_ids(q[None, :], np.array([2], dtype=np.int64))
    r._summary_weight = 1.0

    class Store:
        def get(self, fid):
            return {"file": "f.py", "scope": f"f.py::s{fid}", "tier": "tier1_surgical", "text": ""}
    r._doc_store = Store()
    monkeypatch.setattr(hr, "embed", lambda text: q)

    assert [c.faiss_id for c in r._semantic_search("q", 10)] == [2, 1]
    r._summary_weight = 0.0
    assert [c.faiss_id for c in r._semantic_search("q", 10)] == [1, 2]
    r._summary = None
    assert [c.faiss_id for c in r._semantic_search("q", 10)] == [1, 2]
