"""scan_policy.py — the single decision point for "does the indexer look at this file?"

**Why this module exists (ADR-026 §2).** The scan gate has two consumers that must
never disagree:

- :func:`incremental_indexer.scan_disk` walks the tree and sees directory entries.
- ``MCPServer._CodeChangeHandler._is_relevant`` receives a bare path string on every
  filesystem event and never touches the disk.

Before this module the second re-implemented the first by hand, importing raw
constants inside a function body. A hand-mirrored rule is a rule that can drift, and
a *shared accessor* would not have been enough — that synchronizes values, not
behaviour. Both consumers now call the same predicate.

**Leaf placement is load-bearing, not cosmetic.** This module imports only ``os``,
``dataclasses`` and ``config``. A policy helper living in ``incremental_indexer`` and
imported by ``config`` deadlocks at import time, and would surface only through the
server's function-local import path — the hardest possible place to see it.

**Cache semantics.** The resolved policy is cached per scan root, like
``core.py::_emb_cfg()``. Tests that write a temporary ``indexer.toml`` must call
:func:`reset` — and, if they also touch ``[summarization]`` or ``[embeddings]``,
``config.reset_config_cache()`` and ``core``'s own cache, which are separate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property

from config import find_config_path, load_indexer_config

# ---------------------------------------------------------------------------
# Defaults — one home per knob (ADR-026 §8)
#
# These are the ONLY defaults for the scan gate. `indexer.toml`'s [ignore] block
# documents them; tests/test_config_drift.py enforces that the two agree.
# ---------------------------------------------------------------------------

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    ".next", "node_modules", "dist", ".git", "build",
    ".code-index", ".continue", ".claude", ".vs", ".vscode",
    "Modelfiles", "playwright-report", "test-results",
    ".github", ".firebase", ".idx", "genkit", "indexer", "public", "mocks",
})

DEFAULT_IGNORE_ROOT_DIRS: frozenset[str] = frozenset({"functions"})

# Source extensions the scan chunks + embeds. C#/C++ have full Tier-A adapters
# (ADR-003; adapters/__init__.py registers .cs/.cpp/.cc/.cxx/.h/.hpp) and ADR-017 §1
# lists them as Tier A, so the gate has to admit them or the Tier-A claim is a
# statement about code that never runs.
DEFAULT_INDEXABLE_EXTS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx",     # Tier-A: JS/TS/Python
    ".cs",                                    # Tier-A: C#
    ".cpp", ".cc", ".cxx", ".h", ".hpp",      # Tier-A: C++ (.h routed to the C++ adapter)
})

# Project descriptor files: edges only, no chunking or embedding. Not operator knobs —
# these name a *capability* of the adapters, not a preference about a tree.
PROJECT_EXTS: frozenset[str] = frozenset({".csproj", ".sln"})
PROJECT_FILES: frozenset[str] = frozenset({"compile_commands.json"})

# Excluded regardless of configuration (ADR-026 §1). Not knobs: indexing `.git`
# means embedding packfiles, and indexing the index means the index contains itself.
#
# `.code-index` is a literal here rather than a read of `[indexer].index_dir`, which
# is still inert (`INDEX_DIR` is a module constant in incremental_indexer). Wiring it
# for exclusion alone would create exactly the half-migrated split-brain ADR-020 was
# written about. When that key is wired, this set reads it.
ALWAYS_IGNORED_DIRS: frozenset[str] = frozenset({".git", ".code-index"})


def _fold(name: str) -> str:
    """Normalization applied to directory names on both sides of a comparison.

    Identity today — matching is case-sensitive, which is the behaviour ADR-026
    commit 3 inverts. It is a function so that the flip is one line rather than a
    scatter of ``.casefold()`` calls that can be applied to one side only.
    """
    return name


# ---------------------------------------------------------------------------
# Config resolution: extra_* extends, the bare key replaces
# ---------------------------------------------------------------------------

def _as_str_list(value: object, key: str) -> list[str]:
    """Validate a config value is a list of strings, or raise naming the key.

    ``dirs = "foo"`` is the failure this catches: a bare string is iterable, so
    without the check it would silently resolve to the character set
    ``{"f", "o"}`` and exclude nothing anybody meant.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(
            f"[ignore].{key} must be a list of strings, got {value!r}. "
            f"Example: {key} = [\"one\", \"two\"]"
        )
    return list(value)


def _resolve(block: dict, key: str, defaults: frozenset[str]) -> frozenset[str]:
    """Apply the ADR-026 §1 precedence table to one knob.

    ============  =============  =========================================
    bare key      ``extra_*``    result
    ============  =============  =========================================
    absent        absent         defaults
    absent        present        defaults union extra
    present       absent         the bare value; defaults discarded
    present       present        ``ValueError`` naming both keys
    ============  =============  =========================================

    An explicitly empty list is honored as written and is *not* the same as an
    absent key: ``extra_dirs = []`` is a no-op and ``dirs = []`` means "exclude
    nothing" — legal, destructive, and exactly what someone asking for it asked for.

    Both keys set is an error rather than a silent precedence because there is no
    reading of "extend *and* replace" a user could have meant, and guessing is how
    the defect this ADR removes was born in the first place.
    """
    extra_key = f"extra_{key}"
    bare = block.get(key)
    extra = block.get(extra_key)

    if bare is not None and extra is not None:
        raise ValueError(
            f"[ignore] sets both `{key}` and `{extra_key}`. They mean opposite things "
            f"— `{key}` replaces the built-in defaults, `{extra_key}` adds to them — "
            f"so there is no combined reading. Keep one."
        )

    if bare is not None:
        return frozenset(_as_str_list(bare, key))
    if extra is not None:
        return frozenset(defaults) | frozenset(_as_str_list(extra, extra_key))
    return frozenset(defaults)


def _resolve_exts(block: dict, defaults: frozenset[str]) -> frozenset[str]:
    """Same precedence as :func:`_resolve`, plus extension-shaped validation.

    Extensions are lower-cased here because both consumers compare against
    ``Path(name).suffix.lower()``. The leading dot is required rather than inferred:
    ``extensions = ["rs"]`` would match nothing at all, and failing loudly at load
    beats a config that quietly indexes zero files.
    """
    resolved = _resolve(block, "extensions", defaults)
    bad = sorted(e for e in resolved if not e.startswith("."))
    if bad:
        raise ValueError(
            f"[ignore].extensions entries must start with a dot: {bad}. "
            f"Write \".rs\", not \"rs\"."
        )
    return frozenset(e.lower() for e in resolved)


# ---------------------------------------------------------------------------
# The policy object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanPolicy:
    """A resolved answer to "what does the indexer look at, in this tree?"."""

    root: str                          # absolute; the directory the scan is anchored to
    config_path: str | None            # the indexer.toml this came from, if any
    ignore_dirs: frozenset[str]        # excluded at any depth
    ignore_root_dirs: frozenset[str]   # excluded at the repository root only
    extensions: frozenset[str]         # chunked + embedded
    project_exts: frozenset[str] = PROJECT_EXTS
    project_files: frozenset[str] = PROJECT_FILES

    # -- directory-level ---------------------------------------------------

    @cached_property
    def _folded_dirs(self) -> frozenset[str]:
        return frozenset(_fold(d) for d in (self.ignore_dirs | ALWAYS_IGNORED_DIRS))

    @cached_property
    def _folded_root_dirs(self) -> frozenset[str]:
        return frozenset(_fold(d) for d in self.ignore_root_dirs)

    def is_ignored_dir(self, name: str, *, at_root: bool = False) -> bool:
        """Whether ``os.walk`` should prune a directory entry named ``name``.

        Folded once at construction rather than per comparison — this runs for every
        directory in the walk.
        """
        folded = _fold(name)
        if folded in self._folded_dirs:
            return True
        return at_root and folded in self._folded_root_dirs

    def prune(self, dirs: list[str], *, at_root: bool) -> None:
        """Prune ``os.walk``'s ``dirs`` list **in place**.

        In place is not a style choice: ``os.walk`` only honors mutation of the list
        it handed you. A ``dirs = [...]`` rebinding inside the loop compiles, reads
        correctly, and silently does nothing.
        """
        dirs[:] = [d for d in dirs if not self.is_ignored_dir(d, at_root=at_root)]

    # -- path-level --------------------------------------------------------

    def _under_ignored_dir(self, rel_path: str) -> bool:
        parts = rel_path.split("/")
        if not parts:
            return False
        if parts[0] and self.is_ignored_dir(parts[0], at_root=True) and len(parts) > 1:
            return True
        return any(self.is_ignored_dir(part) for part in parts[:-1])

    def is_indexable(self, rel_path: str) -> bool:
        """Is this repo-relative path a source file the scan chunks and embeds?"""
        if self._under_ignored_dir(rel_path):
            return False
        return _suffix(rel_path) in self.extensions

    def is_scannable(self, rel_path: str) -> bool:
        """Is this path picked up by the scan *at all* — source or project descriptor?

        Broader than :meth:`is_indexable` by exactly the descriptor files, which are
        parsed for edges but never chunked or embedded.
        """
        if self._under_ignored_dir(rel_path):
            return False
        name = rel_path.rsplit("/", 1)[-1]
        return (
            _suffix(rel_path) in self.extensions
            or _suffix(rel_path) in self.project_exts
            or name in self.project_files
        )

    # -- reporting ---------------------------------------------------------

    def describe(self) -> str:
        """One line for the startup log (ADR-026 §2).

        A cached config that needs a restart to re-read is acceptable for a local
        tool. One that fails invisibly is not — this is what makes "why is that
        directory in my index?" a question you can answer by reading the log.
        """
        origin = self.config_path or "built-in defaults (no indexer.toml found)"
        return (
            f"[scan] config={origin} root={self.root} "
            f"ignore_dirs={len(self.ignore_dirs)} "
            f"ignore_root_dirs={sorted(self.ignore_root_dirs)} "
            f"extensions={len(self.extensions)}"
        )


def _suffix(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


# ---------------------------------------------------------------------------
# Cached resolution
# ---------------------------------------------------------------------------

_cache: dict[str, ScanPolicy] = {}


def reset() -> None:
    """Drop every cached policy. Tests that write an ``indexer.toml`` must call this."""
    _cache.clear()


def scan_policy(start_dir: str | None = None) -> ScanPolicy:
    """Resolve (and cache) the scan policy anchored at ``start_dir``.

    The anchor is **the directory containing ``indexer.toml``**, not the caller's cwd
    (ADR-026 §6). ``MCPServer`` sets ``repo_path = os.getcwd()`` and is sometimes
    launched from ``src/``; resolved from cwd, ``extra_root_dirs = ["benchmarks"]``
    would be matched against ``src/`` and quietly do nothing.
    """
    key = os.path.abspath(start_dir or os.getcwd())
    cached = _cache.get(key)
    if cached is not None:
        return cached

    config_path = find_config_path(key)
    root = os.path.dirname(config_path) if config_path else key
    block = (load_indexer_config(key) or {}).get("ignore", {}) or {}

    policy = ScanPolicy(
        root=root,
        config_path=config_path,
        ignore_dirs=_resolve(block, "dirs", DEFAULT_IGNORE_DIRS),
        ignore_root_dirs=_resolve(block, "root_dirs", DEFAULT_IGNORE_ROOT_DIRS),
        extensions=_resolve_exts(block, DEFAULT_INDEXABLE_EXTS),
    )
    _cache[key] = policy
    return policy


def is_indexable(rel_path: str, start_dir: str | None = None) -> bool:
    """Module-level convenience over :meth:`ScanPolicy.is_indexable`."""
    return scan_policy(start_dir).is_indexable(rel_path)


def is_scannable(rel_path: str, start_dir: str | None = None) -> bool:
    """Module-level convenience over :meth:`ScanPolicy.is_scannable`."""
    return scan_policy(start_dir).is_scannable(rel_path)
