"""config.py — locate and parse the per-repo ``indexer.toml``.

The indexer and MCP server run with the working directory at the repo root that
holds ``indexer.toml`` (see CLAUDE.md / the file's own header). We search upward
from ``start_dir`` (default: cwd) so the config is found whether the process
starts at the repo root or a subdirectory.

Returns ``{}`` when no ``indexer.toml`` is found — every caller supplies its own
defaults, so a missing config is never fatal.

Consumers: the embedder (``core.py``, ADR-009 §P1), the reranker and fusion mode
(``hybrid_retriever.py``), the eval harness (``tools/coir_eval.py``), and the
summarizer (ADR-026 — the accessors at the bottom of this module).

This module is deliberately a **leaf**: it imports only ``os`` and ``tomllib``.
Config accessors live here rather than in the modules that consume them so that
``incremental_indexer`` can ask whether summarization is enabled without importing
``summarizer`` (and therefore torch). Do not add imports of sibling ``src``
modules — ADR-026 §2 depends on this staying acyclic.
"""
from __future__ import annotations

import os
import tomllib


def find_config_path(start_dir: str | None = None) -> str | None:
    """Walk up from ``start_dir`` (default cwd) to the first ``indexer.toml``."""
    d = os.path.abspath(start_dir or os.getcwd())
    while True:
        candidate = os.path.join(d, "indexer.toml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_indexer_config(start_dir: str | None = None) -> dict:
    """Parsed ``indexer.toml`` as a dict, or ``{}`` if none is found."""
    path = find_config_path(start_dir)
    if path is None:
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Summarization knobs (ADR-026) — [summarization] in indexer.toml.
#
# Before ADR-026 both of these were unreachable: the gate was the module constant
# ``incremental_indexer.ENABLE_SUMMARIZATION`` and the model id was a default baked
# into two separate summarizer class signatures, so ``[summarization]`` in
# indexer.toml was documented and inert. The defaults below are now the ONLY
# defaults for these knobs — see the drift test in tests/test_config_drift.py.
# ---------------------------------------------------------------------------

DEFAULT_SUMMARIZATION_ENABLED = True
DEFAULT_SUMMARIZER_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B-Instruct"

_sum_cfg_cache: dict | None = None


def _sum_cfg() -> dict:
    global _sum_cfg_cache
    if _sum_cfg_cache is None:
        _sum_cfg_cache = load_indexer_config().get("summarization", {})
    return _sum_cfg_cache


def reset_config_cache() -> None:
    """Drop cached config views.

    Long-running processes (the MCP server) read config once; tests that write a
    temporary ``indexer.toml`` must call this between cases or they will see the
    first case's values. ``core.py`` keeps its own embedder cache — reset that
    separately if a test changes ``[embeddings]``.
    """
    global _sum_cfg_cache
    _sum_cfg_cache = None


def summarization_enabled() -> bool:
    """Whether the indexer runs LLM chunk summarization.

    On CPU this is the difference between an index that completes and one that
    does not, which is why it has to be reachable without editing source.
    """
    return bool(_sum_cfg().get("enabled", DEFAULT_SUMMARIZATION_ENABLED))


def summarizer_model_id() -> str:
    """HuggingFace model id for chunk summarization."""
    return str(_sum_cfg().get("model_id", DEFAULT_SUMMARIZER_MODEL_ID))
