"""index_status `since` handling (B-040).

1. A bad `since` passed through as a text cutoff, so since="garbage" reported
   "0 files changed" as if that were an answer. It is now a tool error.
2. content_changed_at carries the committer's offset ("...-05:00") and was compared
   to a UTC cutoff as a string, so the window was off by the offset.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import incremental_indexer  # noqa: E402
import MCPServer as M  # noqa: E402
from db import CodeDB  # noqa: E402


@pytest.fixture
def stamped_index(tmp_path, monkeypatch):
    """An index dir whose files table holds the given (path, content_changed_at) rows."""
    monkeypatch.setattr(incremental_indexer, "INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(M, "_ensure_indexes", lambda: None)
    for name in ("t1_index", "t2_index", "t3_index"):
        monkeypatch.setattr(M, name, SimpleNamespace(ntotal=0))

    def install(rows):
        with CodeDB(str(tmp_path / "graph.db")) as db, db._tx() as cur:
            for path, ts in rows:
                cur.execute(
                    "INSERT INTO files(path, content_hash, content_changed_at) VALUES (?, ?, ?)",
                    (path, "h:" + path, ts),
                )
    return install


@pytest.mark.parametrize("bad", ["garbage", "7 days", "yesterday", "2026-13-45"])
def test_bad_since_is_an_error(stamped_index, bad):
    stamped_index([])
    with pytest.raises(ValueError, match="since="):
        M.index_status(since=bad)


def test_offset_stamps_compare_as_instants(stamped_index):
    """16:30-05:00 is 21:30Z: after a 20:00Z cutoff, although '16' < '20' as text.
    19:00+02:00 is 17:00Z: before it, although '19' < '20' only by luck of the digits."""
    stamped_index([
        ("src/late.py", "2026-09-26T16:30:47-05:00"),
        ("src/early.py", "2026-09-26T19:00:00+02:00"),
    ])
    out = M.index_status(since="2026-09-26T20:00:00Z")
    changed = out.split("files with content changed since")[1]
    assert "src/late.py" in changed
    assert "src/early.py" not in changed
    assert "(1)" in changed


def test_relative_and_iso_forms_still_work(stamped_index):
    stamped_index([("src/a.py", "2000-01-01T00:00:00Z")])
    assert "(0)" in M.index_status(since="7d").split("files with content changed since")[1]
    assert "src/a.py" in M.index_status(since="1999-12-31T00:00:00")


def test_the_recent_list_is_capped_at_the_newest_entries(stamped_index):
    # A bulk change (a merge, a source switch) used to list every file to every session.
    stamped_index([(f"src/f{i:03}.py", f"2026-09-28T12:{i // 60:02}:{i % 60:02}Z")
                   for i in range(100)])
    out = M.index_status(since="2026-09-01T00:00:00Z")
    changed = out.split("files with content changed since")[1]
    assert "(100)" in changed                         # the count is the full one
    listed = [ln for ln in changed.splitlines() if ln.startswith("    2026-")]
    assert len(listed) == 20
    assert listed[0].endswith("src/f099.py")          # newest first
    assert "… 80 more; pass limit=0" in changed
    everything = M.index_status(since="2026-09-01T00:00:00Z", limit=0)
    assert "more; pass limit" not in everything
    assert sum(ln.startswith("    2026-") for ln in
               everything.split("files with content changed since")[1].splitlines()) == 100
