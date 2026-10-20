# ADR-051: Change Keys That Ignore Line Endings and Slice Positions

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-051-stable-change-keys`
**Reviewer:** @edb
**Backlog:** [B-054](../backlog.md#b-054) (line endings), [B-048](../backlog.md#b-048) fix 1 only
(the `(Part i/N)` key), [B-034](../backlog.md#b-034) tier 1 only (reuse unchanged tier-1 vectors)
**Depends on:** [ADR-045](./ADR-045-summary-cache-key-without-line-numbers.md), whose
`summary_cache_key` and one-time re-key this extends; [ADR-046](./ADR-046-atomic-migrations-and-reported-failures.md)
for how a migration commits; [ADR-033](./ADR-033-chunker-version.md), whose version this ADR
deliberately does not bump (§2); the unmerged ADR-029, whose §1 design §3 follows.
**Depended on by:** none yet.

## Context

An incremental run decides what to redo with three keys. Each one changes in a case where the
code did not:

| Key | What it is | Changes when the code didn't |
|---|---|---|
| file key | `files.content_hash`: MD5 of the file's raw bytes (`source.md5_file`) | the file's line endings flip (B-054) |
| summary key | `chunk_summaries.text_hash`: `summary_cache_key(chunk text)` (ADR-045) | a tier-2/3 slice's `(Part i/N)` changes because a window was added or dropped elsewhere in the file (B-048) |
| vector | none: a modified file's vectors are all removed and re-embedded | any edit anywhere in the file (B-034) |

**B-054.** With `core.autocrlf=true` the same file is LF in one checkout and CRLF in another.
InventoryApp-V2's fork reprocessed 239 files, 27 of them identical apart from line endings.

**Does chunk text keep `\r` today? No.** The backlog left this unverified. Both readers already
normalize: `WorkingTreeSource.read` opens the file in text mode, whose universal newlines turn
`\r\n` and a lone `\r` into `\n`, and `GitCommitSource.read` goes through `source._decode`, which
does the same replacement by hand (ADR-042). So the chunks, the summary keys and the embedded text
of an LF file and its CRLF twin are already identical, and only the file key differs.
`tests/test_change_keys_eol.py` pins this. The consequence is that a CRLF flip costs a re-embed of
the file, never a re-summarize, and that fixing the file key alone fixes B-054.

**B-048.** `fallback_token_chunker` writes `Scope: <parent> (Part {idx + 1}/{total})` into every
slice it makes: every tier-2/3 slice, and the parts of an oversized tier-1 symbol. ADR-045 kept
that line in the key. So an edit that adds or drops one window changes `N` in every slice of the
file, and every slice misses the cache even where its code did not move. These are the long
prompts (about 1,500 and 4,000 tokens). Repacking after an insert, the other half of B-048, is out
of scope here: fixes 2 and 3 change what is retrieved and need a re-measure on a GPU.

**B-034, tier 1.** A tier-1 chunk's id is `stable_id(tier, file, scope)`, and its summary is
already reused when its code didn't change (ADR-045). Its vector is not: `_run_incremental`
removes every vector of a modified file before re-indexing it.

## Decision

1. **The file key is the MD5 of the bytes with line endings normalized** (`source.md5_file`).
   `\r\n` and a lone `\r` both become `\n`, which is exactly what the two readers do, so the file
   key changes if and only if the text the indexer reads can change. It still reads in 64 KiB
   blocks; a `\r` that ends a block is held back until the next block shows whether `\n` follows.
   An LF-only file hashes exactly as before.

   **Git mode keeps the blob id as its file key** (ADR-042 §1). Normalizing it would mean reading
   every blob on every listing, and ADR-042 chose the blob id precisely so that listing and
   diffing read no file contents. `core.autocrlf` changes the working tree, not blobs, so the
   B-054 case does not arise there: a blob whose line endings differ was committed that way, and
   that commit is re-embedded once. Its chunks and summary keys are EOL-independent already
   (`_decode`), so it is never re-summarized.

2. **Existing worktree indexes re-key in place, once, without re-indexing anything.**
   `index_meta.file_key` records the formula (`"md5-lf"`). On the first run of an index without
   it, a modified-looking worktree file whose stored hash equals the **old** raw-byte MD5 of the
   file as it is now has not changed at all: its stored hash is rewritten to the new key and it
   leaves the diff. Every completed run, a no-op included, records the marker, and a run on a
   marked index never computes the old hash again.

   **`CHUNKER_VERSION` is not bumped**, and that departs from the backlog item and the task
   prompt. ADR-033 says to bump it "in the same commit as any change to what chunks a file
   produces (scope, text, count, ids)", and nothing here changes a chunk: the text never held
   `\r`, and §3 changes a key, not a text. A bump would make every existing index print "Results
   may mix old and new chunks. Run a full re-index" until it was rebuilt, which is untrue and
   would cost each user a full re-embed for nothing. The marker in this section is the "re-key
   cleanly" the backlog asked for, and it touches only the files that need it.

   **What the upgrade costs a user:** on the first run, one extra raw MD5 per file that looks
   modified, which is a handful of files on a quiet repo and the CRLF files of a fork. Nothing is
   re-embedded or re-summarized because of the new formula. Without the in-place re-key, every
   file with a `\r` in it would be re-embedded once (summaries cached).

3. **The summary key and the summarizer's input drop the slice position** (ADR-029 §1).
   - `db.summary_input(text)` turns the slice header's `(Part i/N)` into `(Part)`. Only the
     `Scope:` line that ends the header, right before `Code:`, is touched, as with ADR-045's
     `Lines:`. The summarizer is sent `summary_input(text)` in both passes, so a cached summary
     cannot quote a position that is no longer true.
   - `summary_cache_key(text)` is the MD5 of `summary_input(text)` with the `Lines:` header
     removed (ADR-045). Text without a slice header keys exactly as before, so every tier-1 cache
     row stays valid.
   - **The embedded and stored text is unchanged.** Positions are still shown in search results,
     and the vectors need no re-embedding.

4. **Existing caches carry over by ADR-045's one-time re-key, not ADR-029's lookup fallback.**
   `CodeDB._migrate_summary_keys` now copies each indexed chunk's summary from whichever older
   key it has (the raw MD5 before ADR-045, or ADR-045's key) to the new key, once per database,
   marked by `index_meta.summary_key = "no-lines-no-part"`. The copy and the marker commit in one
   transaction (ADR-046). Old rows stay, as in ADR-045.

   ADR-029 proposed trying the old key on a lookup miss instead. That fallback computes the old
   key from the *current* text, so it misses exactly when `N` has changed since the summary was
   written, which is the case this ADR is for. The migration re-keys from the text the summary
   was written for (`chunks`), so after it a slice whose code did not change hits whatever `N`
   becomes, and lookups stay one query.

5. **B-034, tier 1: an unchanged tier-1 chunk keeps its vector.** See the Implementation Log for
   whether it was built and how it holds the three invariants (vectors == rows, ADR-037; the
   ADR-040 two-pass build is byte-identical with the overlap on or off; the ADR-038 writer lock).

## Consequences

**Better:**
- A checkout whose line endings differ from the one indexed reprocesses only the files whose
  code differs.
- Adding or dropping a window keeps the cached summary of every slice whose code did not move.
- Upgrading costs no re-embed and no re-summarize.

**Worse:**
- A summary cached for one slice is shared with any slice of the same file and parent scope that
  has the same code at another position. The summary no longer names a position, so this is the
  intent.
- The summarizer now sees `(Part)` where it saw `(Part 2/5)`. Unmeasured, as ADR-029 noted; no
  cached summary of this repo was checked for quoting a part number. See Open questions.
- `chunk_summaries` keeps one more old row per slice, as ADR-045's re-key did.

**Neutral:**
- Repacking (B-048 fixes 2 and 3) is untouched: one inserted line still changes the text of every
  slice after it.
- A file whose only change is a lone-`\r` (classic Mac) line ending is treated as unchanged, which
  is what the readers already made of it.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Bump `CHUNKER_VERSION` for B-054 (the backlog's and prompt's fix) | No chunk changes, so ADR-033's warning would be false on every existing index and would ask for a full re-index (§2) |
| Fold a key version into `files.content_hash` so every file looks modified | ADR-033 rejected this: a silent full re-embed on the next ordinary run |
| Let CRLF files re-key by simply being re-indexed once | Correct but costs a re-embed of every CRLF file; the in-place re-key is a few lines and costs one extra MD5 per changed-looking file, once |
| Hash the decoded text (`read()` output) instead of normalized bytes | Needs the whole file in memory and a UTF-8 decode per file on every scan; bytes with EOL normalized give the same equivalence for the case at hand |
| Normalize the git-mode key too | Reads every blob on every listing, which ADR-042 §1 was designed to avoid, for a case `core.autocrlf` cannot produce |
| ADR-029's lookup-time fallback to the old key | Misses exactly when `N` changed (§4) |
| Strip `(Part i/N)` from the stored chunk text | Changes the embedded text, so every slice re-embeds, and search results lose the part number |
| Key on the summary input but leave the summarizer input as it is | A summary written for "Part 2/5" could be served for a slice that is now part 3/6 |

## Open questions for @edb

1. **Not bumping `CHUNKER_VERSION`** (§2) departs from B-054's stated fix. Agree?
2. **The summarizer input change** (§3) is unmeasured. A quick check on a GPU: summarize a few
   tier-2 slices with and without the part number and compare. ADR-045 found that no summary of
   7,258 mentioned a line number; nobody has counted part numbers.

## Implementation Log

- [ ] B-054: `md5_file` normalizes line endings; `file_key` marker and in-place re-key
- [ ] B-048 fix 1: `summary_input`, `summary_cache_key`, `_migrate_summary_keys`
- [ ] B-034 tier 1: design and build, or a write-up of why not
- [ ] Tests
- [ ] Gates

**Notes:**
