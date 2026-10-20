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

5. **B-034, tier 1: an unchanged tier-1 chunk keeps its vector.**
   - **Where the vector comes from: the FAISS id map, not a cache.** Right before
     `purge_stale_vectors`, `tier1_vectors_to_reuse` reads each modified file's tier-1 rows from
     `chunks` and, for each one with a vector, `reconstruct`s it from the `IndexIDMap`'s flat
     index at its id-map position. It keeps `{chunk id: (chunk_text_hash, vector)}` in memory for
     the run. A cache keyed by text hash would be a second store to keep in step with FAISS and
     to prune; the vector is already where the purge is about to drop it.
   - **When it is reused.** `_prepare_file` gives a tier-1 chunk the old vector when its id
     (file, scope) and its `chunk_text_hash` both match. The hash leaves out `Lines:` (and a
     split part's position), so a symbol that only moved keeps its vector and gets its new line
     range in its row. Its vector was embedded with the old line numbers; an unchanged file's
     chunks already carry vectors embedded with whatever their header said then.
   - **What is not reused.** Tiers 2 and 3: a slice's text changes with any edit above it, so
     a reuse there almost never fires (B-048 fixes 2 and 3 are the way in). Summary vectors:
     their text is the cached summary, which is the same for an unchanged key, but a reuse
     would need the summary index's vectors read the same way and the condition proven for every
     cache history. Left as a follow-up; with summaries on, an unchanged chunk now costs one
     embed instead of two.
   - **Not on a pre-ADR-030 index** (`embed_layout` unset): its code vectors have summaries folded
     in, and ADR-030 §5 asks for a full re-index there anyway.
   - **The three invariants:**
     - *vectors == rows (ADR-037).* A reused vector goes through the same `_write_plan` as an
       embedded one: `add_with_ids` under the chunk's id, then the file's rows. The commit point
       and what a kill leaves behind are unchanged, and `reconcile_vectors` heals the same way.
       A healed file's missing vectors are not in the id map, so they are embedded.
     - *ADR-040's build is byte-identical with the overlap on or off.* Which vectors are reused
       is decided in `_prepare_file` on the main thread from a snapshot taken before pass 2;
       `scatter` puts reused and embedded vectors back in chunk order. The embed thread never
       sees a reused text. A test builds and edits the same repo both ways and compares bytes.
     - *The writer lock (ADR-038).* The snapshot is taken inside `_run_incremental`, under the
       lock, and lives only for that run.

## Consequences

**Better:**
- A checkout whose line endings differ from the one indexed reprocesses only the files whose
  code differs.
- Adding or dropping a window keeps the cached summary of every slice whose code did not move.
  Replaying this repo's last 40 commits that touched `src/*.py`, 665 of 1,578 tier-2/3 slices
  hit the cache with the new key against 550 with ADR-045's: 115 fewer long prompts (see
  Measurements for the stack and its caveat).
- An edit re-embeds only the tier-1 chunks it changed. In the same replay, 4,252 of 4,774
  tier-1 chunks of the edited files (89%) came back unchanged and keep their vectors. Switching
  `[indexer].source` (ADR-042), which makes every file look modified, keeps every tier-1 vector.
- Upgrading costs no re-embed and no re-summarize.

**Worse:**
- A summary cached for one slice is shared with any slice of the same file and parent scope that
  has the same code at another position. The summary no longer names a position, so this is the
  intent.
- The summarizer now sees `(Part)` where it saw `(Part 2/5)`. Unmeasured, as ADR-029 noted; no
  cached summary of this repo was checked for quoting a part number. See Open questions.
- `chunk_summaries` keeps one more old row per slice, as ADR-045's re-key did.
- A modified file's tier-1 vectors are held in memory between the purge and the end of pass 2:
  about 6 KB each at 1,536 dimensions, so 120 MB for 20,000 chunks when a source switch makes
  every file modified. A normal edit holds a few files' worth.
- A moved symbol's vector was embedded with its old `Lines:`, so an incremental index and a
  fresh build of the same tree can differ in those vectors. They differed already: an unchanged
  file keeps its vectors across any number of edits elsewhere.

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
| B-034: cache vectors by text hash in SQLite | A second store of every tier-1 vector to keep in step with FAISS and prune; `reconstruct` from the id map needs neither |
| B-034: reuse tiers 2 and 3 too | Any edit above a slice changes its text, so it would rarely fire until slices stop repacking (B-048 fixes 2, 3) |
| B-034: skip it as not worth the complexity | About 90 lines, and it removes most of an edit's tier-1 embeds (89% in the replay) and all of a source switch's |

## Open questions for @edb

1. **Not bumping `CHUNKER_VERSION`** (§2) departs from B-054's stated fix. Agree?
2. **The summarizer input change** (§3) is unmeasured. A quick check on a GPU: summarize a few
   tier-2 slices with and without the part number and compare. ADR-045 found that no summary of
   7,258 mentioned a line number; nobody has counted part numbers.
3. **Reusing summary vectors** too (§5) would halve the remaining tier-1 embeds with summaries on.
   Worth a follow-up?
4. **The replay's numbers** used a stand-in tokenizer (Measurements). Re-running
   the replay with bge-code-v1's tokenizer would give the real slice counts; the tier-1 number
   barely depends on it (only oversized symbols split by tokens).

## Implementation Log

- [x] B-054: `source.md5_file` normalizes line endings, block-safe; `md5_file_raw` keeps the old
  formula for the re-key. `incremental_indexer.rekey_unchanged_files` and the
  `index_meta.file_key` marker, written by `_write_index_meta` on every completed run.
- [x] B-048 fix 1: `db.summary_input`, `summary_cache_key` on top of it, `_migrate_summary_keys`
  copying from either older key; `incremental_indexer.summary_text` sends the normalized text to
  the summarizer in both passes.
- [x] B-034 tier 1: `tier1_vectors_to_reuse`, `_FilePlan.tier_reused`, `scatter` reassembling
  reused and embedded vectors; `scatter(None)` when a file has nothing pending.
- [x] Tests, 25 new:
  - `tests/test_change_keys_eol.py` (11): LF, CRLF and lone-CR files hash alike, including a
    `\r\n` split across 64 KiB blocks; an LF file hashes as before; chunk text never keeps
    `\r`, in worktree and git mode; an LF-built index does not reprocess its CRLF twin; an older
    index re-keys once without re-embedding and never computes the old hash again; a real edit
    on an older index is still re-indexed.
  - `tests/test_change_keys_parts.py` (9): the key ignores the position, not the code or scope;
    only the header is touched; tier-1 keys are ADR-045's; adding a window keeps the unchanged
    slices' keys; a cache from either older key carries over once; a carried-over row hits after
    N changes; the summarizer gets `(Part)` and the embedder the original text.
  - `tests/test_change_keys_reuse.py` (5): a one-function edit embeds that function's tier-1
    chunk only, and vectors == rows after; a moved function keeps its vector bytes and gets its
    new lines; overlap on and off write identical bytes with reuse; an all-reused file still
    writes its vectors, through `run_incremental` and through `_prepare_file` + `scatter(None)`.
  - `tests/test_summary_cache_key.py`: one assertion changed. It pinned a `(Part 1/1)` slice's
    key to its plain MD5, which this ADR changes on purpose.
- [x] Gates: flake8 clean; `pytest --co` 729 collected; `conformance_eval --check-baseline`,
  `resolution_eval --check-baseline`, `l5x_fixture_teeth --check` all pass. MCP Inspector not
  run: no tool changed.
- [x] `CHUNKER_VERSION` left at 3 (§2).

**Notes:**
<!-- 2026-10-20 -->
- **Test environment (2026-10-20):** Linux, Python 3.12, CPU only, torch from PyPI (the CPU wheel
  index was blocked), and no network to huggingface.co. Every test that chunks needs
  bge-code-v1's tokenizer, so 48 existing tests fail here, identically on `871b55c` and on this
  branch (654 → 679 passed, 48 failed). The new tests patch `count_tokens` with a character
  count, so they run offline. To check the 48, both trees were also run with a stand-in
  word/punctuation tokenizer patched into `core.jina_tokenizer` by a pytest plugin outside the
  repo: `871b55c` 702 passed, this branch 727 passed, 2 skipped each, no failures.
- **Chunk text and `\r`:** verified, never kept (Context). So B-054 was a file-key bug only.
- **Not bumping `CHUNKER_VERSION`** departs from B-054's text and the task prompt (§2).
- **ADR-029's carry-over** is done as ADR-045's migration, not as its lookup fallback (§4).
- **`src/summarizer.py`'s module docstring** still describes the key as "MD5 of the chunk text
  without its `Lines:` header". It is in another session's file, so it is left for a follow-up.

### Measurements

**Replay of this repo's history, 2026-10-20.** Stack: the sandbox above, the chunker at this
branch, **a stand-in tokenizer** (one token per word, run of whitespace or punctuation mark)
because bge-code-v1's could not be downloaded. Token counts decide tier-2/3 window boundaries and
which tier-1 symbols are split, so the slice numbers are indicative, not production. For the last
40 non-merge commits before `871b55c` touching `src/*.py` (90 file edits), each edited file was
chunked before and after:

| | Chunks after the edit | Unchanged by the key | Share |
|---|---|---|---|
| Tier 1, same (scope, `chunk_text_hash`): vector kept (§5) | 4,774 | 4,252 | 89% |
| Tiers 2+3, cache hit with ADR-045's key | 1,578 | 550 | 35% |
| Tiers 2+3, cache hit with this ADR's key (§3) | 1,578 | 665 | 42% |

The script is not committed; it is about 30 lines over `chunk_all_tiers` and `git show`.
