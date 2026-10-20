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
allocates a buffer for each thread it might use (14 there) when the library loads. `core.py` pins
faiss's OpenMP pool to one thread, but only after the import, so the buffers are already committed.
@edb measured this with `GetProcessMemoryInfo().PrivateUsage` on Windows, Python 3.14 and
faiss-cpu 1.14.3:

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
  thread runs both summaries and every embed, query embeds included, one at a time. It yields
  summaries at a batch boundary when an embed arrives.
- **the isolated summarizer subprocess** (`IsolatedChunkSummarizer`, a one-worker
  `ProcessPoolExecutor`). It is used when the host is off, which is the default on CPU, and as the
  fallback when no host can be used (ADR-048).

## Decision

1. **`OPENBLAS_NUM_THREADS` defaults to 1 before the first numpy or faiss import, in every process.**
   A new leaf module, `src/blas_threads.py`, does `os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")`
   and nothing else. It is the first import of `core.py` and of every entry module that could load
   numpy or faiss before `core`:
   - `MCPServer.py` (`code-indexer-serve`, and the TUI, which drives it);
   - `incremental_indexer.py` (`code-indexer`), which imports faiss and numpy above `core`;
   - `hybrid_retriever.py`, which does the same and is what the eval tools import first;
   - `model_host.py`, which the clients start as a subprocess.

   `setdefault` keeps an explicit value. Child processes (the model host, the summarizer worker)
   inherit the variable through the environment. It is not an `indexer.toml` knob: it has to be set
   before config could be read, and the environment variable is already the override.
2. **The isolated summarizer worker lowers its own CPU priority when it starts**, before it imports
   torch: `os.nice(10)` on POSIX and `SetPriorityClass(BELOW_NORMAL_PRIORITY_CLASS)` on Windows, both
   through the standard library (`os`, `ctypes`), with no psutil. It is the pool's initializer
   (`_isolated_worker_init`), because `ProcessPoolExecutor` takes no creation flags. A failure is
   logged and the worker runs at normal priority. **Not configurable:** nothing interactive ever waits
   on that process.
3. **The model host keeps normal priority, for now.** Its one scheduler thread runs summaries and
   query embeds, and a user waits on every query embed. Lowering the host would make searches wait
   behind the developer's foreground work too. Lowering only while summarizing is practical on
   Windows (`SetThreadPriority` goes both ways) but not on Linux, where an unprivileged thread cannot
   raise its priority back (`RLIMIT_NICE` defaults to 0). On a GPU, the CPU side of decode is mostly
   kernel launches, so CPU priority is the wrong lever anyway; see the open questions.
4. **The stretch goal, loading `sentence_transformers`/`transformers` lazily in `core`, is not
   built.** It would save nothing on the pinned transformers 4.53.3: `transformers.utils.generic`
   imports torch whenever torch is installed, so `AutoTokenizer` alone loads it. The MCP server counts
   tokens with `AutoTokenizer` on every `semantic_code_search` (`_pack_results`) and on every
   watchdog reindex (`ast_chunker`), and the shipped `[model_host].enabled = "auto"` imports torch to
   check for CUDA. Lazy imports would only move torch's load to the first search. See the open
   questions for what would actually remove it.

## Consequences

**Better:**
- On @edb's Windows stack the variable is the difference between 2.10 GB and 0.07 GB of commit for
  `import faiss`, and about 2.5 GB per process for `import core` (B-062's table). With three MCP
  servers, a model host and an indexer run, that is on the order of 12 GB of commit, and the page
  file that backs it. **Not yet re-measured on Windows with this branch;** see the open questions.
- A reindex on CPU, the default host-off path, no longer competes at equal priority with the
  developer's foreground work for the minutes its summaries take.
- A test now fails if a future import puts numpy or faiss ahead of `blas_threads` in any entry point.

**Worse:**
- Batched numpy and faiss BLAS work in those processes is single-threaded unless the user sets the
  variable. Nothing on the hot path is batched BLAS: a search sends one query per tier, and faiss
  only uses BLAS from 20 queries up (`distance_compute_blas_threshold`). The flat-search timings
  below show no change on Linux.
- On CPU, summaries take longer when the machine is busy. That is the point of B-013, but a reindex
  started during heavy foreground work now finishes later.
- One more module in `src/`, and four entry modules carry an import whose only job is its side
  effect.

**Neutral:**
- torch's own CPU threads are not BLAS (`libgomp`/MKL), so in-process CPU embedding and
  summarization keep their parallelism. `OMP_NUM_THREADS` was deliberately not touched: it would also
  make torch single-threaded.
- On Linux the faiss-cpu wheel bundles an OpenMP-threaded OpenBLAS, which ignores
  `OPENBLAS_NUM_THREADS` (it follows `OMP_NUM_THREADS`). There the variable reaches only numpy's and
  scipy's OpenBLAS. @edb's numbers show that the Windows wheel honours it.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Set the variable in `core.py` only | Too late wherever numpy or faiss is imported before `core`, as `incremental_indexer.py` and `hybrid_retriever.py` do. |
| `faiss.omp_set_num_threads` / `threadpoolctl` after import | The buffers are committed when the library loads; nothing after the import gets them back. |
| Also set `OMP_NUM_THREADS=1` | It would reach faiss's OpenMP-built OpenBLAS on Linux, but it also makes torch single-threaded: in-process CPU embedding and summarization would slow down several times. |
| A `[performance]` knob in `indexer.toml` | It has to take effect before numpy loads, which is before config is read. The environment variable is already the override. |
| Lower the whole model host (`BELOW_NORMAL_PRIORITY_CLASS` in `model_client._spawn`, `os.nice` in `serve()`) | The same thread embeds search queries. A query would wait behind foreground CPU work too. Kept as the first thing to try if @edb's measurement shows the host summarizing on GPU does hurt foreground work. |
| Lower only the host's thread while it summarizes | Windows can drop and restore a thread's priority; unprivileged Linux cannot restore it. On a GPU the CPU side is mostly kernel launches, so it would buy little. |
| Run the host's summaries in a separate low-priority worker process | Two processes on the card, which is what ADR-028's one-model-at-a-time scheduler exists to prevent. |
| `psutil.Process().nice()` | psutil is not a dependency; `os.nice` and `ctypes` do the same in a few lines. |
| Lazy `sentence_transformers`/`transformers` imports in `core` (the stretch goal) | Saves nothing in a live MCP server on transformers 4.53.3; see Decision §4. |

## Open questions for @edb

1. **Re-measure B-062 on Windows** with this branch, one command each:
   `python tools/footprint_probe.py` (PrivateUsage per import step, unset/4/1) and
   `python tools/footprint_probe.py --search --threads unset 1` (flat-search timing). The probe also
   prints each BLAS library's threading layer, which shows whether the Windows faiss wheel's OpenBLAS
   is pthreads-threaded (it should be, given your numbers).
2. **B-013 on the GPU path is unmeasured.** It could not be measured here (no GPU). To decide whether
   the host should be lowered too: run a full reindex with summaries through the host, and meanwhile
   (a) time `semantic_code_search` calls, and (b) run a CPU-heavy foreground job (a build, or
   `python -c "while 1: pass"` per core). Compare foreground job time and search latency with the host
   at normal priority and at below-normal (Task Manager → Details → Set priority on the host's
   `python.exe`). If below-normal costs searches nothing noticeable and helps the foreground job,
   lowering the host in `_spawn` is a two-line follow-up.
3. **The CPU host-off path** is what this branch changes. Worth one look: during a CPU reindex with
   summaries, the worker `python.exe` should show "Below normal" in Task Manager.
4. **Torch in the MCP server (the stretch goal).** Removing it would take two changes, both outside
   this ADR: a torch-free token counter (the `tokenizers` library loading the embedder's
   `tokenizer.json` directly, checked for identical counts against `AutoTokenizer` on real chunks,
   which needs the model files), and a CUDA check for `[model_host].enabled = "auto"` that does not
   import torch (or setting `enabled = true` explicitly on the GPU machine). Worth a backlog item?
5. **faiss's OpenMP pin covers only the thread that imported `core`.** `omp_set_num_threads` sets a
   per-thread value, and the MCP server runs tools in worker threads (ADR-047), so their searches use
   every core. Measured here: single-query flat search took 2,554 ms per 100 queries on the main
   thread and 1,510 ms on a worker thread. That is probably fine and possibly good, but it is not what
   B-062 assumed. A backlog item if you want the pin to mean what it says.

## Implementation Log

- [x] B-062: `src/blas_threads.py`, imported first in `core.py`, `MCPServer.py`,
  `incremental_indexer.py`, `hybrid_retriever.py` and `model_host.py` (one line each)
- [x] B-062: `tests/test_footprint_blas_threads.py` (15 tests). Each imports an entry module in a
  fresh interpreter with a `sys.meta_path` finder recording the variable's value when numpy, faiss or
  torch is first looked up. The cases: every entry point, an explicit value kept, a negative control
  (numpy imported before `core` is caught) and `blas_threads` staying a leaf.
- [x] B-062: `tools/footprint_probe.py`, with Linux measurements below
- [x] B-013: `_lower_priority` / `_isolated_worker_init` in `summarizer.py`;
  `tests/test_footprint_summarizer_priority.py` (10 tests). It covers both OS branches with
  `sys.platform`, `os.nice` and `ctypes.windll` mocked, failures logged rather than raised, the
  executor built with the lowering initializer, a real worker on Linux reporting nice 10, and the
  model host's spawn and summarizer load leaving priority alone on both OS branches.
- [x] Stretch: investigated and not built (Decision §4)

**Notes:**

2026-10-20, **memory, Linux.** Python 3.12.3, faiss-cpu 1.15.1, numpy 2.5.4, torch 2.14.1 (the
PyPI CUDA wheel; the CPU wheel's index was blocked), transformers 4.53.3, sentence-transformers
5.5.0, on a 4-CPU cloud container. `python tools/footprint_probe.py`, cumulative, in GB. **Linux
overcommits, so neither column is Windows commit charge:** VmSize is reserved address space, VmRSS
is pages touched. With 4 CPUs, "unset" and "4" are the same configuration and measured identically.

| Step | VmSize unset | VmSize 1 | VmRSS unset | VmRSS 1 |
|---|---|---|---|---|
| bare python | 0.02 | 0.02 | 0.01 | 0.01 |
| `import numpy` | 0.21 | 0.10 | 0.03 | 0.03 |
| `import faiss` | 0.79 | 0.67 | 0.04 | 0.04 |
| `import torch` | 3.74 | 3.63 | 0.50 | 0.50 |
| `import transformers, sentence_transformers` | 4.30 | 4.07 | 0.76 | 0.76 |
| `import core` | 4.30 | 4.07 | 0.76 | 0.76 |

The variable saves 0.23 GB of address space here and no resident memory. That is the expected Linux
result, not evidence against the Windows one: at 4 CPUs there are few buffers to save, Linux backs
them lazily, and faiss's own OpenBLAS here is the OpenMP build that ignores the variable (the
`import faiss` step costs +0.57 GB either way). The 0.11 GB saved at `import numpy` is numpy's
pthreads OpenBLAS, and the rest is scipy's, loaded via sentence-transformers.

2026-10-20, **flat FAISS search, Linux**, same stack. `python tools/footprint_probe.py --search`:
synthetic `IndexIDMap(IndexFlatIP)`, 50,000 × 1536 float32, 100 queries, top-50, faiss OpenMP
pinned to 1 as `core` does, best of 5, run twice in each order:

| `OPENBLAS_NUM_THREADS` | batch of 100 queries | 100 single queries |
|---|---|---|
| unset | 589 / 546 ms | 2,698 / 2,733 ms |
| 1 | 592 / 554 ms | 2,669 / 2,727 ms |

No slowdown. On Linux that is partly by construction, since faiss's BLAS here does not read the
variable. The Windows question is the batch column only: single queries (what a search sends, one
per tier) stay under faiss's BLAS threshold and never reach BLAS. For scale, letting faiss use 4
threads (threadpoolctl, which on this OpenMP build also lifts the OpenMP pin) gave 151–161 ms for
the batch and 1,701–1,715 ms for single queries. That is what lifting the pin would buy, not what
this variable costs.

2026-10-20, **surprise:** the Linux faiss-cpu wheel's OpenBLAS (`libopenblaso`, threading layer
`openmp`) reports 4 threads with `OPENBLAS_NUM_THREADS=1` and 1 with `OMP_NUM_THREADS=1`
(threadpoolctl). That is why decision §1 gives no faiss saving on Linux and why `OMP_NUM_THREADS` was
considered and rejected (see the alternatives).

2026-10-20, **stretch, investigated and dropped.** I built lazy imports in `core` first, with a module
`__getattr__` so tests could still patch `core.SentenceTransformer`, plus a test that `import core`
with the host on loads no torch. It passed, but tracing imports showed `from transformers import
AutoTokenizer` loads torch (`transformers/utils/generic.py:46`), and the MCP server calls it on its
first search. I reverted it rather than ship a change that saves memory only until the first search.
