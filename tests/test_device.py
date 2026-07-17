"""Tests for the shared device-resolution helper (src/device.py, ADR-024, ADR-020).

The ADR-020 tests below prove the override actually REACHES the embedder and both
summarizers — the gap ADR-024 left. All CPU-only: real model loads are faked/monkeypatched
so nothing downloads or touches a GPU (honouring the no-local-GPU constraint)."""
from device import resolve_device


def test_resolve_device_env_override_wins(monkeypatch):
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    assert resolve_device() == "cpu"


def test_resolve_device_env_override_arbitrary_value(monkeypatch):
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "mps")
    assert resolve_device() == "mps"


def test_resolve_device_falls_back_to_cpu_without_cuda(monkeypatch):
    monkeypatch.delenv("CODE_INDEXER_DEVICE", raising=False)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert resolve_device() == "cpu"


def test_resolve_device_picks_cuda_when_available(monkeypatch):
    monkeypatch.delenv("CODE_INDEXER_DEVICE", raising=False)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert resolve_device() == "cuda"


# --------------------------------------------------------------------------- #
# ADR-020 — CODE_INDEXER_DEVICE reaches the embedder + summarizer (the footgun fix)
# --------------------------------------------------------------------------- #

class _FakeST:
    """Stand-in for SentenceTransformer that records the device it was constructed with."""
    last_device = None

    def __init__(self, model_id, trust_remote_code=False, device=None):
        _FakeST.last_device = device
        self.max_seq_length = None


def test_embedder_forces_cpu_when_overridden(monkeypatch):
    """CODE_INDEXER_DEVICE=cpu must make the embedder load on CPU — before ADR-020 the
    embedder ignored the override and sentence-transformers grabbed CUDA regardless."""
    import core
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    monkeypatch.setattr(core, "SentenceTransformer", _FakeST)
    monkeypatch.setattr(core, "_embed_model", None)   # reset lazy singleton; auto-restored
    _FakeST.last_device = None

    core._get_embed_model()
    assert _FakeST.last_device == "cpu"


def test_embedder_honours_cuda_override(monkeypatch):
    """The same channel forces cuda when asked — proving it's the override, not a CPU fallback."""
    import core
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cuda")
    monkeypatch.setattr(core, "SentenceTransformer", _FakeST)
    monkeypatch.setattr(core, "_embed_model", None)
    _FakeST.last_device = None

    core._get_embed_model()
    assert _FakeST.last_device == "cuda"


def test_chunk_summarizer_device_follows_override(monkeypatch):
    from summarizer import ChunkSummarizer
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    assert ChunkSummarizer()._device == "cpu"


def test_isolated_summarizer_device_follows_override(monkeypatch):
    """The isolated (subprocess) summarizer is the one the indexer actually uses."""
    from summarizer import IsolatedChunkSummarizer
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    assert IsolatedChunkSummarizer()._device == "cpu"


def test_summarizer_explicit_device_beats_override(monkeypatch):
    """An explicit device= still wins over the env default (callers keep control)."""
    from summarizer import ChunkSummarizer, IsolatedChunkSummarizer
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    assert ChunkSummarizer(device="cuda")._device == "cuda"
    assert IsolatedChunkSummarizer(device="cuda")._device == "cuda"


def test_chunk_summarizer_dtype_keyed_off_resolved_device(monkeypatch):
    """dtype must follow the RESOLVED device, not raw cuda availability: device=cpu → float32,
    so CODE_INDEXER_DEVICE=cpu avoids float16-on-CPU even when a GPU exists."""
    import torch
    import transformers
    from summarizer import ChunkSummarizer

    captured = {}

    def _fake_pipeline(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(transformers, "pipeline", _fake_pipeline)

    cpu_summarizer = ChunkSummarizer(device="cpu")
    cpu_summarizer._load()
    assert captured["torch_dtype"] == torch.float32
    assert captured["device_map"] == "cpu"

    captured.clear()
    cuda_summarizer = ChunkSummarizer(device="cuda")
    cuda_summarizer._load()
    assert captured["torch_dtype"] == torch.float16
    assert captured["device_map"] == "cuda"
