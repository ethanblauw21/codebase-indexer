# ADR-048: A Model-Host Failure Never Puts Two Model Copies on the GPU

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-048-host-fallback`
**Reviewer:** @edb
**Backlog:** [B-061](../backlog.md#b-061), from GitHub #66
**Depends on:** ADR-028 (the model host), ADR-041 (the host's recorded device)
**Depended on by:** none yet

## Context

The model host (ADR-028) exists so that one process holds the models on the 8 GB card for every project. The client, `model_client`, falls back to in-process models whenever a host request fails. #66 suspected that this fallback could load a second copy beside the host's. **Reproduced**, on CPU with a private host (`CODE_INDEXER_HOST_DIR`), by killing the host during an `embed_batch` call:

| Step | Client RSS | Client holds a model | Host RSS |
|---|---|---|---|
| 1. host serving | 0.71 GB | no | 5.67 GB |
| 2–3. host killed during a 400-text `embed_batch` | **9.13 GB** | **yes**, loaded for that one batch | dead |
| 4. next call: a new host starts | **9.13 GB** | **yes, still** | 5.67 GB |

With this ADR, the same run (100 texts) instead:

| Step | Client RSS | Client holds a model | Host RSS |
|---|---|---|---|
| 1. host serving | 0.71 GB | no | 5.67 GB |
| 2–3. host killed mid-call; a new host serves the same batch | 0.71 GB | no | 7.86 GB (a new host, mid-batch) |
| 4. next call | 0.71 GB | no | 7.85 GB |

The script is `gpu-crash-repro/adr048/repro.py` (gitignored), run with `CODE_INDEXER_HOST_DIR` pointing at a scratch dir, `CODE_INDEXER_DEVICE=cpu`, and `[model_host] enabled = true`.

What happened:
1. **One failed request loaded a full model into the client.** `_call` raised `HostUnavailable` when the connection dropped. `embed_batch` caught it and called `core.embed_batch`, which loads the embedder into the calling process.
2. **The copy was never released.** The next call's `ensure_host` started a new host, which loaded its own copy. `core` caches its model for the life of the process. A live MCP server lives for days.
3. **On CUDA, both copies go on the card:** ~3 GB each in bf16. With the summarizer (~3 GB) or a second client in the same state, it overflows 8 GB, and Windows pages the overflow to system RAM without an error. That is the ~50× silent slowdown documented for WDDM.

The summarizer's fallback has the same shape. Its first failure starts an `IsolatedChunkSummarizer` worker on the GPU for the rest of the run, even if the host that failed is still running and holding its own copy.

## Decision

1. **A host that dies mid-request is replaced, not fallen back from.**
   - `_host_call` sends the request. If it fails and the host no longer answers `/v1/status`, it calls `ensure_host` once more, which starts a new host, and resends the request.
   - A host that still answers is not restarted: the failure was this request's, and the host may be serving others.
2. **A fallback never joins a live GPU host on the card.** `_host_may_hold_gpu()` is true when the host answers and its `host.json` device is CUDA, or unrecorded (a host older than ADR-041). While it is true:
   - **embeds** fall back in-process **on the CPU**. `core.embed` and `core.embed_batch` take a `device`, and a copy loaded on another device is released first;
   - **summaries** for that slice come back empty, and the next slice tries the host again. Empty summaries aren't cached (ADR-040's pass 1), so the next run summarizes them. `stats_line` counts them as skipped.
3. **With no usable host, the fallback is unchanged:** in-process on the resolved device, and a sticky summarizer worker. It is then the only copy, and ADR-041's busy CPU host (a CUDA client refusing it) lands here too.
4. **The in-process embedder is released once the host serves again.** `_release_fallback_embedder()` runs after every successful host embed and calls `core.unload_embed_model()`. That drops the model, runs `gc`, and empties the CUDA cache. A fallback copy now lives from the failure until the next successful host call, not for the rest of the process.

## Consequences

**Better:** a host crash, kill or restart costs a respawn instead of a permanent second model copy in every client that had a request in flight. The card holds one copy of each model through a failure, as ADR-028 intended.

**Worse:**
- A failed request against a live CUDA host is embedded on the CPU: seconds for a query, much longer for an index batch. That's slower, but not silently 50× slower from paging.
- Summaries skipped while a CUDA host misbehaves are missing until the next run, and search over those chunks uses the raw text alone for that time.
- Checking whether the host answers costs one status call (2 s timeout) per failed request.

**Neutral:**
- A host that serves a different model (`_check_models`) still fails every request. Embeds then go to the CPU and summaries are skipped for the run, where before a GPU copy loaded beside the host. That's a configuration error, and the log says so once.
- `IsolatedChunkSummarizer` itself is unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Never fall back; fail the call | ADR-028 promises a failed host never fails an index or a search. Replacing the host keeps that promise without the second copy. |
| Always fall back on the CPU | Needlessly slow when there is no host at all, the common case on a machine where the host never started. The card is only at risk while a host holds it. |
| Queue requests until the host returns (#66's suggestion) | Searches would hang for the length of a host restart and indexing would stall. Respawning at once is faster than waiting for someone else to. |
| Summarize on the CPU instead of skipping | A 1.5B summarizer on the CPU runs at a small fraction of GPU speed, so a run could take hours. Skipped summaries are retried by the next run. |

## Implementation Log

- [x] `core`: `unload_embed_model()`, a `device` parameter on `_get_embed_model` / `embed` / `embed_batch`, `_embed_device`
- [x] `model_client`: `_host_call` (retry through a new host), `_host_answers`, `_host_may_hold_gpu`, `_fallback_embed_device`, `_release_fallback_embedder`; `embed`, `embed_batch` and `HostSummarizer` use them; `HostSummarizer.skipped`
- [x] Tests, `tests/test_host_fallback.py` (9):
  - a host that died mid-call is replaced;
  - a live host that failed is not restarted;
  - the fallback goes on the CPU beside a CUDA host, and beside a host with no recorded device;
  - with no host at all, the fallback is unchanged;
  - the fallback embedder is released when the host returns;
  - summaries are skipped beside a CUDA host, not sticky, and retried after a host death;
  - `core` moves the model to the requested device.

  `test_model_host.py`'s fake `core` takes the new `device` argument. Full suite 650 passed, 1 skipped; flake8 clean on `src/`.
- [x] Reproduced before and after on CPU with a private host (see Context): client 9.13 GB with its own model beside a new host, then 0.71 GB with none.
