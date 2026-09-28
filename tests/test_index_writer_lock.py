"""ADR-038 (B-033, B-053): one writer and one watchdog per index, across processes.

A second process really holds each lock here (a child Python that takes it and
waits), because a threading test can't show what the OS lock does. No models, no GPU.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

import pytest

import config
import incremental_indexer
import index_lock
import MCPServer

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class Holder:
    """A child process that holds ``name`` in ``index_dir`` until stopped."""

    def __init__(self, index_dir: str, name: str) -> None:
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import index_lock; "
            "h = index_lock.try_acquire(sys.argv[2], sys.argv[3], 'test holder'); "
            "print('held' if h else 'busy', flush=True); time.sleep(60)"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, _SRC, index_dir, name],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
        )
        assert self.proc.stdout.readline().strip() == "held"

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait(10)


@pytest.fixture
def index_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return os.path.join(str(tmp_path), incremental_indexer.INDEX_DIR)


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


# ── the lock itself ──────────────────────────────────────────────────────────

def test_the_same_process_can_take_its_own_lock_again(index_dir):
    outer = index_lock.acquire(index_dir, index_lock.WRITE_LOCK, "outer")
    inner = index_lock.acquire(index_dir, index_lock.WRITE_LOCK, "inner")
    inner.release()                     # a child must still see it held after this
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import index_lock; "
            "print(index_lock.try_acquire(sys.argv[2], 'write.lock', 'probe') is None)")
    out = subprocess.run([sys.executable, "-c", code, _SRC, index_dir],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.stdout.strip() == "True"
    outer.release()
    out = subprocess.run([sys.executable, "-c", code, _SRC, index_dir],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.stdout.strip() == "False"


def test_a_second_process_is_refused_and_told_who_holds_it(index_dir):
    h = Holder(index_dir, index_lock.WRITE_LOCK)
    try:
        assert index_lock.try_acquire(index_dir, index_lock.WRITE_LOCK, "me") is None
        with pytest.raises(index_lock.IndexBusy) as exc:
            index_lock.acquire(index_dir, index_lock.WRITE_LOCK, "me")
        assert f"pid {h.proc.pid}" in str(exc.value)
        assert "test holder" in str(exc.value)
    finally:
        h.stop()


def test_a_killed_holder_leaves_no_stale_lock(index_dir):
    h = Holder(index_dir, index_lock.WRITE_LOCK)
    h.stop()
    lock = index_lock.try_acquire(index_dir, index_lock.WRITE_LOCK, "me")
    assert lock is not None
    lock.release()


# ── writers: CLI/run_incremental, the reindex tool, the watchdog ─────────────

def test_run_incremental_refuses_while_another_process_writes(index_dir, monkeypatch):
    ran = []
    monkeypatch.setattr(incremental_indexer, "_run_incremental", lambda *a, **k: ran.append(1))
    h = Holder(index_dir, index_lock.WRITE_LOCK)
    try:
        with pytest.raises(index_lock.IndexBusy):
            incremental_indexer.run_incremental(os.getcwd(), interactive=False)
    finally:
        h.stop()
    assert ran == []
    incremental_indexer.run_incremental(os.getcwd(), interactive=False)
    assert ran == [1]


def test_the_reindex_tool_fails_at_once_while_another_process_writes(index_dir, monkeypatch):
    monkeypatch.setattr(MCPServer, "_reindex", lambda changed_files_only: "done")
    h = Holder(index_dir, index_lock.WRITE_LOCK)
    try:
        with pytest.raises(RuntimeError, match="another process is writing"):
            MCPServer.reindex(changed_files_only=True)
    finally:
        h.stop()
    assert MCPServer.reindex(changed_files_only=True) == "done"


def test_the_reindex_tool_refuses_in_a_linked_worktree(index_dir, monkeypatch):
    monkeypatch.setattr(MCPServer, "_reindex", lambda changed_files_only: "done")
    monkeypatch.setattr(index_lock, "worktree_refusal", lambda path: "a linked git worktree")
    with pytest.raises(RuntimeError, match="linked git worktree"):
        MCPServer.reindex(changed_files_only=True)


def _built_index(index_dir: str) -> None:
    os.makedirs(index_dir, exist_ok=True)
    con = sqlite3.connect(os.path.join(index_dir, "graph.db"))
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO index_meta VALUES ('last_verified_at', '2026-09-28T10:13:18Z')")
    con.execute("INSERT INTO index_meta VALUES ('last_indexed_commit', 'abcdef0123456789')")
    con.commit()
    con.close()


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_indexer, "run_incremental", lambda **k: calls.append(k))
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: None)
    return calls


def test_the_watchdog_skips_when_no_build_has_finished(index_dir, runs, capsys):
    d = MCPServer._ReindexDebouncer(delay=0.01)
    d._fire()
    assert runs == []
    assert "No finished index" in capsys.readouterr().out


def test_the_watchdog_runs_on_a_finished_index(index_dir, runs):
    _built_index(index_dir)
    MCPServer._ReindexDebouncer(delay=0.01)._fire()
    assert len(runs) == 1


def test_the_watchdog_retries_later_while_another_process_writes(index_dir, runs, monkeypatch, capsys):
    _built_index(index_dir)
    retries = []
    d = MCPServer._ReindexDebouncer(delay=0.01)
    monkeypatch.setattr(d, "schedule", lambda delay=None: retries.append(delay))
    h = Holder(index_dir, index_lock.WRITE_LOCK)
    try:
        d._fire()
    finally:
        h.stop()
    assert runs == []
    assert retries == [MCPServer._BUSY_RETRY_S]
    assert f"pid {h.proc.pid}" in capsys.readouterr().out


# ── one watchdog per index ───────────────────────────────────────────────────

@pytest.mark.skipif(not MCPServer._WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_a_second_server_stands_by_and_takes_over_when_the_first_exits(index_dir, monkeypatch):
    monkeypatch.setattr(MCPServer, "_WATCH_RETRY_S", 0.1)
    started = []
    monkeypatch.setattr(MCPServer, "_start_observer",
                        lambda repo, debounce, lock: started.append(lock) or "observer")
    h = Holder(index_dir, index_lock.WATCH_LOCK)
    try:
        assert MCPServer.start_watchdog(os.getcwd()) is None
        time.sleep(0.3)
        assert started == []            # still standing by
    finally:
        h.stop()
    _until(lambda: started)
    started[0].release()


@pytest.mark.skipif(not MCPServer._WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_no_watchdog_in_a_linked_worktree(index_dir, monkeypatch, capsys):
    monkeypatch.setattr(index_lock, "worktree_refusal", lambda path: "a linked git worktree")
    assert MCPServer.start_watchdog(os.getcwd()) is None
    assert "Off:" in capsys.readouterr().out


# ── linked worktrees and the opt-in ──────────────────────────────────────────

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


def test_linked_worktrees_are_refused_unless_they_opt_in(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "init", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)

    assert not index_lock.linked_worktree(str(tmp_path))       # not a repo
    assert not index_lock.linked_worktree(str(main))
    assert index_lock.linked_worktree(str(wt))
    assert index_lock.worktree_refusal(str(main)) is None
    assert "linked git worktree" in index_lock.worktree_refusal(str(wt))

    (wt / "indexer.toml").write_text("[indexer]\nallow_linked_worktree = true\n")
    assert index_lock.worktree_refusal(str(wt)) is None
    config.reset_config_cache()


# ── server instructions ──────────────────────────────────────────────────────

def test_instructions_name_the_checkout_and_its_commit(index_dir):
    _built_index(index_dir)
    text = MCPServer._server_instructions(os.getcwd())
    assert os.getcwd() in text and "abcdef0123" in text
    assert "reindex" in text and "Read" in text
    assert len(text) < 1000             # every connected session pays for this


def test_instructions_without_a_build(index_dir):
    assert "no finished build yet" in MCPServer._server_instructions(os.getcwd())


# ── a server that did not write reloads what another process saved ───────────

def test_a_save_by_another_process_is_reloaded_before_the_next_call(index_dir, monkeypatch):
    os.makedirs(index_dir)
    faiss_file = os.path.join(index_dir, "tier1_surgical.faiss")
    with open(faiss_file, "wb") as fh:
        fh.write(b"v1")
    reloads = []
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: reloads.append(1))
    monkeypatch.setattr(MCPServer, "doc_store", object())
    monkeypatch.setattr(MCPServer, "_loaded_stamp", MCPServer._faiss_stamp())

    MCPServer._ensure_indexes()
    assert reloads == []                # nothing saved since the load

    with open(faiss_file, "wb") as fh:
        fh.write(b"v2 from the watching server")
    MCPServer._ensure_indexes()
    assert reloads == [1]
