# ADR-031: One FAISS Vector per Chunk Row

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-031-one-vector-per-chunk-row`
**Reviewer:** @edb
**Backlog:** [B-028](../backlog.md#b-028) — symbols that share an FQN leave ghost vectors
**Depends on:** none
**Depended on by:** ADR-030 (its summary index has the same surplus, and its numbers are re-measured
on top of this); B-026 (its baseline is taken after this lands)

## Context

A chunk's FAISS id is `stable_id(tier, file, scope)`. Two symbols in one file can have the same
scope: a getter/setter pair, an overload set, or a test helper redeclared in several `it` blocks.
`ingest_file` embedded every chunk and called `add_with_ids` with the duplicate ids, and
`IndexIDMap` accepts them. The database, though, keeps one row per `(file_id, scope, tier)`
(`INSERT OR REPLACE`, `db.py:745`), and it keeps the last one.

So each collision left an extra vector under an id whose text now belongs to the other symbol. A
query could match the getter's vector and get the setter's text back. Counted on the ADR-030 eval
builds (e1e9491):

| Repo | Tier-1 vectors | Tier-1 rows |
|---|---|---|
| p-queue | 76 | 75 |
| zustand | 323 | 231 |
| click | 1,334 | 1,298 |

Tiers 2 and 3 matched, since their `Full File_part_N` scopes are unique by construction.

## Decision

1. **Dedupe by scope before embedding** (`dedupe_chunks_by_scope`, `incremental_indexer.py`). Each
   tier's chunk list keeps the *last* chunk per scope, which is the one the database keeps. The
   same deduped list goes to the summarizer, the embedder, `add_with_ids`, the `DocumentStore`
   cache and `upsert_file`, so all four stores agree. The number dropped is logged per file.
2. **`index_status` reports vectors against chunk rows per tier** and flags a mismatch with
   "rebuild the index". Existing indexes keep their ghosts until rebuilt; this is how a user
   finds out.
3. **A test** (`tests/test_ghost_vectors.py`) ingests a getter/setter pair with a stubbed embedder
   and asserts `ntotal` equals the row count for every tier.

This does not merge the colliding symbols. Which symbol a shared scope should stand for, and
whether accessor pairs and overloads should become one chunk, is B-026 Stage 1.

## Consequences

**Better:** every vector resolves to the text it was embedded from. Retrieval numbers stop
counting ghost hits, and the dropped chunks are no longer summarized or embedded.
**Worse:** the first chunk of a collision is not searchable at all now, where before it was
searchable but returned the wrong text. For a getter/setter pair the setter wins. B-026 decides
whether to merge them instead.
**Neutral:** no schema or id change, so no forced rebuild; an index is clean once rebuilt.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep the first chunk per scope | Disagrees with the row `INSERT OR REPLACE` keeps, so the fix would need a matching database change |
| Make the scope unique (suffix `:get`/`:set`, `#2`) | Changes stable ids and FQNs, breaks the grader's gold matching, and is B-026's decision to make |
| Dedupe ids just before `add_with_ids` | Leaves the summarizer and `DocumentStore` working on the dropped chunk; deduping the chunk list fixes every consumer at once |

## Implementation Log

- [x] `dedupe_chunks_by_scope` in `ingest_file`, with a log line per file
- [x] `index_status` vectors against chunk rows, per tier
- [x] `tests/test_ghost_vectors.py` (the pre-fix ingest gives 3 tier-1 vectors for 2 rows)
- [ ] Carry the dedupe into ADR-030's summary index (its `add_with_ids` for `summary.faiss`)
- [ ] Rebuild the three eval indexes and re-measure ADR-030 `none` and `store`
- [x] MCP Inspector: `tools/list --strict` exits 0; `index_status` returns the per-tier check; a bad argument returns `isError: true`
- [x] **Found on the way:** `index_status` never answered over stdio on Windows, on `master` too (Inspector timed out at 60 s; the same function called directly takes 0.1 s). Its `git` subprocesses inherited the MCP stdin pipe. All seven `git` calls in `MCPServer.py` and `incremental_indexer.py` now pass `stdin=subprocess.DEVNULL`; `reindex` had the same calls.
