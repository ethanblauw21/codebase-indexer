"""ADR-051 (B-048 fix 1): a slice's summary key leaves out its `(Part i/N)`.

Adding or dropping one window changed N in every slice of the file, so every slice
missed the summary cache. The summarizer is sent the same normalized text; the
embedded and stored text keep the position. No model loads.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

import core
import incremental_indexer as ii
from ast_chunker import fallback_token_chunker
from core import embed_dimension
from db import SUMMARY_KEY_VERSION, CodeDB, summary_cache_key, summary_input

_SLICE = "File: a.py\nScope: Full File (Part {i}/{n})\nCode:\nimport os\nx = 1\n"
_T1 = ("File: a.py\nEntity: a.py::f (function)\nLines: {lines}\n"
       "Code:\ndef f():\n    return 1\n")


@pytest.fixture(autouse=True)
def _offline_tokens(monkeypatch):
    """Token counts by characters, so chunking needs no tokenizer download."""
    monkeypatch.setattr(core.jina_tokenizer, "count_tokens", lambda s: max(1, len(s) // 4))


def _md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def test_the_key_ignores_the_slice_position():
    assert summary_cache_key(_SLICE.format(i=2, n=5)) == summary_cache_key(_SLICE.format(i=3, n=6))
    assert summary_cache_key(_SLICE.format(i=2, n=5)) == _md5(_SLICE.format(i="", n="")
                                                              .replace(" (Part /)", " (Part)"))


def test_the_code_and_the_scope_still_count():
    a = _SLICE.format(i=1, n=2)
    assert summary_cache_key(a) != summary_cache_key(a.replace("x = 1", "x = 2"))
    assert summary_cache_key(a) != summary_cache_key(a.replace("Full File", "a.py::Big"))


def test_only_the_header_is_normalized():
    """A line in the code that looks like a slice header is code."""
    text = _SLICE.format(i=1, n=2) + "Scope: Full File (Part 1/2)\n"
    assert summary_input(text).endswith("Scope: Full File (Part 1/2)\n")
    assert summary_input(text).startswith("File: a.py\nScope: Full File (Part)\nCode:\n")


def test_tier1_text_keys_as_under_adr045():
    """No slice header: the key is ADR-045's, so every tier-1 cache row stays valid."""
    text = _T1.format(lines="6-7")
    assert summary_input(text) == text
    assert summary_cache_key(text) == _md5(text.replace("\nLines: 6-7", ""))


@pytest.fixture
def small_tokens(monkeypatch):
    """One token per character keeps the windows small and their boundaries obvious."""
    monkeypatch.setattr(core.jina_tokenizer, "count_tokens", lambda s: len(s))


def test_adding_a_window_keeps_the_unchanged_slices_keys(small_tokens):
    lines = [f"value_{i:03d} = {i}" for i in range(40)]
    before = fallback_token_chunker("\n".join(lines), "a.py", 200, 0, parent_scope="Full File")
    after = fallback_token_chunker("\n".join(lines + [f"value_{i:03d} = {i}" for i in range(40, 52)]),
                                   "a.py", 200, 0, parent_scope="Full File")
    assert len(after) > len(before)
    same_code = [(o, n) for o, n in zip(before, after)
                 if o.text.partition("\nCode:\n")[2] == n.text.partition("\nCode:\n")[2]]
    assert len(same_code) >= len(before) - 1                         # all but the old last slice
    for old, new in same_code:
        assert old.text != new.text                                   # N changed in the text
        assert summary_cache_key(old.text) == summary_cache_key(new.text)
        assert ii.chunk_text_hash(old.text) == ii.chunk_text_hash(new.text)

# ── carrying an existing cache over ──────────────────────────────────────────

def _old_db(tmp_path, texts, old_key, version):
    """A database as an older indexer left it: summaries under `old_key(text)`."""
    path = str(tmp_path / "graph.db")
    db = CodeDB(path)
    with db._tx() as cur:
        cur.execute("INSERT INTO files(path, content_hash) VALUES ('a.py', 'h')")
        fid = cur.execute("SELECT id FROM files").fetchone()[0]
        for i, t in enumerate(texts):
            cur.execute("INSERT INTO chunks(file_id, scope, tier, start_line, end_line, text, tags)"
                        " VALUES (?, ?, 2, 1, 2, ?, '')", (fid, f"s{i}", t))
            cur.execute("INSERT INTO chunk_summaries(text_hash, summary) VALUES (?, ?)",
                        (old_key(t), f"summary {i}"))
        if version is None:
            cur.execute("DELETE FROM index_meta WHERE key = 'summary_key'")
        else:
            cur.execute("UPDATE index_meta SET value = ? WHERE key = 'summary_key'", (version,))
    db.close()
    return path


def _adr045_key(text):
    head, marker, body = text.partition("\nCode:\n")
    import re
    head = re.sub(r"\nLines: \d+-\d+\Z", "", head)
    return _md5(head + marker + body)


@pytest.mark.parametrize("old_key,version", [(_adr045_key, "no-lines"), (_md5, None)],
                         ids=["from-adr045", "from-raw-md5"])
def test_an_existing_cache_carries_over_once(tmp_path, old_key, version):
    texts = [_SLICE.format(i=1, n=2), _SLICE.format(i=2, n=2).replace("x = 1", "y = 2"),
             _T1.format(lines="3-4")]
    path = _old_db(tmp_path, texts, old_key, version)
    with CodeDB(path) as db:
        assert db.meta_get("summary_key") == SUMMARY_KEY_VERSION
        got = db.get_cached_summaries([summary_cache_key(t) for t in texts])
        assert got == {summary_cache_key(t): f"summary {i}" for i, t in enumerate(texts)}
        assert db.get_cached_summaries([old_key(texts[0])])           # old rows stay
        n = db._conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0]
    with CodeDB(path) as db:                                           # runs once
        assert db._conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0] == n


def test_a_carried_over_slice_hits_after_a_window_is_added(tmp_path):
    """The pre-upgrade row was written for "Part 1/2"; the file now has three parts."""
    path = _old_db(tmp_path, [_SLICE.format(i=1, n=2)], _adr045_key, "no-lines")
    with CodeDB(path) as db:
        assert db.get_cached_summaries([summary_cache_key(_SLICE.format(i=1, n=3))]) == {
            summary_cache_key(_SLICE.format(i=1, n=3)): "summary 0"}


# ── what the summarizer and the embedder see ─────────────────────────────────

class _Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def summarize_batch(self, codes):
        self.seen.extend(codes)
        return [f"summary {len(c)}" for c in codes]

    def shutdown(self):
        pass


def test_the_summarizer_gets_normalized_text_and_the_embedder_the_original(
        tmp_path, monkeypatch, small_tokens):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "big.py").write_text("\n".join(f"v{i:04d} = {i}" for i in range(600)), encoding="utf-8")
    index = tmp_path / "index"
    index.mkdir()
    embedded: list[str] = []

    def fake_embed(texts, batch_size=32):
        embedded.extend(texts)
        return np.ones((len(texts), embed_dimension()), dtype=np.float32)

    recorder = _Recorder()
    monkeypatch.setattr(core, "embed_batch", fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: True)
    monkeypatch.setattr(ii, "summarizer_model_id", lambda: "fake")
    monkeypatch.setattr("model_client.make_summarizer", lambda: recorder)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    ii.run_incremental(str(repo), interactive=False)

    sent = [s for s in recorder.seen if s.startswith("File: big.py\nScope: Full File")]
    assert len(sent) > 1
    assert all(s.startswith("File: big.py\nScope: Full File (Part)\nCode:\n") for s in sent)
    sliced = [t for t in embedded if t.startswith("File: big.py\nScope: Full File")]
    assert len(sliced) == len(sent)
    assert all(t.startswith("File: big.py\nScope: Full File (Part ") for t in sliced)
    assert not any("(Part)" in t for t in sliced)                      # positions kept
