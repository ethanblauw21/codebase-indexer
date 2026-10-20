# ADR-054: Lean Indexer Processes and a Polite Background Summarizer

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-054-process-footprint`
**Reviewer:** @edb
**Backlog:** [B-062](../backlog.md#b-062) (OpenBLAS thread buffers), [B-013](../backlog.md#b-013) (the summarizer competes with foreground work)
**Depends on:** none
**Depended on by:** none yet

## Context

**B-062.** On @edb's machine every indexer process commits about 3.6 GB of private memory before it
loads a model, and 2.1 GB of that is `import faiss`. The faiss-cpu wheel bundles OpenBLAS, which
allocates a buffer for every thread it might use (14 there) when the library loads. `core.py` pins
faiss's OpenMP pool to one thread, but only after the import, so the buffers are already committed.
Measured by @edb with `GetProcessMemoryInfo().PrivateUsage` on Windows, Python 3.14, faiss-cpu 1.14.3:

| `OPENBLAS_NUM_THREADS` | `import faiss` | `import core` |
|---|---|---|
| unset | 2.10 GB | 3.58 GB |
| 4 | 0.54 GB | 1.69 GB |
| 1 | 0.07 GB | 1.12 GB |

Three MCP servers and a model host held about 17 GB of commit between them, and Windows grew
`pagefile.sys` to 58 GB to back it.

**B-013.** With `[summarization].enabled = true`, every reindex generates a summary per new chunk with
a 1.5B model. That is the indexer's dominant cost, and it runs at normal priority beside whatever the
developer is doing. Summarization runs in one of two places:

- **the model host** (ADR-028), the default on a GPU (`[model_host].enabled = "auto"`). One scheduler
  thread runs both summaries and every query embed, one at a time, and yields summaries at a batch
  boundary when an embed arrives;
- **the isolated summarizer subprocess** (`IsolatedChunkSummarizer`, a one-worker
  `ProcessPoolExecutor`), used when the host is off (the default on CPU) and as the fallback when no
  host can be used (ADR-048).

## Decision

1. **`OPENBLAS_NUM_THREADS` defaults to 1 before the first numpy or faiss import, in every process.**
   A new leaf module, `src/blas_threads.py`, does `os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")`
   and nothing else. It is the first import of `core.py` and of each entry module that could pull in
   numpy or faiss before `core`: `MCPServer.py`, `incremental_indexer.py`, `hybrid_retriever.py` and
   `model_host.py`. `setdefault` keeps an explicit value. Child processes (the model host, the
   summarizer worker) inherit the variable through the environment.
2. **The isolated summarizer worker lowers its own priority when it starts**: `os.nice(10)` on POSIX,
   `SetPriorityClass(BELOW_NORMAL_PRIORITY_CLASS)` on Windows, before it imports torch. Not
   configurable: nothing interactive ever waits on that process.
3. **The model host keeps normal priority.** Its one scheduler thread also embeds every search query.
   See the alternatives below and the open questions.

## Consequences

To be completed as the build lands.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Set the variable in `core.py` only | Too late wherever numpy or faiss is imported before `core`, as `incremental_indexer.py` and `hybrid_retriever.py` do. |
| `faiss.omp_set_num_threads` / `threadpoolctl` after import | The buffers are committed when the library loads; nothing after the import gets them back. |
| Lower the whole model host (`BELOW_NORMAL_PRIORITY_CLASS` in `model_client._spawn`) | The same thread embeds search queries, which a user waits on. Under foreground CPU load a query would wait too. |
| Lower only the summarizing thread in the host | See the Implementation Log. |

## Implementation Log

- [ ] B-062: leaf module, imported first in `core` and the entry modules
- [ ] B-062: subprocess test that the variable is set before numpy/faiss on every entry point
- [ ] B-062: Linux memory and FAISS-speed measurements, and a `tools/` probe for Windows
- [ ] B-013: lower the isolated summarizer worker; tests for both OS branches
- [ ] Stretch: lazy `sentence_transformers`/`transformers` imports in `core`

**Notes:**
