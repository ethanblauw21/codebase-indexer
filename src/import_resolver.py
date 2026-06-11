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
"""
from __future__ import annotations

import json
import os
import re
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

    def __init__(self, repo_root: str) -> None:
        self.repo_root = os.path.abspath(repo_root)
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
            if not os.path.exists(tsconfig_path):
                continue
            try:
                with open(tsconfig_path, "r", encoding="utf-8") as f:
                    # Strip single-line // comments (not valid JSON but common in tsconfig)
                    raw = re.sub(r'//[^\n]*', '', f.read())
                    tsconfig = json.loads(raw)
                paths = (
                    tsconfig.get("compilerOptions", {}).get("paths", {})
                )
                base_url = tsconfig.get("compilerOptions", {}).get("baseUrl", ".")
                for alias_pattern, targets in paths.items():
                    if not targets:
                        continue
                    target = targets[0]
                    # Strip trailing /* from both sides
                    alias_prefix = alias_pattern.rstrip("/*").rstrip("*")
                    target_dir   = target.rstrip("/*").rstrip("*")
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
            with open(barrel_path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
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
            return base if os.path.isfile(base) else None

        # Try appending each extension
        for ext in self._TS_EXTENSIONS:
            candidate = base + ext
            if os.path.isfile(candidate):
                return candidate

        # Try as directory with index file
        for ext in self._TS_EXTENSIONS:
            candidate = os.path.join(base, "index" + ext)
            if os.path.isfile(candidate):
                return candidate

        return None
