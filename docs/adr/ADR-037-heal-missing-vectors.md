# ADR-037: Every Run Heals Chunks That Have No Vector

**Status:** accepted
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

- [x] `reconcile_vectors()` and its call in `run_incremental`
- [x] `save_all` writes the summary index first
- [x] `index_status` message
- [x] Tests (`tests/test_heal_missing_vectors.py`, 5). Each drives `run_incremental` with a stubbed
  embedder and kills a run by raising `KeyboardInterrupt` from `ingest_file`:
  - A build killed before its save. Without the fix, tier 1 ends with 1 vector for 3 rows.
  - A modify killed before its save. Without the fix, 4 vectors and 3 distinct ids for 3 rows: the
    file's old vector comes back as a duplicate, a second bug B-035 had not named.
  - A stray vector on an unchanged repo is removed and saved.
  - An intact index with summaries off reconciles to nothing.
  - `save_all` writes the summary index first.
  - The four bug tests fail without the fix.
- [x] Suite: 389 passed, 1 skipped. The 6 `test_adapter_snapshots` failures are the known worktree
  path issue (they pass in the main checkout and CI).
- [x] End to end on the GPU
- [x] MCP Inspector

**Notes:**
<!-- 2026-09-25: branch cut from master (0f39e44). -->

**2026-09-25, cost.** `reconcile_vectors` on a copy of a real 1,601-chunk index (bullmq, summaries
on) takes 3 ms. It found nothing on the intact copy. After every tier-1 vector of two files was
removed, it flagged exactly those two files.

**2026-09-25, end to end** (`gpu-crash-repro/kill_heal_e2e.py`, results in
`telemetry/kill_heal/`). A real `run_incremental` (GPU, summaries on, as shipped) on a scratch
p-queue is killed hard (`TerminateProcess`) after pass 2 has ingested 5 of 14 files. The same run
is then started again and left to finish.

| After the kill and one ordinary run | tier 1 | tier 2 | tier 3 |
|---|---|---|---|
| `master` (vectors / chunk rows) | 24 / 94 | 30 / 40 | 15 / 21 |
| this branch | 94 / 94 | 40 / 40 | 21 / 21 |

- **`master`:** the resume indexes the 9 files it had not reached and skips the 5 it had. Their 70
  tier-1 chunks stay unsearchable.
- **This branch:** the resume prints `[reconcile] 5 file(s) had chunks with no vector and will be
  re-indexed` and re-indexes them (Δ 9 new, 5 modified). It took 175 s against master's 164 s.
- The harness first reported 0 vectors for both. It read the id map from a temporary index that
  Python freed at once. It was fixed and the saved indexes recounted.

**2026-09-25, MCP Inspector** (`gpu-crash-repro/telemetry/inspector_037/`). This branch's server
ran on CPU against the scratch p-queue index.
- `tools/list --strict` exits 0 and lists 13 tools. `index_status` answers, with vectors equal to
  chunk rows in every tier.
- **Found, not fixed here; both predate this branch:**
  - No tool in `MCPServer.py` declares `readOnlyHint`.
  - `index_status(since="notaduration")` returns a normal report with 0 changed files instead of
    an error. `_parse_since` passes an unparsed value through as the cutoff, and SQLite compares
    it as text.
