"""ADR-045: a chunk's summary is keyed without its `Lines: a-b` header.

Lines inserted above a symbol shift its line numbers without changing its code; keyed on
them, every symbol below an edit was re-summarized.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ast_chunker import chunk_file_ast                           # noqa: E402
from db import SUMMARY_KEY_VERSION, CodeDB, summary_cache_key    # noqa: E402
from incremental_indexer import chunk_text_hash                  # noqa: E402

_T1 = ("File: a.py\nEntity: a.py::f (function)\nTags: [CAT_PARSE]\nLines: {lines}\n"
       "Code:\ndef f():\n    return 1\n")
_T2 = "File: a.py\nScope: Full File (Part 1/1)\nCode:\nimport os\nLines: 1-2\n"


def _md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def test_moved_symbol_keeps_its_key():
    assert summary_cache_key(_T1.format(lines="6-7")) == summary_cache_key(_T1.format(lines="80-81"))


def test_changed_code_changes_the_key():
    a = _T1.format(lines="6-7")
    assert summary_cache_key(a) != summary_cache_key(a.replace("return 1", "return 2"))


def test_other_header_lines_still_count():
    a = _T1.format(lines="6-7")
    assert summary_cache_key(a) != summary_cache_key(a.replace("[CAT_PARSE]", "[CAT_IO]"))


def test_text_without_the_header_keys_as_before():
    """Tier 2/3 slices have no `Lines:` header: their existing cache rows stay valid,
    even when the code itself contains a line that looks like one."""
    assert summary_cache_key(_T2) == _md5(_T2)
    assert summary_cache_key("no header at all") == _md5("no header at all")


def test_both_passes_use_the_same_key():
    text = _T1.format(lines="6-7")
    assert chunk_text_hash(text) == summary_cache_key(text)


def test_real_chunker_output_moves_without_rekeying():
    """End to end on the real tier-1 text builder: two blank lines above a function."""
    src = "def f(x):\n    return x + 1\n"
    before = [c.text for c in chunk_file_ast("m.py", src)]
    after = [c.text for c in chunk_file_ast("m.py", "\n\n" + src)]
    assert before != after                                   # the header did change
    assert [summary_cache_key(t) for t in before] == [summary_cache_key(t) for t in after]


def _db_with_old_rows(tmp_path, texts):
    db = CodeDB(str(tmp_path / "graph.db"))
    with db._tx() as cur:
        cur.execute("INSERT INTO files(path, content_hash) VALUES ('a.py', 'h')")
        fid = cur.execute("SELECT id FROM files").fetchone()[0]
        for i, t in enumerate(texts):
            cur.execute("INSERT INTO chunks(file_id, scope, tier, start_line, end_line, text, tags)"
                        " VALUES (?, ?, 1, 1, 2, ?, '')", (fid, f"s{i}", t))
            cur.execute("INSERT INTO chunk_summaries(text_hash, summary) VALUES (?, ?)",
                        (_md5(t), f"summary {i}"))
        cur.execute("DELETE FROM index_meta WHERE key = 'summary_key'")   # a pre-ADR database
    db.close()
    return str(tmp_path / "graph.db")


def test_migration_rekeys_every_cached_summary(tmp_path):
    texts = [_T1.format(lines="6-7"), _T2]
    path = _db_with_old_rows(tmp_path, texts)
    db = CodeDB(path)
    try:
        got = db.get_cached_summaries([summary_cache_key(t) for t in texts])
        assert got == {summary_cache_key(texts[0]): "summary 0", summary_cache_key(texts[1]): "summary 1"}
        assert db.get_cached_summaries([_md5(texts[0])])       # old rows stay
        assert db.meta_get("summary_key") == SUMMARY_KEY_VERSION
    finally:
        db.close()


def test_migration_runs_once(tmp_path):
    path = _db_with_old_rows(tmp_path, [_T1.format(lines="6-7")])
    CodeDB(path).close()
    db = CodeDB(path)
    try:
        n = db._conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0]
        assert n == 2                                           # old + re-keyed, no more
    finally:
        db.close()


def test_fresh_database_is_marked(tmp_path):
    db = CodeDB(str(tmp_path / "graph.db"))
    try:
        assert db.meta_get("summary_key") == SUMMARY_KEY_VERSION
    finally:
        db.close()

