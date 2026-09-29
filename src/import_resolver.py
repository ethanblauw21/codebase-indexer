"""
import_resolver.py — Canonical import resolution for the Code Intelligence Engine.

Resolves raw module specifiers to canonical repo-relative paths so the graph
stores actual dependency topology instead of raw import strings.

Handles:
  - tsconfig.json compilerOptions.paths aliases  (@/lib/data → src/lib/data.ts)
  - Relative imports  (../../components/Button → src/components/Button.tsx)
  - Index barrel files  (components/Button → components/Button/index.ts)
  - Extension inference  (.ts / .tsx / .js / .jsx)

Non-repo specifiers (node_modules, bare package names without path aliases)
return None so callers can skip resolved_target storage and fall back to the
raw target string.

Python imports are resolved separately, by `resolve_python_imports(db)` (ADR-044): a
pass over the IMPORTS edges against the indexed file list, run on every indexing run.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional


class ImportResolver:
    """
    Resolves raw TypeScript/JavaScript import specifiers to canonical
    repo-relative POSIX paths (forward slashes, no leading slash).

    Parameters
    ----------
    repo_root : str
        Absolute path to the repository root (where tsconfig.json lives).
    """

    _TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")
    EXTENSIONS = _TS_EXTENSIONS   # the files whose imports this resolves and classifies

    def __init__(self, repo_root: str, source=None) -> None:
        self.repo_root = os.path.abspath(repo_root)
        # ADR-042 §7: file checks and reads go through the build's source, so a git
        # source resolves imports against its commit, not the checked-out folder.
        # Paths stay absolute here; they are made repo-relative only at the source.
        self._source = source
        self.path_aliases: dict[str, str] = self._load_tsconfig_aliases()
        self._barrel_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # tsconfig alias loading
    # ------------------------------------------------------------------

    def _load_tsconfig_aliases(self) -> dict[str, str]:
        """
        Read tsconfig.json compilerOptions.paths and return a dict mapping
        alias prefix (without trailing *) to resolved directory.

        Example: {"@/*": ["./src/*"]} → {"@/": "src/"}
        """
        aliases: dict[str, str] = {}
        for candidate in ("tsconfig.json", "tsconfig.base.json"):
            tsconfig_path = os.path.join(self.repo_root, candidate)
            if not self._isfile(tsconfig_path):
                continue
            try:
                # Strip single-line // comments (not valid JSON but common in tsconfig)
                raw = re.sub(r'//[^\n]*', '', self._read(tsconfig_path))
                tsconfig = json.loads(raw)
                paths = (
                    tsconfig.get("compilerOptions", {}).get("paths", {})
                )
                base_url = tsconfig.get("compilerOptions", {}).get("baseUrl", ".")
                for alias_pattern, targets in paths.items():
                    if not targets:
                        continue
                    target = targets[0]
                    # Strip the trailing `*` from both sides and keep the `/`: "@/*" is the
                    # prefix "@/", so "@scope/pkg" is not under it (ADR-044). The old
                    # rstrip("/*") made it "@", and "@/lib/x" expanded to the absolute "/lib/x".
                    alias_prefix = alias_pattern.removesuffix("*")
                    target_dir   = target.removesuffix("*")
                    # Resolve target_dir relative to baseUrl
                    resolved = os.path.normpath(
                        os.path.join(self.repo_root, base_url, target_dir)
                    )
                    aliases[alias_prefix] = resolved
            except Exception:
                pass
            break  # use first found
        return aliases

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, specifier: str, from_file: str) -> Optional[str]:
        """
        Return a canonical repo-relative POSIX path for `specifier` imported
        from `from_file` (which may be repo-relative or absolute).

        Returns None if:
          - the resolved path is outside the repo root (e.g. node_modules)
          - the specifier looks like a bare package name with no path alias match

        Steps
        -----
        1. Expand tsconfig path aliases
        2. If relative (starts with ./ or ../) → join with dirname(from_file)
        3. Try file with known TS extensions
        4. Try as directory with /index.{ts,tsx,...}
        5. Normalise to forward-slash repo-relative path
        """
        # Normalise from_file to absolute
        if not os.path.isabs(from_file):
            from_file = os.path.join(self.repo_root, from_file)
        from_dir = os.path.dirname(from_file)

        # ── 1. tsconfig alias expansion ──────────────────────────────────────
        abs_candidate = self._expand_alias(specifier)

        # ── 2. Relative specifier ────────────────────────────────────────────
        if abs_candidate is None:
            if specifier.startswith("./") or specifier.startswith("../"):
                abs_candidate = os.path.normpath(os.path.join(from_dir, specifier))
            else:
                # Bare package name or unknown alias → not a repo file
                return None

        # ── 3. Try as-is + known extensions ──────────────────────────────────
        resolved = self._find_file(abs_candidate)
        if resolved is None:
            return None

        # ── 4. Normalise to repo-relative POSIX path ──────────────────────────
        try:
            rel = os.path.relpath(resolved, self.repo_root)
        except ValueError:
            return None  # different drive on Windows

        if rel.startswith(".."):
            return None  # outside repo root

        return rel.replace("\\", "/")

    def classify(self, specifier: str, resolved: Optional[str] = None) -> Optional[bool]:
        """ADR-044 §2: True for a package, False for an in-repo module, None if unsure.

        Relative and tsconfig-alias specifiers are in-repo even when no file was found (a
        missing file is not a package). Any other bare specifier, `node:fs` included, is a
        package. An absolute path is neither.
        """
        if resolved or specifier.startswith(".") or self._expand_alias(specifier) is not None:
            return False
        if specifier.startswith("/"):
            return None
        return True

    def get_barrel_exports(self, barrel_path: str) -> list[str]:
        """
        Parse an index.ts barrel file and return its re-exported symbol names.
        Result is cached.

        `barrel_path` may be repo-relative or absolute.
        """
        if not os.path.isabs(barrel_path):
            barrel_path = os.path.join(self.repo_root, barrel_path)
        barrel_path = os.path.normpath(barrel_path)

        if barrel_path in self._barrel_cache:
            return self._barrel_cache[barrel_path]

        names: list[str] = []
        try:
            src = self._read(barrel_path)
            # Match: export { Foo, Bar } from '...'  or  export * from '...'
            for m in re.finditer(
                r'export\s+\{([^}]*)\}\s+from',
                src,
            ):
                for name in m.group(1).split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name:
                        names.append(name)
        except OSError:
            pass

        self._barrel_cache[barrel_path] = names
        return names

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rel(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.repo_root).replace("\\", "/")

    def _isfile(self, abs_path: str) -> bool:
        if self._source is None:
            return os.path.isfile(abs_path)
        try:
            return self._source.exists(self._rel(abs_path))
        except ValueError:
            return False    # another drive on Windows: not in this repository

    def _read(self, abs_path: str) -> str:
        if self._source is None:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        return self._source.read(self._rel(abs_path))

    def _expand_alias(self, specifier: str) -> Optional[str]:
        """Expand a tsconfig path alias to an absolute path, or return None."""
        for alias_prefix, target_dir in self.path_aliases.items():
            if specifier.startswith(alias_prefix):
                remainder = specifier[len(alias_prefix):]
                return os.path.normpath(os.path.join(target_dir, remainder))
        return None

    def _find_file(self, base: str) -> Optional[str]:
        """
        Try `base` with each known extension, then as a directory index.
        Returns the first existing absolute path, or None.
        """
        # Already has a recognised extension
        _, ext = os.path.splitext(base)
        if ext.lower() in self._TS_EXTENSIONS:
            return base if self._isfile(base) else None

        # Try appending each extension
        for ext in self._TS_EXTENSIONS:
            candidate = base + ext
            if self._isfile(candidate):
                return candidate

        # Try as directory with index file
        for ext in self._TS_EXTENSIONS:
            candidate = os.path.join(base, "index" + ext)
            if self._isfile(candidate):
                return candidate

        return None


# ---------------------------------------------------------------------------
# Python (ADR-044)
# ---------------------------------------------------------------------------

def _python_candidates(parts: list[str]) -> tuple[str, str]:
    """The two files a dotted module can be: `a/b.py` and `a/b/__init__.py`."""
    base = "/".join(parts)
    return base + ".py", base + "/__init__.py"


class PythonModuleIndex:
    """Resolve Python module specifiers against a set of repo-relative `.py` paths.

    Paths use forward slashes, as the `files` table stores them. Built once per pass.
    """

    def __init__(self, py_paths) -> None:
        self.paths = set(py_paths)
        # Every path suffix that starts at a directory boundary → the full paths ending in
        # it, so `import config` finds `src/config.py` without knowing any source roots.
        self._by_suffix: dict[str, list[str]] = {}
        for p in self.paths:
            segs = p.split("/")
            for i in range(len(segs)):
                self._by_suffix.setdefault("/".join(segs[i:]), []).append(p)

    def resolve(self, spec: str, from_file: str) -> Optional[str]:
        """The repo file `spec` (as written in `from_file`) refers to, or None."""
        if spec.startswith("."):
            return self._resolve_relative(spec, from_file)
        return self._resolve_absolute(spec.split("."))

    def _resolve_relative(self, spec: str, from_file: str) -> Optional[str]:
        level = len(spec) - len(spec.lstrip("."))
        rest = [p for p in spec[level:].split(".") if p]
        base = from_file.split("/")[:-1]             # the importing file's package
        if level - 1 > len(base):
            return None                               # climbs above the repo root
        base = base[: len(base) - (level - 1)]
        if rest:
            for cand in _python_candidates(base + rest):
                if cand in self.paths:
                    return cand
            if len(rest) == 1:
                # `from . import name` (ADR-044 §1 emits it as `.name`): `name` may be an
                # attribute of the package rather than a submodule; the package is the edge.
                pkg = "/".join(base + ["__init__.py"])
                return pkg if pkg in self.paths else None
            return None
        pkg = "/".join(base + ["__init__.py"])        # bare `.` (a wildcard import)
        return pkg if pkg in self.paths else None

    def _resolve_absolute(self, parts: list[str]) -> Optional[str]:
        if not all(parts):
            return None
        matches: list[str] = []
        for suffix in _python_candidates(parts):
            matches += self._by_suffix.get(suffix, [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        # Several modules share this dotted tail: the shallowest wins, if it is alone at
        # its depth (`config` → `src/config.py` over `tests/fixtures/x/config.py`).
        depth = min(m.count("/") for m in matches)
        shallowest = [m for m in matches if m.count("/") == depth]
        return shallowest[0] if len(shallowest) == 1 else None


def _python_external(spec: str, resolved: Optional[str], repo_names: set[str]) -> Optional[bool]:
    """ADR-044 §2: in-repo if resolved or relative; a dependency if the standard library or a
    top-level name found nowhere in the repo; otherwise unknown (None)."""
    if resolved or spec.startswith("."):
        return False
    top = spec.split(".")[0]
    if top in sys.stdlib_module_names or top not in repo_names:
        return True
    return None


def resolve_python_imports(db) -> dict:
    """Recompute `resolved_target` and `external` on every IMPORTS edge from a `.py` file
    (ADR-044 §2).

    Runs on every indexing run, against the `files` table as it is now, so an import
    resolves as soon as its target file is indexed and un-resolves when it is deleted.
    Returns counts: ``resolved`` / ``unresolved``, and ``external`` of the unresolved.
    """
    conn = db._conn
    index = PythonModuleIndex(
        p for (p,) in conn.execute("SELECT path FROM files WHERE path LIKE '%.py'")
    )
    # Every directory and module name in the repo: a top-level import name that is none of
    # these cannot be an in-repo module under any source root.
    repo_names = {seg.removesuffix(".py") for p in index.paths for seg in p.split("/")}
    updates: list[tuple[Optional[str], Optional[int], int]] = []
    stats = {"resolved": 0, "unresolved": 0, "external": 0}
    for eid, source, target, current, cur_ext in conn.execute(
        "SELECT id, source_fqn, target, resolved_target, external FROM edges "
        "WHERE kind = 'IMPORTS' AND source_fqn LIKE '%.py'"
    ):
        resolved = index.resolve(target, source)
        external = _python_external(target, resolved, repo_names)
        stats["resolved" if resolved else "unresolved"] += 1
        stats["external"] += bool(external)
        ext_int = None if external is None else int(external)
        if resolved != current or ext_int != cur_ext:
            updates.append((resolved, ext_int, eid))
    if updates:
        with db._tx() as cur:
            cur.executemany(
                "UPDATE edges SET resolved_target = ?, external = ? WHERE id = ?", updates)
        db.invalidate_graph_cache()
    return stats
