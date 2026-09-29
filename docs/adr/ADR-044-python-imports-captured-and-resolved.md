# ADR-044: Python Imports Are Captured and Resolved to Repo Files

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-044-python-imports`
**Reviewer:** @edb
**Backlog:** [B-056](../backlog.md#b-056) — the correctness half. Also unblocks part of
[B-043](../backlog.md#b-043).
**Depends on:** [ADR-021](./ADR-021-call-edge-resolution.md) — its import-scoped strategy,
which reads `IMPORTS.resolved_target` and has never had a Python edge to read.
**Depended on by:** ADR-045 — it tells in-repo from external imports using the resolution this
ADR adds.

## Context

Two defects, found while grilling B-056 and checked against sample source on 2026-09-28:

1. **Relative imports are dropped.** `python_adapter._IMPORT_QUERY` matches only `dotted_name`,
   and tree-sitter parses the module of `from .x import y` as a `relative_import` node. So
   `from .x import y`, `from . import y` and `from ..a.b import y` produce no edge at all. A
   package's links between its own modules are the edges most worth having, and they're the ones
   missing.
2. **No Python import resolves.** `ImportResolver` (`import_resolver.py`) resolves TS/JS only. For
   a specifier without `./`, `../` or a tsconfig alias it returns `None`, and every Python module
   name is shaped like that. On this repo's index (5abfa39), **0 of 184** Python IMPORTS edges
   have a `resolved_target`.

What it costs:
- ADR-021's import-scoped strategy ("exactly one candidate in a file the caller imports") never
  fires for Python.
- `get_importers_resolved()` returns nothing for Python, so every structural hit on a Python repo
  is labeled uncorroborated.
- B-043's community map falls back to raw targets, and names from outside the repo fill it.

## Decision

1. **Capture relative imports** (`python_adapter.py`). A `relative_import` module becomes an
   `import` edge whose target is the specifier as written: `.x`, `..a.b`. For `from . import a, b`,
   where the module is dots only, emit one edge per imported name (`.a`, `.b`). Python binds a
   submodule when one exists, and a bare `.` edge would point at nothing specific. Absolute
   imports keep their current targets.
2. **Resolve Python imports in a pass, not at ingest** (`import_resolver.resolve_python_imports(db)`).
   It runs just before `resolve_call_edges` on every indexing run and recomputes `resolved_target`
   for every IMPORTS edge from a `.py` file, against the `files` table as it is now. Resolving at
   ingest would go stale: `a.py`'s `import b` would stay unresolved after `b.py` is added, until
   `a.py` itself changed. The pass is string work over the path list and needs no file reads.
   - **Relative** `..a.b` from `pkg/sub/m.py`: go up one directory per extra dot, then look for
     `a/b.py` or `a/b/__init__.py`. Dots only (`from .. import`, which item 1 turns into `..name`
     edges) looks for `name.py` or `name/__init__.py` in that directory, and otherwise the
     package's own `__init__.py`.
   - **Absolute** `a.b`: the indexed `.py` files whose path is, or ends with, `/a/b.py` or
     `/a/b/__init__.py`. One match resolves. With several, the match with the fewest path segments
     wins if it's the only one at that depth; otherwise the edge stays unresolved. This finds
     `src/`-layout modules (`import config` → `src/config.py`) without knowing any source roots.
   - **No match**: NULL, as today. Standard library and third-party imports simply don't resolve.
     Telling those apart is ADR-045's job.
3. **`CHUNKER_VERSION` 3 → 4** (ADR-033). Item 1 changes adapter output, so existing indexes warn
   until they're rebuilt.

## Consequences

**Better:**
- Python gets a real import graph, including package-internal edges.
- ADR-021's import-scoped strategy works for Python.
- Structural hits on Python code can show as corroborated.
- B-043 gets resolved import links to use instead of raw strings.

**Worse:**
- The pass adds work on every indexing run. It's linear in IMPORTS edges times Python files, via
  a suffix map built once per pass; measured below.
- Suffix matching can be wrong where a repo has two modules with the same dotted tail at the same
  depth. That case stays unresolved rather than guessing.
- **Retrieval can move:** more CALLS edges resolve (import-scoped), so the graph step walks more
  neighbours. Measured below.

**Neutral:**
- `find_dead_code` and blast radius compare module names (`_module_stem`) and read `resolved or
  target`, so they see the same stems as before. Relative edges add importers they used to miss
  through their regex fallback.
- ADR-006's community map changes, and its modularity numbers with it.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Resolve at ingest, like `ImportResolver` does for TS | It goes stale as files are added or removed, and the resolution needs the whole file list, which a single-file watchdog ingest doesn't have. |
| Resolve against configured source roots (`pyproject` `package-dir`, `pythonpath`) | Every layout declares roots differently (setuptools, poetry, pytest `pythonpath`, none at all). Suffix matching over indexed files needs no configuration and finds the same modules. |
| Keep a single `.` edge for `from . import a` | It resolves to `__init__.py` at best, and names nothing about which submodule is used. |

## Implementation Log

- [ ] `python_adapter.py`: capture `relative_import` modules, with one edge per name for dots-only
- [ ] `import_resolver.resolve_python_imports(db)` + call site before `resolve_call_edges`
- [ ] `CHUNKER_VERSION` = 4
- [ ] Extraction fixture `tests/fixtures/conformance/python/relative_imports` (authored from source semantics)
- [ ] Unit tests: relative (single and double dot, dots-only), absolute (src-layout, package `__init__`), same-depth ambiguity → NULL, stdlib → NULL
- [ ] Measure on this repo: IMPORTS resolved before/after; CALLS resolved/ambiguous/external before/after, with a reviewed sample of newly resolved calls; pass time
- [ ] Retrieval check: `tools/eval_retrieval.py` before/after on this repo
