"""Cross-process locks on one index directory (ADR-038, B-033 + B-053).

Two locks, both OS file locks the kernel drops when their process dies, so a
killed build or a closed session never leaves a stale lock to clean up (the same
mechanism as ADR-028's host.lock):

  write.lock   held for a whole indexing run (CLI, watchdog, `reindex` tool). A
               second writer does not wait an hour behind the first: the caller
               decides whether to skip, retry later, or refuse.
  watch.lock   held for the life of the MCP server that runs the watchdog. Every
               other server on the same index serves the read tools only.

`threading.Lock` (ADR-036) still serializes runs inside one process; these locks
serialize processes. Acquiring a lock this process already holds is a no-op that
returns a handle, so `_reindex` can take write.lock around its wipe and let
`run_incremental` take it again inside.

Also here: `linked_worktree()`, the check behind B-053's rule that a Claude-made
worktree must not drive a shared index.

A leaf module: stdlib, plus `config` (itself a leaf) for the worktree opt-in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

WRITE_LOCK = "write.lock"
WATCH_LOCK = "watch.lock"

_held: dict[str, list] = {}         # abs path -> [file handle, reentry count]
_held_guard = threading.Lock()


class IndexBusy(RuntimeError):
    """Another process holds the lock. ``holder`` is what it wrote there, if readable."""

    def __init__(self, path: str, holder: dict | None) -> None:
        self.path = path
        self.holder = holder or {}
        super().__init__(f"{os.path.basename(path)} is held by {describe_holder(self.holder)}")


def describe_holder(holder: dict) -> str:
    if not holder:
        return "another process"
    since = holder.get("since")
    when = time.strftime("%H:%M:%S", time.localtime(since)) if since else "?"
    return f"pid {holder.get('pid', '?')} ({holder.get('purpose', '?')}, since {when})"


def _os_lock(fh) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _read_holder(path: str) -> dict | None:
    # The first byte is the locked region on Windows, so the record starts at byte 1.
    # A holder that is mid-write reads as unknown; that only affects the message.
    try:
        with open(path, "rb") as fh:
            fh.seek(1)
            return json.loads(fh.read().decode("utf-8") or "null")
    except (OSError, ValueError):
        return None


class _Handle:
    def __init__(self, path: str) -> None:
        self.path = path
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _held_guard:
            entry = _held.get(self.path)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            del _held[self.path]
        fh = entry[0]
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fh.close()      # closing drops the flock on POSIX

    def __enter__(self) -> "_Handle":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def try_acquire(index_dir: str, name: str, purpose: str) -> _Handle | None:
    """Take ``name`` in ``index_dir`` without waiting, or return None if another process has it."""
    os.makedirs(index_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(index_dir, name))
    with _held_guard:
        entry = _held.get(path)
        if entry is not None:
            entry[1] += 1
            return _Handle(path)
        fh = open(path, "a+b")
        if not _os_lock(fh):
            fh.close()
            return None
        _held[path] = [fh, 1]
    record = {"pid": os.getpid(), "purpose": purpose, "since": time.time(), "cwd": os.getcwd()}
    try:
        fh.seek(1)
        fh.truncate()
        fh.write(json.dumps(record).encode("utf-8"))
        fh.flush()
    except OSError:
        pass            # the record is for messages only; the lock is what matters
    return _Handle(path)


def acquire(index_dir: str, name: str, purpose: str) -> _Handle:
    """Like `try_acquire`, but raises `IndexBusy` naming the holder."""
    handle = try_acquire(index_dir, name, purpose)
    if handle is None:
        path = os.path.join(index_dir, name)
        raise IndexBusy(path, _read_holder(path))
    return handle


def holder(index_dir: str, name: str) -> dict | None:
    """What the current holder of ``name`` wrote, for messages. None if unreadable."""
    return _read_holder(os.path.join(index_dir, name))


def linked_worktree(path: str) -> bool:
    """True if ``path`` is inside a linked git worktree (``git worktree add``), not the main one.

    In a linked worktree ``--git-dir`` is ``<common>/worktrees/<name>``, which differs
    from ``--git-common-dir``. False outside git or if git is missing.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=path, capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    lines = out.stdout.splitlines()
    if out.returncode != 0 or len(lines) != 2:
        return False
    return os.path.normcase(os.path.normpath(lines[0])) != os.path.normcase(os.path.normpath(lines[1]))


def worktree_refusal(repo_path: str) -> str | None:
    """Why ``repo_path`` must not write its index, or None if it may (B-053).

    A linked worktree is refused unless its indexer.toml sets
    ``[indexer] allow_linked_worktree = true``.
    """
    if not linked_worktree(repo_path):
        return None
    from config import allow_linked_worktree     # leaf too; imported late to keep this stdlib-first
    if allow_linked_worktree(repo_path):
        return None
    return (f"'{repo_path}' is a linked git worktree. The code index is not built or "
            f"updated from worktrees made for parallel work: each would start its own "
            f"rebuild on the shared model host. Read the files your branch changed instead. "
            f"If this worktree exists only to hold the index, set "
            f"[indexer] allow_linked_worktree = true in its indexer.toml.")
