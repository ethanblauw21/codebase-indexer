# ADR-037: Every Run Heals Chunks That Have No Vector

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-037-heal-missing-vectors`
**Reviewer:** @edb
**Backlog:** [B-035](../backlog.md#b-035) — a reindex killed before its FAISS save leaves files that look indexed and have no vectors, forever
**Depends on:** none
**Depended on by:** the live watchdog daemon (ADR-036). Related: [ADR-031](./ADR-031-one-vector-per-chunk-row.md), which made "one vector per chunk row" the invariant this ADR restores; B-033, which covers truncated FAISS files from two writers.

## Context

`run_incremental` writes to two stores at different times:
- **SQLite:** each file's chunk rows and its MD5 are committed as the file is indexed (`upsert_file`).
- **FAISS:** the indexes are written once, by `save_all`, at the end of the run.

A process that dies in between leaves rows whose vectors were never saved. The next run's diff
compares MD5s, finds those files unchanged, and skips them. Their chunks are in the database and
unreachable by search, permanently, and nothing short of deleting `.code-index` repairs them.
- **Seen for real on 2026-09-25.** A study build of click was killed mid-run, then rebuilt in the
  same directory. It ended with 1,577 chunk rows and 17% fewer tier-1 vectors. 8 of 15 file
  questions found nothing in the top 50, which looked like a model regression until the vectors
  were counted.
- **The same gap leaves ghosts in the other direction.** Step 4 deletes a modified or deleted
  file's rows and commits. Its vectors are removed only in memory. A kill before `save_all` leaves
  those vectors on disk with no rows behind them.
- **Why now.** The watchdog daemon (ADR-036) runs reindexes in the background of an MCP server.
  Closing Claude Code, a crash or a reboot during a run is ordinary. A first index of a project
  with summaries on takes minutes, a wide window.
- **Detection exists, repair doesn't.** `index_status` prints a MISMATCH when a tier's vector
  count differs from its chunk rows, and tells the user to rebuild.

## Decision

1. **`reconcile_vectors()`**, in `incremental_indexer.py`, runs at the start of every run, after
   the diff and before the "nothing changed" check.
   - For each tier, the expected ids are `stable_id(tier, path, scope)` of every chunk row, as
     `get_stale_ids` already derives them. The actual ids are the FAISS `IndexIDMap`'s id map.
   - **Missing:** a file with any chunk whose id is not in its tier's index joins `diff.modified`,
     so the ordinary path purges and re-indexes it. Files already in the diff, or gone from disk,
     are left to the diff.
   - **Surplus:** ids in a tier index that no chunk row expects, and ids in the summary index that
     no chunk row of any tier expects, are removed with `purge_stale_vectors`.
   - It prints one line when it finds anything. A run where only surplus was removed still saves.
2. **Only tier vectors are checked for missing ids.** Every chunk row has exactly one tier vector
   (ADR-031). A summary vector exists only when a summary did and summarization was on, which a
   later run can't reconstruct: turning summaries off would flag every file on every run.
3. **`save_all` writes the summary index first.** A kill during the save then leaves either
   everything old, or a new summary index with some tier indexes old. Either way the files involved
   show missing tier vectors, and 1 re-indexes them, summary vectors included.
4. **`index_status`** keeps its check. The message now says the next reindex repairs it.

## Consequences

**Better:**
- A killed run costs a re-index of the files it touched on the next run, instead of permanent
  holes. Indexes damaged before this ADR are healed by their next run too.
- Ghost vectors left by a killed run are removed.

**Worse:**
- Every run now derives the stable id of every chunk row and reads each index's id map. That is an
  MD5 per chunk plus a set comparison; the cost on a real index is measured in the log below.
- A missing vector now triggers a re-index silently, apart from the one printed line. A bug that
  kept losing vectors would show up as the same files re-indexing on every run, not as an error.

**Neutral:** a truncated `.faiss` file (a kill during `write_index` itself) is still B-033's atomic
save, not this.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Stamp each file's MD5 only after `save_all` succeeds (B-035 option 2) | It stops new damage but heals none that exists, and it adds a second write to every file row. Reconciling covers both. |
| Save FAISS every N files during a run | It narrows the window without closing it, and each save rewrites every index. |
| Check the summary index for missing ids too | Whether a chunk should have a summary vector depends on the config of the run that indexed it (Decision 2). |
| Leave it to `index_status` and a manual rebuild | The daemon has nobody watching, and a rebuild with summaries is minutes of GPU for a problem that touched a few files. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `reconcile_vectors()` and its call in `run_incremental`
- [ ] `save_all` writes the summary index first
- [ ] `index_status` message
- [ ] Tests: a run killed before its save, then an ordinary run, gives vectors equal to chunk rows
  in every tier, with search finding the healed file; surplus ids are removed; an intact index
  reconciles to nothing; summaries off does not flag files
- [ ] Suite

**Notes:**
<!-- 2026-09-25: branch cut from master (0f39e44). -->
