"""
test_scan_disk.py — baseline coverage for the disk scan gate (ADR-026, commit 1).

**Why this file exists.** Before ADR-026 there was no test anywhere in `tests/`
referencing `scan_disk`, `IGNORE_DIRS` or `IGNORE_ROOT_DIRS`. That absence is how a
JavaScript-seeded exclusion list ("node_modules", ".next", "public", "mocks") survived
on a Python tool long enough to index 503 files of third-party eval corpus from this
repo's own `benchmarks/` directory.

These tests characterize the gate **as it behaves today**, deliberately including the
behaviour ADR-026 intends to change. Commit 3 flips those expectations; the point of
writing them first is that the flip becomes visible in a diff rather than happening
silently. Tests that will change are marked `# ADR-026 commit 3 will invert this`.

No embedding, no model load, no GPU — this is `os.walk` and hashing only.
"""
from __future__ import annotations

import os

import pytest

import incremental_indexer as ii


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write(root: str, rel: str, text: str = "x = 1\n") -> str:
    """Create a file (and its parents) under ``root``; return the absolute path."""
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture
def repo(tmp_path):
    """A small tree covering every branch of the scan gate."""
    root = str(tmp_path)
    _write(root, "src/app.py")
    _write(root, "src/nested/deep/util.py")
    _write(root, "README.md")                    # not an indexable extension
    _write(root, "node_modules/pkg/index.js")    # IGNORE_DIRS, any depth
    _write(root, "src/node_modules/pkg/x.js")    # IGNORE_DIRS, nested
    _write(root, "functions/handler.py")         # IGNORE_ROOT_DIRS, root only
    _write(root, "src/functions/helper.py")      # same name, NOT at root
    return root


# ---------------------------------------------------------------------------
# What the scan includes
# ---------------------------------------------------------------------------

def test_indexable_extensions_are_scanned(repo):
    found = ii.scan_disk(repo)
    assert "src/app.py" in found
    assert "src/nested/deep/util.py" in found


def test_non_indexable_extensions_are_skipped(repo):
    assert "README.md" not in ii.scan_disk(repo)


def test_paths_are_repo_relative_with_forward_slashes(repo):
    """Path separators are normalized so the SQLite `files` table is portable."""
    for path in ii.scan_disk(repo):
        assert not os.path.isabs(path)
        assert "\\" not in path


def test_values_are_md5_hashes(repo):
    for digest in ii.scan_disk(repo).values():
        assert len(digest) == 32
        int(digest, 16)          # raises if not hex


def test_hash_changes_when_content_changes(repo):
    before = ii.scan_disk(repo)["src/app.py"]
    _write(repo, "src/app.py", "x = 2\n")
    assert ii.scan_disk(repo)["src/app.py"] != before


# ---------------------------------------------------------------------------
# What the scan excludes
# ---------------------------------------------------------------------------

def test_ignore_dirs_apply_at_every_depth(repo):
    found = ii.scan_disk(repo)
    assert "node_modules/pkg/index.js" not in found
    assert "src/node_modules/pkg/x.js" not in found


def test_ignore_root_dirs_apply_only_at_the_root(repo):
    """`functions/` is skipped at the root; a nested `functions/` is real source."""
    found = ii.scan_disk(repo)
    assert "functions/handler.py" not in found
    assert "src/functions/helper.py" in found


# ---------------------------------------------------------------------------
# Behaviour ADR-026 intends to change — pinned so the change is visible
# ---------------------------------------------------------------------------

def test_python_virtualenvs_are_currently_indexed(repo):
    """A `venv/` with a `pyvenv.cfg` is walked today, site-packages and all.

    This is B-001: index any Python repo with an in-tree virtualenv and the scan
    embeds thousands of third-party files as if they were the user's code.
    """
    _write(repo, "venv/pyvenv.cfg", "home = /usr\n")
    _write(repo, "venv/Lib/site-packages/requests/api.py")
    found = ii.scan_disk(repo)
    # ADR-026 commit 3 will invert this: the assertion becomes `not in`.
    assert "venv/Lib/site-packages/requests/api.py" in found


def test_pycache_is_currently_indexed(repo):
    _write(repo, "src/__pycache__/app.cpython-311.py")
    # ADR-026 commit 3 will invert this.
    assert "src/__pycache__/app.cpython-311.py" in ii.scan_disk(repo)


def test_public_and_mocks_are_currently_skipped(repo):
    """Leftovers from the JavaScript project this list was seeded from.

    `public/` is a real source directory in many web projects, so excluding it
    silently under-indexes them.
    """
    _write(repo, "public/widget.js")
    _write(repo, "mocks/server.js")
    found = ii.scan_disk(repo)
    # ADR-026 commit 3 will invert both of these.
    assert "public/widget.js" not in found
    assert "mocks/server.js" not in found


def test_directory_name_matching_is_currently_case_sensitive(repo):
    """A case-variant of an ignored directory is walked, because matching is exact.

    `"dist"` is in IGNORE_DIRS; `Dist/` is not, so its contents are indexed even
    though on Windows and macOS they are the same directory name.

    Note the fixture uses `Dist` rather than `Node_Modules`: the fixture already
    contains `node_modules/`, and on a case-insensitive filesystem creating
    `Node_Modules/` resolves into the existing directory, so the file would land in
    the correctly-ignored path and the test would pass for the wrong reason. The
    variant has to name a directory the tree does not already have.
    """
    _write(repo, "Dist/bundle.js")
    # ADR-026 commit 3 will invert this (matching becomes case-folded).
    assert "Dist/bundle.js" in ii.scan_disk(repo)


# ---------------------------------------------------------------------------
# The second consumer — MCPServer's watchdog filter must agree with scan_disk
# ---------------------------------------------------------------------------

def test_watchdog_filter_shares_the_gate_instead_of_mirroring_it():
    """`MCPServer._is_relevant` calls the shared export (ADR-026 §2, commit 2).

    Commit 1's version of this test asserted the opposite — that the filter imported
    the raw constants and walked the path components by hand. That was the coupling
    to remove; this is the inversion.
    """
    import inspect
    import MCPServer

    source = inspect.getsource(MCPServer._CodeChangeHandler._is_relevant)
    assert "from scan_policy import scan_policy" in source
    assert "is_scannable" in source
    for stale in ("IGNORE_DIRS", "IGNORE_ROOT_DIRS", "INDEXABLE_EXTS"):
        assert stale not in source, f"{stale} is gone; the filter must not name it"


def test_the_two_consumers_agree_on_every_scanned_path(repo):
    """The property the shared export exists to guarantee, checked directly.

    Whatever `scan_disk` picks up, the watchdog must consider relevant — otherwise a
    file is indexed once and then never refreshed when it changes.
    """
    import scan_policy

    _write(repo, "venv/Lib/site-packages/requests/api.py")
    _write(repo, "public/widget.js")
    _write(repo, "Dist/bundle.js")
    _write(repo, "src/app.csproj", "<Project />\n")

    scan_policy.reset()
    policy = scan_policy.scan_policy(repo)
    scanned = set(ii.scan_disk(repo))

    for rel in scanned:
        assert policy.is_scannable(rel), f"scan_disk took {rel}; the watchdog would ignore it"

    # ...and the converse, over every file actually on disk.
    for root, _dirs, files in os.walk(repo):
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), repo).replace(os.sep, "/")
            if policy.is_scannable(rel):
                assert rel in scanned, f"the watchdog would fire on {rel}; scan_disk skips it"


def test_stale_constant_imports_raise(repo):
    """`from incremental_indexer import IGNORE_DIRS` must fail loudly.

    Leaving a same-named alias behind would be a second definition of the knob —
    the drift this ADR removes, reintroduced as a compatibility shim.
    """
    for name in ("IGNORE_DIRS", "IGNORE_ROOT_DIRS", "INDEXABLE_EXTS", "_ALL_SCAN_EXTS"):
        assert not hasattr(ii, name), f"incremental_indexer still exposes {name}"
