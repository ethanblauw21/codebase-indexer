"""Tests for the shared device-resolution helper (src/device.py, ADR-024)."""
import os

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
