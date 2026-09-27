"""Shared test setup.

The shipped `[model_host].enabled = "auto"` turns the model host on whenever CUDA is
available (B-044). On a GPU machine that would send embeds from tests that fake
`core.embed_batch` to a real host process instead. Pin it off; tests that exercise
the host patch `config.model_host_enabled` themselves.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def _model_host_off(monkeypatch):
    monkeypatch.setattr(config, "_host_enabled_cache", False)
