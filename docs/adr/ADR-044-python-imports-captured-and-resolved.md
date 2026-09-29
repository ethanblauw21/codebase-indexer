# ADR-044: Imports Are Captured and Resolved, and a Call Resolves Only Through the Scope Its Shape Allows

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-044-python-imports`
**Reviewer:** @edb
**Backlog:** [B-056](../backlog.md#b-056) — the correctness half. Also unblocks part of
[B-043](../backlog.md#b-043).
**Depends on:** [ADR-021](./ADR-021-baseline-call-edge-resolution.md) (the resolution order this
amends) · [ADR-011](./ADR-011-high-precision-call-resolution.md) (its resolution harness, whose gate this
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
- **Bound to an in-repo import** (`external = 0`) → only the imported file's candidates. One
  match resolves; several stay unresolved. When the file has **none**, only the "unique
  repo-wide" rule may still apply. That covers a barrel (`tb/index.ts` that only re-exports) and
  an in-repo import that didn't resolve to a file. Without it, every call routed through a barrel
  would be lost; "unique repo-wide" for a name imported from the repo is the same evidence it
  was before this ADR.
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

### 5. tsconfig alias prefixes keep their slash

Found while writing `classify`. `ImportResolver._load_tsconfig_aliases` stripped `"@/*"` with
`rstrip("/*")`, which also removes the slash. The prefix became `@`, so `@/lib/x` expanded to
`join(src, "/lib/x")`, an absolute path that never exists. **No `@/` import has ever resolved.**
It also made every scoped package (`@tanstack/…`) look like an alias, which `classify` would
have called in-repo. Now only the trailing `*` is removed (`removesuffix("*")`).

### 6. `CHUNKER_VERSION` is unchanged

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
- **`from pkg import submodule` binds to the package.** `t.make()` after `from ib import tools as
  t` scopes to `ib/__init__.py`, which has no `make`. The adapter can't tell a submodule from an
  attribute. The call stays unresolved: a miss, never a wrong edge. The `python/import_binding`
  resolution fixture records it (typed rate 0.75, not 1.0).
- Bindings are file-wide and ignore shadowing. A local variable or parameter that reuses an import
  name (`json = load()`) is still treated as the import. A method on an object imported from an
  in-repo module (`from store import registry; registry.get()`) scopes to that module's file,
  which is usually right and never reaches another file.

**Neutral:**
- For TS/JS member calls on non-import receivers, the import-scoped step is now skipped. Some
  of today's TS resolutions (those that were unsound) become unresolved after a rebuild. That's
  measured below on a copy of a TS index.
- ADR-006's community map and its modularity numbers change.
- §5 changes any repo with `"@/*"`-style aliases a lot: on InventoryApp-V2, 1,343 IMPORTS edges
  gain a `resolved_target`. Blast radius, test coverage and import corroboration, which read
  them, see those links for the first time.

## Results (2026-09-29, copies of each index; nothing live was written)

`gpu-crash-repro/adr044_measure.py` copies a `graph.db` through SQLite's backup API. It re-parses
every Python/TS/JS file with this branch's adapters (content at the index's commit in git mode).
It replaces those files' edges, runs `resolve_python_imports` and `resolve_call_edges`, and
compares against the CALLS resolutions stored in the live index. The "stripped" regime is this
branch with the two hints nulled.

| Index | Stored → stripped | Stripped → after | Wrong edges removed | Wrong edges corrected | Added |
|---|---|---|---|---|---|
| indexer (Python, 141 files) | identical (2,429) | 2,429 → 2,462 | 22 | 4 | 55 |
| GanttWebApp (TS, 269 files) | identical (1,940) | 1,940 → 1,928 | 15 | 0 | 3 |
| InventoryApp-V2 (TS, 479 files) | 1,087 → 1,099 (alias fix) | 1,099 → 1,095 | 4 | 0 | 12 |

- **Stripped equals stored** on the two repos without aliases. The legacy rule does its job: the
  trap's 147 unsound method-call resolutions don't appear.
- **Every lost, changed and gained edge was reviewed by hand, not sampled.**
  - Every lost edge was wrong: `time.sleep` → a test's `FakeClock.sleep`, `numpy.empty` →
    `BatchStats.empty`, textual's `Label` → a C# `Geo.Label`, testing-library's
    `fireEvent.click` → a test file's `click` helper, the Firebase client SDK's `getAuth` → the
    repo's admin wrapper.
  - Every changed edge was wrong before and right after. `model_client.embed` really calls
    `core.embed`; the old edge pointed at itself.
  - Every gained edge is a call through an in-repo module (`index_location.index_dir()`,
    `@/lib/utils`), plus one lucky bare call on a parameter named like the function it's passed.
- Imports: this repo, 825 Python IMPORTS: 275 resolved, 550 classified external, 0 unknown.
  Gantt, 1,064 TS: 684 resolved, 362 external. V2, 2,865 TS: 1,881 resolved (was 538), 981 external.
- Follow-up number (Alternatives, row 4): unbound member calls that still resolve through
  "unique repo-wide": 511 here, 114 on Gantt, 9 on V2.
- Resolution harness: Python baseline rate 0.25 / precision 0.50 → typed 0.75 / 1.00. TS 0.33 /
  0.33 → 1.00 / 1.00. C# and C++ unchanged at 0.40 → 1.00, precision 1.00.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Ship import resolution first and binding later (the original 044/045 split) | Measured: import resolution alone adds 147 unsound method-call resolutions on this repo. |
| A separate `file_imports` table, as the B-056 grill proposed | The grill wanted it for the display half, which was dropped. Binding needs only the specifier on the edge. |
| Store one CALLS edge per call site, so mixed sites don't collapse | That changes the `UNIQUE(source_fqn, target, kind)` contract that ADR-021, ADR-011 and every graph reader rely on. The conservative collapse gives up a little coverage, never precision. |
| Also drop "unique repo-wide" for unbound member calls | That's a separate precision question with its own blast radius (every language). Out of scope here, and measured below as a follow-up number. |
| Bump `CHUNKER_VERSION` to force rebuilds | It would re-embed every chunk for an edge-only change. The legacy rule makes old edges safe instead. |

## Implementation Log

- [x] `Edge.bound_module`, `Edge.member_call`, IMPORTS `external`: dataclass, schema, additive migration (`_migrate_edge_import_binding`, run after `_migrate_edge_kinds` so its table rebuild can't drop them), `upsert_file` (f9f6a6e)
- [x] Python adapter: relative imports, bindings, per-site call shape → `bound_module` / `member_call`
- [x] TS/JS adapter: bindings (default, namespace, named, aliases), per-site call shape
- [x] `ImportResolver.classify` at TS ingest; `resolve_python_imports` resolves and classifies
- [x] `call_resolver`: §3 rules, including the legacy rule and the barrel fallback; stats gain `bound_external` / `bound_scoped`, printed by `code-indexer`
- [x] §5: tsconfig alias prefix fix, with a regression test (no `ImportResolver` alias test existed)
- [x] Resolution harness: support files, strip the new hints in baseline, amend the gate, Python and TS fixtures, `baseline.json` (78984c6)
- [x] Extraction fixture `python/relative_imports`: Python extraction stays 1.000 / 1.000
- [x] Unit tests (`tests/test_call_shape_resolution.py`, 21): bindings and collapse rules per adapter; classification; module index; round trip; every §3 rule; legacy `.py` and `.ts` rows. `test_import_scoped_tiebreak` moved to `.ts` paths, since a legacy `.py` row no longer import-scopes. Full suite: 667 passed, 1 skipped.
- [x] Measure on a copy of this repo's index (Results)
- [x] Measure on copies of TS indexes: GanttWebApp and InventoryApp-V2 (Results)
- [ ] Retrieval check before/after
