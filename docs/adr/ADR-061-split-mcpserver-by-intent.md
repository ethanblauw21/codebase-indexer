# ADR-061: Split `MCPServer.py`'s Tools into Modules by Intent

**Status:** proposed (plan only, not built; see Notes)
**Date:** 2026-11-12
**Branch:** `feature/adr-061-split-mcpserver`
**Reviewer:** @edb
**Backlog:** [B-075](../backlog.md#b-075)
**Depends on:** #102 (ADR-057, B-076), ADR-059 (B-074) and ADR-060 (B-073). All three edit the
tools this moves.
**Depended on by:** none yet

## Context

`src/MCPServer.py` is 3,540 lines on master (3,693 with B-073 and B-074) and holds all 17 tools,
the `_tool` decorator, the ADR-047 `IndexState` snapshot, the shared retrieval helpers, the
watchdog and the server setup. Every tool change touches it.

Measured on the B-073 branch (tool bodies only):

| Group (`src/CLAUDE.md`) | Tools | Tool lines | Helpers used only by this group |
|---|---|---|---|
| Search | `semantic_code_search`, `find_similar_code` | 174 | `_fit_budget`, `_pack_json`, `_pack_results`, `_chunk_lines` |
| Lookup | `get_file_outline`, `get_symbol_source`, `changed_symbols` | 249 | `_indexed_path`, `_signature`, `_nesting`, `_locate`, `_match_symbols`, `_open_source`, `_source_label`, `_file_unchanged`, `_require_index`, `_git_run`, `_check_ref_shape`, `_verify_ref`, `_diff_hunks`, `_touched` (~290) |
| Impact | `analyze_blast_radius`, `detect_pattern_violations` | 281 | `_module_stem` |
| Tracing | `trace_data_flow`, `investigate_architecture`, `what_writes` | 454 | `_analyze_risks`, `_classify_relationship`, `_data_flow_rules`, `_rule_entry`, `_rule_match`, `_focused_snippet`, `_layer_tag`, `_trace_layer` (~190) |
| Discovery | `find_test_coverage`, `find_dead_code`, `find_unabstracted_collection_reads`, `map_module_communities`, `verify_candidate_edges` | 502 | `_get_test_patterns`, `_test_dir_relates` |
| Maintenance | `reindex`, `index_status` | 199 | `_reindex`, `_freshness_target` |

Each group with its own helpers is about 200 to 650 lines.

### The trap: tests patch `MCPServer`'s module globals

Tests replace `MCPServer` attributes about 50 times: `_ensure_indexes` (12), `_reload_indexes` (7),
`_search` (5), `_reindex` (5), `_db` (4), `_state` (3), and ten others once or twice. A tool moved to
`mcp_tools/lookup.py` that does `from MCPServer import _db` binds the real `_db` at import time.
`monkeypatch.setattr(M, "_db", fake)` then no longer reaches it. Some tests fail. Others pass while
reading whatever real state is there, which is worse.

### A second trap: `python src/MCPServer.py`

Run as a script, the module is `__main__`. A tool module that does `import MCPServer` then loads a
**second** copy, with its own `IndexState`, `_bound` and `_read_limiter`: the loose second state
ADR-047 removed.

## Decision (proposed)

1. **A package `src/mcp_tools/`**, one module per group above: `search.py`, `lookup.py`,
   `impact.py`, `tracing.py`, `discovery.py`, `maintenance.py`. Each tool moves with the helpers only
   its group uses. Shared helpers stay in `MCPServer.py`: `_tool`, `IndexState`/`_bind_index`/
   `_ensure_indexes`, `_db`, `_index`, `_search`, `get_clean_scope`, `_texts_by_file`,
   `_caller_evidence`, `_import_relations`, `_is_anchor_path`, `_detect_layer`,
   `_require_built_index`, `OutputFormat`/`_check_format`/`_json`, plus the watchdog, instructions
   and `main`.
2. **Tool modules reach shared state through the module object, at call time.**
   `import MCPServer as srv` at the top, then `srv._db()`, `srv._search(...)` and
   `@srv._tool()`. Attribute lookup at call time means every existing `monkeypatch.setattr(M, …)`
   keeps working unchanged. Never `from MCPServer import _db`. A test enforces that: it parses
   each `mcp_tools/*.py` and fails on any `from MCPServer import`.
3. **`MCPServer.py` imports the tool modules last**, after the shared helpers exist, in today's
   tool order, so FastMCP registers the 17 tools in the same order and `tools/list` is
   byte-identical. It re-exports every tool and every helper a test patches or calls
   (`from mcp_tools.lookup import get_file_outline, _signature, …`), so `M.get_file_outline(...)`,
   the TUI and the tests keep calling plain functions.
4. **One copy of the module, however it starts.** `if __name__ == "__main__":` becomes
   `import MCPServer; MCPServer.main()`, so a script run uses the same module object the tool
   modules import. `code-indexer-serve = "MCPServer:main"` is unchanged.
5. **Size limit: 650 lines per tool module**, checked by a test. Tracing is the one near it. If it
   grows past the limit, `what_writes` moves to its own `plc.py` (it is unlike its group anyway:
   ADR-013, no retrieval).

## Consequences

**Better:** a tool change opens one 200–650-line file. Review diffs stop colliding in one file.
**Worse:** a circular import by design (MCPServer → mcp_tools → MCPServer), safe only because the
tool modules read `srv.*` at call time. Rule 2's test is what keeps it safe.
**Neutral:** no behaviour, schema or output change.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Shared state in a new `server_state.py`, tests re-pointed at it | changes ~50 patch targets; a missed one passes silently against real state |
| `from MCPServer import …` in the tool modules | breaks every patch the tests rely on (above) |
| Split now, before #102 / B-074 / B-073 merge | all three PRs would have to be redone on moved code; the backlog item says do whichever lands first, then the other |
| One module per tool (17 files) | many 40-line files; the intent groups are what `src/CLAUDE.md` and the server instructions already use |

## Implementation Log (to do once the three PRs above are on master)

- [ ] Inspector `tools/list --strict --format json` on master, saved as the baseline.
- [ ] `src/mcp_tools/` with the six modules; move tools and group-only helpers; re-exports in `MCPServer.py`.
- [ ] `__main__` runs `MCPServer.main()` through the imported module.
- [ ] Tests: no `from MCPServer import` in `mcp_tools/`; each tool module ≤ 650 lines;
      `M.<tool> is mcp_tools.<group>.<tool>` for all 17; tools/list equals the baseline.
- [ ] Full suite unchanged; Inspector `tools/list` byte-identical; one `tools/call` per module.
- [ ] `src/CLAUDE.md` "MCP Tools" section names the modules.

**Notes:**
- 2026-11-12: planned, not built. The task allowed stopping at a plan if this was too big to finish
  well in one day. It also lands on top of three open PRs that edit the same functions, so building
  it now would mean redoing them.
