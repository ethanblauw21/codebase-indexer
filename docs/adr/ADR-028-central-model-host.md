# ADR-028: One Local Model Host Owns the GPU, With One Queue for Embeds and Summaries

**Status:** proposed
**Date:** 2026-09-24
**Branch:** `feature/adr-028-central-model-host` (rebased 2026-09-24 onto `feature/adr-027-summarizer-adaptive-batching` at 64a38be, the token-budget build)
**Reviewer:** @edb
**Backlog:** [B-023](../backlog.md#b-023) — several projects watched at once cannot share one 8 GB card.
**Depends on:**
- [ADR-027](./ADR-027-summarizer-adaptive-batching.md) — the host runs its batching loop and memory cap for summaries, and this ADR adds one hook to that loop (`should_yield`, §3).
- The bf16 embedder change (not written yet, its own branch). `src/core.py` loads `bge-code-v1` in fp32, about 6.2 GB. Even alone, that does not leave ADR-027's 1 GB reserve on an 8 GB card. **The host does not get turned on for real until that change lands.**

**Depended on by:** the follow-up ADR for §4 and §5 below. It is not written yet, and it gets created on the branch that builds it.

## Context

The indexer loads its models inside whatever process needs them. There are four entry points, and they are the whole surface:

- `core.embed()`, one query at a time, from `hybrid_retriever.py`
- `core.embed_batch()`, when indexing, from `incremental_indexer.py`
- `IsolatedChunkSummarizer.summarize_batch()`, in a child process of the indexer
- the reranker (`src/reranker.py`), off by default

That was fine for one project at a time. It stops working in the setup this machine is heading for: the watchdog daemon on for several projects, with several Claude sessions open. There are three problems, listed by how soon they cause trouble.

**1. Every MCP server loads its own embedder.** The MCP server is a stdio process started per Claude session per project. Two projects already have `repo-indexer` configured (SOPCentral and InventoryApp-V2), and two indexer servers were running at once on 2026-09-24. They only fit because `CUDA_VISIBLE_DEVICES=-1` kept them on the CPU. On the GPU, `bge-code-v1` peaks at 3.9 GiB in bf16 and about 6.2 GB in fp32. Two projects fill the card before any summary runs.

**2. Two-pass does not hold in the daemon.** Two-pass (5f03798) keeps the summarizer and the embedder apart within one full index. But the MCP server keeps its embedder loaded after the first search or reindex. On each save, the daemon then starts a summarizer worker beside it. So on every save with a cache miss, the daemon puts both models on the GPU at once, which is what two-pass exists to avoid. Stage 9 showed what that looks like:
- both models fit only with the summarizer at batch size 1
- dedicated memory stayed pinned at 7,843 of 8,151 MiB
- shared usage spiked up to 384 MiB, which is the early shape of WDDM paging

**3. Summaries are the cost, and they do not need to be immediate.** Measured on this repository on 2026-09-24, pass 1 (summaries) against pass 2 (embed and write):

| Build | Pass 1 | Pass 2 |
|---|---|---|
| batch size 1 | 67.0 min | ~1.5 min |
| ADR-027 cross-file | 17.2 min | 96 s |
| ADR-027 token budget (stage 7) | 16.3 min (975.5 s) | 98 s |

Search needs an embedding right after a save. It does not need the summary right away. A chunk embedded without its summary can still be searched. It just matches intent-style queries less well.

## Decision

### §1. One model host per user

A single local process, the model host (`src/model_host.py`), owns every GPU model. Project processes are its clients: the MCP servers, the watchdog and `code-indexer`.

- **Started on demand.** The first client that cannot reach it starts it, detached, with its output in `host.log`.
  - A lock file (`host.lock`, held with `msvcrt.locking` or `flock`) keeps it to one per user.
  - The OS drops the lock when the process dies, so a crashed host leaves nothing stale behind.
- **Found through files.** Everything lives in `%LOCALAPPDATA%\code-indexer\model-host\`. `CODE_INDEXER_HOST_DIR` overrides it, which is what the tests use. The files:
  - `host.json`: the port and pid, written after the port is bound
  - `token`
  - `host.lock`
  - `host.log`
- **Loopback plus a token.** HTTP on `127.0.0.1` only, on a free port.
  - Every request carries `Authorization: Bearer <token>`, compared in constant time. Any local process can open a loopback port, so loopback alone is not access control.
  - The token file is created once with `secrets.token_urlsafe(32)`, and the directory's ACL already limits it to the user.
  - Nothing logs or prints the value.
- **Models in the host's own process.** The embedder loads through `core._get_embed_model()` and the summarizer through ADR-027's `_worker_init()`, so both behave exactly as they do in the indexer.
  - Today the summarizer runs in a child process for crash isolation. In the host, the host process provides that isolation: if it dies, clients fall back (§2) and the next request starts a new one.
  - Running the summarizer in-process is also what lets an embed stop it at a batch boundary (§3).
- **Idle exit.** With no model loaded and nothing queued for `idle_exit_s`, the host exits.
- **No index files.** It never opens a project's FAISS or SQLite files. It turns text into vectors and summaries, and each project's own process still writes its own index.

Endpoints:

| Endpoint | Takes | Returns |
|---|---|---|
| `POST /v1/embed` | `texts`, `kind` (`query` or `index`), `project` | float32 vectors as base64, with their shape |
| `POST /v1/summarize` | `codes`, `project` | one summary per code |
| `GET /v1/status` | | the §6 state |
| `POST /v1/shutdown` | | |

### §2. The client is a drop-in behind the same calls

`src/model_client.py` provides `embed`, `embed_batch` and `make_summarizer()` with the signatures the callers use today. `hybrid_retriever.py` and `incremental_indexer.py` import from it instead of from `core` and `summarizer`. `[model_host].enabled` picks between the host and today's in-process loading, and defaults to false.

- **Fallback.** If the host is enabled but cannot be reached or started within `spawn_timeout_s`, the call runs in-process.
  - The client prints one line saying so. It does not fail silently, and it does not fail the index.
  - After a failure it skips the host for 60 s, so a dead host costs one timeout rather than one per file.
  - The first time the host fails, `HostSummarizer` switches to a real `IsolatedChunkSummarizer` for the rest of the run. That way one run never has the host and a worker loading models at the same time.
- **Model check.** The host reads the `indexer.toml` of whichever project started it.
  - A client compares the host's embedder id, dimension and summarizer id with its own config, and falls back if any of them differ.
  - Without this check, vectors from another embedder would load into the index without error and be wrong.
- **Reranker.** Stays in-process for now. It is off by default. Moving it to the host is a later task in the log.

### §3. Two queues, one model on the card at a time

`HostScheduler` holds the policy, and it is plain Python so it can be tested with a fake backend. Clients submit from any thread. Only the scheduler's thread touches the models, so two models can never be loaded at once.

- **Embed queue.** Small and urgent.
  - Always served before summaries.
  - Queries go before save-time and indexing embeds. Within each kind, first come first served.
  - Waiting requests of the same kind are combined into one model call.
- **Summary queue.** Large and can wait. Every project's pending texts are merged into one run, deduplicated by text, and sent through ADR-027's loop longest first.
- **Embedder lifetime.** Loaded when an embed arrives. It stays loaded for `embed_idle_s` after the last request, because searches come in bursts and a reload per query would add seconds to each one. Summaries wait during that time.
- **Summarizer lifetime.** Loaded once the embedder is unloaded and summaries are waiting. It stays loaded for `_SUMMARY_LINGER_S` after its queue empties. The indexer sends pass 1 in slices of 192, and unloading between slices would reload the model for every slice.
- **Preemption at the batch boundary** (settled 2026-09-24, @edb). `run_adaptive_batches` takes a `should_yield` callback and calls it before every batch after the first. The host's callback returns true when an embed is waiting. When it does:
  - the run stops before the next batch
  - every text not yet attempted comes back as `None`, not `""`, which means attempted and empty
  - the embed is served, and the rest of the summaries run later, so nothing is summarized twice
  - A search that arrives during a summary run waits for the batch in flight, then a summarizer unload, then an embedder load. **How long that takes has not been measured** (see the gaps table).

The earlier draft had a `summary_window_s` setting that capped each summary run so searches could get in. Preemption makes it unnecessary, so it has been removed.

**Other GPU users** (open question 5 in the earlier draft). The host does not watch for other programs. ADR-027's memory cap and pause already handle another program taking memory: the summarizer backs off and waits, and drops to batch size 1 if the memory does not come back. Backing off entirely, for example while a game runs, is left until it is needed.

### §4. Deferred: summaries shared across projects

This is a shared summary cache in the user profile, keyed by ADR-029's position-independent key, so vendored or copied code is summarized once across repos. It moved out of this ADR on 2026-09-24 (@edb), because it needs ADR-029 and can be reverted on its own. The host already deduplicates across projects within one run, which is the cheap half.

### §5. Deferred: embed now, summarize later on save

On a save, the indexer would:
1. embed at once, using cached summaries where they exist and bare text where they do not
2. mark the rest pending
3. re-embed each chunk when its summary lands

This also moved out, because it changes the indexer's save path and adds a pending state to the index, and it can be built and reverted separately. Until it lands, a save through the host behaves as it does today, summaries first and then the embed. A search that arrives meanwhile preempts the summaries.

### §6. Visible state

`GET /v1/status` reports:
- the loaded model
- each project's queued embed texts and pending summaries
- counters: embeds, summaries, yields, loads, unloads
- the last few load times per model
- the device and the model ids

Surfacing this in `index_status` is a task in the log.

## Measured values this ADR still needs

These get filled in from stress-kit stages as they run. Nothing here is guessed. The provisional defaults are marked in `config.py` and `indexer.toml`.

| Value | Used for | Source | Result |
|---|---|---|---|
| Embedder load time, bf16, weights in the OS file cache | `embed_idle_s` (provisional 60 s) | host `status.load_seconds` | 4.1–5.5 s warm, 9.6 s the first time (5 runs, 2026-09-25) |
| Summarizer load time in the host process | `_SUMMARY_LINGER_S` (provisional 15 s) | host `status.load_seconds` | 2.6–4.3 s, one 6.2 s (5 runs) |
| Longest single batch at the token budget | worst-case search wait under preemption | per-batch timing, not logged yet | _gap_ |
| Search latency during a summary run | Verification 3 | a host test run | 8.6–24.6 s (13 probes across 4 lru-cache runs and the two-project run); about 5 s of that is the swap, the rest the batch in flight |
| Memory after unloading each model | whether an idle host really holds nothing | `nvidia-smi` during a host run | 119 MiB, the CUDA context, with torch reserving 0 (after 7508ead; 3,511 MiB before it, see Notes) |
| Full pass 1 through the host vs in-process | whether the HTTP hop costs anything that matters | a whole lru-cache index, summaries on, a search every 90 s | 308.9 s through the host (after both fixes) against 284.6 s in-process: +8.5% |
| Whether summary batching changes retrieval | whether batched summaries are safe to share | ADR-027 stages 13 to 16 | _gap_ (running) |

## Consequences

**Better:**
- One copy of each model on the card, however many projects or sessions are open.
- Summaries batch across projects, which is where ADR-027's batching gains the most.
- Identical texts are summarized once per run.
- A search is never stuck behind a whole summary pass, only behind one batch.

**Worse:**
- A long-running local service, with a lifecycle, a port, a token, and failure modes the per-process design does not have.
- The summarizer loses its own crash isolation inside the host. A native crash takes the host down with it, and every client falls back until the next start.
- Debugging a slow search can now mean looking at another process.
- The host takes the config of whichever project started it. Projects configured with different models cannot share it, and they fall back to in-process, with one log line saying so.

**Neutral:** with `[model_host].enabled = false` the indexer behaves as it does today. CI, CPU-only machines and single-project use are unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep per-process models, add a machine-wide GPU lock file | Serializes GPU use, but still loads one embedder per process, so two projects still do not fit. No batching across projects. |
| Keep both models resident together | Fits only at summarizer batch size 1 (stage 9), with the card at its limit and early paging. Gives up batching's speed-up to save a swap of a few seconds. |
| A fixed summary window instead of preemption | A search could wait a full window. Preemption limits the wait to one batch and needs no tuning setting. |
| A CPU copy of the embedder for searches during a summary run | Several GB of RAM, 0.2 to 2.7 s per query on this CPU, and vectors from a different device and dtype. |
| Named pipe instead of HTTP | No port held, but more code and harder to test by hand. |
| An existing serving runtime (Ollama, TEI, vLLM) as the host | A second inference stack. TEI and vLLM have no native Windows support. None of them handle our summary queue or swap policy, which is most of the value. Possible later as the engine inside the host. |
| Embed on the CPU and keep the GPU for summaries | A full index would go back to CPU embedding speed: about 32 min instead of about 1.5 on this repository. |
| One daemon that indexes every project itself | Moves index ownership out of each project and duplicates the MCP server's work. The host only needs the models. |

## Verification

1. Two projects watched and one search session each, on the GPU: at most one embedder resident, measured with `nvidia-smi` and the WDDM shared-usage counter.
2. A save in each project: the index completes through the host, and `status` shows both projects' summaries drain to zero.
3. A search during a summary run returns within one batch plus a swap. The bound comes from the gaps table.
4. Host unreachable: the client falls back to in-process, prints one line, and the index still completes. `tests/test_model_host.py` covers this with a host that never starts. Killing a real host mid-run is still to do.
5. The token is required: a request without it gets a 401. Covered by `tests/test_model_host.py`.
6. Retrieval through the host matches in-process on the real-repo eval. The vectors should be identical, since the same loader runs.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] **Gate:** record the stage-9 single-pass fit result here and settle open question 1 (see Notes)
- [x] Settle open questions 3 to 5 (grill, 2026-09-24): preempt at the batch boundary, HTTP on loopback, no watching for other GPU users. §4 and §5 split out.
- [x] `should_yield` hook in `run_adaptive_batches` and `_worker_summarize`, and `BatchStats.yielded`
- [x] `src/model_host.py`: `HostScheduler`, `TorchBackend`, HTTP server, token, lock, `host.json`
- [x] `src/model_client.py`: drop-ins, spawn, fallback, model check, `HostSummarizer`
- [x] Callers switched: `hybrid_retriever.py` (`embed`), `incremental_indexer.py` (`embed_batch`, `make_summarizer`)
- [x] `[model_host]` config (`enabled`, `embed_idle_s`, `idle_exit_s`, `spawn_timeout_s`) and the drift test
- [x] `tests/test_model_host.py`: policy with a fake backend and clock, HTTP with the token, client fallback and model check. Suite: 346 passed, plus the 6 snapshot failures that were already failing.
- [x] **Gate:** bf16 embedder landed: ADR-035 (#39), merged to `master` 2026-09-25. The host loads the embedder through `core._get_embed_model()`, so it gets bf16 (2,944 MiB) with no change here
- [x] First real run on the GPU: fill the gaps table (load times, memory after unload, pass 1 through the host). It found two bugs, both fixed: summaries starved by steady searches (db8dc77) and an idle host holding 3.5 GB (7508ead). See Notes
- [ ] Per-batch timing in `BatchStats` (the longest batch's seconds), for the preemption bound
- [ ] Watchdog: confirm the MCP server's save path goes through the client end to end. It calls `run_incremental`, so it should.
- [ ] `index_status` shows the host's view for its project
- [ ] Reranker through the host
- [x] MCP Inspector run on the MCP server with the host enabled (the global CLAUDE.md rule for a changed MCP server). See Notes
- [x] Verification 1, 2 and 6 on the GPU; 3 measured (the bound it gives is in Notes); 4 and 5 by the unit tests
- [ ] Verification 4 on a real host: kill it mid-run and check the index still completes
- [ ] Set status to `accepted` in the PR

**Notes:**

- 2026-09-24, **stage 9, single-pass fit probe** (`master` at 18a7059, summarizer at batch size 1 through the pipeline, embedder bf16 via the stress-kit patch): both models loaded together and a 73-chunk embed batch completed. Dedicated memory reached 7,862 MiB of 8,151, and shared usage went from 64 to 128 MiB, under the 512 MiB abort line. So they fit, with about 290 MiB to spare, and only with the summarizer at batch size 1. With ADR-027 batching the summarizer peaked at 4.76 GB on its own, which with the embedder comes to about 8.7 GB, and it would also leave no room for ADR-027's 1 GB reserve.
- 2026-09-24, stage 9 over its full 10 min: with both models loaded, dedicated memory stayed pinned at 7,843 MiB while shared usage kept jumping between 64 and 384 MiB and back. That is the early shape of WDDM paging, short spills that return. It never crossed the 512 MiB abort line, but the card was at its limit the whole time. No PCIe replays, no WHEA events. Telemetry: `gpu-crash-repro/telemetry/stress_20260924_155718`.
- 2026-09-24, **decision (@edb):** do not keep the models resident together. Load each on demand and unload it when its queue is empty (§3). Resident mode would give up batching's measured 3.03 times speed-up to save a swap of a few seconds, and it would leave nothing on the card for the desktop or a burst of searches.
- 2026-09-24, **grill (@edb):** building now.
  - A search that arrives during a summary run preempts it at the next batch boundary, which removes `summary_window_s`.
  - HTTP on loopback.
  - The shared cache (§4) and the save path (§5) go to a follow-up ADR, so this one can be reverted on its own.
  - The branch was first rebased onto ADR-027's token-budget tip (64a38be).
- 2026-09-24, **deviation:** the summarizer runs inside the host process instead of a child process. The child existed to protect the indexer from a native crash, and the host process now provides that protection. Running in-process also allows a batch-boundary yield without a second protocol between the host and a worker.
- 2026-09-24, **stage 7 on the token budget** (ADR-027 at 64a38be): pass 1 975.5 s for 1,496 distinct texts, largest batch 48, 0 OOM, peak 5,898 MiB, no paging (shared usage flat at 64 MiB). That is only 5% faster than cross-file (1,030 s).
  - From 19:26 to the end, the card sat in P4 at about 1,200 MHz and 30 W while reporting 95% utilization. That looks like the short-chunk tail being limited by kernel launches rather than by the GPU.
  - Not investigated yet. It belongs in ADR-027's log. It matters here only because it sets how long the host's summary runs take.
- 2026-09-25, **first real GPU runs** (`gpu-crash-repro/host_run.py`; runs in `telemetry/host028_runs.jsonl`, logs `telemetry/host028*.log`). Setup: `master` merged in (cf52430), so bf16 embedder (ADR-035), ADR-030's summary index and ADR-034's chunks. Each run is a fresh index of lru-cache (v11.5.3) with summaries on. The host runs use a scratch `CODE_INDEXER_HOST_DIR`, and a probe thread embeds a search while the index builds.
  - **Bug 1: summaries starved by steady searches.** The first run probed every 15 s. Each probe kept the embedder warm for another `embed_idle_s` (60 s), and summaries waited while it was warm, so they never ran. The GPU sat at 0% with the embedder loaded and the indexer blocked. Fix (db8dc77): a summary job waits behind a warm embedder for at most `embed_idle_s` from when it arrived. After that the summarizer loads, and later searches preempt it at a batch boundary as designed. New test: `test_steady_searches_cannot_hold_summaries_back_forever`, which fails on the old code.
  - **Bug 2: an idle host held 3,511 MiB.** After both models unloaded, `nvidia-smi` still showed 3,511 MiB, all in the host (Windows' per-process GPU counter). torch reported 9 MiB allocated and 3,392 MiB reserved, and a census of live CUDA tensors found none. Unloading alone freed everything in isolation (`unload_check.py`, `unload_check2.py`, `unload_check3.py`: 139 MiB left), so something in a full build kept segments pinned. Cause: cuBLAS workspaces are allocated through the caching allocator and land inside the model's big segments, so `empty_cache` cannot release those segments. Fix (7508ead): `torch._C._cuda_clearCublasWorkspaces()` before `empty_cache`. After it: 119 MiB (the CUDA context), reserved 0.
  - `/v1/status` now reports `cuda_allocated_mib` and `cuda_reserved_mib`. A debug census at `/v1/debug/cuda-tensors` (only with `CODE_INDEXER_HOST_DEBUG`) walks every object in the process. The first version sat inside `/v1/status` and made it slow enough that the client timed out, declared the host dead, and silently embedded in-process. So a slow status answer is an outage from the client's side; keep status cheap.
  - **Wall time, lru-cache:** in-process 284.6 s (peak 5,629 MiB). Through the host: 336.4 s after fix 1, then 326.6 and 326.0, then **308.9 s after fix 2** (peak 5,691 MiB), which is +8.5%. The overhead is the design's own waits, not the HTTP hop: 8 loads at about 4–5 s each, plus summaries holding up to 60 s behind an embedder a probe just warmed. Real use with fewer searches during an index should see less.
  - **Load times:** embedder 4.1–5.5 s warm (9.6 s the first time), summarizer 2.6–4.3 s (one 6.2 s). A swap costs about 5 s, far under the 60 s idle window, so `embed_idle_s` could come down if searches during indexing matter more than reload cost. Left at 60 s.
  - **Verification 3, search during a summary run:** 8.6–24.6 s over 13 probes. About 5 s is the swap. The rest is the batch in flight, so the longest batch is up to about 20 s. The per-batch timing task stays open: it is what would let the host cap a batch's length when searches are waiting.
  - **Verification 6, vectors:** the host-built index matches the in-process one bit for bit: tier 1 213/213 identical, tier 2 71/71, tier 3 48/48.
  - **Verifications 1 and 2, two projects at once** (p-queue as `proj-pqueue`, zustand as `proj-zustand`, one host): both exit 0. GPU peak 5,783 MiB, so one model on the card at a time held. p-queue finished in 416.4 s and zustand in 508.9 s, while sharing the card. The host's status showed zustand's 94 pending summaries while p-queue finished, then both drained to zero. 5 yields, 12 loads.
- 2026-09-25, **MCP Inspector with the host enabled** (`gpu-crash-repro/telemetry/inspector_028/`). The server ran as on this branch, against a scratch p-queue index. The only changes: `[model_host].enabled` turned on, and a scratch host directory.
  - `tools/list --strict` exits 0.
  - `semantic_code_search("pause the queue so no new tasks start")` returns `PQueue.pause` first. **The host served the query:** its status afterwards showed 2 embeds and 1 load, and there was no in-process fallback line.
  - `index_status` answers.
  - A search with no `query` returns `isError: true` (exit 5).
- 2026-09-25, **the watchdog under an MCP client** (the Watchdog task above) turned up two bugs that had nothing to do with the host. Every watchdog reindex failed on its first print (a cp1252 pipe), and the summarizer's `multiprocessing` worker hung on the MCP stdin pipe. Both are fixed in ADR-036 (#40). The host's own spawn already passes `stdin=DEVNULL`, so it is not affected.
