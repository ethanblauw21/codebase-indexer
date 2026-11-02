"""ADR-055 (B-010): one result per text in a file, collapsed before the top-k cut.

A file smaller than the tier-2 window has identical tier-2 and tier-3 chunks, and both
were returned. These pin: the twins become one hit from the most specific tier, the
freed slot refills from the next candidate, copies in other files are kept, and every
other hit keeps its order. No model: the retriever is built bare, as in
test_summary_index.py.
"""
import faiss
import numpy as np

import hybrid_retriever as hr

T1, T2, T3 = hr._TIER1_NAME, hr._TIER2_NAME, hr._TIER3_NAME
WHOLE_FILE = "def f():\n    return 1\n"


def mk(fid, score, text, tier=T1, file="f.py", scope=None):
    return hr.RetrievedChunk(faiss_id=fid, score=score, file=file,
                             scope=scope or f"{file}::s{fid}", tier=tier, text=text,
                             source="semantic")


def _bare(monkeypatch, reranker=None):
    r = hr.HybridRetriever.__new__(hr.HybridRetriever)
    r._reranker_enabled = reranker is not None
    r._reranker = reranker
    r._reranker_failed = False
    monkeypatch.setattr(hr, "classify_query", lambda q: [])
    return r


def test_tier2_and_tier3_twins_become_one_tier2_hit_and_the_slot_refills(monkeypatch):
    r = _bare(monkeypatch)
    pool = [mk(1, 0.9, WHOLE_FILE, T3, scope="Full File_part_1"),
            mk(2, 0.8, WHOLE_FILE, T2, scope="Full File_part_1"),
            mk(3, 0.7, "c"), mk(4, 0.6, "d")]
    out = r._rerank("q", pool, top_n=3)
    assert [c.faiss_id for c in out] == [2, 3, 4]   # 4 refills the freed slot
    assert out[0].tier == T2
    assert out[0].score == 0.9                      # the text keeps its best rank


def test_the_more_specific_tier_wins_when_it_ranks_first_too():
    out = hr._dedupe_same_text([mk(2, 0.9, WHOLE_FILE, T2), mk(1, 0.8, WHOLE_FILE, T3)])
    assert [(c.faiss_id, c.score) for c in out] == [(2, 0.9)]


def test_a_tier1_chunk_equal_to_its_whole_file_stands_for_it():
    out = hr._dedupe_same_text([mk(3, 0.9, WHOLE_FILE, T3), mk(2, 0.8, WHOLE_FILE, T2),
                                mk(1, 0.7, WHOLE_FILE, T1)])
    assert [(c.faiss_id, c.tier, c.score) for c in out] == [(1, T1, 0.9)]


def test_identical_text_in_two_files_keeps_both_copies(monkeypatch):
    # find_similar_code exists to find copies, and the scan tools report per file.
    r = _bare(monkeypatch)
    pool = [mk(1, 0.9, WHOLE_FILE, T2, file="a.py"),
            mk(2, 0.8, WHOLE_FILE, T2, file="vendor/a.py"),
            mk(3, 0.7, "c")]
    out = r._rerank("q", pool, top_n=3)
    assert [(c.faiss_id, c.file) for c in out] == [(1, "a.py"), (2, "vendor/a.py"), (3, "f.py")]


def test_distinct_texts_keep_their_order():
    ranked = [mk(i, 1.0 - i / 10, f"text {i}", tier) for i, tier in
              enumerate([T1, T3, T2, T1, T2, T3, T1])]
    assert hr._dedupe_same_text(list(ranked)) == ranked


def test_a_collapse_leaves_every_other_hit_in_order():
    ranked = [mk(1, 0.9, "a"), mk(2, 0.8, WHOLE_FILE, T3), mk(3, 0.7, "b"),
              mk(4, 0.6, WHOLE_FILE, T2), mk(5, 0.5, "c")]
    assert [c.faiss_id for c in hr._dedupe_same_text(ranked)] == [1, 4, 3, 5]


def test_empty_text_is_never_a_match():
    ranked = [mk(1, 0.9, "", T2), mk(2, 0.8, "", T3)]
    assert [c.faiss_id for c in hr._dedupe_same_text(ranked)] == [1, 2]


def test_the_reranked_path_collapses_too(monkeypatch):
    class Scorer:
        def predict(self, pairs, batch_size):
            return [1.0 if text == WHOLE_FILE else 0.5 for _, text in pairs]

    r = _bare(monkeypatch, reranker=Scorer())
    pool = [mk(1, 0.9, WHOLE_FILE, T3, scope="Full File_part_1"),
            mk(2, 0.8, WHOLE_FILE, T2, scope="Full File_part_1"),
            mk(3, 0.7, "c"), mk(4, 0.6, "d")]
    out = r._rerank("q", pool, top_n=2)
    assert len(out) == 2 and out[0].faiss_id == 2
    assert len({c.text for c in out}) == 2


def test_a_summary_only_twin_does_not_take_a_second_slot(monkeypatch):
    # Chunk 2 (tier 2) is ranked; its tier-3 twin 1 arrives only through its summary,
    # and at a different scope label, so the ADR-030 part filter alone would keep both.
    dim = 4
    q = np.zeros(dim, dtype=np.float32)
    q[0] = 1.0
    r = hr.HybridRetriever.__new__(hr.HybridRetriever)
    r._summary = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
    r._summary.add_with_ids(np.stack([q]), np.array([1], dtype=np.int64))
    r._summary_weight = 1.0
    r._file_chunk_weight = 1.0

    class Store:
        def get(self, fid):
            return {"file": "f.py", "scope": "Full File", "tier": T3, "text": WHOLE_FILE}

    r._doc_store = Store()
    monkeypatch.setattr(hr, "embed", lambda text: q)
    ranked = [mk(2, 1.0, WHOLE_FILE, T2, scope="Full File_part_1"), mk(3, 0.9, "c")]
    out = r._fuse_summaries("q", ranked, 2)
    assert [c.faiss_id for c in out] == [2, 3]
