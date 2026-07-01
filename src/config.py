"""config.py — locate and parse the per-repo ``indexer.toml``.

The indexer and MCP server run with the working directory at the repo root that
holds ``indexer.toml`` (see CLAUDE.md / the file's own header). We search upward
from ``start_dir`` (default: cwd) so the config is found whether the process
starts at the repo root or a subdirectory.

Returns ``{}`` when no ``indexer.toml`` is found — every caller supplies its own
defaults, so a missing config is never fatal. This is currently consumed by the
production reranker (ADR-009); the embedder and summarizer still hardcode their
model ids and have not yet been migrated to read this config.
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
