# ADR-044: Imports Are Captured and Resolved, and a Call Resolves Only Through the Scope Its Shape Allows

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-044-python-imports`
**Reviewer:** @edb
**Backlog:** [B-056](../backlog.md#b-056) — the correctness half. Also unblocks part of
[B-043](../backlog.md#b-043).
**Depends on:** [ADR-021](./ADR-021-baseline-call-edge-resolution.md) (the resolution order this
amends) · [ADR-011](./ADR-011-receiver-type-inference.md) (its resolution harness, whose gate this
amends).
**Depended on by:** none yet.

> **Revised on its branch, 2026-09-29.** The first draft covered only Python import capture and
> resolution. Measuring it on this repo showed that it can't ship alone (Context, *The trap*). It now
> also covers the call-shape and binding work that was planned as a separate ADR-045. That number
> was never created.

## Context

Three defects, found while grilling B-056:

1. **Python relative imports are dropped.** `python_adapter._IMPORT_QUERY` matches only
   `dotted_name`, and tree-sitter parses `from .x import y`'s module as a `relative_import`. So
   `from .x import y`, `from . import y` and `from ..a.b import y` produce no edge.
2. **No Python import resolves.** `ImportResolver` handles TS/JS only. On this repo's index
   (5abfa39), **0 of 184** Python IMPORTS edges have a `resolved_target`.
3. **A call into a dependency can resolve to an in-repo symbol.** A CALLS edge stores only the
   bare callee name. The Python and TS call queries record `json.loads(x)` and `z.object()` as calls
   to `loads` and `object`. ADR-021 then tries "unique repo-wide name" first, so if the repo defines
   one `loads`, `json.loads` resolves to it at full confidence.

**The trap (measured 2026-09-29, a copy of this repo's index).** Fixing 1 and 2 alone resolves 275
of 825 Python IMPORTS edges. That switches on ADR-021's import-scoped step ("exactly one candidate
in a file the caller imports") for Python for the first time. It resolved **167 more calls. 147 of
them are method calls**, where the step is unsound, because the edge doesn't say what the method is
called on. `_Handler._authorized → get` resolved to `DocumentStore.get`; the call is a `dict.get`.
Only the 17 bare calls (`index_dir()`, `scan_instructions()`) are sound.

The missing fact is the same one defect 3 needs: **the shape of the call.** Is it bare (`f()`), on
an imported module (`json.loads()`, `ns.helper()`), or on some other object (`self.x()`,
`store.get()`)?

## Decision

### 1. Adapters record import bindings and call shape (Python, TS/JS)

- **Python relative imports** become `import` edges with the specifier as written: `.x`, `..a.b`.
  For dots only (`from . import a, b`), one edge per name (`.a`, `.b`). A wildcard keeps `.`.
- **Bindings.** Each adapter builds the file's `local name → import specifier` map from its own
  import statements:
  - Python: `import a.b` binds `a`, `import a.b as x` binds `x` to `a.b`, and `from m import n as
    k` binds `k` to `m`.
  - TS/JS: default, namespace (`* as ns`) and named imports, with aliases.
- **Two new CALLS fields**, computed over every call site the edge collapses. The edge table is
  `UNIQUE(source_fqn, target, kind)`, so one caller's `json.loads()` and its bare `loads()` are one
  edge.
  - `bound_module`: the specifier that the callee name (bare call) or its receiver (member call)
    is bound to. Set only when **every** site binds through the same import, otherwise NULL. For a
    dotted receiver, the longest prefix that is itself an imported module wins (`a.b.f()` with
    `import a.b` binds `a.b`).
  - `member_call`: 1 if any site is a member call whose receiver is **not** an import binding,
    otherwise 0. Adapters that don't compute it (C#, C++, L5X) leave it NULL.

### 2. Imports are classified; Python imports resolve in a pass

A new IMPORTS column, `external`: 1 for a dependency, 0 for in-repo, NULL for unknown.
- **TS/JS, at ingest.** `ImportResolver.classify()`: relative or alias → 0; any other bare
  specifier → 1.
- **Python, in `import_resolver.resolve_python_imports(db)`.** This runs before
  `resolve_call_edges` on every indexing run, against the `files` table as it is now (resolving
  at ingest would go stale as files are added).
  - Resolution: relative by path arithmetic; absolute by matching indexed `.py` files on the
    dotted suffix (`a/b.py`, `a/b/__init__.py`). With several matches, the shallowest wins if it's
    the only one at its depth, otherwise NULL.
  - Classification: resolved or relative → 0. Otherwise the standard library, or a top-level name
    that appears nowhere in the repo, → 1. Anything else → NULL.

### 3. Resolution by shape (`call_resolver`)

- **Bound to an external import** → `external`, unresolved. This fixes defect 3.
- **Bound to an import that resolved to a repo file** → only that file's candidates. One match
  resolves; otherwise it stays unresolved. It never falls back to repo-wide.
- **Bound to an import of unknown status** → unresolved (prefer-unknown).
- **Unbound bare call** (`member_call = 0`) → the ADR-021 order, unchanged.
- **Unbound member call** (`member_call = 1`) → the ADR-021 order **without** the import-scoped
  step. Unique repo-wide and same-file stay as they are today, so current behavior doesn't
  change there.
- **Receiver-typed** (ADR-011) → unchanged.
- **Edges written before this ADR** (`member_call IS NULL`) resolve **exactly as they did before
  it**. That means import-scoping is skipped for Python sources, which never reached that step
  before; otherwise the trap above would fire on existing indexes at the first run after merge.
  The new behavior reaches a file when it's reindexed. A full rebuild is recommended: about 3 min
  on this repo, with the summary cache warm.

### 4. The ADR-011 harness learns the new hints

- `tools/resolution_eval.py` strips `bound_module` and `member_call` along with `receiver_type`
  in its baseline regime, and adds Python and TS fixtures.
- **Gate amendment:** ADR-011's rule that the baseline's precision is exactly 1.0 doesn't hold
  here, because the ADR-021 baseline mis-resolves bound calls, which is defect 3. The new gate
  per language: typed precision == 1.0; typed rate ≥ baseline rate; and a lift in rate **or** in
  precision.
- The committed baseline file now also guards **baseline** precision against regression, so
  C++ and C#, whose baseline precision is 1.0, lose none of the protection they had.

### 5. `CHUNKER_VERSION` is unchanged

It tracks what chunks a file produces (see its comment), and chunks stay byte-identical. Edges
upgrade per file as files are reindexed, which is the reason for §3's legacy rule.

## Consequences

**Better:**
- Python gets a real import graph, including package-internal edges.
- Calls through an external import stop resolving to in-repo namesakes.
- Calls on an imported in-repo module resolve precisely (`index_location.index_dir()`).
- A class of wrong edges disappears, with no change to calls that don't touch an import.
- B-043 gets resolved import links for Python.

**Worse:**
- There are three more edge columns and more adapter code. The collapsed-edge rule is
  conservative: one mixed call site disables binding for that name in that caller.
- Existing indexes keep today's behavior until rebuilt.
- Retrieval can move, because the graph step walks a different set of neighbours (measured below).

**Neutral:**
- For TS/JS member calls on non-import receivers, the import-scoped step is now skipped. Some
  of today's TS resolutions (those that were unsound) become unresolved after a rebuild. That's
  measured below on a copy of a TS index.
- ADR-006's community map and its modularity numbers change.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Ship import resolution first and binding later (the original 044/045 split) | Measured: import resolution alone adds 147 unsound method-call resolutions on this repo. |
| A separate `file_imports` table, as the B-056 grill proposed | The grill wanted it for the display half, which was dropped. Binding needs only the specifier on the edge. |
| Store one CALLS edge per call site, so mixed sites don't collapse | That changes the `UNIQUE(source_fqn, target, kind)` contract that ADR-021, ADR-011 and every graph reader rely on. The conservative collapse gives up a little coverage, never precision. |
| Also drop "unique repo-wide" for unbound member calls | That's a separate precision question with its own blast radius (every language). Out of scope here, and measured below as a follow-up number. |
| Bump `CHUNKER_VERSION` to force rebuilds | It would re-embed every chunk for an edge-only change. The legacy rule makes old edges safe instead. |

## Implementation Log

- [ ] `Edge.bound_module`, `Edge.member_call`, IMPORTS `external`: dataclass, schema, additive migrations, `upsert_file`, `_migrate_edges` column list
- [ ] Python adapter: relative imports, bindings, per-site call shape → `bound_module` / `member_call`
- [ ] TS/JS adapter: bindings (default, namespace, named, aliases), per-site call shape
- [ ] `ImportResolver.classify` at TS ingest; `resolve_python_imports` resolves and classifies
- [ ] `call_resolver`: §3 rules, including the legacy rule; stats gain `bound_external` / `bound_scoped`
- [ ] Resolution harness: strip the new hints in baseline, amend the gate, add Python and TS fixtures, update `baseline.json`
- [ ] Extraction fixture `python/relative_imports`
- [ ] Unit tests: bindings and collapse rules per adapter; resolver classification; legacy rows unchanged
- [ ] Measure on a copy of this repo's index: IMPORTS resolved and classified; CALLS before/after, with a reviewed sample; how many unbound member calls still resolve through "unique repo-wide"
- [ ] Measure on a copy of a TS index (GanttWebApp): CALLS before/after, rebuilt from its sources
- [ ] Retrieval check before/after
