"""
test_example_indexer_toml.py — the per-repo starter config (B-077).

`examples/indexer.toml` is what a new repository copies. It must hold only keys the
code reads (so a copied line is never a dead knob), carry no `[eval]` block (that is
this repo's harness config), and, as shipped, configure exactly what a repository with
no `indexer.toml` gets: every value it sets is the code default.
"""
from __future__ import annotations

import os
import shutil
import tomllib

import pytest

import config
import core
from tests.test_config_drift import WIRED_NO_DEFAULT, _wired

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "examples", "indexer.toml")


@pytest.fixture(scope="module")
def template() -> dict:
    with open(_TEMPLATE, "rb") as fh:
        return tomllib.load(fh)


def test_template_has_no_eval_block(template):
    assert "eval" not in template


def test_every_template_key_is_a_known_wired_key(template):
    wired = _wired()
    unknown = [
        f"[{block}].{key}"
        for block, body in template.items()
        for key in body
        if (block, key) not in wired and (block, key) not in WIRED_NO_DEFAULT
    ]
    assert unknown == []


def test_every_template_value_is_the_code_default(template):
    wired = _wired()
    differs = [
        f"[{block}].{key} = {value!r}, default {wired[(block, key)]!r}"
        for block, body in template.items()
        for key, value in body.items()
        if (block, key) in wired and wired[(block, key)] != value
    ]
    assert differs == []


def _accessors() -> dict:
    return {
        "summarization_enabled": config.summarization_enabled(),
        "summarizer_model_id": config.summarizer_model_id(),
        "summarizer_tiers": config.summarizer_tiers(),
        "index_source": config.index_source(),
        "index_dir_setting": config.index_dir_setting(),
        "index_ref_poll_s": config.index_ref_poll_s(),
        "embed_overlap": config.embed_overlap(),
        "allow_linked_worktree": config.allow_linked_worktree(),
        "model_host_embed_idle_s": config.model_host_embed_idle_s(),
        "embed_model_id": core.embed_model_id(),
        "embed_dimension": core.embed_dimension(),
    }


def _read_in(folder, monkeypatch, find) -> dict:
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config, "find_config_path", find)
    monkeypatch.setattr(core, "_emb_cfg_cache", None)
    config.reset_config_cache()
    try:
        return _accessors()
    finally:
        config.reset_config_cache()
        monkeypatch.setattr(core, "_emb_cfg_cache", None)


def test_template_loads_through_config_to_the_no_file_values(tmp_path, monkeypatch):
    with_file = tmp_path / "with"
    without = tmp_path / "without"
    with_file.mkdir()
    without.mkdir()
    shutil.copy(_TEMPLATE, with_file / "indexer.toml")

    copied = str(with_file / "indexer.toml")
    assert config.load_indexer_config(str(with_file))["indexer"]["source"] == "worktree"

    got = _read_in(with_file, monkeypatch, lambda *_a, **_k: copied)
    baseline = _read_in(without, monkeypatch, lambda *_a, **_k: None)
    assert got == baseline


def test_uncommenting_any_example_line_still_parses():
    # A commented copy of a live key ("source = …" above "# source = …") is a trap:
    # uncomment it without deleting the other and tomllib raises on the duplicate,
    # which stops the MCP server at start. Every commented assignment must be safe
    # to uncomment on its own.
    with open(_TEMPLATE, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    broken = []
    for i, line in enumerate(lines):
        body = line.lstrip("# ")
        if not line.startswith("#") or "=" not in body or " " in body.split("=")[0].strip():
            continue
        trial = lines[:i] + [body] + lines[i + 1:]
        try:
            tomllib.loads("\n".join(trial))
        except tomllib.TOMLDecodeError as exc:
            broken.append(f"line {i + 1}: {line.strip()} ({exc})")
    assert broken == []
