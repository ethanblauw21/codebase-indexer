"""ADR-054 (B-013): the isolated summarizer worker drops to below-normal priority; the host does not."""
import concurrent.futures
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model_client as mc     # noqa: E402
import summarizer as sm       # noqa: E402

BELOW_NORMAL = 0x00004000


class _FakeKernel32:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def GetCurrentProcess(self):        # noqa: N802 — the Win32 name
        return -1

    def SetPriorityClass(self, handle, cls):   # noqa: N802
        self.calls.append((handle, cls))
        return 1 if self.ok else 0

    def GetLastError(self):             # noqa: N802
        return 5


@pytest.fixture
def posix(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "nice", lambda inc: calls.append(inc) or inc)
    return calls


@pytest.fixture
def windows(monkeypatch):
    import ctypes
    kernel32 = _FakeKernel32()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(os, "nice", lambda inc: pytest.fail("os.nice on Windows"), raising=False)
    return kernel32


def test_posix_worker_renices_itself(posix):
    sm._lower_priority()
    assert posix == [sm._WORKER_NICE] and sm._WORKER_NICE > 0


def test_windows_worker_asks_for_below_normal(windows):
    sm._lower_priority()
    assert windows.calls == [(-1, BELOW_NORMAL)]


def test_windows_failure_is_logged_not_raised(windows, caplog):
    windows.ok = False
    sm._lower_priority()
    assert "could not lower its priority" in caplog.text


def test_posix_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "linux")

    def refuse(_inc):
        raise PermissionError("not allowed")
    monkeypatch.setattr(os, "nice", refuse)
    sm._lower_priority()
    assert "could not lower its priority" in caplog.text


def test_worker_lowers_priority_before_loading_the_model(monkeypatch):
    order = []
    monkeypatch.setattr(sm, "_lower_priority", lambda: order.append("lower"))
    monkeypatch.setattr(sm, "_worker_init", lambda *a: order.append(("load", a)))
    sm._isolated_worker_init("m", "cpu", "float16")
    assert order == ["lower", ("load", ("m", "cpu", "float16"))]


def test_spawn_path_uses_the_lowering_initializer(monkeypatch):
    seen = {}

    class FakePool:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def shutdown(self, **_kw):
            pass
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", FakePool)
    s = sm.IsolatedChunkSummarizer(model_id="m", device="cpu")
    s._ensure_executor()
    assert seen["initializer"] is sm._isolated_worker_init
    assert seen["initargs"] == ("m", "cpu", "float16")
    s._shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="os.nice is POSIX")
def test_a_real_worker_runs_niced():
    parent = os.nice(0)
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, initializer=sm._lower_priority) as pool:
        child = pool.submit(os.nice, 0).result(timeout=60)
    assert child == min(parent + sm._WORKER_NICE, 19)


# ── the model host keeps normal priority (ADR-054 §3) ──────────────────────

@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_model_host_spawn_keeps_normal_priority(platform, monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_INDEXER_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "platform", platform)
    for name, value in (("DETACHED_PROCESS", 0x8), ("CREATE_NEW_PROCESS_GROUP", 0x200)):
        monkeypatch.setattr(subprocess, name, value, raising=False)
    spawned = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: spawned.update(kw))
    mc._spawn()
    spawned["stdout"].close()
    assert not spawned.get("creationflags", 0) & BELOW_NORMAL
    assert "preexec_fn" not in spawned


def test_model_host_summarizer_load_does_not_lower_priority(monkeypatch):
    import model_host
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    monkeypatch.setattr(sm, "_lower_priority", lambda: pytest.fail("the host lowered itself"))
    loaded = []
    monkeypatch.setattr(sm, "_worker_init", lambda *a: loaded.append(a))
    model_host.TorchBackend().load(model_host.SUMMARIZER)
    assert loaded and loaded[0][1] == "cpu"
