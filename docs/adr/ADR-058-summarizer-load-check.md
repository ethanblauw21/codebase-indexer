# ADR-058: Refuse to Start Pass 1 When the Summarizer Can't Load, and Stop Blaming Chunking for Its Failures

**Status:** proposed
**Date:** 2026-11-12
**Branch:** `feature/adr-058-summarizer-load-check`
**Reviewer:** @edb
**Backlog:** [B-079](../backlog.md#b-079)
**Depends on:** none
**Depended on by:** none yet

## Context

A fresh install on a second PC had no `accelerate`: neither dependency file declared it. The
summarizer worker loads its model with `device_map`, which transformers refuses without
`accelerate`, and that import only happens inside the worker. So the worker failed twice, the
summarizer turned itself off, every text came back empty, and the run carried on. It ended with two
wrong diagnoses: the final line pointed at "oom = the GPU was full", and pass 2's stand-in counted
every chunk as a cache miss and said "the two passes disagree about chunking". `_CacheOnlySummarizer`
counted every miss as a disagreement, because it couldn't know which texts pass 1 had failed on.

## Decision

1. **Declare `accelerate`** in `pyproject.toml` and `requirements.txt`.
2. **Check before pass 1.** `summarizer.summarizer_unavailable_reason()` checks that `torch`,
   `transformers` and `accelerate` import (`importlib.util.find_spec`; nothing is loaded). When
   `[summarization].enabled` is true and one is missing, `run_incremental` raises
   `SummarizerUnavailable` before any summarizer is made, naming the package, the `pip install`
   line, and `[summarization].enabled = false` as the other way out. The CLI prints it and exits 2,
   like `IndexBusy`; the `reindex` tool and the watchdog surface it as their existing errors.
3. **Pass 2 tells the two causes apart.** `run_summarization_pass(..., failed_out=set)` collects the
   texts pass 1 got nothing back for, and `_CacheOnlySummarizer` takes that set: a miss on one of
   those counts as `pass1_failed` (one plain line, pointing at the pass 1 stats), and only other
   misses raise the "disagree about chunking" warning. Pass 1 also says when it produced no summaries
   at all. The final line no longer glosses the counts as "oom = the GPU was full".

## Consequences

**Better:** a missing package stops the run in its first second with the fix in the message,
instead of after a full build with the wrong cause. The chunking-disagreement warning means what it
says again.
**Worse:** a run with summarization enabled and a broken environment now builds nothing, where it
used to build an index without summaries. That is the point: the config asked for summaries.
**Neutral:** the check reads the indexer's own interpreter. With `[model_host].enabled`, the host is
started from the same interpreter (B-051), so the answer holds there too; a host already running
under a different interpreter is not consulted.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Load the model up front to prove it works | costs a 3 GB load before the scan; the import check catches the reported failure for free |
| Warn and carry on without summaries | that is what happened, and the warning was misread; the config asked for summaries |
| Only fix the dependency files | the next missing package would fail the same silent way |

## Implementation Log

- [x] `accelerate` in `pyproject.toml` and `requirements.txt`.
- [x] `summarizer_unavailable_reason()` / `SummarizerUnavailable`; `run_incremental` refuses; CLI exits 2.
- [x] `failed_out` + `_CacheOnlySummarizer(pass1_failed)`; pass 1 "no summaries at all" line.
- [x] `tests/test_summarizer_preflight.py`: refuses before pass 1 with `accelerate` hidden; no check when
      disabled; a pass 1 that summarized nothing gives no chunking-disagreement warning.

**Notes:**
<!-- 2026-11-12: the same root cause was found on 2026-09-10 and fixed in that environment only. -->
