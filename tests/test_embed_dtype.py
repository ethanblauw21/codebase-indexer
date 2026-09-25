"""ADR-035: the embedder loads in bf16 on a GPU that supports it, fp32 elsewhere.

CPU-only: CUDA support is faked and SentenceTransformer is replaced, so nothing
downloads or touches a GPU."""
import pytest
import torch

import core


class _FakeST:
    """Stand-in for SentenceTransformer that records the device and dtype it was built with."""
    last_device = None
    last_dtype = None

    def __init__(self, model_id, trust_remote_code=False, device=None, model_kwargs=None):
        _FakeST.last_device = device
        _FakeST.last_dtype = (model_kwargs or {}).get("torch_dtype")
        self.max_seq_length = None


@pytest.fixture
def cfg(monkeypatch):
    """Set [embeddings] for one test; the cache is restored afterwards."""
    def set_cfg(**values):
        monkeypatch.setattr(core, "_emb_cfg_cache", dict(values))
    return set_cfg


def _cuda(monkeypatch, available=True, bf16=True):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: available)
    monkeypatch.setattr(torch.cuda, "is_bf16_supported", lambda *a, **k: bf16)


def test_auto_is_bf16_on_a_cuda_gpu_that_supports_it(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch)
    assert core.embed_dtype("cuda") is torch.bfloat16
    assert core.embed_dtype("cuda:0") is torch.bfloat16


def test_auto_is_fp32_on_cpu(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch)                      # a GPU exists, but the device resolved to CPU
    assert core.embed_dtype("cpu") is torch.float32


def test_auto_is_fp32_on_a_gpu_without_bf16(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch, bf16=False)
    assert core.embed_dtype("cuda") is torch.float32


def test_auto_is_fp32_when_cuda_is_forced_but_absent(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch, available=False, bf16=False)
    assert core.embed_dtype("cuda") is torch.float32


@pytest.mark.parametrize("name", ["float32", "bfloat16", "float16"])
def test_an_explicit_dtype_is_used_as_given(cfg, monkeypatch, name):
    cfg(dtype=name)
    _cuda(monkeypatch, available=False, bf16=False)
    assert core.embed_dtype("cpu") is getattr(torch, name)


def test_an_unknown_dtype_raises_and_names_the_setting(cfg):
    cfg(dtype="int8")
    with pytest.raises(ValueError, match=r"\[embeddings\]\.dtype"):
        core.embed_dtype("cpu")


def test_the_loaded_model_gets_the_dtype(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch)
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cuda")
    monkeypatch.setattr(core, "SentenceTransformer", _FakeST)
    monkeypatch.setattr(core, "_embed_model", None)
    core._get_embed_model()
    assert _FakeST.last_dtype is torch.bfloat16


def test_the_cpu_kill_switch_keeps_fp32(cfg, monkeypatch):
    cfg()
    _cuda(monkeypatch)
    monkeypatch.setenv("CODE_INDEXER_DEVICE", "cpu")
    monkeypatch.setattr(core, "SentenceTransformer", _FakeST)
    monkeypatch.setattr(core, "_embed_model", None)
    core._get_embed_model()
    assert _FakeST.last_device == "cpu" and _FakeST.last_dtype is torch.float32
