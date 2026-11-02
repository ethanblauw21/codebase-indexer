"""ADR-049 (B-057): a killed full rebuild never costs the only good copy of the
index, and a bare `reindex()` is not a full rebuild.

Replays the 2026-09-29 incident in-process: two full rebuilds, each killed
partway, after which the original index must still be restorable byte for byte.
A real build runs through run_incremental with a stubbed embedder and the
summarizer off, so no model loads (as test_index_data_safety does). A kill is a
BaseException raised from inside the build, and the restore that `_reindex`'s
handler would run is made to die as well: a killed process runs no handler.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

import anyio
import numpy as np
import pytest
from types import SimpleNamespace

import core
import incremental_indexer as ii
import MCPServer
from core import embed_dimension

BACKUP = ".pre-full-reindex"


class Killed(BaseException):
    """The server process dying (Inspector's 60 s timeout): nothing after it runs."""


def _fake_embed(texts, batch_size=32):
    rng = np.random.default_rng(len(texts))
    return rng.standard_normal((len(texts), embed_dimension())).astype(np.float32)


class _CharTokenizer:
    """One token per character, so the chunker runs without fetching the embedder's
    tokenizer from the network."""
    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]

    def decode(self, tokens):
        return "".join(chr(t) for t in tokens)


def _checkpoint(db_path):
    """Fold the WAL into graph.db, so the file's bytes are the whole database."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _db_bytes(data: bytes) -> bytes:
    """graph.db's bytes without the three 4-byte header counters SQLite's backup API
    bumps on the file it writes: the file change counter (offset 24), the schema
    cookie (40), so open connections reload the schema, and version-valid-for (92).
    Every other byte, every page included, is compared as is."""
    return data[:24] + data[28:40] + data[44:92] + data[96:]


def _fingerprint(index_dir):
    """{file: sha256} of graph.db and every .faiss file, after a checkpoint."""
    db_path = os.path.join(index_dir, "graph.db")
    if os.path.exists(db_path):
        _checkpoint(db_path)
    out = {}
    for name in sorted(os.listdir(index_dir)):
        if name == "graph.db" or name.endswith(".faiss"):
            with open(os.path.join(index_dir, name), "rb") as fh:
                data = fh.read()
            out[name] = hashlib.sha256(_db_bytes(data) if name == "graph.db" else data).hexdigest()
    return out


def _file_rows(index_dir):
    con = sqlite3.connect(os.path.join(index_dir, "graph.db"))
    try:
        return con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A repo of six files, fully indexed, wired so `_reindex` rebuilds it."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(core.jina_tokenizer, "_tokenizer", _CharTokenizer())
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    ii.run_incremental(str(repo), interactive=False)
    real_run = ii.run_incremental
    monkeypatch.setattr(ii, "run_incremental", lambda **kw: real_run(str(repo), **kw))
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: None)
    return str(index)


@pytest.fixture
def kill_partway(monkeypatch):
    """``arm()`` makes the next full rebuild die after embedding its first batch;
    ``revive()`` is the server starting again afterwards."""
    real_restore = MCPServer._restore_index
    state = {"dead": False}

    def arm():
        state["dead"] = False
        calls = []

        def embed_then_die(texts, batch_size=32):
            calls.append(1)
            if len(calls) > 1:
                state["dead"] = True
                raise Killed
            return _fake_embed(texts, batch_size)

        # Small batches, so the build dies with part of the repo written.
        monkeypatch.setattr(core, "embed_batch", embed_then_die)
        monkeypatch.setattr(ii, "_EMBED_WINDOW_TEXTS", 1)

    def restore_unless_dead(*args):
        if state["dead"]:
            raise Killed                  # the process is gone; the handler never runs
        return real_restore(*args)

    def revive():
        state["dead"] = False
        monkeypatch.setattr(core, "embed_batch", _fake_embed)

    monkeypatch.setattr(MCPServer, "_restore_index", restore_unless_dead)
    return SimpleNamespace(arm=arm, revive=revive)


def test_two_killed_full_rebuilds_leave_the_original_index_restorable(live, kill_partway):
    """The incident: run 1 killed, run 2 killed. Run 2 used to delete run 1's backup."""
    original = _fingerprint(live)
    assert any(n.endswith(".faiss") for n in original)

    kill_partway.arm()
    with pytest.raises(Killed):
        MCPServer._reindex(changed_files_only=False)
    kill_partway.revive()
    assert os.path.isdir(os.path.join(live, BACKUP)), "run 1's kill took the backup with it"
    assert _file_rows(live) < 6                           # the live index is half-built

    kill_partway.arm()
    with pytest.raises(Killed):
        MCPServer._reindex(changed_files_only=False)
    kill_partway.revive()
    assert _file_rows(live) < 6
    backup = os.path.join(live, BACKUP)
    assert os.path.isdir(backup), "run 2 deleted the only good copy"

    MCPServer._restore_index(backup, live, os.path.join(live, "graph.db"))
    assert _fingerprint(live) == original


def test_a_full_rebuild_after_a_killed_one_finishes_and_removes_the_backup(live, kill_partway):
    original = _fingerprint(live)
    kill_partway.arm()
    with pytest.raises(Killed):
        MCPServer._reindex(changed_files_only=False)
    kill_partway.revive()

    MCPServer._reindex(changed_files_only=False)

    assert not os.path.exists(os.path.join(live, BACKUP))
    assert _file_rows(live) == 6
    assert {n for n in _fingerprint(live) if n.endswith(".faiss")} == \
        {n for n in original if n.endswith(".faiss")}


def test_a_backup_found_at_startup_is_restored_never_deleted(live):
    original = _fingerprint(live)
    db_path = os.path.join(live, "graph.db")
    backup = MCPServer._snapshot_index(live, db_path)
    # What a killed run leaves: the live index wiped, the backup still there.
    con = sqlite3.connect(db_path)
    with con:
        con.execute("DELETE FROM files")
    con.close()
    for name in os.listdir(live):
        if name.endswith(".faiss"):
            os.remove(os.path.join(live, name))

    again = MCPServer._snapshot_index(live, db_path)

    assert again == backup and os.path.isdir(backup)
    assert _fingerprint(live) == original                 # restored before the new run wipes it
    assert _fingerprint(backup) == original


def test_a_snapshot_killed_while_copying_is_discarded_and_the_live_index_kept(live):
    """A .partial directory was never renamed into place, so the wipe never ran."""
    original = _fingerprint(live)
    partial = os.path.join(live, BACKUP + ".partial")
    os.makedirs(partial)
    with open(os.path.join(partial, "tier1_surgical.faiss"), "wb") as fh:
        fh.write(b"\x00" * 16)                              # a truncated copy

    backup = MCPServer._snapshot_index(live, os.path.join(live, "graph.db"))

    assert not os.path.exists(partial)
    assert _fingerprint(live) == original
    assert _fingerprint(backup) == original


def test_a_restore_killed_partway_can_run_again(live, monkeypatch):
    """The restore copies out of the backup instead of moving files, so a kill
    mid-restore leaves the backup whole."""
    original = _fingerprint(live)
    db_path = os.path.join(live, "graph.db")
    backup = MCPServer._snapshot_index(live, db_path)
    for name in os.listdir(live):
        if name.endswith(".faiss"):
            os.remove(os.path.join(live, name))

    import shutil
    real_copy = shutil.copy2
    copies = []

    def copy_then_die(src, dst, **kw):
        copies.append(src)
        if len(copies) > 1:
            raise Killed
        return real_copy(src, dst, **kw)

    monkeypatch.setattr(shutil, "copy2", copy_then_die)
    with pytest.raises(Killed):
        MCPServer._restore_index(backup, live, db_path)
    monkeypatch.setattr(shutil, "copy2", real_copy)

    assert _fingerprint(backup) == original
    MCPServer._restore_index(backup, live, db_path)
    assert _fingerprint(live) == original


# --------------------------------------------------------------------------
# A bare reindex() is incremental
# --------------------------------------------------------------------------

@pytest.fixture
def recorded_reindex(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(MCPServer, "_reindex", lambda changed_files_only: seen.append(changed_files_only) or "ok")
    import index_lock
    monkeypatch.setattr(index_lock, "worktree_refusal", lambda path: None)   # tests run in worktrees
    monkeypatch.setattr(ii, "INDEX_DIR", str(tmp_path))
    return seen


def test_a_bare_reindex_call_is_incremental(recorded_reindex):
    MCPServer.reindex()
    assert recorded_reindex == [True]


def test_a_bare_reindex_through_mcp_is_incremental(recorded_reindex):
    """What a client sends when it omits the argument: the schema's default."""
    anyio.run(MCPServer.mcp.call_tool, "reindex", {})
    assert recorded_reindex == [True]


def test_a_full_rebuild_still_runs_when_asked_for_by_name(recorded_reindex):
    anyio.run(MCPServer.mcp.call_tool, "reindex", {"changed_files_only": False})
    assert recorded_reindex == [False]


def test_the_listed_schema_defaults_to_incremental():
    (t,) = [t for t in MCPServer.mcp._tool_manager.list_tools() if t.name == "reindex"]
    assert t.parameters["properties"]["changed_files_only"]["default"] is True
    assert "destructive" in t.description and "Slow" in t.description
