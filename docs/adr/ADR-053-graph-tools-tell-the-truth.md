# ADR-053: Communities Hold Only Repo Nodes, and Data Flow Has Per-Language Rules

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-053-graph-tool-accuracy`
**Reviewer:** @edb
**Backlog:** [B-043](../backlog.md#b-043) (communities filled with names from outside the repo) ·
[B-042](../backlog.md#b-042) (`trace_data_flow` only understands Firestore and Next.js)
**Depends on:** [ADR-044](./ADR-044-python-imports-captured-and-resolved.md) — Python IMPORTS edges
carry a `resolved_target`, which is what lets the B-043 filter keep import links.
**Depended on by:** none yet.

## Context

Two exploratory tools give wrong answers about this repo.

**B-043: `map_module_communities`.** `CodeDB.get_graph_edges()` returns
`COALESCE(resolved_target, target)`, so an edge that never resolved points at its raw name. Every
unresolved `append()`, `bool()`, `ArgumentParser()`, `import os` and `class X(Protocol)` became a
graph node, and then a community member and a betweenness hub. The backlog's caveat (no Python
import resolves, so a filter would drop every import link) no longer holds: ADR-044 resolves them.

Measured on this repo at `871b55c`, graph built SQLite-only (scan → `parse_file` → `upsert_file` →
`resolve_python_imports` → `resolve_call_edges`, the path `tools/resolution_eval.py` uses; no
embedder). 172 files, tests and conformance fixtures included, as the indexer scans them.

| kind | resolved | unresolved | of the unresolved |
|---|---|---|---|
| CALLS | 2,664 | 4,572 | none is a repo node |
| IMPORTS | 297 (to files) | 596 | 578 `external = 1`, 6 broken relative imports in fixtures, 12 C#/C++ unknown |
| EXTENDS | 0 | 27 | bare names; 9 name an in-repo type |
| IMPLEMENTS | 0 | 4 | bare names; all 4 name an in-repo type |
| OWNS | 0 | 480 | the raw target is already a symbol FQN |

So "resolved" is the wrong test: OWNS targets never get a `resolved_target` and are all real, and
heritage edges never get one either. **615 community members were not a symbol or file of the
repo.**

**B-042: `trace_data_flow`.** Its layers are path tests (`firebase/admin`, `functions/src`,
`page.tsx`), a definition is `export interface|type|class`, and a producer is a Firestore
`.set/.add/.update` in an admin or functions file. On this repo `trace_data_flow("chunk_summaries")`
found no definition (`CREATE TABLE chunk_summaries` in `db.py`) and no producer, and listed the
writer `cache_summaries` (`INSERT OR IGNORE INTO chunk_summaries`) as a consumer. On Apps Script it
finds nothing either: there is no `export`, and writes are `setValue(s)`, `appendRow`,
`setProperty`.

## Decision

### 1. A graph edge must end at a node the repo defines (B-043)

`get_graph_edges()` returns an edge only when its effective target (`resolved_target`, else
`target`) is a symbol FQN or an indexed file path. Everything else (an unresolved call, an
external import, a broken relative import) is dropped, for every kind, not only CALLS.

**EXTENDS/IMPLEMENTS are bound at read time**, because nothing else binds them: a bare base name
binds to the type of that name (class, interface, struct, record, enum, type alias) in the
subclass's own file, else to the repo's only type of that name. `Protocol`, `NamedTuple`,
`Exception` have no in-repo type and drop. An ambiguous name across files (`Animal` in three
fixtures, with none in the subclass's file) drops too: the prefer-unknown rule of ADR-021. On this
repo 13 of 31 heritage edges bind, including `TypeScriptAdapter → _WebAdapter`.

The filter lives in `get_graph_edges()`, so `graph_viz`'s DSM, its other reader, gets the same
graph.

### 2. Data-flow rules are per language and live on the adapters (B-042)

Each adapter that has rules gets `data_flow_rules()`, returning
`{"definition": [...], "producer": [...], "file_marker": [...]}`. Each rule is a regex in which
`{name}` stands for the traced name (and its PascalCase variant, as today). `file_marker`, when
present, gates the whole set: the rules apply only to files whose text matches one marker.

- **Python** (`python_adapter.py`):
  - definition: `CREATE [TEMP] TABLE|VIEW [IF NOT EXISTS] name`, `class Name` (dataclasses
    included), a module-level `NAME = …` or `NAME: T = …`;
  - producer: `INSERT [OR …] INTO name`, `REPLACE INTO name`, `UPDATE name … SET`,
    `DELETE FROM name`; an attribute or subscript write (`self.name = …`, `x.name[k] = …`,
    `name[k] = …`, `+=` too, never `==`); a mutating call (`name.append|extend|insert|update|add|
    setdefault|pop|remove|discard|clear(`).
  - SQL keywords match **upper case only**, the house SQL style, so a docstring's "Update
    chunk_summaries when …" is not a writer. A bare local rebinding (`name = fetch()`) is not a
    producer: it is usually a read into a local.
- **TS/JS, Firestore and Next.js:** unchanged, and still in `trace_data_flow`, line for line. Its
  layer labels, its file-level producer pre-pass and its `lib/ types/ models/` definition fallback
  stay as they were (a regression test pins their output).
- **Apps Script** (`ts_adapter.py`, for `.js`/`.ts` — clasp pulls `.gs` files as `.js`), gated on
  a file marker (`SpreadsheetApp`, `PropertiesService`, `DocumentApp`, `DriveApp`, `ScriptApp`), so
  a Next.js file's `form.setValue()` changes nothing:
  - definition: `function name`, `class Name`, `const|let|var name =`;
  - producer: `setValue(s)`, `setFormula(s)`, `appendRow`, `insertRow…`, `setProperty`,
    `setProperties`, `deleteProperty`, `deleteAllProperties`.
- **C#** (`csharp_adapter.py`) gets rules now, because they are cheap and unambiguous:
  - definition: `class|record|struct|interface|enum Name`;
  - producer: the SQL rules (Dapper and raw ADO.NET), and EF Core's
    `.Name.Add|AddRange|Update|UpdateRange|Remove|RemoveRange|Attach(` and
    `Set<Name>().Add…(`.
  They are tested on fixtures only; B-063 (a real ASP.NET build) is where they get checked.
- **C++ and L5X:** no rules. L5X writers are `what_writes`' job, exactly, and are not routed
  through here (ADR-013, `src/CLAUDE.md`).

### 3. How `trace_data_flow` uses the rules

- In the ranked loop, a chunk is a **definition** if the legacy `export` test or one of its file's
  definition rules matches; else a **producer** if its file is a legacy Firestore producer or one of
  its file's producer rules matches **in the chunk**; else transformer or consumer, as before.
- The rules match per chunk, not per file: a Python file usually holds both the writer and the
  readers of a table, so marking the whole file (as the Firestore pre-pass does) would make every
  reader a producer.
- **The answer is no longer limited by ranking for rule hits.** After the ranked loop, two sweeps
  over the whole doc store add what ranking missed: a definition sweep, when nothing was found (the
  legacy `lib/` fallback first, then the rules, up to 3 entries), and a producer sweep over
  tier-1 chunks (up to 15). This is what finds `CREATE TABLE chunk_summaries`, which sits in a
  tier-2 window the ranked loop skips once a tier-1 chunk of `db.py` has been seen.
- Neither sweep runs a rule on a file with none, so a Firestore/Next.js repo's output is unchanged.

### 4. No index is an error

Both tools now raise when the index has never been built (`index_meta.last_verified_at` unset,
ADR-025 §4), so a client sees `isError: true`. `map_module_communities` used to return its "No
index found" text as a successful result, and since ADR-047 binds the index at entry, the empty
`graph.db` that binding creates made even that check pass. `trace_data_flow` went on to search an
empty index.

## Consequences

**Better:**
- Communities and god objects describe the repo. On this repo, modularity rises from 0.576 to
  0.861 and `CodeDB` (51 in, 51 out, 10 coupling communities) is the top god object, which it is;
  before, external hubs hid it (Implementation Log).
- In-repo import links survive: all 297 resolved IMPORTS edges stay.
- `trace_data_flow` names SQL tables' definitions and writers, Python classes, attribute and
  collection writes, Apps Script writers and EF/SQL writers in C#.

**Worse:**
- ADR-006's numbers on this repo change: 73 → 84 communities, nodes 2,620 → 1,930, edges
  8,614 → 3,428. Anyone comparing reports across this change compares two graphs.
- The graph no longer shows what the repo leans on outside itself (`requests`, `os`). That was
  never a useful community member, but it is information, and only `analyze_blast_radius`'s import
  text scan still sees it.
- Rule hits are regexes over chunk text, not resolved relations: a producer is a chunk that writes
  the name, not a proven writer. The tool stays exploratory.
- `get_graph_edges()` does a little name binding. Binding heritage edges belongs in the resolver,
  and would move there if a second reader needed it.

**Neutral:**
- `what_writes` is untouched and still not routed through `trace_data_flow`.
- Python and C# outputs keep today's layer labels (`UTILITY` for most Python files). Per-language
  layers are an open question below.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Drop unresolved CALLS targets only | Leaves 118 non-repo members on this repo: `os`, `json`, `Protocol`, broken relative imports. Measured below. |
| Drop every edge with no `resolved_target` | Drops all 480 OWNS edges (their targets are FQNs, never "resolved") unless special-cased, and every in-repo heritage edge. With OWNS special-cased it is close to the decision but loses `TypeScriptAdapter → _WebAdapter` and the C# base types. |
| Keep external nodes, exclude them from membership | They still shape Louvain and betweenness: `os` is a hub between modules that share nothing else, and fan-out counts would still include every `append`. Excluding them from the printout only hides the distortion. |
| Rules in `indexer.toml` | Users could add their own, but a rule needs `{name}` templating, flags and a file gate, which is a small language in TOML; and a config default must equal the code default (`test_config_drift.py`), so the built-ins would be duplicated in two places. Adapters already own each language's idioms (`test_conventions`). A `[data_flow]` extension block can be added later on top of this. |
| Route producers through the resolved edge graph (like `what_writes`) | Only L5X has `writes` edges. For SQL, attributes and Apps Script there is no relation to read; the regex is what there is. |
| Mark whole files as producers, as the Firestore pre-pass does | Python files hold readers and writers together (`db.py`), so every reader would become a producer. |

## Implementation Log

- [x] Measure the base graph and the alternatives on this repo (below)
- [x] `get_graph_edges()` keeps only edges to repo nodes and binds heritage edges
- [x] `data_flow_rules()` on the Python, TS/JS (Apps Script) and C# adapters; the shared SQL
  rules in `adapters/base.py`
- [x] `trace_data_flow` reads the rules, sweeps for definitions and producers
- [x] Both tools raise when there is no index (`_require_built_index`)
- [x] Tests: `tests/test_graph_tools_communities.py` (7), `tests/test_graph_tools_data_flow.py`
  (13), one existing test updated
- [x] `src/CLAUDE.md` paragraphs, MCP Inspector run

**Notes:**

<!-- 2026-10-20 -->
**2026-10-20, B-043 measurements.** Stack: commit `871b55c`, Python 3.13.16 (the venv; CI runs
3.12), networkx 3.7, tree-sitter 0.26.0 (python 0.25.0, javascript 0.25.0, typescript 0.23.2,
c-sharp 0.23.5, cpp 0.23.4), CPU, no embedder. Graph built SQLite-only from this repo's 172
scannable files, then `graph_analytics.analyze()` over each edge set:

| edge set | nodes | edges | communities (≥3) | modularity | non-repo members | top 5 sizes | top god objects |
|---|---|---|---|---|---|---|---|
| base (raw fallback) | 2,620 | 8,614 | 73 (47) | 0.5755 | 615 | 322, 313, 241, 213, 211 | HybridRetriever, IsolatedChunkSummarizer, DocumentStore, ScanPolicy, SearchScreen |
| drop unresolved CALLS only | 2,068 | 4,042 | 93 (66) | 0.8605 | 118 | 210, 159, 143, 132, 107 | CodeDB, HybridRetriever, IsolatedChunkSummarizer, DocumentStore, PythonAdapter |
| drop every unresolved target (OWNS kept) | 1,929 | 3,415 | 95 (67) | 0.8609 | 0 | 135, 129, 119, 116, 106 | CodeDB, HybridRetriever, IsolatedChunkSummarizer, DocumentStore, PythonAdapter |
| **ADR-053 (repo nodes + heritage)** | 1,930 | 3,428 | 84 (64) | 0.8613 | 0 | 138, 138, 124, 119, 116 | CodeDB, HybridRetriever, DocumentStore, PythonAdapter, SearchScreen |

Kept edges under ADR-053: CALLS 2,664, OWNS 480, IMPORTS 297, EXTENDS 9, IMPLEMENTS 4. Top god
object before: `HybridRetriever` (score 0.429, span 3); after: `CodeDB` (score 0.770, fan-in 51,
fan-out 51, span 10). Base examples of non-repo members: `ArgumentParser`, `AssertionError`, `App`,
`<string>`, `<vector>`, `.store`, `..shared.config`.

**2026-10-20, B-042 on this repo's code.** No index can be built here (no model), so the trace ran
over a doc store made from this repo's symbols (each symbol's text as a tier-1 chunk, each file's
whole text as a tier-2 window), with the 80 first chunks naming the target standing in for the
search. Same stack as above. `trace_data_flow("chunk_summaries")`:
- before (`871b55c`): no definition, no producer; `cache_summaries` a consumer (the backlog's
  report, reproduced);
- after: definition `CREATE TABLE IF NOT EXISTS chunk_summaries` in `src/db.py`; producers
  `CodeDB.cache_summaries`, `CodeDB._migrate_summary_keys`, and two test/tool seeders; consumers
  `get_cached_summaries`, `summary_cache_key`, `run_summarization_pass` and others.

**2026-10-20, Firestore/Next.js regression guard.** The golden in
`test_firestore_and_nextjs_output_is_unchanged` was captured by running `871b55c`'s
`MCPServer.trace_data_flow` on the test's fixture (and on a ranked-to-one-chunk variant). The
branch's output is byte-identical on both.

**2026-10-20, deviations and surprises.**
- **Outside the ownership list:** `src/adapters/base.py` gains `SQL_DEFINITION_RULES` and
  `SQL_PRODUCER_RULES` (15 lines, additive), so the Python and C# adapters share one SQL rule set
  instead of two copies. `tests/test_graph_analytics.py`'s `get_graph_edges` test now inserts the
  symbols its edges point at, since an edge to a non-node is dropped by design. In `src/CLAUDE.md`
  the `what_writes` paragraph's clause about `trace_data_flow` ("hardcoded to Firebase/TypeScript
  idioms") was no longer true and is shortened.
- The inline layer code of `trace_data_flow` moved into `_trace_layer()`, unchanged, so the sweeps
  label their entries the same way.
- **The old no-index check never fired** (Decision §4): the base server, run from an empty
  directory, returned an empty community map as a success.
- Heritage binding was not in the prompt's three options; it was added after the measurement showed
  13 in-repo heritage edges the plain filter would lose.

**2026-10-20, MCP Inspector 2.8.0**, server run from an empty `/tmp/insp`, CPU:
- `tools/list --strict`: exit 0, 14 tools. `trace_data_flow`'s description is 721 characters,
  `map_module_communities`' 1,267. No tool declares `readOnlyHint` (B-045, `_tool()`, not this
  ADR's).
- `trace_data_flow target_symbol=chunk_summaries` and `map_module_communities target_path=src/`:
  exit 5, `isError: true`, "no index has been built in .code-index. Run `reindex` first".
- `trace_data_flow` with no argument and `map_module_communities min_community_size=notanint`:
  exit 5, `isError: true`, argument validation errors.
- The success paths are covered in pytest (`test_graph_tools_*.py`), since there is no index here.

## Open questions for @edb

1. **Per-language layers.** Python and C# entries are labelled `UTILITY` or `CORE LOGIC / LIB` by
   the old path tests. Worth a `layer` rule on the adapters (tests, migrations, `db.py`-style data
   modules), or is the bucket (definition/producer/consumer) enough?
2. **`.gs` files are not scanned.** Apps Script rules run on `.js`/`.ts` (clasp's local form). A
   repo that keeps raw `.gs` files gets nothing until `scan_policy` and the registry learn `.gs`;
   that is the scan session's file, so it is not done here.
3. **User-defined rules.** Is a `[data_flow]` block in `indexer.toml` that adds to the adapter
   rules wanted? Nothing here needs it.
4. **Heritage binding in the resolver.** If another reader needs bound EXTENDS/IMPLEMENTS edges,
   move the binding from `get_graph_edges()` into `call_resolver` and store it as
   `resolved_target`.
