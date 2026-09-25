"""ADR-028: the model host's policy, transport and client fallback.

No torch and no GPU: the backend is a fake that records what was loaded and when.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model_host as mh  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeBackend:
    """Summarizes one code per 'batch', asking should_yield between batches like ADR-027's loop."""

    def __init__(self, on_batch=None):
        self.loaded: set[str] = set()
        self.events: list[str] = []
        self.summarized: list[str] = []
        self.on_batch = on_batch or (lambda n: None)

    def describe(self):
        return {"embed_model_id": "fake-embed", "embed_dimension": 4, "summarizer_model_id": "fake-sum"}

    def load(self, model):
        assert not self.loaded, f"loading {model} while {self.loaded} is resident"
        self.loaded.add(model)
        self.events.append(f"load {model}")

    def unload(self, model):
        self.loaded.discard(model)
        self.events.append(f"unload {model}")

    def embed(self, texts, kind):
        assert self.loaded == {mh.EMBEDDER}
        self.events.append(f"embed {kind} {len(texts)}")
        return np.array([[float(len(t)), 0, 0, 0] for t in texts], dtype=np.float32)

    def summarize(self, codes, should_yield):
        assert self.loaded == {mh.SUMMARIZER}
        out: list[str | None] = [None] * len(codes)
        for i, code in enumerate(codes):
            if i > 0 and should_yield():
                break
            out[i] = f"sum:{code}"
            self.summarized.append(code)
            self.on_batch(i + 1)
        return out


def make(backend=None, embed_idle_s=10.0, idle_exit_s=100.0, linger=5.0):
    clock = Clock()
    sched = mh.HostScheduler(backend or FakeBackend(), embed_idle_s=embed_idle_s,
                             idle_exit_s=idle_exit_s, summary_linger_s=linger, clock=clock)
    return sched, clock


# ── policy ──────────────────────────────────────────────────────────────────

def test_embeds_go_before_summaries_and_queries_before_index_embeds():
    b = FakeBackend()
    sched, _ = make(b)
    s = sched.submit_summary(["a"])
    i = sched.submit_embed(["xx"], "index")
    q = sched.submit_embed(["yyy"], "query")
    assert sched.step() == "embed" and q.done() and not i.done()
    assert sched.step() == "embed" and i.done()
    assert q.result()[0][0] == 3.0
    assert not s.done()


def test_summaries_wait_while_the_embedder_is_warm_then_swap():
    b = FakeBackend()
    sched, clock = make(b, embed_idle_s=10.0)
    sched.submit_embed(["x"], "query")
    sched.step()
    s = sched.submit_summary(["a", "b"])
    assert sched.step() == "hold"
    clock.t = 10.0
    assert sched.step() == "summarize"
    assert s.result() == ["sum:a", "sum:b"]
    assert b.events == ["load embedder", "embed query 1", "unload embedder", "load summarizer"]


def test_an_embed_preempts_a_summary_run_at_the_next_batch_boundary():
    holder = {}
    b = FakeBackend(on_batch=lambda n: n == 2 and holder.setdefault(
        "q", holder["sched"].submit_embed(["q"], "query")))
    sched, clock = make(b)
    holder["sched"] = sched
    job = sched.submit_summary(["a", "b", "c", "d"])
    assert sched.step() == "summarize"
    assert b.summarized == ["a", "b"]                   # stopped after the batch in flight
    assert not job.done() and sched.counters["yields"] == 1
    assert sched.step() == "embed" and holder["q"].done()
    clock.t = 100.0                                      # embedder goes cold
    assert sched.step() == "summarize"
    assert job.result() == ["sum:a", "sum:b", "sum:c", "sum:d"]
    assert b.summarized == ["a", "b", "c", "d"]         # nothing summarized twice


def test_the_same_text_from_two_projects_is_summarized_once():
    b = FakeBackend()
    sched, _ = make(b)
    one = sched.submit_summary(["shared", "only-one"], project="p1")
    two = sched.submit_summary(["shared"], project="p2")
    sched.step()
    assert b.summarized.count("shared") == 1
    assert one.result() == ["sum:shared", "sum:only-one"] and two.result() == ["sum:shared"]


def test_models_unload_when_idle_and_the_host_exits_later():
    b = FakeBackend()
    sched, clock = make(b, embed_idle_s=10.0, idle_exit_s=100.0, linger=5.0)
    sched.submit_embed(["x"])
    sched.step()
    clock.t = 9.0
    assert sched.step() == "idle" and sched.loaded == mh.EMBEDDER
    clock.t = 10.0
    assert sched.step() == "unload" and sched.loaded is None
    sched.submit_summary(["a"])
    sched.step()
    clock.t = 14.0
    assert sched.step() == "idle" and sched.loaded == mh.SUMMARIZER     # lingering for the next slice
    clock.t = 15.0
    assert sched.step() == "unload"
    clock.t = 109.0                                      # idle_exit_s after the last work
    assert sched.step() == "idle"
    clock.t = 110.0
    assert sched.step() == "exit"


def test_a_backend_failure_reaches_the_waiting_clients():
    class Broken(FakeBackend):
        def embed(self, texts, kind):
            raise RuntimeError("boom")

        def summarize(self, codes, should_yield):
            raise RuntimeError("boom")

    sched, clock = make(Broken())
    e = sched.submit_embed(["x"])
    s = sched.submit_summary(["a"])
    sched.step()
    with pytest.raises(RuntimeError):
        e.result()
    clock.t = 100.0                  # past embed_idle_s, so the summaries get their turn
    assert sched.step() == "summarize"
    assert s.result() == [""]        # the summarizer's contract: "" on failure


def test_stopping_fails_everything_still_queued():
    sched, _ = make()
    s = sched.submit_summary(["a"])
    sched.stop()
    sched.run()
    with pytest.raises(RuntimeError):
        s.result()


# ── transport ───────────────────────────────────────────────────────────────

@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_INDEXER_HOST_DIR", str(tmp_path))
    b = FakeBackend()
    sched = mh.HostScheduler(b, embed_idle_s=0.0, idle_exit_s=60.0, summary_linger_s=0.0)
    token = mh.load_or_create_token()
    server = mh.start_server(sched, token, b.describe)
    mh._write_host_json(server.server_address[1])
    import threading
    t = threading.Thread(target=sched.run, daemon=True)
    t.start()
    yield server, token
    sched.stop()
    t.join(5)
    server.shutdown()


def _post(port, path, body, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_a_request_without_the_token_is_rejected(host):
    server, _token = host
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server.server_address[1], "/v1/embed", {"texts": ["x"]}, "wrong")
    assert err.value.code == 401


def test_embed_and_summarize_round_trip(host):
    server, token = host
    port = server.server_address[1]
    vec = mh.decode_vectors(_post(port, "/v1/embed", {"texts": ["abc", "de"], "kind": "index"}, token))
    assert vec.shape == (2, 4) and vec[0][0] == 3.0
    assert _post(port, "/v1/summarize", {"codes": ["a"]}, token) == {"summaries": ["sum:a"]}


def test_the_token_file_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_INDEXER_HOST_DIR", str(tmp_path))
    assert mh.load_or_create_token() == mh.load_or_create_token()


# ── client ──────────────────────────────────────────────────────────────────

def test_client_uses_the_host_when_enabled(host, monkeypatch):
    import config
    import model_client as mc
    monkeypatch.setattr(config, "model_host_enabled", lambda: True)
    monkeypatch.setattr(mc, "_check_models", lambda info: None)
    monkeypatch.setattr(mc, "_skip_until", 0.0)
    assert mc.embed_batch(["abcd"]).shape == (1, 4)
    assert mc.HostSummarizer().summarize_batch(["a", "b"]) == ["sum:a", "sum:b"]


def test_client_falls_back_in_process_when_the_host_cannot_start(tmp_path, monkeypatch, capsys):
    import config
    import model_client as mc
    monkeypatch.setenv("CODE_INDEXER_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(config, "model_host_enabled", lambda: True)
    monkeypatch.setattr(config, "model_host_spawn_timeout_s", lambda: 0.3)
    monkeypatch.setattr(mc, "_spawn", lambda: None)                 # a host that never comes up
    monkeypatch.setattr(mc, "_skip_until", 0.0)
    monkeypatch.setattr(mc, "_warned", set())
    fake_core = type(sys)("core")
    fake_core.embed_batch = lambda texts, batch_size=32: np.ones((len(texts), 4), np.float32)
    monkeypatch.setitem(sys.modules, "core", fake_core)
    assert mc.embed_batch(["x", "y"]).shape == (2, 4)
    assert "embedding in-process" in capsys.readouterr().out
    # A second call inside the retry window does not wait for the host again.
    assert mc._skip_until > 0


def test_client_refuses_a_host_serving_another_embedder(monkeypatch):
    import model_client as mc
    fake_core = type(sys)("core")
    fake_core.embed_model_id = lambda: "BAAI/bge-code-v1"
    fake_core.embed_dimension = lambda: 1536
    monkeypatch.setitem(sys.modules, "core", fake_core)
    with pytest.raises(mc.HostUnavailable):
        mc._check_models({"embed_model_id": "jina", "embed_dimension": 768,
                          "summarizer_model_id": "x"})


def test_disabled_is_todays_in_process_path(monkeypatch):
    import config
    import model_client as mc
    monkeypatch.setattr(config, "model_host_enabled", lambda: False)
    from summarizer import IsolatedChunkSummarizer
    assert isinstance(mc.make_summarizer(), IsolatedChunkSummarizer)
