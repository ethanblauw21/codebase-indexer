"""ADR-025 — per-file content-change timestamps + index freshness metadata.

These tests exercise the DB schema/accessors and the git-derived timestamp helpers
directly. They deliberately never call run_incremental / the embedder, so the whole
suite is pure SQLite + git subprocess — no model load, no GPU, ~instant.

Acceptance mapping (#20):
  - a file's stamp advances iff content changed .......... test_unchanged_leaves_stamp,
                                                            test_changed_restamps
  - WHERE content_changed_at > ? returns only real changes  test_query_excludes_null_and_old
  - MAX(indexed_at) must not regress (segmem's live query)  test_max_indexed_at_still_answers
  - full-rebuild neutrality (capture/restore principle) ... test_restore_on_hash_match
  - git helpers never raise off a git tree ............... test_git_helpers_on_non_git_dir
"""
from __future__ import annotations

import os
import tempfile

from db import CodeDB
from incremental_indexer import (
    git_change_times,
    git_dirty_paths,
    git_head_commit,
    _now_iso,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fresh_db(tmp_path) -> CodeDB:
    return CodeDB(os.path.join(str(tmp_path), "graph.db"))


# --------------------------------------------------------------------------- #
# §1 schema + §4 index_meta
# --------------------------------------------------------------------------- #

def test_migration_adds_columns_and_meta(tmp_path):
    db = _fresh_db(tmp_path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(files)").fetchall()}
    assert "content_changed_at" in cols
    assert "authored_at" in cols
    # index_meta exists and is seeded with schema_version
    assert db.meta_get("schema_version") == "1"
    db.close()


def test_migration_is_idempotent_on_existing_db(tmp_path):
    p = os.path.join(str(tmp_path), "graph.db")
    CodeDB(p).close()          # create + migrate once
    db = CodeDB(p)             # re-open: migration must no-op, not raise
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(files)").fetchall()}
    assert "content_changed_at" in cols and "authored_at" in cols
    db.close()


def test_meta_get_set_roundtrip(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.meta_get("nope") is None
    db.meta_set("last_indexed_commit", "abc123")
    assert db.meta_get("last_indexed_commit") == "abc123"
    db.meta_set("last_indexed_commit", "def456")   # upsert overwrites
    assert db.meta_get("last_indexed_commit") == "def456"
    db.close()


def test_seed_does_not_clobber_existing_schema_version(tmp_path):
    p = os.path.join(str(tmp_path), "graph.db")
    db = CodeDB(p)
    db.meta_set("schema_version", "99")
    db.close()
    db2 = CodeDB(p)            # _seed_index_meta uses INSERT OR IGNORE
    assert db2.meta_get("schema_version") == "99"
    db2.close()


# --------------------------------------------------------------------------- #
# §1/§2 upsert stamping
# --------------------------------------------------------------------------- #

def _stamp_of(db, path):
    row = db._conn.execute(
        "SELECT content_changed_at, authored_at FROM files WHERE path = ?", (path,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def test_first_index_stores_backdated_stamp(tmp_path):
    db = _fresh_db(tmp_path)
    wrote = db.upsert_file(
        path="a.py", content_hash="h1", symbols=[], edges=[],
        content_changed_at="2020-01-01T00:00:00Z", authored_at="2019-01-01T00:00:00Z",
    )
    assert wrote is True
    assert _stamp_of(db, "a.py") == ("2020-01-01T00:00:00Z", "2019-01-01T00:00:00Z")
    db.close()


def test_untracked_null_stamp_is_stored_as_null(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert_file(path="vendored.py", content_hash="h", symbols=[], edges=[],
                   content_changed_at=None, authored_at=None)
    assert _stamp_of(db, "vendored.py") == (None, None)
    db.close()


def test_unchanged_leaves_stamp(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert_file(path="a.py", content_hash="h1", symbols=[], edges=[],
                   content_changed_at="2020-01-01T00:00:00Z")
    # Re-upsert with the SAME hash → early return, no write, stamp preserved.
    wrote = db.upsert_file(path="a.py", content_hash="h1", symbols=[], edges=[],
                           content_changed_at="2099-12-31T00:00:00Z")
    assert wrote is False
    assert _stamp_of(db, "a.py")[0] == "2020-01-01T00:00:00Z"
    db.close()


def test_changed_restamps(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert_file(path="a.py", content_hash="h1", symbols=[], edges=[],
                   content_changed_at="2020-01-01T00:00:00Z")
    # Same path, DIFFERENT hash → conflict branch stamps now().
    wrote = db.upsert_file(path="a.py", content_hash="h2", symbols=[], edges=[],
                           content_changed_at="2020-01-01T00:00:00Z")
    assert wrote is True
    assert _stamp_of(db, "a.py")[0] != "2020-01-01T00:00:00Z"
    db.close()


# --------------------------------------------------------------------------- #
# §3 full-rebuild neutrality (restore-on-hash-match principle)
# --------------------------------------------------------------------------- #

def test_restore_on_hash_match(tmp_path):
    """The MCPServer §3 restore updates the stamp only when content_hash matches.
    Verified here at the SQL level the restore uses."""
    db = _fresh_db(tmp_path)
    # Simulate a rebuild that re-stamped two files to 'now', then restore old stamps.
    db.upsert_file(path="same.py", content_hash="h", symbols=[], edges=[],
                   content_changed_at="2030-01-01T00:00:00Z")   # rebuilt stamp
    db.upsert_file(path="changed.py", content_hash="hNEW", symbols=[], edges=[],
                   content_changed_at="2030-01-01T00:00:00Z")   # rebuilt stamp
    preserved = {
        "same.py":    ("h",    "2021-06-01T00:00:00Z", None),   # hash matches → restore
        "changed.py": ("hOLD", "2021-06-01T00:00:00Z", None),   # hash differs → keep rebuilt
    }
    with db._tx() as cur:
        for p, (h, cc, au) in preserved.items():
            cur.execute(
                "UPDATE files SET content_changed_at = ?, authored_at = ? "
                "WHERE path = ? AND content_hash = ?",
                (cc, au, p, h),
            )
    assert _stamp_of(db, "same.py")[0] == "2021-06-01T00:00:00Z"      # restored
    assert _stamp_of(db, "changed.py")[0] == "2030-01-01T00:00:00Z"   # untouched
    db.close()


# --------------------------------------------------------------------------- #
# §1 one-time backfill of legacy NULL stamps
# --------------------------------------------------------------------------- #

def test_backfill_null_stamps_fills_only_nulls(tmp_path):
    from incremental_indexer import _backfill_null_stamps
    db = _fresh_db(tmp_path)
    db.upsert_file(path="legacy.py", content_hash="h", symbols=[], edges=[],
                   content_changed_at=None)                       # legacy NULL row
    db.upsert_file(path="keep.py", content_hash="h2", symbols=[], edges=[],
                   content_changed_at="2020-01-01T00:00:00Z")     # already stamped
    git_times = {
        "legacy.py": ("2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z"),
        "keep.py":   ("2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
    }
    n = _backfill_null_stamps(db, git_times)
    assert n == 1                                                 # only the NULL row filled
    assert _stamp_of(db, "legacy.py") == ("2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z")
    assert _stamp_of(db, "keep.py")[0] == "2020-01-01T00:00:00Z"  # existing stamp untouched
    assert _backfill_null_stamps(db, git_times) == 0              # idempotent second run
    db.close()


# --------------------------------------------------------------------------- #
# §7 the consumer contract: the queries segmem runs
# --------------------------------------------------------------------------- #

def test_query_excludes_null_and_old(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert_file(path="recent.py",  content_hash="a", symbols=[], edges=[],
                   content_changed_at="2026-07-16T00:00:00Z")
    db.upsert_file(path="old.py",     content_hash="b", symbols=[], edges=[],
                   content_changed_at="2020-01-01T00:00:00Z")
    db.upsert_file(path="untracked.py", content_hash="c", symbols=[], edges=[],
                   content_changed_at=None)
    rows = db._conn.execute(
        "SELECT path FROM files "
        "WHERE content_changed_at IS NOT NULL AND content_changed_at > ? "
        "ORDER BY path",
        ("2026-07-01T00:00:00Z",),
    ).fetchall()
    paths = [r[0] for r in rows]
    assert paths == ["recent.py"]     # NULL excluded, old excluded
    db.close()


def test_max_indexed_at_still_answers(tmp_path):
    """segmem's codemap connector runs SELECT MAX(indexed_at) FROM files — the
    additive columns must not break it."""
    db = _fresh_db(tmp_path)
    db.upsert_file(path="a.py", content_hash="h", symbols=[], edges=[],
                   content_changed_at="2026-07-16T00:00:00Z")
    val = db._conn.execute("SELECT MAX(indexed_at) FROM files").fetchone()[0]
    assert val is not None and val.endswith("Z")
    db.close()


# --------------------------------------------------------------------------- #
# §2 git helpers
# --------------------------------------------------------------------------- #

def test_now_iso_shape():
    ts = _now_iso()
    assert ts.endswith("Z") and "T" in ts and len(ts) == 20


def test_git_change_times_real_repo():
    times = git_change_times(_REPO_ROOT)
    assert isinstance(times, dict) and times, "expected non-empty map for this git repo"
    # a long-lived tracked file should be present with ISO committer/author stamps
    committer, author = times["src/db.py"]
    assert committer.endswith(("Z", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) or "T" in committer
    assert "T" in committer and "T" in author


def test_git_dirty_paths_returns_set():
    dirty = git_dirty_paths(_REPO_ROOT)
    assert isinstance(dirty, set)   # content depends on working tree; just must not raise


def test_git_head_commit_real_repo():
    head = git_head_commit(_REPO_ROOT)
    assert head is not None and len(head) == 40


def test_git_helpers_on_non_git_dir():
    with tempfile.TemporaryDirectory() as d:
        # A directory with no git repo — every helper must degrade, never raise.
        assert git_change_times(d) == {}
        assert git_dirty_paths(d) == set()
        assert git_head_commit(d) is None
