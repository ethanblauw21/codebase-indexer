# ADR-033: The Index Records Which Chunker Built It, and Warns When That Is Stale

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-033-chunker-version` (stacked on `feature/adr-031-one-vector-per-chunk-row`)
**Reviewer:** @edb
**Backlog:** [B-029](../backlog.md#b-029) — parser and chunker changes never reach existing indexes
**Depends on:** ADR-031 — its dedupe is the change that version 2 records
**Depended on by:** B-026 Stage 1 and B-027 (each bumps the version)

## Context

`compute_diff` marks a file modified only when its MD5 changes. `schema_version` in `index_meta`
covers the table layout, not what the chunker produces. So after any change to parsing or
chunking, unchanged files keep their old chunks, vectors and summaries, edited files get new ones,
and nothing says the index is now a mix of two generations. ADR-031 is the first such change:
every index built before it keeps its ghost vectors until rebuilt, and today nothing tells the
user.

## Decision

1. **`CHUNKER_VERSION` in `incremental_indexer.py`**, currently 2, with a history comment. It is
   bumped in the same commit as any change to what chunks a file produces (scope, text, count,
   ids). No marker means "built before this ADR".
2. **Only a fresh build records it.** `run_incremental` writes `index_meta.chunker_version` at the
   end of a run that started with no files indexed. That covers `reindex(changed_files_only=False)`,
   which wipes the index first, and a first `code-indexer` run. A build killed midway leaves no
   marker. Per-file failures in a fresh build still record it, because those files are retried on
   the next run with the same chunker.
3. **A mismatch warns and never rebuilds.** `chunker_version_warning(db)` returns one line, shown
   at the start of `run_incremental`, as the first line of `semantic_code_search` output, and in
   `index_status`. It is not `isError`: the results are still the best the index has. An automatic
   rebuild at MCP startup could block the first search for minutes, or for GPU-hours with
   summaries on.

## Consequences

**Better:** a user whose index predates a chunker change is told, in the tool output an agent
reads, and told how to fix it.
**Worse:** every existing index, this repo's included, shows the warning until it is rebuilt once.
That is intended, since all of them carry ADR-031's ghost vectors. An incremental run on an
unmarked index never clears the warning; only a full rebuild does.
**Neutral:** one `index_meta` read per search.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Rebuild automatically on a mismatch | Can block the first search for a long time, and a run cut off midway leaves a mixed index |
| Fold the version into `content_hash` so every file looks modified | Turns a chunker change into a silent full re-index on the next ordinary run, with the same cost and interruption risk |
| Build aside and swap in | Right for large indexes, but a bigger change; the warning is needed first either way |

## Implementation Log

- [x] `CHUNKER_VERSION` (2) and `chunker_version_warning` in `incremental_indexer.py`
- [x] Marker written only at the end of a fresh build; warning printed on other runs
- [x] Warning in `semantic_code_search` output and `index_status`
- [x] `tests/test_chunker_version.py`: a fresh build records it; a build killed midway leaves no marker, and the following incremental run does not add one; an older marker warns and is not overwritten
- [x] MCP Inspector: `tools/list --strict` exits 0; `index_status` and `semantic_code_search` (GPU) both show the warning on this repo's unmarked index
- [ ] When merged with ADR-032 (`_pack_results`), keep the warning ahead of the header
