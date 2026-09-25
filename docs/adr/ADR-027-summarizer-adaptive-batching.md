# ADR-027: Summarizer Batching With a Self-Imposed GPU Memory Cap

**Status:** proposed
**Date:** 2026-09-24
**Branch:** `feature/adr-027-summarizer-adaptive-batching` (cut from `fix/two-pass-summarization` at 5f03798)
**Reviewer:** @edb
**Backlog:** [B-022](../backlog.md#b-022) — the summarizer runs one chunk at a time on the GPU. Related: [B-014](../backlog.md#b-014) (the local GPU baseline this was measured during) and [B-013](../backlog.md#b-013) (summarizer priority on the watchdog daemon).
**Depends on:** the two-pass split on `fix/two-pass-summarization` (5f03798). That branch has no ADR of its own and has not been merged. This branch is cut from it and its PR carries both, so this ADR records the two-pass decision as §0 below rather than leaving it unowned.
**Depended on by:** [ADR-028](./ADR-028-central-model-host.md) — the model host runs this batching loop and memory cap for its summary queue, and needs the full-pass throughput (Verification 3) to size its summary window.

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

### §3. Batch size from a token budget that adjusts itself

*Revised 2026-09-24 after the full-pass and profile measurements in the Implementation Log. As first written, the batch started at 4 chunks, halved on OOM, and grew by one chunk after 8 clean batches, up to 16.*

GPU memory for a batch grows with the number of chunks times the padded prompt length, so the batch is sized in prompt tokens, not chunks. Chunks go longest first, so the first chunk of a batch sets its padded length, and a batch holds `budget // that length` chunks, never more than `[summarization].max_batch_size` (default 48). The budget starts at `[summarization].batch_token_budget` (default 16,000). Long chunks go in small batches and short chunks in large ones, with no per-length table to keep.

- On `OutOfMemoryError`: free the cache and retry the same chunks with one chunk fewer. After two OOMs in a row, halve instead, so a sudden grab by another process is escaped quickly. The budget drops to what was retried, so later batches do not walk back into the same wall. Nothing is dropped.
- At batch size 1, if it still does not fit: that chunk gets no summary, the reason is counted, and the pass moves on.
- After 8 batches in a row with no error: the budget grows by room for one more chunk at the current length, up to the maximum batch.
- The worker keeps the budget and the clean-batch streak between calls, so each group of chunks carries on from what the last one learned.

`max_batch_size = 1` turns batching off and gives the old behavior. On CPU the worker always uses 1, since batching buys little there and the cap only means anything on CUDA.

### §4. Pause instead of pushing in

If free memory is below the reserve before a batch, another process has taken it. The worker waits and checks again every 5 s, up to `pause_timeout_s` (120 s, a module constant). If memory comes back it carries on. If it does not, it continues at batch size 1. In the worst case it should be as slow as today, and it should not spill.

### §5. Bounded jobs and visible failures

The parent sends chunks to the worker in groups of at most 96 (32 as first written; a group also caps the batch, so it is kept at twice the default maximum), with a timeout scaled to the group size, instead of one job per tier with a flat 300 s. A timeout restarts the worker once and retries that group. It no longer disables summarization for the whole run.

The worker returns counts with each result: chunks summarized, chunks left empty and why (OOM at batch 1, generation error, timeout), OOM backoffs, pauses, and peak memory. Pass 1 ends with one line that shows them, so a partly failed pass is visible without counting rows.

## Consequences

**Better:** the pass should be several times faster on this card. 3 to 5 times is a plausible guess, not a measurement. The watchdog daemon's per-save summarizer cost drops with it. Large tiers stop turning summarization off, and failures show up in the log.

**Worse:** the worker owns its tokenization and generation loop instead of handing that to the pipeline, which is more code to keep correct across `transformers` upgrades. Batched fp16 kernels do not always produce exactly the same greedy output as batch size 1, so a small number of summaries may differ from an unbatched run. That changes the embedded text, so it is checked below rather than assumed away.

**Neutral:** the summary cache is keyed by chunk text, not by how the summary was produced. Summaries already cached from batch-size-1 runs are kept, and new ones come from batched runs. Three new `[summarization]` keys, each with a code default that must match `indexer.toml` under `tests/test_config_drift.py`.

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

- [x] Record the batch-size-1 baseline from the 2026-09-24 run: pass-1 time, chunk count, `chunk_summaries` rows, peak memory (see Notes)
- [x] Verification 1 on the GPU (see Notes)
- [x] Resolve the obligation to ADR-028: hand it the full-pass throughput once Verification 3 runs (see Notes; update it after the token-budget run)
- [x] §1 worker generation loop on `model.generate` with left padding and length-sorted batches
- [x] §2 per-batch memory cap from `mem_get_info`
- [x] §3 adaptive batch size, and `max_batch_size = 1` reproduces today's behavior
- [x] §4 pause on low free memory
- [x] §5 bounded job groups, worker restart on timeout, end-of-pass counts
- [x] `[summarization].max_batch_size` and `vram_reserve_mb` in `indexer.toml`, `src/config.py`, and the drift test
- [x] §3 revised to a token budget with step-back-one on OOM; `[summarization].batch_token_budget`
- [x] Verification 3 on the token-budget build (16.3 min, see Notes)
- [ ] Verification 1 rerun on the token-budget build
- [x] Verification 2 noise floor (see Notes: the drop is real, small)
- [ ] Decision (@edb): accept the small retrieval loss for the speed, or find why batched output differs systematically
- [x] Unit tests (Verification 6): `tests/test_summarizer_batching.py`, 20 tests, no GPU
- [x] `tools/summarizer_batch_equivalence.py` for Verification 1 (not run yet)
- [ ] Verification 1 to 5 on the GPU, results recorded here with provenance
- [ ] Set status to `accepted` in the PR

**Notes:**
<!-- 2026-09-24: Written while the batch-size-1 baseline was still running. The 3 s per chunk and 3.65 GB figures are from its first 10 minutes. -->

- 2026-09-24: The batching loop is `run_adaptive_batches()` in `src/summarizer.py`. It takes the model call and the memory probe as arguments and imports no torch, which is what lets the unit tests drive OOM at chosen batch sizes with fakes.
- 2026-09-24: Found while building §5. On a timeout the old code called `executor.shutdown(wait=False)`, which leaves the stuck worker running and still holding its GPU memory. A restarted worker would load a second copy of the model beside it. The retry path now kills the worker first (`kill_workers()` on Python 3.14, the pool's processes otherwise). The between-passes `shutdown()` now waits, so the summarizer's memory is back before the embedder loads.
- 2026-09-24: The worker no longer uses the text-generation pipeline, so `pad_token_id` is the tokenizer's pad token rather than EOS. Generation settings are otherwise the same as before, including whatever the model's own generation config sets.
- 2026-09-24, **batch-size-1 baseline** (`fix/two-pass-summarization` at 5f03798, driver 582.70, BIOS 1.19.2, AC power, Qwen2.5-Coder-1.5B fp16, embedder bf16 via stress-kit patch, fresh index): 104 files, 1,476 chunks, 1,414 distinct chunk texts. Pass 1 took 4,022 s (67.0 min), pass 2 took 90 s. All 1,414 distinct texts got a summary. Dedicated memory 3.67 GB average, 4.65 GB peak, no shared-memory growth, 0 PCIe replays, 0 WHEA. Telemetry: `gpu-crash-repro/telemetry/stress_20260924_144636`.
- 2026-09-24, **Verification 1** (this branch at 9f096a6, same stack, 200 chunks, seed 27, max batch 16, reserve 1024 MiB): batch size 1 took 482 s, batched took 159 s, **3.03 times faster**. 200 of 200 summarized both ways, no OOM backoffs, no pauses, peak 4.76 GB. The batch only reached 8, because it grows one step per 8 clean batches and 200 chunks ran out first, so a full pass should do better than 3 times. **Exact match 94.5 percent (189/200).** All 11 differences read by hand: wording changes in one field, with no lost purpose, inputs, or outputs. Two batched summaries followed the requested format better than their batch-1 pair. One batched summary invented a detail ("using a specified summarizer and embedder" for pass 1, which has no embedder). Batch size 1 is not ground truth either, so the retrieval check (Verification 2) is the real gate. Results: `tools/results/summarizer_batch_equivalence.json`.
- 2026-09-24: Full suite on CPU: 323 passed, 1 skipped, 6 failed. The 6 are `test_adapter_snapshots.py`, and they fail the same way on the base commit and on the L5X branch checkout, so they are not from this change. Likely `core.autocrlf=true` rewriting fixture line endings on this Windows checkout; CI on GitHub was green.
- 2026-09-24, **Verification 3, as first built** (this branch at 9e06356, same stack, fresh index, telemetry `stress_20260924_161948`): pass 1 took 1,858 s (31.0 min) for 1,480 distinct texts, 0.80 per second against the baseline's 0.35, **2.3 times faster**. 0 empty, 0 OOM backoffs, 0 pauses, peak 4,770 MiB, no shared-memory growth. But **the largest batch was 4**: the batch never grew. Pass 1 called `summarize_batch()` once per file and tier, about 4 chunks a call on this repository, and every worker call started over at batch size 4 with a zero clean-batch streak. Growing takes 8 clean batches in a row, so it never happened. The 3.03 times in Verification 1 came from handing the worker all 200 chunks in one call, which the indexer never does. (This branch has more files than the baseline branch, so the chunk counts differ; the per-second rates are the comparison.)
- 2026-09-24, **deviation, fixed in f94bc7d and 43d5281:** pass 1 now collects every uncached chunk in the repository first, keeps one copy of each distinct text, sorts longest first across the whole repository, and summarizes in slices of 64, writing the cache after each slice. The worker keeps its batch size and clean-batch streak between calls (`_w_next_batch`, `_w_next_streak`), so the daemon's small per-save calls also keep what they learned. The equivalence tool resets that state before each timed run. 7 new unit tests, no GPU.
- 2026-09-24, **Verification 3 after the fix** (43d5281, fresh index, telemetry `stress_20260924_165353`): pass 1 took 1,030 s (**17.2 min**) for 1,490 distinct texts, **1.45 per second, 4.1 times the batch-1 baseline.** Largest batch 16, 0 empty, 0 OOM backoffs, 0 pauses, peak 4,896 MiB, shared usage flat at its 64 MiB baseline. Pass 2 took 96 s. Dedicated memory rose only about 40 MiB as the batch grew, because chunks go longest first: the batch gets bigger as the chunks get shorter.
- 2026-09-24, **Verification 1 rerun** (43d5281): batch size 1 took 478 s, batched 187 s, 2.56 times, largest batch 8. **Exact match 92.0 percent (184/200).** The sample is not the same 200 chunks as the first run, because this branch has more files and the seed draws from a different list. All 16 differences read by hand. Most are rewordings or more or less detail in one field. Two batched summaries are more accurate than their batch-1 pair (a C# fixture's constructor and `Add` method, which batch 1 left out, and a scan-policy test that batch 1 described wrongly). No batched summary invents content its batch-1 pair lacks. Two batch-1 summaries were cut off at the 160-token limit. One chunk (`run_summarization_pass`) got invented parameter names at both batch sizes, which is the model, not batching.
- 2026-09-24, **batch-size sweep** (stage 10, same 200 chunks, fixed sizes, telemetry `stress_20260924_172617`): batch 1 took 477 s. Sizes 4, 8, 12, 16, 24, 32 took 209, 165, 156, 163, 156, 167 s (2.28 to 3.06 times), with 92.0 to 93.5 percent exact match at every size. Every size from 8 up hit the cap on the first, longest chunks, halved, and never climbed back within 200 chunks, so sizes 12 to 32 all really ran at 8 or 9. Two things from this: long chunks top out near batch 8 on this card under the 1 GB reserve, and §3's halve-then-add-one loses most of its capacity after one OOM. Every OOM was caught by the cap; no summary was lost and nothing paged.
- 2026-09-24, **length by batch-size profile** (stage 11, telemetry `stress_20260924_175310`, `gpu-crash-repro/telemetry/summarizer_batch_profile.json`): real chunks binned by prompt length, each batch forced to the full 160 new tokens, under the cap. Seconds per chunk:

  | Prompt tokens | 1 | 4 | 8 | 12 | 16 | 24 | 32 | 48 | 64 |
  |---|---|---|---|---|---|---|---|---|---|
  | ~256 | 3.9 | 0.91 | 0.57 | 0.44 | 0.37 | 0.32 | 0.28 | 0.25 | 0.23 |
  | ~512 | 3.6 | 1.10 | 0.70 | 0.57 | 0.51 | 0.45 | 0.43 | 0.40 | OOM |
  | ~1,024 | 5.0 | 1.66 | 1.34 | 1.25 | 1.23 | OOM | | | |
  | ~2,048 | 5.8 | 2.00 | 1.61 | 1.51 | OOM | | | | |
  | ~3,500 | 5.1 | 2.91 | OOM | | | | | | |

  Peak memory depends on batch size times padded length and little else. At about 16,000 prompt tokens per batch it is 4.3 to 4.6 GB at every length (64 x 256, 32 x 512, 16 x 1,024, 8 x 2,048, 4 x 3,500), and the cap on this card with the desktop running allowed about 5.9 GB. That budget also sits near where time per chunk stops falling at each length. Short chunks gain 17 times from batching and the longest gain 1.8 times, because a long chunk's time is mostly reading the prompt, which is already a full workload at batch size 1.
- 2026-09-24, **stop at the first blank line** (stage 12, same 200 chunks, batches packed to 16,000 prompt tokens and at most 48, telemetry `stress_20260924_180121`, `summarizer_stop_waste.json`): 21.8 percent of generated tokens were past the first blank line and thrown away by `_clean()`, and stopping there removes 19.9 percent of decode steps. With the stop the set took 89.1 s against 107.3 s, **1.2 times faster, and all 200 cleaned summaries were identical.** Against batch size 1 (477 s) the token-budget batches alone were 4.45 times faster and with the stop 5.35 times. The token-budget outputs were not compared with batch size 1 in this run. Separately, 39 of 200 summaries (about 1 in 5) hit the 160-token limit and are cut off mid-sentence. That is a quality question for its own change, not for this ADR.
- 2026-09-24, **handed to ADR-028** (open question 2): a summary window drains about 1.45 chunks per second on this card with growth, and should do better with the token budget. A 5-minute window clears roughly 400 to 500 chunks. The summarizer loads in about 14 s.
- 2026-09-24: GPU health across all of the above, about 6 hours of load: 0 WHEA events, 0 PCIe replays, link at Gen5 x4 throughout, 70 C maximum.
- 2026-09-24, **deviation, §3 revised to a token budget** (suggested by @edb after the profile): batch size is `budget // longest prompt in the batch`, starting at 16,000 prompt tokens, at most 48 chunks. An OOM retries one chunk smaller (twice), then halves, and lowers the budget to what was retried. The budget grows by one chunk's worth after 8 clean batches. Worker groups went from 32 to 96 chunks and pass-1 slices from 64 to 192, because a group caps the batch. 3 new unit tests (sizing by length, step-back to the exact ceiling, step-back then halving); 332 tests pass on CPU, with the 6 pre-existing snapshot failures deselected. Measured on a 200-chunk sample in stage 12 at 4.45 times batch size 1; the full pass has not been run on this build yet.
- 2026-09-24, **CPU baseline, projected** (no GPU, 14 torch threads, same repository, 1,552 chunks; `gpu-crash-repro/cpu_baseline.py`, `telemetry/cpu_baseline.json`): a full CPU index would pin the CPU for hours, so a few chunks were timed from the short end, the middle, and the long end of the length range, and a straight-line fit of seconds against tokens was applied to every chunk. Summarizer, fp32, batch size 1: 5.1 to 5.4 s at ~143 prompt tokens, 12.4 to 17.8 s at ~290, 57 to 71 s at ~4,000, **projected 6.3 h** for the repository (about 6.1 h for the 1,490 distinct texts). The indexer's CPU default is fp16, and three fp16 chunks timed the same or a little faster (4.0 s, 9.5 s, 15.5 s), so the projection stands for either. Embedder, fp32: 0.19 s at ~26 tokens, 0.73 s at ~168, 2.73 s at the 512-token cap, **projected 31.8 min**. Against the GPU: summarizing 67 min at batch size 1 (about 5.5 times the CPU), 17.2 min after the cross-file fix (**about 21 times the CPU**); embedding 90 s in bf16 (about 21 times). A projection from 6 summarizer points, not a measured full run.
- 2026-09-24, **Verification 3 on the token budget** (stage 7, 64a38be, fresh index, telemetry `stress_20260924_192215`): pass 1 took 975.5 s (**16.3 min**) for 1,496 distinct texts, 1.53 per second. The batch-1 baseline was 67.0 min, so this is 4.1 to 4.3 times faster. It is only 5 percent faster than the cross-file build (1,030 s).
  - Largest batch 48, 0 OOM backoffs, 0 pauses, peak 5,898 MiB, shared usage flat at 64 MiB. Pass 2 took 98 s.
  - From 19:26 to the end, the card sat in P4 at about 1,200 MHz and 30 W while reporting 95 percent utilization. Later, when the laptop's battery ran low during stage 15, utilization fell from about 70 to about 37 percent while the SM clock held at about 2,550 MHz. Both point the same way: the short-chunk tail is limited by the CPU launching kernels, not by the GPU. More batch size will not fix that. `torch.compile` or CUDA graphs might.
- 2026-09-24, **defect in §4, found and fixed** (5bc3f60): after a pause timed out, the loop paused again before every batch, so memory held by another process ran one chunk every 120 s.
  - Found when the retrieval driver built three repos in one process and left the embedder from zustand resident while click summarized. Stage 13 crawled and was stopped by the stall guard after 20 min with no output.
  - The loop now carries on at batch size 1 without waiting again for the rest of the call, as §4 always said it should. There is a test for it.
  - The driver now frees the embedder between repos, and stage 13 was rerun from scratch.
- 2026-09-24, **Verification 2, retrieval** (stages 13 to 16, 5bc3f60, telemetry `stress_20260924_202823`, `gpu-crash-repro/telemetry/retrieval/results.json`).
  - Setup: three pinned eval repos (p-queue, zustand, click), 83 queries, the shipped arm (graph on, reranker off, RRF), and the embedder in bf16 for index and queries alike. Each variant has a fresh index: no summaries, batch size 1, and batched.

    | Variant | MRR@10 | nDCG@10 | p-queue MRR | zustand MRR | click MRR |
    |---|---|---|---|---|---|
    | no summaries | 0.4427 | 0.5379 | 0.5500 | 0.3126 | 0.4719 |
    | batch size 1 | 0.4490 | 0.5482 | 0.5771 | 0.2825 | 0.4935 |
    | batched | 0.4410 | 0.5417 | 0.5771 | 0.2733 | 0.4805 |

  - Batched against batch size 1: MRR 0.008 lower and nDCG 0.0065 lower. **5 of 83 queries changed, all 5 down**, each by one or two places (3rd to 4th three times, 2nd to 4th, 2nd to 3rd).
    - The three zustand queries all target `persist.ts`, and in each one a test chunk from `tests/basic.test.tsx`, whose summaries were worded differently, moved into the top 3.
    - The two click queries moved on a reworded summary: once on the gold chunk's own summary (`UsageError`) and once on a competing test's (`test_getchar`).
    - Summaries identical between the two runs: 94.6, 93.9 and 92.6 percent per repo. Where they differ, batched is shorter 76 times and longer 67, so there is no systematic bias.
  - Summaries themselves barely move this eval: no summaries against batch size 1 changed 40 queries, 22 up and 18 down, for +0.006 MRR, and zustand got worse with summaries. The batching drop is the same size as the whole benefit of summarizing here.
  - **Verdict: not resolved.** Five down and none up is suggestive (a sign test gives about p = 0.06, and three of the five are one event), but it is not a demonstrated loss. The eval cannot separate the two either way. The missing number is the noise floor: two batched runs with different batch compositions. If those also move about 5 queries against each other, this is noise.
- 2026-09-24, **separate finding, both batch sizes:** 17.5 percent of summaries are one paragraph, because `_clean()` keeps only the text before the first blank line, and the model often puts one after "Purpose:". Those summaries lose their Inputs, Outputs and Key operations lines. This is independent of batching (17.5 against 17.7 percent), and it probably costs more retrieval than batching does. Not changed here.
- 2026-09-24, **Verification 2, noise floor and the summary-fields fix** (stages 17 to 19, 77733c0 and `fix/summary-keep-all-fields` at 31a6f3a, telemetry `stress_20260924_224220`, `retrieval/results.json`; the stage 16 file is kept as `results_stage16.json`). Two more fresh builds of the same three repos.
  - `batched2` is batched with a 12,000-token budget, so the chunks group into different batches.
  - `fields` is batched with `_clean()` keeping every field.

    | Variant | MRR@10 | nDCG@10 | Queries changed against batch size 1 |
    |---|---|---|---|
    | no summaries | 0.4427 | 0.5379 | |
    | batch size 1 | 0.4490 | 0.5482 | |
    | batched | 0.4410 | 0.5417 | 5, all down |
    | batched2 | 0.4330 | 0.5357 | 7, all down |
    | fields | 0.4344 | 0.5367 | |

  - **The drop is real, not noise.** The two batched builds differ from each other on only 2 queries. Both drop the same 5 queries against batch size 1, and batched2 drops 2 more, 7 of 7 down (a sign test gives about p = 0.016). With different batch groupings, the same chunks come out worded the same way, which suggests something systematic in batched generation (left padding under fp16 is the first suspect), not chance. The loss is small: each query moves one or two places, and MRR falls 0.008 to 0.016. That brings batched summaries down to about the no-summary score.
  - **Summaries barely help on this eval at any batch size.** The best case, batch size 1, is +0.006 MRR over no summaries, and zustand scores worse with summaries than without.
  - **The fields fix did not help retrieval.** One-paragraph summaries fell from 17.7 to 0.7 percent and mean length rose from 322 to 467 characters, but MRR fell 0.007 against batched (14 queries changed, 6 up and 8 down; p-queue lost the most). A likely reason is that longer summaries crowd the code out of the embedder's 512-token window for short chunks. So it is not merged, and the branch stays for reference.
  - @edb's condition was that batching must not reduce retrieval accuracy. **On this eval it does, slightly.** That is recorded as the outcome and left for @edb to decide, with the size of the loss above.
