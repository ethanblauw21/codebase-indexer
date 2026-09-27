"""B-044: `[model_host].enabled = "auto"` turns the model host on when the models run on CUDA.

Without the host, a search loads the embedder into the MCP server, and the next in-process
reindex has no room for the summarizer on an 8 GB card: it skips every summary. CPU-only
machines and CI keep the in-process path.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402
import device  # noqa: E402


@pytest.mark.parametrize("value, dev, expected", [
    ("auto", "cuda", True),
    ("auto", "cuda:1", True),
    ("AUTO", "cuda", True),
    ("auto", "cpu", False),
    ("auto", "mps", False),
    (True, "cpu", True),
    (False, "cuda", False),
])
def test_resolve(value, dev, expected):
    assert config.resolve_model_host_enabled(value, dev) is expected


@pytest.mark.parametrize("value", ["yes", "on", 1, None])
def test_a_value_that_is_not_true_false_or_auto_is_refused(value):
    with pytest.raises(ValueError):
        config.resolve_model_host_enabled(value, "cuda")


def test_the_shipped_default_is_auto():
    assert config.DEFAULT_MODEL_HOST_ENABLED == "auto"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory; the fixture's caller writes its indexer.toml, if any."""
    (tmp_path / ".git").mkdir()                      # repo boundary: no inherited config
    monkeypatch.chdir(tmp_path)
    config.reset_config_cache()
    yield tmp_path
    config.reset_config_cache()


def test_a_project_without_indexer_toml_uses_the_host_on_a_gpu(project, monkeypatch):
    monkeypatch.setattr(device, "resolve_device", lambda: "cuda")
    assert config.model_host_enabled() is True


def test_a_project_without_indexer_toml_stays_in_process_on_cpu(project, monkeypatch):
    monkeypatch.setattr(device, "resolve_device", lambda: "cpu")
    assert config.model_host_enabled() is False


def test_an_explicit_false_wins_on_a_gpu(project, monkeypatch):
    (project / "indexer.toml").write_text("[model_host]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setattr(device, "resolve_device", lambda: pytest.fail("false must not probe the device"))
    assert config.model_host_enabled() is False
