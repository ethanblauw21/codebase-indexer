"""Where this process's index lives (ADR-042 §3).

Worktree mode (the default) keeps today's layout exactly: ``[indexer] index_dir``,
default ``.code-index``, returned as given, so a relative path still resolves against
the working directory at each use (tests and the MCP server rely on that).

Git mode (``[indexer] source = "git:<ref>"``) indexes a commit, not a folder, so the
index belongs to the repository: ``<git common dir>/code-index``, an absolute path
that is the same from the main folder and every linked worktree.

Resolved once per process from the working directory, like ``REPO_PATH``. Tests
that change directory or config call ``reset()``.

A leaf module: ``os``, ``subprocess`` and ``config``.
"""
from __future__ import annotations

import os
import subprocess

import config

GIT_INDEX_DIRNAME = "code-index"

_cached: str | None = None


def git_common_dir(path: str) -> str | None:
    """Absolute ``git rev-parse --git-common-dir`` for ``path``, or None outside git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=path, capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    return os.path.normpath(line) if out.returncode == 0 and line else None


def git_ref(start_dir: str | None = None) -> str | None:
    """The ref git mode indexes (``origin/main`` for ``"git:origin/main"``), or None in worktree mode."""
    source = config.index_source(start_dir)
    return source[len("git:"):] if source.startswith("git:") else None


def ref_display(repo_path: str, ref: str) -> str:
    """``ref``, plus what it points to when it is a symbolic ref.

    A repository whose shared baseline is a series of dated branches (GanttWebApp's
    ``integration/staging-<date>``) indexes a local alias re-pointed each cycle. The
    index is "current" for the alias either way, so naming its target is what shows
    a cycle that was never re-pointed.
    """
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "-q", "--short", ref],
            cwd=repo_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ref
    target = out.stdout.strip() if out.returncode == 0 else ""
    return f"{ref} (→ {target})" if target else ref


def resolve_index_dir(repo_path: str | None = None) -> str:
    """Compute the index directory for ``repo_path`` (default: cwd). No caching."""
    repo_path = repo_path or os.getcwd()
    if git_ref(repo_path) is None:
        return config.index_dir_setting(repo_path)
    common = git_common_dir(repo_path)
    if common is None:
        raise ValueError(f"[indexer].source is {config.index_source(repo_path)!r}, "
                         f"but {repo_path!r} is not inside a git repository")
    return os.path.join(common, GIT_INDEX_DIRNAME)


def index_dir() -> str:
    """This process's index directory, resolved from the working directory on first use."""
    global _cached
    if _cached is None:
        _cached = resolve_index_dir()
    return _cached


def reset() -> None:
    """Forget the resolved directory (tests that change cwd or config)."""
    global _cached
    _cached = None
