# MCP tool details (what_writes, trace_data_flow, verify_candidate_edges, lookups, communities)

Moved from src/CLAUDE.md, verbatim (source lines 152-203). Read when working on this area.

### MCP Tools (MCPServer.py)

Seventeen AI-facing tools grouped by intent:

- **Search**: `semantic_code_search`, `find_similar_code`
- **Lookup**: `get_file_outline`, `get_symbol_source`, `changed_symbols`
- **Impact**: `analyze_blast_radius`, `detect_pattern_violations`
- **Tracing**: `trace_data_flow`, `investigate_architecture`, `what_writes`
- **Discovery**: `find_test_coverage`, `find_dead_code`, `find_unabstracted_collection_reads`, `map_module_communities`, `verify_candidate_edges`
- **Maintenance**: `reindex`, `index_status`

**`what_writes` is the only tool here that does no retrieval at all** (ADR-013). Given a
PLC tag it returns every routine that writes it, with rung numbers, straight out of the
resolved `writes` edges — no embedder, no RRF, no reranker, and the answer is *complete*
rather than top-k. It is grouped under Tracing but it is not `trace_data_flow`'s sibling:
that tool detects producers by regex over chunk text. Do not "unify" them — routing an
exactly-resolved relation through the ranked surface can only lose writers (ADR-019's
hop-decay finding).

`trace_data_flow` buckets a data name's chunks into definitions, producers, transformers
and consumers. Its Firestore/Next.js rules are inline in the tool and unchanged; every other
language's rules come from its adapter's `data_flow_rules()` (ADR-053): `{name}`-templated
regexes for `definition` and `producer`, gated by an optional `file_marker` (Apps Script's
rules apply only to files that use `SpreadsheetApp`/`PropertiesService`). Rule hits are swept
across the whole doc store, not just the ranked top-80. Add a language's idioms to its adapter,
not to the tool. The shared SQL rules are in `adapters/base.py`.

`verify_candidate_edges` (ADR-023) reports edge-aware three-state verdicts over
candidate (name-based / unresolved) call edges. `index_status` (ADR-025) reports
index freshness — `last_verified_at`, `last_indexed_commit` vs HEAD, and files whose
content changed within a window — for agents and downstream context hubs.

The three **Lookup** tools (ADR-050) answer straight from the symbol table, like
`what_writes`: no retrieval and no model. `get_symbol_source` never serves the stored
copy of a symbol. It reads the file through `source.py` from what the index was built
from (the indexed commit in git mode) and checks it against the stored text: it serves
the symbol in place, serves it from its new lines when it moved, or refuses when it
changed. `changed_symbols` diffs with git from the ref's merge base; the ref is
validated before git sees it, and every git call has a timeout.

`map_module_communities` (ADR-006) is the whole-graph structural view: it reads the
SQLite edge graph, runs Louvain community detection + betweenness centrality over it
(`src/graph_analytics.py`, pure NetworkX), and renders a community map + god-object
report (`src/graph_report.py`). Inspired by Graphify (MIT); reimplemented natively
over our EXTRACTED edges. **Its nodes are the repo's own symbols and files** (ADR-053):
`get_graph_edges()` drops every edge whose target is neither, so an unresolved `append` or an
external `import os` is not a community member, and it binds a bare EXTENDS/IMPLEMENTS base
name to the in-repo type of that name. Both it and `trace_data_flow` raise, so the client sees
`isError`, when no index has been built. Output is explicitly exploratory — its quality bar is
deferred to ADR-008.

Tool docstrings are intentionally written for AI consumption — they describe *when* to call the tool, not just what it does.
