"""ADR-048 (#66): a model-host failure never leaves two model copies on the GPU."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config                 # noqa: E402
import model_client as mc     # noqa: E402


class _FakeCore:
    """Stands in for core: records where each in-process embed asked to load."""
    def __init__(self):
        self.devices = []
        self.loaded = False
        self.unloads = 0

    def embed_batch(self, texts, batch_size=32, device=None):
        self.devices.append(device)
        self.loaded = True
        return np.zeros((len(texts), 4), np.float32)

    def embed(self, text, device=None):
        return self.embed_batch([text], device=device)[0]

    def unload_embed_model(self):
        was, self.loaded = self.loaded, False
        self.unloads += was
        return was


@pytest.fixture
def client(monkeypatch):
    """model_client with the host transport stubbed: `state` drives what it sees."""
    state = {"answers": True, "device": "cuda:0", "fail_posts": 0, "posts": 0, "ensures": 0}
    fake = _FakeCore()
    monkeypatch.setitem(sys.modules, "core", fake)
    monkeypatch.setattr(config, "model_host_enabled", lambda: True)
    monkeypatch.setattr(mc, "_warned", set())

    def ensure_host():
        state["ensures"] += 1
        return {}

    def call(method, path, body=None, timeout=0):
        if method == "GET":
            if state["answers"]:
                return {}
            raise mc.HostUnavailable("connection refused")
        state["posts"] += 1
        if state["fail_posts"]:
            state["fail_posts"] -= 1
            raise mc.HostUnavailable("ConnectionResetError on " + path)
        if path == "/v1/summarize":
            return {"summaries": [f"sum:{c}" for c in body["codes"]]}
        return {"shape": [len(body["texts"]), 4], "data": ""}

    monkeypatch.setattr(mc, "ensure_host", ensure_host)
    monkeypatch.setattr(mc, "_call", call)
    monkeypatch.setattr(mc, "_host_device", lambda: state["device"])
    monkeypatch.setattr(mc.model_host, "decode_vectors",
                        lambda d: np.ones(tuple(d["shape"]), np.float32))
    return state, fake


def test_a_host_that_died_mid_call_is_replaced_not_fallen_back_from(client):
    """The #66 trigger: one failed request loaded a whole model copy in the client."""
    state, fake = client
    state.update(fail_posts=1, answers=False)     # the request dies with the host
    out = mc.embed_batch(["a", "b"])
    assert out.shape == (2, 4) and out[0][0] == 1.0      # served by the new host
    assert fake.devices == []                            # nothing loaded in-process
    assert state["ensures"] == 2 and state["posts"] == 2


def test_a_live_host_that_failed_is_not_restarted(client):
    state, fake = client
    state.update(fail_posts=1, answers=True, device="cpu")
    mc.embed_batch(["a"])
    assert state["ensures"] == 1 and state["posts"] == 1     # no second attempt
    assert fake.devices == [None]         # a CPU host: the fallback may use the GPU


def test_the_fallback_stays_off_the_gpu_while_a_cuda_host_runs(client):
    state, fake = client
    state.update(fail_posts=2, answers=True, device="cuda:0")
    mc.embed_batch(["a"])
    mc.embed("a query")
    assert fake.devices == ["cpu", "cpu"]


def test_a_host_that_records_no_device_counts_as_maybe_on_the_gpu(client):
    state, fake = client
    state.update(fail_posts=1, answers=True, device=None)
    mc.embed_batch(["a"])
    assert fake.devices == ["cpu"]


def test_no_host_at_all_falls_back_as_before(client, monkeypatch):
    state, fake = client
    state.update(answers=False)

    def no_host():
        raise mc.HostUnavailable("could not start")
    monkeypatch.setattr(mc, "ensure_host", no_host)
    mc.embed_batch(["a"])
    assert fake.devices == [None]         # the only copy: it may use the GPU


def test_the_fallback_embedder_is_released_once_the_host_serves_again(client, capsys):
    state, fake = client
    state.update(fail_posts=1, answers=True)
    mc.embed_batch(["a"])                 # falls back: a model is loaded in-process
    assert fake.loaded
    mc.embed_batch(["b"])                 # the host serves again
    assert not fake.loaded and fake.unloads == 1
    assert "released the in-process embedder" in capsys.readouterr().out


def test_summaries_are_skipped_not_loaded_beside_a_cuda_host(client, monkeypatch):
    state, _ = client
    state.update(fail_posts=1, answers=True, device="cuda:0")
    import summarizer

    def no_worker():
        raise AssertionError("a second summarizer must not load beside the host")
    monkeypatch.setattr(summarizer, "IsolatedChunkSummarizer", no_worker)
    sm = mc.HostSummarizer()
    assert sm.summarize_batch(["a", "b"]) == ["", ""]
    assert sm.summarize_batch(["c"]) == ["sum:c"]         # not sticky: the host is back
    assert sm.skipped == 2 and "2 skipped" in sm.stats_line()


def test_summaries_retry_a_host_that_died(client):
    state, _ = client
    state.update(fail_posts=1, answers=False)
    assert mc.HostSummarizer().summarize_batch(["a"]) == ["sum:a"]


def test_core_moves_the_model_to_the_requested_device(monkeypatch):
    """core releases a copy loaded elsewhere before loading on the device asked for."""
    import core
    loads = []

    class FakeST:
        def __init__(self, model_id, trust_remote_code, device, model_kwargs):
            loads.append(device)
            self.max_seq_length = None

    monkeypatch.setattr(core, "SentenceTransformer", FakeST)
    monkeypatch.setattr(core, "_embed_model", None)
    monkeypatch.setattr(core, "_embed_device", None)
    monkeypatch.setattr(core, "resolve_device", lambda: "cuda")
    monkeypatch.setattr(core, "embed_dtype", lambda d: None)

    first = core._get_embed_model()
    assert core._get_embed_model() is first                  # cached
    assert core._get_embed_model("cuda") is first            # same device: kept
    core._get_embed_model("cpu")
    assert loads == ["cuda", "cpu"] and core._embed_device == "cpu"
    assert core.unload_embed_model() and core._embed_model is None
    assert core.unload_embed_model() is False
