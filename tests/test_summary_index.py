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

def _retriever(monkeypatch, weight):
    import hybrid_retriever as hr
    q = np.zeros(DIM, dtype=np.float32); q[0] = 1.0
    r = hr.HybridRetriever.__new__(hr.HybridRetriever)
    r._summary = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
    # chunk 3's summary matches the query best, then chunk 2's
    r._summary.add_with_ids(np.stack([q, np.array([0.6, 0.8, 0, 0, 0, 0, 0, 0], np.float32)]),
                            np.array([3, 2], dtype=np.int64))
    r._summary_weight = weight
    r._file_chunk_weight = hr._DEFAULT_FILE_CHUNK_WEIGHT

    class Store:
        def get(self, fid):
            return {"file": "f.py", "scope": f"f.py::s{fid}", "tier": "tier1_surgical", "text": ""}
    r._doc_store = Store()
    monkeypatch.setattr(hr, "embed", lambda text: q)
    ranked = [hr.RetrievedChunk(faiss_id=i, score=1.0, file="f.py", scope=f"f.py::s{i}",
                                tier="tier1_surgical", text="", source="semantic") for i in (1, 2)]
    return r, ranked


def test_a_summary_hit_lifts_its_chunk_and_adds_a_summary_only_chunk(monkeypatch):
    r, ranked = _retriever(monkeypatch, 1.0)
    out = r._fuse_summaries("q", ranked, 10)
    # 2 is in both lists; 1 is code-only at rank 1; 3 is summary-only at rank 1
    assert [c.faiss_id for c in out][0] == 2
    assert {c.faiss_id for c in out} == {1, 2, 3}
    assert next(c for c in out if c.faiss_id == 3).source == "summary"


def test_fusion_respects_top_n_and_a_low_weight_keeps_code_first(monkeypatch):
    r, ranked = _retriever(monkeypatch, 0.5)
    out = r._fuse_summaries("q", ranked, 2)
    assert len(out) == 2 and out[0].faiss_id == 2


def test_fusion_keeps_one_part_per_split_parent_and_fills_past_them(monkeypatch):
    import hybrid_retriever as hr
    r, _ = _retriever(monkeypatch, 0.5)
    def mk(i, scope, file="f.py", tier="tier1_surgical"):
        return hr.RetrievedChunk(faiss_id=i, score=1.0, file=file, scope=scope,
                                 tier=tier, text="", source="semantic")
    ranked = [mk(10, "f.py::C_part_1"), mk(11, "f.py::C_part_2"),
              mk(12, "Full File_part_1", tier="tier2_component"),
              mk(13, "Full File_part_1", tier="tier3_architectural"),   # same file, other tier
              mk(14, "Full File_part_1", file="g.py", tier="tier2_component"),  # other file
              mk(15, "f.py::m")]
    r._file_chunk_weight = 1.0   # rank order as given; the dedup alone is under test
    out = r._fuse_summaries("q", ranked, 4)
    assert [c.faiss_id for c in out if c.faiss_id >= 10] == [10, 12, 14, 15]


def test_a_whole_file_chunk_counts_less_in_the_code_ranking(monkeypatch):
    import hybrid_retriever as hr
    r, _ = _retriever(monkeypatch, 0.5)
    ranked = [hr.RetrievedChunk(faiss_id=20, score=1.0, file="f.py", scope="Full File_part_1",
                                tier="tier2_component", text="", source="semantic"),
              hr.RetrievedChunk(faiss_id=21, score=1.0, file="f.py", scope="f.py::m",
                                tier="tier1_surgical", text="", source="semantic")]
    r._file_chunk_weight = 1.0
    assert r._fuse_summaries("q", ranked, 2)[0].faiss_id == 20
    r._file_chunk_weight = 0.5
    assert r._fuse_summaries("q", ranked, 2)[0].faiss_id == 21


def test_retrieve_skips_fusion_without_a_summary_index(monkeypatch):
    import hybrid_retriever as hr
    r = hr.HybridRetriever.__new__(hr.HybridRetriever)
    r._summary, r._summary_weight, r._graph_enabled = None, 0.5, False
    r._import_cache = {}
    called = {}
    monkeypatch.setattr(r, "_semantic_search", lambda q, k: called.setdefault("k", k) and [])
    monkeypatch.setattr(r, "_rerank", lambda q, pool, top_n: called.setdefault("top_n", top_n) and [])
    r.retrieve("q")
    assert called == {"k": hr._SEMANTIC_K, "top_n": hr._RERANK_TOP_N}   # byte-for-byte today's call
