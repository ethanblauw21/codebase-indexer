# ADR-027: Summarizer Batching With a Self-Imposed GPU Memory Cap

**Status:** proposed
**Date:** 2026-09-24
**Branch:** `feature/adr-027-summarizer-adaptive-batching` (cut from `fix/two-pass-summarization` at 5f03798)
**Reviewer:** @edb
**Backlog:** [B-022](../backlog.md#b-022) — the summarizer runs one chunk at a time on the GPU. Related: [B-014](../backlog.md#b-014) (the local GPU baseline this was measured during) and [B-013](../backlog.md#b-013) (summarizer priority on the watchdog daemon).
**Depends on:** the two-pass split on `fix/two-pass-summarization` (5f03798). That branch has no ADR of its own and has not been merged. This branch is cut from it and its PR carries both, so this ADR records the two-pass decision as §0 below rather than leaving it unowned.
**Depended on by:** none yet.

## Context

With the summarizer on, indexing this repository is dominated by summarization by a very wide margin. On 2026-09-24, on the repaired RTX PRO 1000 (8 GB):

- Embedding only, `bge-code-v1` in bf16: 118 files, 1,731 chunks in 106 s. That run used a stress-kit patch for the dtype, not `src/`.
- Summarization pass, `Qwen/Qwen2.5-Coder-1.5B-Instruct` in fp16, in the isolated worker: about 0.35 chunks/s, roughly 3 s per chunk. Dedicated memory sat flat at 3.65 GB of 8.15 GB and GPU utilization ran 55 to 95 percent. The full pass-1 time for this repository goes in the Implementation Log when the run finishes.

So the summarizer is something like 40 times the cost of embedding, and it is using less than half the card. Provenance for both runs: branch `fix/two-pass-summarization` at 5f03798 for the summarizer run, driver 582.70, BIOS 1.19.2, on AC power, `max_seq_length` 512, `_MAX_NEW_TOKENS` 160. Telemetry is under `gpu-crash-repro/telemetry/stress_20260924_*`.

Three things in the worker path explain most of it, and two of them are defects in their own right.

**1. The worker generates one chunk at a time.** `_worker_batch` passes `batch_size=1` to the pipeline (`src/summarizer.py:116`). The in-process `ChunkSummarizer` uses `min(4, n)` (`:273`), but the indexer does not construct that class. It constructs `IsolatedChunkSummarizer` (`incremental_indexer.py:704` on master), which is the slow one.

**2. A large file tier disables summarization for the rest of the run.** `IsolatedChunkSummarizer.summarize_batch` submits every uncached chunk of a tier as a single job and waits `timeout=300` (`:365-367`). At 3 s per chunk that is about 100 chunks. Anything over that times out, and the handler sets `self._failed = True` and shuts the worker down (`:370-379`), so every later file is indexed with no summaries. The largest tier in this repository is under 80 chunks, which is why it has not shown up here. `click/src/click/core.py` in the benchmark corpus has 177 tier-1 chunks, and it would trip it.

**3. Failures are silent.** The worker catches every exception and returns empty strings (`:118-119`). `db.cache_summaries` drops empty strings, so a failed chunk leaves no row and no message. The only way to see it is to count `chunk_summaries` rows against chunks afterward, which is the same trap recorded for the 2026-09-10 `accelerate` failure.

**Why batching cannot just be turned on.** On Windows, CUDA does not raise an out-of-memory error when the card fills. The display driver pages GPU memory into system RAM and throughput drops by roughly 50 times, with nothing in any log. That was seen on this machine: 77 chunks took 18 minutes with 4,260 MiB spilled, and on 2026-09-24 an fp32 embedder load reached 7,826 MiB and started spilling on its first batch. A design that catches OOM and backs off would never see an OOM to catch. The memory limit has to be one the process imposes on itself.

## Decision

### §0. Two-pass loading (inherited from 5f03798)

Summarize every chunk first, shut the summarizer worker down, then embed. Peak GPU memory is the larger of the two models instead of their sum. This is already built and it passed on the GPU on 2026-09-24. It is recorded here so it has an owner.

### §1. Batched generation through `model.generate`, not the pipeline

The worker loads the tokenizer and model directly and calls `model.generate` on padded batches. It does not pass `batch_size` to the text-generation pipeline. Decoder-only batching needs left padding, and the pipeline does not make that choice visible. Getting it wrong does not error, it just produces worse summaries. The worker sets `tokenizer.padding_side = "left"` itself and uses greedy decoding with the same prompt, `_MAX_CODE_CHARS` and `_MAX_NEW_TOKENS` as today.

Chunks are sorted by token length before they are grouped, so each batch holds similar lengths and little memory goes to padding. Results are returned in the caller's original order.

### §2. A hard memory cap in the worker

Before each batch the worker calls `torch.cuda.mem_get_info()` and sets `torch.cuda.set_per_process_memory_fraction()` so that this process can use what it already holds plus what is free now, minus a reserve. Past that line PyTorch raises `torch.cuda.OutOfMemoryError`, which the worker can catch, instead of the driver quietly paging.

The reserve is `[summarization].vram_reserve_mb`, default 1024. It leaves room for the desktop, a browser, and whatever else is on the card. The cap is recomputed per batch because other processes can grow while the pass runs.

### §3. Batch size that adjusts itself

The batch starts at 4 and never goes above `[summarization].max_batch_size`, default 16.

- On `OutOfMemoryError`: free the cache, halve the batch, and retry the same chunks. Nothing is dropped.
- At batch size 1, if it still does not fit: that chunk gets no summary, the reason is counted, and the pass moves on.
- After 8 batches in a row with no error: raise the batch by 1, up to the maximum.

`max_batch_size = 1` turns batching off and gives today's behavior. On CPU the worker always uses 1, since batching buys little there and the cap only means anything on CUDA.

### §4. Pause instead of pushing in

If free memory is below the reserve before a batch, another process has taken it. The worker waits and checks again every 5 s, up to `pause_timeout_s` (120 s, a module constant). If memory comes back it carries on. If it does not, it continues at batch size 1. In the worst case it should be as slow as today, and it should not spill.

### §5. Bounded jobs and visible failures

The parent sends chunks to the worker in groups of at most 32, with a timeout scaled to the group size, instead of one job per tier with a flat 300 s. A timeout restarts the worker once and retries that group. It no longer disables summarization for the whole run.

The worker returns counts with each result: chunks summarized, chunks left empty and why (OOM at batch 1, generation error, timeout), OOM backoffs, pauses, and peak memory. Pass 1 ends with one line that shows them, so a partly failed pass is visible without counting rows.

## Consequences

**Better:** the pass should be several times faster on this card. 3 to 5 times is a plausible guess, not a measurement. The watchdog daemon's per-save summarizer cost drops with it. Large tiers stop turning summarization off, and failures show up in the log.

**Worse:** the worker owns its tokenization and generation loop instead of handing that to the pipeline, which is more code to keep correct across `transformers` upgrades. Batched fp16 kernels do not always produce exactly the same greedy output as batch size 1, so a small number of summaries may differ from an unbatched run. That changes the embedded text, so it is checked below rather than assumed away.

**Neutral:** the summary cache is keyed by chunk text, not by how the summary was produced. Summaries already cached from batch-size-1 runs are kept, and new ones come from batched runs. Two new `[summarization]` keys, each with a code default that must match `indexer.toml` under `tests/test_config_drift.py`.

## Verification

The PR does not merge until all of these hold on the 8 GB card.

1. **Same summaries.** On a fixed sample of 200 chunks from this repository, compare batched output with batch size 1. Report the exact-match rate and read the ones that differ. A difference in wording is fine; a summary that loses or invents content is not.
2. **Retrieval does not get worse.** `tools/real_repo_tripwire.py` stays at or above its 0.45 MRR@10 floor, and scores no lower on an index built with batched summaries than on one built at batch size 1, beyond run-to-run noise. This was the condition @edb set: faster is only acceptable if accuracy holds.
3. **Faster.** Pass-1 time on this repository against the batch-size-1 baseline from 2026-09-24, both with a fresh index so every summary is generated.
4. **Memory-safe under pressure.** Run pass 1 while a second process holds 2 to 3 GB of the card. Shared usage should stay at its baseline, the log should show OOM backoffs or pauses, and the pass should finish.
5. **No false stop on a large tier.** A tier over 100 chunks, for example `click/core.py`, completes with summaries.
6. **Unit tests** with a fake model that raises `OutOfMemoryError` at chosen batch sizes, covering halve-and-retry, grow-back, the batch-size-1 floor, the pause timeout, and result ordering. These do not need a GPU.
7. MCP Inspector is not needed. No MCP tool changes.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| `pipeline(..., batch_size=N)` with a fixed N | It does not control padding side, and it has no memory control. A fixed N that fits this repository will spill on a longer chunk or on a card with less free memory. |
| Batch, and catch OOM without a cap | On Windows the OOM is never raised. The driver pages instead. |
| A fixed batch size tuned for 8 GB | It breaks as soon as something else is using the card, which is the normal state on a laptop. |
| vLLM, llama.cpp, or another serving runtime | Much faster in principle, but it means a second inference stack, uncertain Windows support, and different outputs to re-validate. Worth a backlog item if batching is not enough, not the first step. |
| Load the summarizer in the indexer process to skip the worker | The worker exists because an OOM inside the indexer process kills it with no traceback (see the `IsolatedChunkSummarizer` docstring). That reason still holds. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Record the batch-size-1 baseline from the 2026-09-24 run: pass-1 time, chunk count, `chunk_summaries` rows, peak memory
- [ ] §1 worker generation loop on `model.generate` with left padding and length-sorted batches
- [ ] §2 per-batch memory cap from `mem_get_info`
- [ ] §3 adaptive batch size, and `max_batch_size = 1` reproduces today's behavior
- [ ] §4 pause on low free memory
- [ ] §5 bounded job groups, worker restart on timeout, end-of-pass counts
- [ ] `[summarization].max_batch_size` and `vram_reserve_mb` in `indexer.toml`, `src/config.py`, and the drift test
- [ ] Unit tests (Verification 6)
- [ ] Verification 1 to 5 on the GPU, results recorded here with provenance
- [ ] Set status to `accepted` in the PR

**Notes:**
<!-- 2026-09-24: Written while the batch-size-1 baseline was still running. The 3 s per chunk and 3.65 GB figures are from its first 10 minutes. -->
