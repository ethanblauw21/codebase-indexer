# ADR-056: `reindex()` Reports Freshness Against the Index's Source

**Status:** accepted
**Date:** 2026-11-02
**Branch:** `feature/adr-056-reindex-freshness`
**Reviewer:** @edb
**Backlog:** [B-070](../backlog.md#b-070)
**Depends on:** none
**Depended on by:** none yet

## Context

On 2026-11-02, InventoryApp-V2 and GanttWebApp ran their first passes on the post-ADR-055 code. Each
`reindex()` result opened with a false alarm:

```
⚠️  INDEX STALENESS DETECTED
   Last indexed at commit: e8fd91e6
   Current HEAD:           39737053
   Files changed since the index was built: [~330 paths]
   Consider reindex(changed_files_only=False) for a clean rebuild.
```

`_reindex()` builds this block **before** the run. It compares `last_indexed_commit` with
`git rev-parse HEAD` and lists `git diff --name-only <last> HEAD`. Both live repos index in git mode
(ADR-042): the index follows a ref (`origin/main`, `refs/code-index/target`), not the folder, and
both folders sit on feature branches. So the block compares the wrong two commits. V2's listed about
600 files. Gantt's listed about 330, and two lines further down the same result said "Nothing
changed — index is up to date", which was true.

Three things are wrong with the block even once it compares the right commits:

1. **It describes the state before the run.** The incremental run that follows catches up to the
   target, so a list of what differed before the run is stale by the time the caller reads it.
2. **Its advice is wrong in both modes.** Since ADR-049, the incremental pass re-checks every file's
   content, so `changed_files_only=False` (a wipe and full re-embed) fixes nothing the block
   describes. An agent that trusts it pays for a full rebuild every time it reindexes from a branch.
3. **`index_status` already answers the question correctly.** It compares against `git_ref() or
   "HEAD"` (ADR-042 §5). The same check lives in two places, and one copy is wrong.

The server instructions (`_server_instructions()`, ADR-038) have the same root cause. They quote
`commit …, last verified …` as it was when the client connected, and Claude Code doesn't re-read
instructions during a session. Gantt's session read 86d34d5 there while `index_status` said
e8fd91e. In git mode, the "files your branch changed" hint also used that frozen commit:
`git diff --name-only 86d34d5...HEAD`.

## Decision

1. **Drop `_reindex()`'s pre-run staleness block.** It's replaced by one line written **after** the
   run, saying whether the index now matches its target. That's the only freshness fact the caller
   can act on:
   - `index: e8fd91e6 (== refs/code-index/target (→ origin/integration/staging-2026-10-01); current)`
   - `index: e8fd91e6  refs/code-index/target: a871f049  ⚠️ STALE (the watching server follows it)`
   - Folder mode reads the same, with `HEAD` as the target.

   No line suggests `changed_files_only=False`.
2. **One definition of "the target".** `_freshness_target(cwd)` returns `(ref, target)`, where
   `target` is `ref or "HEAD"`. `index_status` and `_reindex()` both use it, so the two can't
   disagree again.
3. **The instructions quote nothing that goes stale.** They name the source (the folder, or the
   ref with its symbolic target) and send the caller to `index_status` for the indexed commit and
   freshness. In git mode, the branch hint becomes `git diff --name-only <ref>...HEAD`. That stays
   right as the ref moves, and it's what the caller means: the files their branch changed since it
   left the base. The instructions no longer read the database, so the "no finished build yet"
   clause goes too. It was frozen at connect time like the commit, and `index_status` reports a
   missing build.

## Consequences

**Better:**
- A git-mode `reindex()` from a feature-branch folder no longer prints hundreds of "changed" files
  or advises a full rebuild.
- The reindex result and `index_status` share one target, so they can't contradict each other.
- An agent's instructions stay true for the whole session.

**Worse:**
- A folder-mode `reindex()` no longer lists the files that changed since the last indexed commit
  before the run. The run's own output already names what it processed, so nothing is lost.
- The instructions no longer show the indexed commit at a glance. An agent that wants it calls
  `index_status`, which is one cheap call that is always current.

**Neutral:** `index_meta`'s `last_indexed_commit` (ADR-025, read by segmem) doesn't change.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep the pre-run block and only switch it to `ref or "HEAD"` (B-070's first sketch) | Still reports a state the run is about to change, and still lists every file a moved ref touched. The fact worth reporting is the state after the run. |
| Delete the block and print nothing | The caller then has to call `index_status` to learn whether the run caught up. One line costs nothing. |
| Refresh the instructions when the index moves | MCP has no way to push new instructions to a connected client, and Claude Code reads them once. |
| `git diff <ref>...HEAD` in folder mode too | Folder mode has no ref. The indexed commit is the right base there, and it's in `index_status`. |

## Implementation Log

- [x] `_freshness_target()`; `index_status` switched to it with no change in output
- [x] `_reindex()`: pre-run block removed; post-run freshness line
- [x] `_server_instructions()`: no commit, no database read, `<ref>...HEAD` hint, still under 1,000 characters
- [x] Tests: a git-mode reindex from a folder on another branch prints no staleness and no rebuild advice; a moved ref shows STALE; folder mode reports `HEAD`; the instructions quote no commit
- [x] Full suite and flake8 (CI's flags); MCP Inspector `tools/list --strict` on a scratch folder

**Notes:**
<!-- 2026-11-02: Gate on CPU, offline: flake8 clean, 995 passed / 2 skipped, conformance, resolution and L5X checks hold. Inspector (scratch folder, never a live index): tools/list --strict exit 0, 17 tools, 16 readOnlyHint (all but reindex); index_status isError false. -->
<!-- 2026-11-02: Checked against both live setups with this branch's code, read-only: Gantt resolves to refs/code-index/target (→ origin/integration/staging-2026-10-01), V2 to origin/main. -->
<!-- 2026-11-02: Out of scope, noticed on the empty scratch index: index_status reports "chunker older than v2" and advises a full re-index when no build exists. Separate code path; a backlog item, not this ADR. -->
