# ADR-047: One Index State per Tool Call, Run off the Event Loop

**Status:** accepted
**Date:** 2026-09-29
**Branch:** `feature/adr-047-index-state`
**Reviewer:** @edb
**Backlog:** [B-060](../backlog.md#b-060), from GitHub #52, #53 and #63
**Depends on:** none
**Depended on by:** none yet. It is the first step of #62 (splitting `MCPServer.py`).

## Context

The loaded index lived in loose module globals in `MCPServer.py`, with two separate copies:
- the server's own `doc_store`, `t1_index`, `t2_index`, `t3_index` and `index_manager`;
- the `_hybrid_retriever` and `_iterative_retriever` singletons.

Three bugs come from that shape.

**#52: a swap mid-call mixed generations.**
- The comment at the top of the file said in-flight calls "complete against that generation". They didn't.
- `_reload_lock` was taken only by the writer (`_reload_indexes`). A tool read the globals several times in one call, so a swap between two reads mixed old and new objects: doc ids from one generation looked up in the other's store.
- Swaps used to be rare, but git mode (ADR-042) made them routine on the live servers: the ref poller swaps on every ref move, and a server reloads whenever another process saves.

**#53: one slow call stalled the whole server.**
- All 14 tools were plain `def`, and FastMCP 1.28 calls a sync tool directly on the event loop (`func_metadata.py:93`).
- So one slow call stalled every other request on that server, including pings and the tool listing. Slow calls include a `reindex` (a full rebuild takes 30–40 min), `investigate_architecture`, or a first call that loads the index.

**#63: the index was in memory twice.**
- The server loaded its own `DocumentStore` and three FAISS indexes. `HybridRetriever` loaded another copy of each.
- #63 says the server's copy fed only `index_status`'s counts. It also fed five tools' text scans, so the fix is to share one copy, not drop the server's.

## Decision

1. **One immutable `IndexState`** (`MCPServer.py`):
   - It holds the `HybridRetriever`, the FAISS stamp it was loaded under, and a generation number.
   - Its `doc_store`, `db` and `tiers` are the retriever's own. The iterative retriever is built on first use, from the same retriever.
   - `_load_state()` builds a state. `_ensure_indexes()` returns the current state, loading it once (under `_load_lock`) and reloading when another process has saved (ADR-038). `_reload_indexes()` builds a new state outside the lock and swaps the single `_state` reference under it.
2. **Every tool call binds one state at entry.**
   - `_bind_index` sets a `ContextVar` with the current state. `_index()` returns it, and every helper that used a global now asks `_index()`: `_get_hybrid_retriever`, `_db`, `_get_iterative_retriever`, and the six scans that walked `doc_store`.
   - A swap during the call changes nothing the call reads. The old state is freed when its last call returns.
   - A tool called from inside another tool reuses the outer call's state.
3. **Tools run in worker threads.**
   - `_tool()` replaces `@mcp.tool()`. It registers an `async` wrapper (with the same name, docstring and signature, so the listing is unchanged) that runs the bound function via `anyio.to_thread.run_sync`.
   - Read tools share a `CapacityLimiter(1)`, so they still run one at a time, as before: they share one SQLite connection and a few lazily built caches.
   - `reindex` (`reads_index=False`) takes neither the limiter nor a snapshot. It would only load the index it is about to rebuild, and a rebuild must not hold up searches. Searches keep reading the state they bound until the rebuild's swap.
4. **The module keeps the plain functions**, bound to a snapshot too. The TUI and the tests call them directly, as before. The TUI's chunk browser reads `srv._ensure_indexes().doc_store`.

## Consequences

**Better:**
- A call never mixes two generations of the index, however often the watchdog or ref poller swaps.
- A slow tool no longer freezes the server. A `reindex` runs while searches keep answering from the previous state.
- **The index's resident memory drops from 101.6 MB to 63.1 MB (−38%)**, measured on a copy of GanttWebApp's index (3,501 chunks) by the RSS growth from loading it, with the embedder not loaded. The saving grows with the index.

**Worse:**
- For the length of a swap, the old state stays in memory beside the new one, while calls that bound it finish. Before, the old objects were dropped at once, and in-flight calls were quietly reading the new ones.
- A search can now run during a tool-triggered `reindex`. Its in-memory vectors and chunk text are the old generation's, but its SQLite graph reads see whatever the rebuild has committed so far. That was already true during watchdog and ref-poller rebuilds, which have always run in their own thread; the `reindex` tool used to block everything instead.
- The first tool call now builds the full `HybridRetriever`, even for a tool that only scans text. Before, the server loaded its own copy first and the retriever on the first search, so the cost is the same, just paid earlier.

**Neutral:**
- Read tools stay serialized, as they always were. Letting them run in parallel would need the shared SQLite connection and the retriever's lazy caches made safe first.
- The tool listing is byte-identical to master's (MCP Inspector `tools/list --strict`, compared as JSON).

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Take `_reload_lock` for the whole of each tool call | Fixes #52, but a rebuild's swap would wait behind every running search, and it does nothing for #53. |
| Pass the state explicitly through every helper | The same guarantee with far more churn: about 30 helper signatures. The `ContextVar` keeps them as they are. Worth revisiting when #62 splits the file. |
| Make every tool `async` and await its helpers | The helpers are CPU- and SQLite-bound; there's nothing to await. It would still block the loop. |
| Let read tools run in parallel | The shared `sqlite3` connection and lazy caches aren't safe for it. Serialized reads were never the bottleneck: the event loop was. |

## Implementation Log

- [x] `IndexState`, `_load_state`, `_ensure_indexes` returning the state, `_reload_indexes` swapping one reference
- [x] `_bind_index` / `_index()` via a `ContextVar`; the retriever accessors and all six `doc_store` scans read the bound state; `index_status` counts from `state.tiers`
- [x] `_tool()`: `async` wrapper, worker thread, a read `CapacityLimiter(1)`, `reindex` outside it and unbound
- [x] Removed the server's own `DocumentStore`/FAISS copy and the `MultiIndexManager` import; `_index_generation` is now `IndexState.generation`
- [x] TUI `get_file_chunks` reads the state's store
- [x] Tests: `tests/test_index_state.py` (6) covers:
  - a swap mid-call leaves the call's reads unchanged;
  - a nested tool call sees the same state;
  - there is one copy of the index;
  - all 14 registered tools are async with unchanged arguments;
  - a 0.6 s tool leaves the event loop ticking;
  - `reindex` runs while a read tool holds the limiter.

  Five existing tests moved from stubbing the old globals to stubbing `_ensure_indexes()`'s state. Full suite 647 passed, 1 skipped; flake8 clean on `src/`.
- [x] MCP Inspector 2.8.0:
  - `tools/list --strict` exit 0, identical to master's listing;
  - against a scratch repo's index on CPU: `index_status`, `semantic_code_search`, `find_dead_code`, `verify_candidate_edges`, `find_test_coverage` and `reindex` (incremental) succeed;
  - a bad `since` and a missing `query` return `isError: true`.
- [x] Memory on a copy of GanttWebApp's index: 101.6 → 63.1 MB resident for the index (two runs each).
