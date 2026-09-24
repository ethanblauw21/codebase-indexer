"""Two-pass split: summarize everything, unload the LLM, then embed.

The summarizer and the embedder do not fit on an 8 GB card together. The
indexer therefore fills the summary cache in one pass and embeds in a second,
with the LLM unloaded in between. That only works if both passes agree exactly
about how a file is chunked, because the cache is keyed by an md5 of the chunk
text. These tests pin that agreement, and the miss-accounting that catches it
if it ever breaks. No model is loaded — the summarizer is a stub.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB                       # noqa: E402
import incremental_indexer as ii            # noqa: E402


SAMPLE = '''\
import os


def alpha(x):
    """First function."""
    return x + 1


class Beta:
    def gamma(self, y):
        return alpha(y) * 2


def delta():
    total = 0
    for i in range(10):
        total += i
    return total
'''


class StubSummarizer:
    """Records what it was asked to summarize and returns deterministic text."""

    def __init__(self):
        self.calls = 0
        self.seen = 0
        self.batches = []

    def summarize_batch(self, codes):
        self.calls += 1
        self.seen += len(codes)
        self.batches.append(list(codes))
        return [f"summary-of-{len(c)}-chars" for c in codes]


@pytest.fixture
def db(tmp_path):
    with CodeDB(str(tmp_path / "graph.db")) as handle:
        yield handle


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "mod.py").write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "other.py").write_text(SAMPLE.replace("alpha", "omega"), encoding="utf-8")
    return tmp_path


def test_chunking_is_deterministic():
    """The cache key survives being recomputed. If this fails, nothing else holds."""
    first = ii.chunk_all_tiers("mod.py", SAMPLE)
    second = ii.chunk_all_tiers("mod.py", SAMPLE)
    assert list(first) == list(second)
    for tier in first:
        h1 = [ii.chunk_text_hash(c.text) for c in first[tier]]
        h2 = [ii.chunk_text_hash(c.text) for c in second[tier]]
        assert h1 == h2, f"{tier} chunk hashes are not reproducible"


def test_pass_one_caches_every_chunk_pass_two_asks_for(db, repo):
    """The whole point: after pass 1, pass 2 finds every chunk already summarized."""
    files = ["mod.py", "other.py"]
    stub = StubSummarizer()
    ii.run_summarization_pass(files, str(repo), db, stub)

    assert stub.seen > 0, "pre-pass summarized nothing"

    # Replay exactly what ingest_file would look up during the embedding pass.
    misses = 0
    total = 0
    for rel in files:
        content = (repo / rel).read_text(encoding="utf-8")
        for _tier, chunks in ii.chunk_all_tiers(rel, content).items():
            hashes = [ii.chunk_text_hash(c.text) for c in chunks]
            if not hashes:
                continue
            total += len(hashes)
            cached = db.get_cached_summaries(hashes)
            misses += sum(1 for h in hashes if h not in cached)

    assert total > 0
    assert misses == 0, f"{misses} of {total} chunks would reload the LLM in pass 2"


def test_pass_one_is_idempotent(db, repo):
    """Re-running the pre-pass hits the cache and never calls the model again."""
    files = ["mod.py"]
    first = StubSummarizer()
    ii.run_summarization_pass(files, str(repo), db, first)
    assert first.seen > 0

    second = StubSummarizer()
    ii.run_summarization_pass(files, str(repo), db, second)
    assert second.seen == 0, "second pre-pass re-summarized already-cached chunks"


def test_cache_only_summarizer_never_summarizes_but_counts(db):
    """Pass 2's stand-in must not produce summaries, and must report misses."""
    stand_in = ii._CacheOnlySummarizer()
    out = stand_in.summarize_batch(["chunk one", "chunk two", "chunk three"])
    assert out == ["", "", ""]
    assert stand_in.misses == 3


def test_pre_pass_skips_project_descriptor_files(db, tmp_path):
    """Descriptor files carry edges, never chunks, so the LLM must not see them."""
    name = next(iter(ii.PROJECT_FILES))
    (tmp_path / name).write_text("irrelevant content", encoding="utf-8")
    stub = StubSummarizer()
    ii.run_summarization_pass([name], str(tmp_path), db, stub)
    assert stub.seen == 0


def test_pre_pass_survives_an_unreadable_file(db, repo):
    """A missing path is pass 2's problem to report, not a crash in pass 1."""
    stub = StubSummarizer()
    ii.run_summarization_pass(["mod.py", "does_not_exist.py"], str(repo), db, stub)
    assert stub.seen > 0


def test_pass_one_batches_across_files_longest_first(db, repo):
    """ADR-027: pass 1 gathers every file's chunks before summarizing, so the
    batch can grow and each batch holds similar lengths. Per-file calls handed
    the summarizer ~4 chunks at a time."""
    stub = StubSummarizer()
    ii.run_summarization_pass(["mod.py", "other.py"], str(repo), db, stub)
    assert stub.calls == -(-stub.seen // ii._SUMMARY_SLICE), "one call per slice, not per file"
    sent = [c for batch in stub.batches for c in batch]
    assert [len(c) for c in sent] == sorted((len(c) for c in sent), reverse=True)
    assert len(set(sent)) == len(sent), "a distinct text is summarized once"
    assert any("mod.py" in c for c in stub.batches[0]) and any("other.py" in c for c in stub.batches[0])


def test_pass_one_summarizes_a_repeated_text_once(db, tmp_path):
    """Two tiers can produce the same text for a tiny file; one summary covers both."""
    (tmp_path / "tiny.py").write_text("x = 1\n", encoding="utf-8")
    stub = StubSummarizer()
    ii.run_summarization_pass(["tiny.py"], str(tmp_path), db, stub)
    texts = [c.text for chunks in ii.chunk_all_tiers("tiny.py", "x = 1\n").values() for c in chunks]
    assert stub.seen == len(set(texts))
