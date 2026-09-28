"""B-051 / ADR-041: a CUDA-capable client must not silently ride a CPU-only model host.

No torch and no GPU, and no real host process: device resolution is mocked and
``host.json`` is a plain file this module writes by hand. ``model_client`` is the
only module under test; ``model_host._write_host_json`` gets its own small unit
tests for the write side.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model_client as mc  # noqa: E402
import model_host as mh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_host_dir(tmp_path, monkeypatch):
    """Every test gets its own host directory and a clean once-only-warning set."""
    monkeypatch.setenv("CODE_INDEXER_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(mc, "_warned", set())
    monkeypatch.setattr(mc, "_skip_until", 0.0)
    return tmp_path


def _write_host_json(tmp_path, **fields) -> None:
    with open(mh.host_file("host.json"), "w", encoding="utf-8") as fh:
        json.dump(fields, fh)


# ── model_host: the write side ────────────────────────────────────────────────

def test_write_host_json_includes_the_device_when_given(tmp_path):
    mh._write_host_json(4242, "cuda")
    with open(mh.host_file("host.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["port"] == 4242
    assert payload["device"] == "cuda"


def test_write_host_json_omits_device_when_not_given(tmp_path):
    """An old host (or a backend that reports none) must not write a guessed value."""
    mh._write_host_json(4242)
    with open(mh.host_file("host.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert "device" not in payload


def test_serve_records_the_backends_resolved_device(tmp_path, monkeypatch, caplog):
    """serve() wires backend.describe()['device'] into host.json and the startup line.

    The scheduler and server are faked so this runs synchronously with no thread,
    no lock contention and no model of any kind. ``_write_host_json`` is spied on
    rather than read back afterwards, because ``serve()``'s own ``finally`` removes
    host.json once ``run()`` returns — which the fake scheduler does at once.
    """
    class FakeBackend:
        def describe(self):
            return {"embed_model_id": "fake", "device": "cpu"}

    class FakeServer:
        server_address = ("127.0.0.1", 5150)

        def shutdown(self):
            pass

    class FakeScheduler:
        def __init__(self, backend, **kwargs):
            self.backend = backend

        def run(self):
            pass  # returns at once: "idle_exit_s already elapsed"

    calls = []
    monkeypatch.setattr(mh, "start_server", lambda sched, token, describe: FakeServer())
    monkeypatch.setattr(mh, "HostScheduler", FakeScheduler)
    monkeypatch.setattr(mh, "_write_host_json", lambda port, device=None: calls.append((port, device)))

    caplog.set_level(logging.INFO, logger=mh.__name__)
    mh.serve(backend=FakeBackend())

    assert calls == [(5150, "cpu")]
    assert "device=cpu" in caplog.text


def test_serve_logs_unknown_when_the_backend_reports_no_device(tmp_path, monkeypatch, caplog):
    class FakeBackend:
        def describe(self):
            return {"embed_model_id": "fake"}

    class FakeServer:
        server_address = ("127.0.0.1", 5151)

        def shutdown(self):
            pass

    class FakeScheduler:
        def __init__(self, backend, **kwargs):
            pass

        def run(self):
            pass

    calls = []
    monkeypatch.setattr(mh, "start_server", lambda sched, token, describe: FakeServer())
    monkeypatch.setattr(mh, "HostScheduler", FakeScheduler)
    monkeypatch.setattr(mh, "_write_host_json", lambda port, device=None: calls.append((port, device)))

    caplog.set_level(logging.INFO, logger=mh.__name__)
    mh.serve(backend=FakeBackend())

    assert calls == [(5151, None)]
    assert "device=unknown" in caplog.text


# ── model_client: _host_device ────────────────────────────────────────────────

def test_host_device_reads_the_recorded_field(tmp_path):
    _write_host_json(tmp_path, port=1, device="cuda")
    assert mc._host_device() == "cuda"


def test_host_device_is_none_when_the_field_is_missing(tmp_path):
    _write_host_json(tmp_path, port=1)
    assert mc._host_device() is None


def test_host_device_is_none_when_there_is_no_host_json(tmp_path):
    assert mc._host_device() is None


# ── model_client: _is_idle ─────────────────────────────────────────────────────

@pytest.mark.parametrize("info, expected", [
    ({"loaded": None, "queued": {}}, True),
    ({"loaded": None, "queued": {"proj": {"embeds": 0, "summaries": 0}}}, False),
    ({"loaded": "summarizer", "queued": {}}, False),
    ({"loaded": "embedder", "queued": {}}, False),
])
def test_is_idle(info, expected):
    assert mc._is_idle(info) is expected


# ── model_client: _check_device ────────────────────────────────────────────────

def test_unknown_device_warns_once_and_is_used_as_is(tmp_path, capsys, monkeypatch):
    # No host.json at all: the "no field" and "missing file" cases are the same.
    info = {"loaded": None, "queued": {}}
    monkeypatch.setattr(mc, "_restart_host", lambda: pytest.fail("must not restart an unknown host"))
    out = mc._check_device(info)
    assert out is info
    assert "unknown" in capsys.readouterr().out


def test_unknown_device_warns_only_once(tmp_path, capsys):
    info = {"loaded": None, "queued": {}}
    mc._check_device(info)
    mc._check_device(info)
    assert capsys.readouterr().out.count("[model-client]") == 1


def test_a_cpu_client_finding_a_cuda_host_needs_no_correction(tmp_path, capsys, monkeypatch):
    _write_host_json(tmp_path, port=1, device="cuda")
    monkeypatch.setattr(mc.device, "resolve_device", lambda: "cpu")
    monkeypatch.setattr(mc, "_restart_host", lambda: pytest.fail("must not restart"))
    info = {"loaded": None, "queued": {}}
    assert mc._check_device(info) is info
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_cuda_client_finding_a_cuda_host_needs_no_correction(tmp_path, capsys, monkeypatch):
    _write_host_json(tmp_path, port=1, device="cuda")
    monkeypatch.setattr(mc.device, "resolve_device", lambda: "cuda")
    monkeypatch.setattr(mc, "_restart_host", lambda: pytest.fail("must not restart"))
    info = {"loaded": None, "queued": {}}
    assert mc._check_device(info) is info
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_cuda_client_restarts_an_idle_cpu_host(tmp_path, capsys, monkeypatch):
    _write_host_json(tmp_path, port=1, device="cpu")
    monkeypatch.setattr(mc.device, "resolve_device", lambda: "cuda")
    restarted = {"device": "cuda", "loaded": None, "queued": {}}
    monkeypatch.setattr(mc, "_restart_host", lambda: restarted)
    info = {"loaded": None, "queued": {}}
    out = mc._check_device(info)
    assert out is restarted
    captured = capsys.readouterr()
    assert "cpu" in captured.err and "cuda" in captured.err
    assert captured.out == ""    # the mismatch warning is loud: stderr, not stdout


def test_a_cuda_client_falls_back_when_the_cpu_host_is_busy(tmp_path, capsys, monkeypatch):
    _write_host_json(tmp_path, port=1, device="cpu")
    monkeypatch.setattr(mc.device, "resolve_device", lambda: "cuda")
    monkeypatch.setattr(mc, "_restart_host", lambda: pytest.fail("must not restart a busy host"))
    info = {"loaded": "summarizer", "queued": {}}
    with pytest.raises(mc.HostUnavailable, match="busy"):
        mc._check_device(info)
    assert "cpu" in capsys.readouterr().err


def test_a_cuda_client_falls_back_when_the_restart_itself_fails(tmp_path, capsys, monkeypatch):
    _write_host_json(tmp_path, port=1, device="cpu")
    monkeypatch.setattr(mc.device, "resolve_device", lambda: "cuda")

    def _boom():
        raise mc.HostUnavailable("never came back")
    monkeypatch.setattr(mc, "_restart_host", _boom)
    info = {"loaded": None, "queued": {}}
    with pytest.raises(mc.HostUnavailable, match="never came back"):
        mc._check_device(info)


# ── model_client: _restart_host ────────────────────────────────────────────────

def test_restart_host_shuts_down_then_spawns_and_waits(tmp_path, monkeypatch):
    calls = []

    def fake_call(method, path, body=None, timeout=mc._STATUS_TIMEOUT_S):
        calls.append(path)
        return {}
    monkeypatch.setattr(mc, "_call", fake_call)
    monkeypatch.setattr(mc, "_spawn", lambda: calls.append("spawn"))
    monkeypatch.setattr(mc, "status", lambda: {"device": "cuda", "loaded": None, "queued": {}})
    # No host.json present: the "wait for it to disappear" loop returns at once.

    info = mc._restart_host()

    assert calls == ["/v1/shutdown", "spawn"]
    assert info["device"] == "cuda"


def test_restart_host_tolerates_the_old_host_already_being_gone(tmp_path, monkeypatch):
    def fake_call(method, path, body=None, timeout=mc._STATUS_TIMEOUT_S):
        raise mc.HostUnavailable("no running host")
    monkeypatch.setattr(mc, "_call", fake_call)
    monkeypatch.setattr(mc, "_spawn", lambda: None)
    monkeypatch.setattr(mc, "status", lambda: {"device": "cuda"})

    assert mc._restart_host() == {"device": "cuda"}


def test_restart_host_gives_up_if_the_old_host_never_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_call", lambda method, path, body=None, timeout=mc._STATUS_TIMEOUT_S: {})
    monkeypatch.setattr(mc.config, "model_host_spawn_timeout_s", lambda: 0.1)
    _write_host_json(tmp_path, port=1, device="cpu")   # never removed in this test
    monkeypatch.setattr(mc, "_spawn", lambda: pytest.fail("must not spawn while the old host is up"))

    with pytest.raises(mc.HostUnavailable, match="did not stop"):
        mc._restart_host()


# ── model_client: ensure_host wiring ───────────────────────────────────────────

def test_ensure_host_checks_the_device_before_the_models(monkeypatch):
    order = []
    monkeypatch.setattr(mc, "status", lambda: {"device": "cuda"})
    monkeypatch.setattr(mc, "_check_device", lambda info: order.append("device") or info)
    monkeypatch.setattr(mc, "_check_models", lambda info: order.append("models"))

    assert mc.ensure_host() == {"device": "cuda"}
    assert order == ["device", "models"]


def test_ensure_host_propagates_a_device_related_fallback(monkeypatch):
    monkeypatch.setattr(mc, "status", lambda: {"device": "cpu"})

    def _refuse(info):
        raise mc.HostUnavailable("host is on cpu and busy")
    monkeypatch.setattr(mc, "_check_device", _refuse)
    monkeypatch.setattr(mc, "_check_models", lambda info: pytest.fail("must not be reached"))

    with pytest.raises(mc.HostUnavailable, match="busy"):
        mc.ensure_host()
    assert mc._skip_until > 0
