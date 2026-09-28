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
            # ADR-042 §2: a linked worktree usually has no indexer.toml of its own
            # (it is untracked), so it reads the main worktree's.
            main_root = _main_worktree_root(d)
            if main_root:
                candidate = os.path.join(main_root, "indexer.toml")
                if os.path.isfile(candidate):
                    return candidate
            return None            # repo boundary — do not inherit from above it
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _main_worktree_root(worktree_root: str) -> str | None:
    """The main worktree's root if ``worktree_root`` is a linked worktree, else None.

    A linked worktree's ``.git`` is a file, ``gitdir: <common>/worktrees/<name>``. The
    common directory is two levels up from that, and the main worktree holds it as
    ``.git``. Read from the file rather than by running git, so this module stays a leaf.
    """
    dot_git = os.path.join(worktree_root, ".git")
    if not os.path.isfile(dot_git):
        return None
    try:
        with open(dot_git, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(worktree_root, gitdir)
    gitdir = os.path.normpath(gitdir)
    if os.path.basename(os.path.dirname(gitdir)) != "worktrees":
        return None                # a submodule, not a linked worktree
    common = os.path.dirname(os.path.dirname(gitdir))
    if os.path.basename(common) != ".git":
        return None                # a bare repository has no main worktree
    return os.path.dirname(common)


def load_indexer_config(start_dir: str | None = None) -> dict:
    """Parsed ``indexer.toml`` as a dict, or ``{}`` if none is found."""
    path = find_config_path(start_dir)
    if path is None:
        return {}
    # utf-8-sig: Notepad and Windows PowerShell 5.1 save UTF-8 with a BOM, which
    # tomllib rejects as "Invalid statement" and which took the MCP server down at start.
    with open(path, encoding="utf-8-sig") as fh:
        return tomllib.loads(fh.read())


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
    global _sum_cfg_cache, _host_cfg_cache, _host_enabled_cache
    _sum_cfg_cache = None
    _host_cfg_cache = None
    _host_enabled_cache = None


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
# One local process owns the GPU models; project processes are its clients.
# `enabled` is true, false or "auto". "auto" (the default, B-044) turns the host on
# when the models would run on CUDA and leaves it off otherwise, so CI and CPU-only
# machines behave as before. On a GPU it has to be on: once a search loads the
# embedder into the MCP server, an in-process reindex has no room for the summarizer
# on an 8 GB card and skips every summary.
# The timing defaults are PROVISIONAL until ADR-028's Implementation Log records the
# measured swap cost and batch durations they are meant to come from.
# ---------------------------------------------------------------------------

DEFAULT_MODEL_HOST_ENABLED = "auto"
DEFAULT_MODEL_HOST_EMBED_IDLE_S = 60.0        # PROVISIONAL: needs the measured embedder reload cost
DEFAULT_MODEL_HOST_IDLE_EXIT_S = 1800.0       # PROVISIONAL
DEFAULT_MODEL_HOST_SPAWN_TIMEOUT_S = 30.0     # host start to listening; models load later, on demand

_host_cfg_cache: dict | None = None


def _host_cfg() -> dict:
    global _host_cfg_cache
    if _host_cfg_cache is None:
        _host_cfg_cache = load_indexer_config().get("model_host", {})
    return _host_cfg_cache


def resolve_model_host_enabled(value, device: str) -> bool:
    """`[model_host].enabled` as a bool: true, false, or "auto" (on when ``device`` is CUDA)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() == "auto":
        return device.startswith("cuda")
    raise ValueError(f'[model_host].enabled must be true, false or "auto", not {value!r}')


_host_enabled_cache: bool | None = None


def model_host_enabled() -> bool:
    """Whether embeds and summaries go to the shared model host (ADR-028)."""
    global _host_enabled_cache
    if _host_enabled_cache is None:
        value = _host_cfg().get("enabled", DEFAULT_MODEL_HOST_ENABLED)
        device = "cpu"
        if not isinstance(value, bool):
            from device import resolve_device     # imports torch; only "auto" needs it
            device = resolve_device()
        _host_enabled_cache = resolve_model_host_enabled(value, device)
    return _host_enabled_cache


def model_host_embed_idle_s() -> float:
    """How long the host keeps the embedder loaded after its last request."""
    return max(0.0, float(_host_cfg().get("embed_idle_s", DEFAULT_MODEL_HOST_EMBED_IDLE_S)))


def model_host_idle_exit_s() -> float:
    """How long the host process stays up with nothing loaded and nothing queued."""
    return max(0.0, float(_host_cfg().get("idle_exit_s", DEFAULT_MODEL_HOST_IDLE_EXIT_S)))


def model_host_spawn_timeout_s() -> float:
    """How long a client waits for a host it started to begin listening."""
    return max(1.0, float(_host_cfg().get("spawn_timeout_s", DEFAULT_MODEL_HOST_SPAWN_TIMEOUT_S)))


# ---------------------------------------------------------------------------
# Index writers (ADR-038) — [indexer] in indexer.toml.
# ---------------------------------------------------------------------------

DEFAULT_ALLOW_LINKED_WORKTREE = False


def allow_linked_worktree(start_dir: str | None = None) -> bool:
    """Whether a linked git worktree may write its index (B-053). Read fresh each call."""
    return bool(load_indexer_config(start_dir).get("indexer", {})
                .get("allow_linked_worktree", DEFAULT_ALLOW_LINKED_WORKTREE))


# ---------------------------------------------------------------------------
# What is indexed, and where the index lives (ADR-042) — [indexer] in indexer.toml.
# ---------------------------------------------------------------------------

DEFAULT_INDEX_SOURCE = "worktree"
DEFAULT_INDEX_DIR = ".code-index"
DEFAULT_REF_POLL_S = 60.0


def index_source(start_dir: str | None = None) -> str:
    """``"worktree"`` (index the folder) or ``"git:<ref>"`` (index that commit's tree)."""
    value = (load_indexer_config(start_dir).get("indexer", {})
             .get("source", DEFAULT_INDEX_SOURCE))
    if not isinstance(value, str) or not (
            value == "worktree" or (value.startswith("git:") and len(value) > 4)):
        raise ValueError(f'[indexer].source must be "worktree" or "git:<ref>", not {value!r}')
    return value


def index_dir_setting(start_dir: str | None = None) -> str:
    """``[indexer] index_dir``: where a worktree-mode index lives, relative to the repo root."""
    return str(load_indexer_config(start_dir).get("indexer", {})
               .get("index_dir", DEFAULT_INDEX_DIR))


def index_ref_poll_s(start_dir: str | None = None) -> float:
    """``[indexer] ref_poll_s``: how often git mode checks whether its ref moved (ADR-042 §4)."""
    return max(1.0, float(load_indexer_config(start_dir).get("indexer", {})
                          .get("ref_poll_s", DEFAULT_REF_POLL_S)))
