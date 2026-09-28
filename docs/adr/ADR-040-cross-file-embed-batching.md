# ADR-040: Pass 2 Batches Embedding Across Files

**Status:** accepted
**Date:** 2026-09-28
**Branch:** `feature/b050-cross-file-embed-batching`
**Reviewer:** @edb
**Backlog:** [B-050](../backlog.md#b-050) — pass 2 embeds in batches of 1-4 texts (per file per
tier), so a full build embeds at about half speed
**Depends on:** none
**Depended on by:** none yet. Related: [ADR-031](./ADR-031-one-vector-per-chunk-row.md), whose
"one vector per chunk row" invariant this must not disturb; [ADR-037](./ADR-037-heal-missing-vectors.md),
whose reconcile-on-next-run healing is what makes a kill mid-batch safe.

## Context

`ingest_file` embeds one file's chunks per tier — and, when summaries are on, that tier's summary
texts — as it goes: up to six `embed_batch` calls per file (three tiers, each with a code call and
a summary call). On InventoryApp-V2's first build the host logged **1,909 `embed_batch` calls for
2,815 chunks**, most with 1-4 texts, because a call's size is set by one file's chunk count in one
tier, never by the GPU. That build measured **115 ms/chunk**, against ~66 ms/chunk on this repo's
2026-09-26 build (bigger chunks explain part of the gap; the small batches are the likely rest,
unverified pending a GPU re-run).

Pass 2 already runs after the summarizer unloads (the two-pass split, `run_summarization_pass` +
`_CacheOnlySummarizer`), specifically so the embedder can have the GPU to itself. Nothing in that
design requires embedding one file at a time — pass 1 already gathers every file's chunks
up front before its own batched summarize calls (ADR-027). `core.embed_batch(texts, batch_size=32)`
already tiles its input into GPU-sized batches internally via `SentenceTransformer.encode`; the
problem is that the *input* to a given call is rarely bigger than a handful of texts, so that
internal tiling never has more than one small tile to work with.

The two invariants a fix must not break:
- **ADR-031:** exactly one FAISS vector per chunk row, per tier.
- **ADR-037:** a run killed before `save_all()` must be healed by the next run's
  `reconcile_vectors()` — no worse than today's exposure, not zero exposure.

## Decision

Split what `ingest_file` did in one synchronous step into three:

1. **`_prepare_file`** — parse, chunk (`chunk_all_tiers`), resolve import edges, and resolve
   summaries from the cache (calling the pass-2 stand-in summarizer for any miss, as today). No
   embedding, no FAISS, no SQLite. Returns a `_FilePlan`: the file's chunk lists, per-tier chunk
   ids, per-tier code texts, and per-tier `(id, summary_text)` pairs still needing a vector.
2. **Cross-file batching, in `run_incremental`'s pass-2 loop.** Files are prepared one at a time (in
   the same order as today) into a small pending window. Once the window's pending texts reach
   `_EMBED_WINDOW_TEXTS` (256 — 8x the embedder's own 32-batch, chosen to amortize the call/RPC
   overhead well past one file without holding an unbounded number of files' text and vectors in
   memory), every text queued so far — every pending file's tier-1, tier-2 and tier-3 code texts,
   then every pending file's summary texts — is embedded in **one** `embed_batch` call and
   normalized once (`normalize_L2` is row-independent, so normalizing the whole returned matrix
   before slicing it is equivalent to normalizing each slice). The window also flushes once at the
   end of `to_index`, whatever is left in it.
3. **`_write_plan`** — once a file's plan has its vectors, add each tier's vectors to that tier's
   FAISS index (`add_with_ids`), add any summary vectors to the summary index, and commit the file's
   SQLite row (`db.upsert_file`) — in that order, same as `ingest_file` always did. Files in a
   flushed window are written in the order they were queued, matching today's per-file order.
4. **`ingest_file` keeps its exact signature and behavior** — `_prepare_file` → embed everything it
   needs in one call → `_write_plan`, for exactly one file, synchronously. It is what a single-file
   watchdog reindex still runs (`to_index` has one file, so the window flushes once with that one
   file's texts — no cross-file batching to do, but tiers and summaries are now one call rather
   than up to six). It also stays the direct entry point four existing tests call standalone
   (`test_ghost_vectors.py`, `test_private_members.py`, `test_summary_index.py`).

Crash safety: a file's SQLite row is committed only after `_write_plan` has added that file's
vectors to the in-memory FAISS index, exactly as before — batching only changes *when the shared
embedding compute happens*, not the write order. A kill during the shared `embed_batch` call
commits nothing for any file in that window (same class of loss as today's kill during one file's
embed, just possibly spanning more files — a redo cost, not a new inconsistency). A kill between
two files' `_write_plan` calls leaves the same partial state ADR-037 already heals: rows with no
vector never get saved to disk (nothing is persisted until the run's final `save_all()`), so
`reconcile_vectors()` on the next run re-indexes exactly the files that did not finish.

`test_chunker_version.py` and `test_heal_missing_vectors.py` used to simulate a kill by
monkeypatching `ii.ingest_file` to raise when reaching a given file — that was always a stand-in for
"the run dies right after finishing the previous file, right before finishing this one." Batching
moves that boundary from `ingest_file` to `_write_plan`, so both tests now patch `_write_plan`
instead; the scenario and the assertions are unchanged.

## Consequences

**Better:** far fewer, larger `embed_batch` calls for any build with more than one file in
`to_index` — a full build or a large diff. To measure on the next real GPU build, against
InventoryApp-V2's 5.4 min / 115 ms/chunk baseline (2026-09-28).

**Worse:** a GPU or host error during a shared `embed_batch` call now fails every file in that
window, not just one — they simply aren't indexed this run and are retried next run, the same
outcome a single file's embed failure already produced, just for more files at once. A little more
code (three functions instead of one) for the same external behavior.

**Neutral:** single-file watchdog runs measure the same result, with a small, unmeasured reduction
in `embed_batch` calls per file (six down to one); the backlog already expected this gain to be
small since there's only one file. No schema, id, or config change — no forced rebuild.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Raise `core.embed_batch`'s internal `batch_size` default | Does nothing: the bottleneck is the *input list* being 1-4 texts, not the internal tile size it's split into. |
| Merge a file's own tiers + summaries into one call, no cross-file batching | Cuts calls per file from up to six to one, but a small file's total chunk count can still be under 32; the backlog's own numbers (1,909 calls, 2,815 chunks) show most of the problem is cross-file, not cross-tier. |
| Batch by a token/char budget instead of a text count | The summarizer's `_SUMMARY_SLICE` does this for prompt-length reasons; the embedder truncates at a fixed `max_seq_length` regardless of input length, so a text-count budget is simpler and just as effective here. |
| Flush the whole run's texts in one call | Unbounded memory for a very large repo — holds every pending file's chunk text and, briefly, every vector, at once. A bounded window gets nearly all of the win with flat memory. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] `_FilePlan`, `_prepare_file`, `_write_plan`, `_embed_texts` in `incremental_indexer.py`
- [x] `run_incremental`'s pass-2 loop batches pending files into `_EMBED_WINDOW_TEXTS`-sized windows
- [x] `ingest_file` re-implemented on top of the same three pieces, signature and behavior unchanged
- [x] `test_chunker_version.py` / `test_heal_missing_vectors.py` kill-hook moved from `ingest_file`
  to `_write_plan` (same scenario, same assertions)
- [x] New tests (`tests/test_cross_file_embed_batching.py`) with a fake embedder that records batch
  sizes: a multi-file build produces one large batch instead of one-per-file-per-tier, a window
  boundary produces more than one batch, and the resulting FAISS ids / SQLite rows are identical to
  running the same files one at a time through `ingest_file`
- [x] Full suite (`python -m pytest tests/ -q`), CPU only, `CUDA_VISIBLE_DEVICES=-1`: 560 passed, 1
  skipped (558 on master + 3 new tests here; the 1 skip is the pre-existing worktree-path issue
  noted in ADR-036/037, unrelated to this change)
- [ ] Measure on the next real GPU build (InventoryApp-V2 or similar), against 5.4 min / 115 ms/chunk

**Notes:**
<!-- 2026-09-28: branch cut from master (cacaebb). No GPU/model access in this worktree; every test
     here stubs embed_batch, per the hard constraint in the backlog item. -->

## Addendum: overlap embedding with preparation (B-055, 2026-09-28)

Batching cut the number of embed calls, but pass 2 still alternated: the CPU chunked and parsed
files until a window filled, then waited while the GPU embedded it, and the GPU sat idle through
the next window's CPU work. Pass 2's time was the sum of the two.

- With `[indexer] embed_overlap = true` (the default), each full window's embed call runs on one
  background thread (`ThreadPoolExecutor(max_workers=1)`) while the main thread prepares the next
  window. When that next window fills, it is queued behind the one in flight, so the GPU moves
  straight on to it, and the finished window is written while it runs.
- Only the model call moves. Every FAISS and SQLite write stays on the main thread, in queue order,
  window k before window k+1, so the per-file commit point and kill-safety are unchanged. Preparing
  a file never reads an earlier file's writes (it reads only the summary cache and fills the
  in-memory document cache), which is what makes this safe.
- `embed_overlap = false` restores back-to-back running, for timing comparisons or a backend that
  misbehaves off the main thread.
- Pass 2 now ends with a `Pass 2 timing` line: embed calls and texts, time inside the embed calls,
  time the main thread waited for vectors, prepare and write time, and wall time. With overlap off,
  `embed` equals `waited`; with it on, their difference is the embedding hidden behind preparation.
  This line is also the measurement the unchecked box above asks for.
- Tests (`tests/test_pass2_embed_overlap.py`): overlapped and back-to-back builds write identical rows
  and vectors; the next window is prepared while one is embedding (the test fails with overlap off);
  writes stay in queue order across windows; an embed failure still fails the run and leaves no
  thread behind; the timing line is printed.
