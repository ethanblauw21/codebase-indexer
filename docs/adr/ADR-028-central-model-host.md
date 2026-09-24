# ADR-028: One Local Model Host Owns the GPU, With One Queue for Embeds and Summaries

**Status:** proposed
**Date:** 2026-09-24
**Branch:** `feature/adr-028-central-model-host` (cut from `feature/adr-027-summarizer-adaptive-batching` at 9f096a6)
**Reviewer:** @edb
**Backlog:** [B-023](../backlog.md#b-023) — several projects watched at once cannot share one 8 GB card.
**Depends on:**
- [ADR-027](./ADR-027-summarizer-adaptive-batching.md) — the host runs its batching loop and memory cap for summaries. Its full-pass speed-up (not measured yet) sets how long a summary window has to be.
- [ADR-029](./ADR-029-position-independent-summary-key.md) — the shared summary cache in §4 is keyed with its position-independent key.
- The single-pass fit probe (stage 9 of `gpu-crash-repro/run_stress.ps1`, 2026-09-24). **Whether the embedder and the summarizer fit on the card together decides §3.** Implementation does not start until that result is in this log.

**Depended on by:** none yet.

## Context

The indexer loads its models inside whatever process needs them. There are four entry points, and they are the whole surface:

- `core.embed()`, one query at a time, from `hybrid_retriever.py:48`
- `core.embed_batch()`, when indexing, from `incremental_indexer.py:653`
- `IsolatedChunkSummarizer.summarize_batch()`, in a child process of the indexer
- the reranker (`src/reranker.py`), off by default

That was fine for one project at a time. It stops working in the setup this machine is heading for: the watchdog daemon on for several projects, with several Claude sessions open. Three problems, in order of how soon they bite.

**1. Every MCP server loads its own embedder.** The MCP server is a stdio process started per Claude session per project. Two projects already have `repo-indexer` configured (SOPCentral and InventoryApp-V2), and two indexer servers were running at once on 2026-09-24. They only fit because `CUDA_VISIBLE_DEVICES=-1` kept them on the CPU. On the GPU, `bge-code-v1` peaks at 3.9 GiB in bf16 and about 6.2 GB in fp32 (the current `src/` default). Two projects fill the card before any summary runs.

**2. Two-pass does not hold in the daemon.** Two-pass (5f03798) keeps the summarizer and the embedder apart within one full index. But the MCP server keeps its embedder loaded after the first search or reindex, and on each save the daemon starts a summarizer worker beside it. So on this card the daemon puts both models on the GPU at once on every save with a cache miss, which is exactly what two-pass exists to avoid.

**3. Summaries are the cost, and they do not need to be immediate.** Measured on this repository on 2026-09-24: 67.0 min to summarize and 90 s to embed, at batch size 1. ADR-027's batching measured 3.03 times faster on a 200-chunk sample. Search needs an embedding right after a save. It does not need the summary right after a save, since a chunk embedded without its summary is still searchable, just less well for intent-style queries.

## Decision

### §1. One model host per machine

A single local process, the model host, owns every GPU model: embedder, summarizer, and reranker. Project processes (the MCP servers, the daemon, `code-indexer`) become its clients.

- It starts on demand. The first client that cannot reach it starts it, and a lock file keeps it to one per machine.
- It listens on `127.0.0.1` only, on a port written to a file in the user profile, and it requires a per-user token read from a file only the user can read. Any local process can open a loopback port, so loopback alone is not access control.
- Each model is loaded only while it has work and unloaded when its queue is empty (§3), so an idle host holds no GPU memory. The host process itself exits after a longer idle period.
- It never opens a project's FAISS or SQLite files. It turns text into vectors, summaries, and scores. Each project's own process still owns and writes its own index.

### §2. The client is a drop-in behind the same four functions

`src/model_client.py` provides `embed`, `embed_batch`, `summarize_batch`, and `rerank` with the signatures the callers use today. A config switch, `[model_host].enabled`, picks between the host and today's in-process loading. If the host is enabled but cannot be reached or started, the client falls back to in-process loading and says so in the log. It does not fail silently, and it does not fail the index.

### §3. Two queues, and how they share the card

- **Embed queue:** small and urgent. Search queries go first, then save-time re-embeds. Always served before summaries.
- **Summary queue:** large and deferrable. Jobs from every project collect here and run in batches through ADR-027's loop.

**One model on the card at a time, loaded only while it has work.** The two models are never resident together, even though stage 9 showed they can be (see the Implementation Log for why that is not worth using).

- **Embedder:** loaded when an embed request arrives. It stays loaded for `embed_idle_s` after the last request, because searches come in bursts and a reload per query would add seconds to each one. Then it is unloaded.
- **Summarizer:** loaded only when the summary queue has work and the embed queue is empty. It drains the queue at whatever batch size ADR-027's loop reaches, with the whole card to itself, and is unloaded as soon as the queue is empty.
- **When both have work** (the "lock the door" pass): embeds go first. Then the embedder is unloaded and the summarizer runs for up to `summary_window_s`, and is unloaded again so the embedder can serve whatever arrived. This repeats while summaries remain. The window has a cap because a search that arrives during a window cannot be answered until it ends.

A swap looks cheap next to a window. On 2026-09-24 the summarizer took about 14 s from process start to loaded, including spawning the process, and the embedder reloaded in about 2 to 4 s with its weights in the OS file cache. These are readings from the monitor, not a timed benchmark, and the implementation should time them properly.

Queries that arrive during a summary window are an open question, listed below.

### §4. Summaries are shared across projects

The host keeps one summary cache in the user profile, keyed by ADR-029's position-independent key. The same chunk text gets the same summary in any project, so vendored or copied code is summarized once. Each project's own `chunk_summaries` table becomes a local copy that the client fills from the host.

### §5. Save path: embed now, summarize later

On a save, the project process re-chunks the file and embeds it straight away. It uses cached summaries where they exist and bare chunk text where they do not. Chunks embedded without a summary are marked pending, and their summary jobs go to the host. When a summary lands, the host tells the client, and the client re-embeds just those chunks with the summary appended. That re-embed goes through the embed queue at a lower priority than searches.

Search stays current within seconds of a save. For the minutes until the summaries catch up, the chunks it searches are the bare ones.

### §6. Visible state

The host reports its mode, loaded models, memory in use, and each queue's depth, per project. `index_status` includes the host's view for its project, including how many chunks are pending a summary, so an agent can tell a stale index from a busy one.

### Open questions, to settle before implementation

1. ~~Resident or swap mode.~~ Settled 2026-09-24: one model at a time, loaded on demand (§3).
2. **`summary_window_s` and `embed_idle_s`:** depends on ADR-027's full-pass throughput and the measured swap cost. Not guessed here.
3. **Queries during a swap window:** wait for the window to end, or answer from a CPU copy of the embedder. The CPU copy costs several GB of RAM and a slower query. Waiting costs up to one window.
4. **Transport:** HTTP on loopback is the simplest to build and to test with ordinary tools. A named pipe avoids holding a port. Leaning HTTP.
5. **Other GPU users:** the host's memory cap (ADR-027 §2) leaves a reserve, but a game or another ML job can still take the card. Whether the host should back off entirely when something else is using the GPU is unanswered.

## Consequences

**Better:** one copy of each model on the card however many projects or sessions are open. Saves update search in seconds. Summaries batch across projects, which is where ADR-027's batching gains the most. The same code is summarized once across repos.

**Worse:** a long-running local service, with a lifecycle, a port, a token, and failure modes the per-process design does not have. Index freshness now has two parts, embedded and summarized, and both need to be visible. Debugging a slow search can now mean looking at another process.

**Neutral:** with `[model_host].enabled = false` the indexer behaves as it does today, which keeps CI, CPU-only machines, and single-project use unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep per-process models, add a machine-wide GPU lock file | Serializes GPU use but still loads one embedder per process, so two projects still do not fit. No batching across projects and no shared cache. |
| An existing serving runtime (Ollama, TEI, vLLM) as the host | A second inference stack. TEI and vLLM have no native Windows support, and none of them handle our summary queue, cache, or swap policy, which is most of the value. Possible later as the engine inside the host. |
| Embed on the CPU and keep the GPU for summaries | A full index would go back to CPU embedding speeds, which is the thing that made this machine's GPU worth fixing. |
| One daemon that indexes every project itself | Moves index ownership out of each project and duplicates the MCP server's work. The host only needs the models. |

## Verification

To be completed once the open questions are settled. At minimum:

1. Two projects watched and one search session each, on the GPU: only one embedder resident, measured with `nvidia-smi` and the WDDM shared-usage counter.
2. A save in each project: search reflects the change within a few seconds, and pending summaries drain to zero.
3. Swap mode, if chosen: a search during a window returns within the chosen bound.
4. Host unreachable: the client falls back to in-process and logs it, and the index still completes.
5. The token is required: a request without it is rejected.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] **Gate:** record the stage-9 single-pass fit result here and settle open question 1 (see Notes)
- [ ] **Gate:** record ADR-027's full-pass throughput (stage 7) and the measured model swap cost; settle open question 2
- [ ] Settle open questions 3 to 5
- [ ] Implementation tasks, to be written once the gates are cleared
- [ ] Set status to `accepted` in the PR

**Notes:**
<!-- 2026-09-24: Written before the stage-9 result. Nothing is to be built until the gates above are recorded. -->

- 2026-09-24, **stage 9, single-pass fit probe** (`master` at 18a7059, summarizer at batch size 1 through the pipeline, embedder bf16 via the stress-kit patch): both models loaded together and a 73-chunk embed batch completed. Dedicated memory reached 7,862 MiB of 8,151, and shared usage went from 64 to 128 MiB, under the 512 MiB abort line. So they fit, with about 290 MiB to spare, and only with the summarizer at batch size 1. With ADR-027 batching the summarizer peaked at 4.76 GB on its own, which with the embedder comes to about 8.7 GB, and it would also leave no room for ADR-027's 1 GB reserve.
- 2026-09-24, **decision (@edb):** do not keep the models resident together. Load each on demand and unload it when its queue is empty (§3). Resident mode would give up batching's measured 3.03 times speed-up to save a swap of a few seconds, and it would leave nothing on the card for the desktop or a burst of searches.
