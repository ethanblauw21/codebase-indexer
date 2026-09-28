"""What a build reads: a folder, or one commit's tree (ADR-042 §1).

Every file the build reads goes through a ``Source``:

  list()       {repo-relative POSIX path: content hash} for every scannable file
  read(rel)    the file's text, decoded as UTF-8 (errors ignored), ``\\r\\n`` -> ``\\n``
  exists(rel)  whether the path is a file in this source

``WorkingTreeSource`` is the folder, as the indexer has always read it: an
``os.walk`` pruned by ``scan_policy`` and a raw-byte MD5 per file.

``GitCommitSource`` is a commit's tree, read from git objects with no checkout.
The ref is resolved to a commit once, when the source is made, so a whole run
sees one tree even if the ref moves meanwhile. ``git ls-tree`` gives every path
with its blob SHA, which is the content hash: listing and diffing read no file
contents. Reads go through one long-lived ``git cat-file --batch``.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from typing import Optional, Protocol

from scan_policy import scan_policy


class Source(Protocol):
    label: str                    # for log lines and index_meta.source
    commit: Optional[str]         # the commit a git source reads; None for a folder

    def list(self, *, quiet: bool = False) -> dict[str, str]: ...
    def read(self, rel_path: str) -> str: ...
    def exists(self, rel_path: str) -> bool: ...
    def close(self) -> None: ...


def _decode(data: bytes) -> str:
    # The same text the folder path gets from open(..., "r", errors="ignore"):
    # universal newlines turn \r\n (and a lone \r) into \n.
    return data.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")


# ─────────────────────────────────────────────────────────────────────────────
# The folder
# ─────────────────────────────────────────────────────────────────────────────

def md5_file(path: str) -> str:
    """
    MD5 digest of a file's raw bytes, read in 64 KiB blocks.

    Block-reading keeps memory usage constant for large generated files
    (e.g. bundled JS).  MD5 is fast and sufficient for change detection
    — we are not using it for authentication or integrity guarantees.
    """
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def check_anchor(policy, repo_path: str) -> None:
    """Refuse a scan whose root is not the directory the config was found in.

    `load_indexer_config()` walks *up* from its start directory while
    `MCPServer.py` sets `repo_path = os.getcwd()`, and this repo's own `.gitignore`
    notes the server is sometimes launched from `src/`. Launched that way the config
    resolves at the real root while the scan root is `src/`, so
    `extra_root_dirs = ["benchmarks"]` is matched against the wrong tree and does
    nothing at all. Silence there is the expensive outcome, so it raises.
    """
    if policy.config_path is None:
        return                      # no config: defaults apply anywhere, nothing to mismatch
    if os.path.normcase(os.path.abspath(repo_path)) != os.path.normcase(policy.root):
        raise ValueError(
            f"Scan root {os.path.abspath(repo_path)!r} is not the directory holding "
            f"{policy.config_path!r}. Run the indexer from {policy.root!r}, or put an "
            f"indexer.toml in the directory you meant to scan — [ignore].root_dirs "
            f"would otherwise be resolved against a tree it was not written for."
        )


def _within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class WorkingTreeSource:
    """The folder at ``repo_path``: what the indexer read before ADR-042, unchanged."""

    commit = None

    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path
        self.label = "worktree"

    def list(self, *, quiet: bool = False) -> dict[str, str]:
        """
        Walk `repo_path` and return {relative_path: md5_hash} for every indexable file.

        What is included and excluded is decided entirely by `scan_policy` (ADR-026 §2),
        which resolves `[ignore]` from `indexer.toml` over the built-in defaults. The
        same policy object answers the MCP server's watchdog filter, so the two cannot
        drift apart.

        Raises `ValueError` when `repo_path` is not the directory holding `indexer.toml`
        (ADR-026 §6) — resolved against the wrong root, a root-only exclusion silently
        matches nothing, and under this gate a silent mismatch decides what gets deleted
        from the index.
        """
        policy = scan_policy(self.repo_path)
        # The anchor check catches a scan started below its config's directory (the
        # src/ launch). A linked worktree reading the main worktree's indexer.toml
        # (ADR-042 §2) is beside that directory, not below it, so it is not checked.
        if _within(self.repo_path, policy.root):
            check_anchor(policy, self.repo_path)

        result: dict[str, str] = {}
        for root, dirs, files in os.walk(self.repo_path):
            rel_root = os.path.relpath(root, self.repo_path).replace("\\", "/")
            policy.prune(dirs, at_root=(rel_root == "."))
            for fname in files:
                rel_path = (fname if rel_root == "." else f"{rel_root}/{fname}")
                if policy.is_scannable(rel_path):
                    try:
                        result[rel_path] = md5_file(os.path.join(root, fname))
                    except OSError:
                        pass    # race: the file disappeared between listing and open()

        if not quiet:
            print(f"{policy.describe()} files={len(result)}")
        return result

    def read(self, rel_path: str) -> str:
        with open(os.path.join(self.repo_path, rel_path), "r",
                  encoding="utf-8", errors="ignore") as fh:
            return fh.read()

    def exists(self, rel_path: str) -> bool:
        return os.path.isfile(os.path.join(self.repo_path, rel_path))

    def close(self) -> None:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# A commit
# ─────────────────────────────────────────────────────────────────────────────

_LFS_POINTER = b"version https://git-lfs.github.com/spec/"


class GitError(RuntimeError):
    pass


def _git(repo_path: str, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo_path, check=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=120,
        ).stdout
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {msg}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc}") from exc


def resolve_ref(repo_path: str, ref: str) -> str:
    """The commit ``ref`` names, as a full SHA. Raises GitError if it names none."""
    return _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()


class GitCommitSource:
    """One commit's tree, read from git objects (ADR-042 §1)."""

    def __init__(self, repo_path: str, ref: str) -> None:
        self.repo_path = repo_path
        self.ref = ref
        self.label = f"git:{ref}"
        try:
            self.commit = resolve_ref(repo_path, ref)
        except GitError as exc:
            raise GitError(f"[indexer].source names {ref!r}, which is not a commit "
                           f"here (fetch it first?): {exc}") from exc
        self._blobs: Optional[dict[str, str]] = None     # every regular file, unfiltered
        self._batch: Optional[subprocess.Popen] = None
        self._batch_lock = threading.Lock()

    def _tree(self) -> dict[str, str]:
        if self._blobs is None:
            out = _git(self.repo_path, "ls-tree", "-r", "-z", "--full-tree", self.commit)
            blobs: dict[str, str] = {}
            for entry in out.split(b"\0"):
                if not entry:
                    continue
                meta, _, path = entry.partition(b"\t")
                mode, kind, sha = meta.split(b" ")
                # Regular files only: symlinks (120000) and submodules (commit) are skipped.
                if kind != b"blob" or mode not in (b"100644", b"100755"):
                    continue
                blobs[path.decode("utf-8", errors="surrogateescape")] = sha.decode()
            self._blobs = blobs
        return self._blobs

    def list(self, *, quiet: bool = False) -> dict[str, str]:
        policy = scan_policy(self.repo_path)
        result = {}
        for rel_path, sha in self._tree().items():
            # is_scannable applies the ignored directories to the whole path, which is
            # what os.walk + policy.prune amounts to for the folder.
            if policy.is_scannable(rel_path):
                result[rel_path] = sha
        if not quiet:
            print(f"{policy.describe()} source={self.label}@{self.commit[:10]} "
                  f"files={len(result)}")
        return result

    def _blob(self, sha: str) -> bytes:
        with self._batch_lock:
            if self._batch is None or self._batch.poll() is not None:
                self._batch = subprocess.Popen(
                    ["git", "cat-file", "--batch"], cwd=self.repo_path,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
            self._batch.stdin.write(sha.encode() + b"\n")
            self._batch.stdin.flush()
            header = self._batch.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise GitError(f"git cat-file: no blob {sha}")
            size = int(header[2])
            data = self._batch.stdout.read(size)
            self._batch.stdout.read(1)          # the newline after each object
            return data

    def read(self, rel_path: str) -> str:
        sha = self._tree().get(rel_path)
        if sha is None:
            raise FileNotFoundError(f"{rel_path} is not in {self.label}@{self.commit[:10]}")
        data = self._blob(sha)
        if data.startswith(_LFS_POINTER):
            return ""                           # an LFS pointer, not the file's content
        return _decode(data)

    def exists(self, rel_path: str) -> bool:
        return rel_path in self._tree()

    def close(self) -> None:
        with self._batch_lock:
            if self._batch is not None:
                try:
                    self._batch.stdin.close()
                    self._batch.wait(10)
                except (OSError, subprocess.SubprocessError):
                    self._batch.kill()
                self._batch = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def make_source(repo_path: str) -> Source:
    """The source ``[indexer] source`` configures for ``repo_path``."""
    from index_location import git_ref
    ref = git_ref(repo_path)
    return WorkingTreeSource(repo_path) if ref is None else GitCommitSource(repo_path, ref)
