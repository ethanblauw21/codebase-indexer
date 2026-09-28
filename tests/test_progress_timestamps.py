"""ADR-039 (B-049): timestamps on the model host log, the build's progress lines, the
phase banners, and the watchdog's start/complete lines.

No GPU, no model host process, no real reindex: pure formatting functions plus a fake
run_incremental for the watchdog lines, in line with the other tests in this suite.
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB
import incremental_indexer as ii
import model_host as mh
import MCPServer

_HMS = r"\d{2}:\d{2}:\d{2}"
_ISO_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"


@pytest.fixture
def db(tmp_path):
    with CodeDB(str(tmp_path / "graph.db")) as handle:
        yield handle


@pytest.fixture
def repo(tmp_path):
    return tmp_path


# ── incremental_indexer: progress line + ETA from the last slice's rate ─────────────────

def test_hms_formats_a_given_moment_as_local_hh_mm_ss():
    now = datetime(2026, 9, 28, 9, 55, 12)
    assert ii._hms(now) == "09:55:12"


def test_progress_line_uses_the_last_slice_rate_not_an_average():
    """B-049: pass 1 is longest-first, so throughput climbs through a run. The line must
    reflect the slice that just finished, not (done / elapsed-since-start)."""
    now = datetime(2026, 9, 28, 9, 55, 12)
    # 96 texts in the last slice took 60s => 96/min, regardless of how long the whole
    # run (with its much slower early slices) has been going.
    line = ii._format_summary_progress(
        done=768, total=2562, slice_count=96, slice_seconds=60.0, now=now,
    )
    # remaining=1794 at 96/min => 18.6875 min => 09:55:12 + 18:41.25 => 10:13:53
    assert line == (
        "  [summarize 768/2562 · 09:55:12 · 96/min last slice · ETA ~10:13]"
    )


def test_progress_line_eta_follows_the_remaining_count_at_the_last_slice_rate():
    now = datetime(2026, 9, 28, 12, 0, 0)
    # 60 texts/min, 120 remaining => 2 minutes to go => ETA 12:02.
    line = ii._format_summary_progress(
        done=100, total=220, slice_count=60, slice_seconds=60.0, now=now,
    )
    assert "ETA ~12:02" in line
    assert "60/min last slice" in line


def test_progress_line_reports_an_unknown_eta_when_the_slice_took_no_measurable_time():
    now = datetime(2026, 9, 28, 9, 0, 0)
    line = ii._format_summary_progress(
        done=10, total=10, slice_count=10, slice_seconds=0.0, now=now,
    )
    assert "0/min last slice" in line
    assert "ETA ~?" in line


def test_progress_line_reports_done_when_nothing_remains():
    now = datetime(2026, 9, 28, 9, 0, 0)
    line = ii._format_summary_progress(
        done=10, total=10, slice_count=10, slice_seconds=1.0, now=now,
    )
    assert "ETA ~09:00" in line  # remaining=0 => eta == now


# ── incremental_indexer: phase banners carry the same [HH:MM:SS] stamp ─────────────────

def test_pass_one_banner_is_stamped(db, repo, capsys):
    """Smoke-checks the banner text itself; run_summarization_pass's other behaviour is
    covered by tests/test_two_pass_summarization.py."""
    class _StubSummarizer:
        def summarize_batch(self, codes):
            return [f"sum:{c}" for c in codes]

    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ii.run_summarization_pass(["a.py"], str(repo), db, _StubSummarizer())
    out = capsys.readouterr().out
    assert re.search(rf"\[{_HMS}\] ━━ Pass 1 of 2", out)


# ── model_host: every log line gets a local ISO timestamp ──────────────────────────────

def test_configure_logging_prefixes_every_line_with_a_local_iso_timestamp():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        mh._configure_logging()
        assert root.handlers, "expected _configure_logging to install a handler"
        stream = io.StringIO()
        root.handlers[-1].stream = stream
        mh.logger.info("pid %d listening on 127.0.0.1:%d", 123, 4567)
        line = stream.getvalue().strip()
        assert re.match(
            rf"^{_ISO_TS} \[model-host\] pid 123 listening on 127\.0\.0\.1:4567$", line,
        )
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_dropped_connection_logs_one_line_instead_of_a_traceback(caplog):
    server = mh._HostServer.__new__(mh._HostServer)  # handle_error touches no instance state
    with caplog.at_level(logging.INFO, logger="model_host"):
        try:
            raise ConnectionAbortedError("client hung up")
        except ConnectionAbortedError:
            server.handle_error(None, ("127.0.0.1", 54321))
    messages = [r.message for r in caplog.records]
    assert any("dropped the connection" in m and "127.0.0.1" in m for m in messages)
    assert not any("Traceback" in m for m in messages)


@pytest.mark.parametrize("exc_type", [BrokenPipeError, ConnectionResetError])
def test_other_connection_drops_are_also_one_line(caplog, exc_type):
    server = mh._HostServer.__new__(mh._HostServer)
    with caplog.at_level(logging.INFO, logger="model_host"):
        try:
            raise exc_type("dropped")
        except exc_type:
            server.handle_error(None, ("127.0.0.1", 1))
    assert any("dropped the connection" in r.message for r in caplog.records)


def test_a_real_error_still_prints_its_traceback(capsys):
    server = mh._HostServer.__new__(mh._HostServer)
    try:
        raise ValueError("boom")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "Traceback" in capsys.readouterr().err


# ── MCPServer: the watchdog's start/complete lines are stamped ─────────────────────────

def test_watchdog_start_and_complete_lines_are_stamped(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)         # ADR-038's lock files go in a scratch .code-index
    monkeypatch.setattr(MCPServer, "_index_built", lambda index_dir: True)
    monkeypatch.setattr(ii, "run_incremental", lambda *a, **k: None)
    monkeypatch.setattr(MCPServer, "_reload_indexes", lambda: None)
    d = MCPServer._ReindexDebouncer(delay=0.01)
    d._fire()
    out = capsys.readouterr().out
    assert re.search(rf"\[{_HMS}\] \[Watchdog\] Change detected", out)
    assert re.search(rf"\[{_HMS}\] \[Watchdog\] Reindex complete", out)
