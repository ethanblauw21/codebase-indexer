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
    """Walk up from ``start_dir`` (default cwd) to the first ``indexer.toml``.

    **The walk stops at a repository boundary** (ADR-026 §6): a directory holding a
    ``.git`` entry is the last one examined. Without that stop, a stray
    ``indexer.toml`` in a parent of the repo — a home directory, a folder holding
    several checkouts — silently configures every repo beneath it. That used to mean
    the wrong reranker settings; once ``[ignore]`` is live it decides what gets
    *deleted* from an index, which is not a mistake worth inheriting from a
    grandparent directory.

    ``.git`` is tested with ``exists`` rather than ``isdir`` on purpose: worktrees and
    submodules record it as a file.
    """
    d = os.path.abspath(start_dir or os.getcwd())
    while True:
        candidate = os.path.join(d, "indexer.toml")
        if os.path.isfile(candidate):
            return candidate
        if os.path.exists(os.path.join(d, ".git")):
            return None            # repo boundary — do not inherit from above it
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
# ADR-027: the batch ceiling and the GPU memory the worker leaves for everything else.
DEFAULT_SUMMARIZER_MAX_BATCH_SIZE = 48
DEFAULT_SUMMARIZER_VRAM_RESERVE_MB = 1024
DEFAULT_SUMMARIZER_BATCH_TOKEN_BUDGET = 16000
# ADR-030: which chunk tiers are summarized (1 functions, 2 components, 3 files).
DEFAULT_SUMMARIZER_TIERS = [1, 2, 3]

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
    global _sum_cfg_cache, _host_cfg_cache
    _sum_cfg_cache = None
    _host_cfg_cache = None


def summarization_enabled() -> bool:
    """Whether the indexer runs LLM chunk summarization.

    On CPU this is the difference between an index that completes and one that
    does not, which is why it has to be reachable without editing source.
    """
    return bool(_sum_cfg().get("enabled", DEFAULT_SUMMARIZATION_ENABLED))


def summarizer_model_id() -> str:
    """HuggingFace model id for chunk summarization."""
    return str(_sum_cfg().get("model_id", DEFAULT_SUMMARIZER_MODEL_ID))


def summarizer_max_batch_size() -> int:
    """Largest batch the summarizer worker may grow to (ADR-027). 1 turns batching off."""
    return max(1, int(_sum_cfg().get("max_batch_size", DEFAULT_SUMMARIZER_MAX_BATCH_SIZE)))


def summarizer_batch_token_budget() -> int:
    """Prompt tokens per summarizer batch at the start of a run (ADR-027).

    A batch holds budget // (its longest prompt) chunks. The budget adjusts during
    the run: down on out-of-memory, up slowly while batches fit.
    """
    return max(1, int(_sum_cfg().get("batch_token_budget", DEFAULT_SUMMARIZER_BATCH_TOKEN_BUDGET)))


def summarizer_vram_reserve_mb() -> int:
    """GPU memory, in MiB, the summarizer worker must leave free for other processes (ADR-027)."""
    return max(0, int(_sum_cfg().get("vram_reserve_mb", DEFAULT_SUMMARIZER_VRAM_RESERVE_MB)))


def summarizer_tiers() -> set[int]:
    """Chunk tiers that get a summary (ADR-030). Tier-2/3 chunks are the long
    prompts, so they are most of the summarizer's GPU time."""
    return {int(t) for t in _sum_cfg().get("tiers", DEFAULT_SUMMARIZER_TIERS)}


# ---------------------------------------------------------------------------
# Model host (ADR-028) — [model_host] in indexer.toml.
#
# One local process owns the GPU models; project processes are its clients. Off by
# default, so CI, CPU-only machines and single-project use behave as before.
# The timing defaults are PROVISIONAL until ADR-028's Implementation Log records the
# measured swap cost and batch durations they are meant to come from.
# ---------------------------------------------------------------------------

DEFAULT_MODEL_HOST_ENABLED = False
DEFAULT_MODEL_HOST_EMBED_IDLE_S = 60.0        # PROVISIONAL: needs the measured embedder reload cost
DEFAULT_MODEL_HOST_IDLE_EXIT_S = 1800.0       # PROVISIONAL
DEFAULT_MODEL_HOST_SPAWN_TIMEOUT_S = 30.0     # host start to listening; models load later, on demand

_host_cfg_cache: dict | None = None


def _host_cfg() -> dict:
    global _host_cfg_cache
    if _host_cfg_cache is None:
        _host_cfg_cache = load_indexer_config().get("model_host", {})
    return _host_cfg_cache


def model_host_enabled() -> bool:
    """Whether embeds and summaries go to the shared model host (ADR-028)."""
    return bool(_host_cfg().get("enabled", DEFAULT_MODEL_HOST_ENABLED))


def model_host_embed_idle_s() -> float:
    """How long the host keeps the embedder loaded after its last request."""
    return max(0.0, float(_host_cfg().get("embed_idle_s", DEFAULT_MODEL_HOST_EMBED_IDLE_S)))


def model_host_idle_exit_s() -> float:
    """How long the host process stays up with nothing loaded and nothing queued."""
    return max(0.0, float(_host_cfg().get("idle_exit_s", DEFAULT_MODEL_HOST_IDLE_EXIT_S)))


def model_host_spawn_timeout_s() -> float:
    """How long a client waits for a host it started to begin listening."""
    return max(1.0, float(_host_cfg().get("spawn_timeout_s", DEFAULT_MODEL_HOST_SPAWN_TIMEOUT_S)))
