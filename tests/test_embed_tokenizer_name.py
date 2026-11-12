"""
test_embed_tokenizer_name.py — the embedder's tokenizer is named for what it follows (ADR-057, B-076).

`core.embed_tokenizer` loads the tokenizer of `config.embed_model_id()`. It used to be called
after jina, a model the index stopped using at ADR-009, which is the same class of misleading
name as the jina default that outlived the config by twenty days (ADR-026 §8). The old name
must not come back in code.
"""
from __future__ import annotations

import pathlib
import re

import core

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_OLD = re.compile(r"\bjina" + r"_tokenizer\b")  # split so this file doesn't match itself


def test_singleton_is_exported_under_the_new_name():
    assert isinstance(core.embed_tokenizer, core.TokenizerManager)
    assert not hasattr(core, "jina" + "_tokenizer")


def test_old_name_is_gone_from_code():
    hits = []
    for top in ("src", "tests", "tools", "benchmarks"):
        for p in (_ROOT / top).rglob("*.py"):
            if _OLD.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(p.relative_to(_ROOT)))
    assert hits == []
