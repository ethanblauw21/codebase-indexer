"""ADR-036 (B-032): one reindex at a time in an MCP server process.

run_incremental is a fake that blocks until the test lets it finish, so each test controls exactly
which events arrive while a run is in flight. No index, no GPU.
"""
from __future__ import annotations

import threading
import time

import pytest

import incremental_indexer
import MCPServer


class Runs:
    """A run_incremental that records how many runs overlap."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = self.peak = self.count = 0
        self.started = threading.Semaphore(0)
        self.permits = threading.Semaphore(0)     # one permit lets one run finish

    def finish(self, n=1):
        self.permits.release(n)

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.active += 1
            self.count += 1
            self.peak = max(self.peak, self.active)
        self.started.release()
        self.permits.acquire(timeout=5)
        with self.lock:
            self.active -= 1
        return ""


@pytest.fixture
def runs(monkeypatch):
    r = Runs()
    monkeypatch.setattr(incremental_indexer, "run_incremental", r)
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: None)
    yield r
    r.finish(100)


def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def _fired(d):
    return lambda: d._timer is None


def test_saves_during_a_run_queue_one_follow_up_instead_of_overlapping(runs):
    d = MCPServer._ReindexDebouncer(delay=0.01)
    d.schedule()
    assert runs.started.acquire(timeout=5)
    for _ in range(5):                  # five separate saves while the first run is in flight
        d.schedule()
        _until(_fired(d))
        time.sleep(0.02)
    runs.finish(2)
    _until(lambda: runs.count == 2 and runs.active == 0)
    time.sleep(0.1)
    assert runs.count == 2              # the first run, then one run covering all five saves
    assert runs.peak == 1


def test_a_save_after_the_follow_up_started_gets_its_own_run(runs):
    d = MCPServer._ReindexDebouncer(delay=0.01)
    d.schedule()
    assert runs.started.acquire(timeout=5)
    d.schedule()                        # queued behind run 1
    _until(lambda: d._queued)
    runs.finish()
    assert runs.started.acquire(timeout=5)   # run 2 is in flight
    d.schedule()
    _until(_fired(d))
    runs.finish(2)
    _until(lambda: runs.count == 3 and runs.active == 0)
    assert runs.peak == 1


def test_the_reindex_tool_waits_for_a_watchdog_run(runs, monkeypatch):
    tool_calls = []

    def fake_reindex(changed_files_only):
        tool_calls.append(runs.active)  # how many watchdog runs were in flight when the tool ran
        return "done"

    monkeypatch.setattr(MCPServer, "_reindex", fake_reindex)
    d = MCPServer._ReindexDebouncer(delay=0.01)
    d.schedule()
    assert runs.started.acquire(timeout=5)
    out = []
    t = threading.Thread(target=lambda: out.append(MCPServer.reindex(changed_files_only=True)))
    t.start()
    time.sleep(0.1)
    assert out == []                    # blocked behind the watchdog run
    runs.finish()
    t.join(5)
    assert out == ["done"] and tool_calls == [0]


def test_the_server_can_print_the_indexer_banner_to_a_windows_pipe():
    """Found on the way: over stdio, stdout is a cp1252 pipe on Windows, and the indexer's
    first print is a "━━" banner. Every watchdog reindex died there with UnicodeEncodeError."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import MCPServer; "
            "MCPServer._utf8_stdio(); print('━━ Incremental Indexer: x ━━')")
    out = subprocess.run([sys.executable, "-c", code, src], capture_output=True, env=env,
                         stdin=subprocess.DEVNULL, timeout=120)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    assert out.stdout.decode("utf-8").strip() == "━━ Incremental Indexer: x ━━"
