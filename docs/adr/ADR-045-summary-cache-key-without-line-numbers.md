# ADR-045: The Summary Cache Is Keyed Without Line Numbers

**Status:** accepted
**Date:** 2026-09-29
**Branch:** `feature/adr-045-summary-cache-key`
**Reviewer:** @edb
**Backlog:** [B-058](../backlog.md#b-058): moving a symbol's line numbers re-summarizes it
**Depends on:** [ADR-040](./ADR-040-cross-file-embed-batching.md), whose two passes must agree
on the key; `chunk_text_hash` stays the one function both call.
**Depended on by:** none yet.

## Context

Repointing GanttWebApp's index to `integration/staging-2026-10-01` was a 57-file change
(+5,008 / −486 lines). It took about 14 minutes, against 30–40 for the whole project. The run
was healthy: the model host held 5.6 GB of real VRAM with no spill to system RAM, and wrote about
3 summaries a second. It was simply summarizing a lot, about 950 chunks, where a full index does
about 3,500.

`chunk_summaries` is keyed by MD5 of the chunk text. Every tier-1 text carries its source lines
in its header (`ast_chunker._symbol_rich_text`):

```
File: src/a.ts
Entity: src/a.ts::parseCSV (function)
Tags: [CAT_PARSE]
Lines: 6-21
Code:
…
```

Insert lines above a symbol and `Lines:` changes while the code doesn't. The key changes with
it, and the symbol is sent to the LLM again. In the 42 code files of that change:

| Tier | Chunks | Unchanged | Differ **only** in `Lines:` | Code really changed |
|---|---|---|---|---|
| 1 (per symbol) | 803 | 122 | **370** | 311 |
| 2 (1,500-token slices) | 206 | 15 | 0 | 191 |
| 3 (4,000-token slices) | 89 | 10 | 0 | 79 |

The header has to stay in the stored text: `MCPServer` reads `Lines: a-b` from it to report a
result's line range (`_CHUNK_LINES_RE`). And the summary doesn't depend on it: **none of the
7,258 cached summaries** (GanttWebApp's 4,144 plus this repo's 3,114) mentions a line number.

## Decision

1. **`db.summary_cache_key(text)`** is MD5 of the text with the header's `Lines: a-b` line
   removed. Only a `Lines:` line that ends the header, right before `Code:`, is removed, so a
   line in the code that happens to look like one still counts. Text without that header (tier
   2 and 3) keys exactly as before, so its cache rows stay valid.
   `incremental_indexer.chunk_text_hash` returns it, so both passes of ADR-040 use it unchanged.
2. **Existing caches are re-keyed, once, without the model.** The old key is MD5 of the text
   in `chunks`, so `CodeDB._migrate_summary_keys` copies each indexed chunk's summary to its
   new key. `index_meta.summary_key = "no-lines"` marks it done. Old rows are kept: an older
   indexer on the same database still finds them.
3. **The summarizer's input is unchanged.** It still sees the header, line numbers included.
   Only the key changes.

## Consequences

**Better:** an edit that inserts or removes lines no longer re-summarizes the unchanged symbols
below it. Replaying the GanttWebApp update against the cache as it stood before the run: **953
LLM calls with the old key, 596 with the new one, 357 fewer (37%).** In time it is much less,
because the calls saved are all short tier-1 prompts. Timing the same prompts through the model
host: **879 s of summarizing with the old key, 771 s with the new one, 108 s saved (12%).** Tier 2
and 3 are 276 of the remaining calls but 596 s of the 771. No summary is regenerated
by the upgrade: the migration takes 0.07 s on GanttWebApp's database, and all 3,501 chunks find
their summary under the new key.

**Worse:**
- Two symbols with identical code share one summary, even at different lines. They already
  would if the header matched, and a summary never names its lines, so nothing changes in
  practice.
- The cache keeps its old rows (1,683 more on GanttWebApp, a few hundred KB). Nothing prunes
  `chunk_summaries` today, so they're no worse than the rows every edited chunk leaves behind.

**Neutral:**
- Tier 2 and 3 misses are untouched. A fixed-size window really does change when lines are
  inserted before it, and those are the long prompts. That is a separate question (slice
  boundaries anchored to symbols, for example), not this ADR.
- The key still includes `File:` and `Entity:`, so a rename re-summarizes. The schema comment's
  "survives file moves and renames" is true for tier 2/3 texts only and is corrected here.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Remove `Lines:` from the chunk text | `MCPServer` reads line ranges from it, and it changes the embedded text, so every tier-1 vector would need re-embedding. |
| Also remove `Lines:` from the summarizer's input | It would make the summary a function of exactly the key. But it changes what the model sees for every new summary, which is unmeasured, and the evidence (0 of 7,258 summaries mention a line) says it isn't needed. |
| Key on the code alone (no header at all) | `Tags:` and `Type:` are in the prompt and can shape the summary. `File:`/`Entity:` would let renames hit the cache, but two same-named functions with the same body in different files would then share a summary that may name the wrong file. Worth measuring separately. |
| Re-key lazily, on each lookup miss | It needs the old text at lookup time, and the old text is only in `chunks` before the file is re-ingested. The one-time migration is cheaper and simpler. |

## Implementation Log

- [x] `db.summary_cache_key`, `SUMMARY_KEY_VERSION`; `incremental_indexer.chunk_text_hash` delegates to it
- [x] `CodeDB._migrate_summary_keys`, once per database via `index_meta.summary_key`
- [x] Tests (`tests/test_summary_cache_key.py`, 9): moved symbol keeps its key; changed code, tags or type change it; headerless text keys as before; both passes agree; the real tier-1 builder with two lines inserted above a function; migration re-keys, keeps old rows, runs once, marks a fresh database. Full suite 650 passed, 1 skipped; flake8 clean.
- [x] Replay of the 2026-09-29 GanttWebApp update: 953 → 596 LLM calls; timed through the model host, 879 s → 771 s of summarizing (−12%). Migration on a copy of its database: 0.07 s, 3,501 / 3,501 chunks keep their summary.
