# ADR-020: Unified Device Resolution Across the Local Model Stack

**Status:** accepted
**Date:** 2026-07-17
**Branch:** `feature/adr-020-unified-device-resolution`
**Reviewer:** @ethanblauw21
**Depends on:** ADR-024 — the shared `resolve_device()` resolver (`src/device.py`) that this ADR extends to the two model components ADR-024 deliberately left untouched.
**Depended on by:** none yet

> **Supersession note.** This replaces the original ADR-020 (*"GPU Acceleration for the Local Model Stack (AMD / DirectML / ROCm)"*, `ADR-020-gpu-acceleration-amd.md`, on branch `feature/adr-020-gpu-acceleration`, never merged to master). All three premises of that ADR are now dead — see Context. The ADR number is retained; the old file and branch are superseded and should be closed.

## Context

The original ADR-020 (2026-06-23) proposed a local GPU execution path — DirectML or ROCm-under-WSL2 — to make bulk embedding (specifically the ADR-009 §P1 embedder-swap validation, measured at ~1 s/doc ≈ 5.5 days for the CoIR core set on CPU) run at same-day cadence. Every premise it rested on has since changed:

1. **The hardware is gone.** That ADR targeted an AMD RX 6700 XT (RDNA2, gfx1031) and its entire investigation — DirectML op-coverage, the `HSA_OVERRIDE_GFX_VERSION=10.3.0` ROCm override — was AMD-on-Windows plumbing. The dev machine is now an NVIDIA (Blackwell) card. CUDA is the native path; the AMD investigation is moot.
2. **The actual need is already solved — in the cloud, not locally.** The `cloud/` spot-T4 harness (ADR-019 infrastructure) runs bulk embedding and reindexing on a self-deleting GPU VM for pennies. ADR-009 §P1 (bge-code-v1) was in fact validated that way, on a T4, not on a local GPU. The "CPU can't validate a 1.5B embedder at reasonable cadence" problem — the whole reason the old ADR-020 existed — is a **cloud** concern now. Local bulk-GPU embedding is no longer needed for it.
3. **The local GPU is currently unusable.** Pinning it causes kernel-level crashes on the dev machine, so *any* local GPU workload is off the table for now. A "use the card on hand" ADR cannot be actioned.

So the original ADR-020's reason to exist is fully obsolete. But rescoping it surfaces a real, still-open problem that ADR-024 left behind — and that the crash situation makes urgent.

**What ADR-024 actually did, and the gap it left.** ADR-024 introduced `resolve_device()` (`src/device.py`): return `CODE_INDEXER_DEVICE` if set, else `"cuda"` if available, else `"cpu"`. It wired that into the **reranker** (`src/reranker.py`) and the **eval harness** (`tools/real_repo_eval.py`), defaulting both to auto-CUDA. It deliberately did **not** touch the embedder or summarizer, on the reasoning that both already auto-select CUDA on their own. That reasoning is correct for *auto-detection* but wrong for *control*. On master today:

- **Embedder** (`src/core.py:66`): `SentenceTransformer(model_id, trust_remote_code=True)` — no `device=` argument, so sentence-transformers picks CUDA when present. Never consults `resolve_device()` or `CODE_INDEXER_DEVICE`.
- **Summarizer** (`src/summarizer.py:169,187`): defaults `device="auto"` and independently picks `float16 if torch.cuda.is_available() else float32`. Never consults `resolve_device()` or `CODE_INDEXER_DEVICE`.
- **Reranker + eval**: governed by `resolve_device()` (ADR-024).

The consequence is a genuine footgun. `src/device.py`'s own docstring calls `resolve_device()` the *"single source of truth for which device GPU-capable components run on"* — but it governs only **two of the four** components, and neither of the two it governs is the one that runs during an ordinary reindex. **Setting `CODE_INDEXER_DEVICE=cpu` does not make indexing CPU-only**, because the embedder — which runs on every index — never reads it. The only reliable local kill-switch today is `CUDA_VISIBLE_DEVICES=""`, which hides the GPU from torch entirely. A user who sets the variable that *looks* like the device control, and reasonably believes they are now CPU-safe, is not. On a machine where GPU use kernel-panics, that gap is a crash waiting to happen.

## Decision

Make `resolve_device()` the **actual** single source of truth for every local model load, so that `CODE_INDEXER_DEVICE` is one authoritative, documented control over the whole stack.

1. **Thread `resolve_device()` into the embedder** (`src/core.py` `_get_embed_model()`): pass `device=resolve_device()` to the `SentenceTransformer` constructor, preserving the existing lazy-load singleton and the `max_seq_length` cap. This is the load that runs on every index — the one that matters most.
2. **Thread `resolve_device()` into the summarizer** (`src/summarizer.py`): default its `device` to `resolve_device()` instead of the literal `"auto"`, and key its `float16`/`float32` choice off the **resolved** device (`== "cuda"`) rather than raw `torch.cuda.is_available()`, so `CODE_INDEXER_DEVICE=cpu` also forces `float32` and avoids float16-on-CPU quirks.
3. **Keep ADR-024's default unchanged** — auto-CUDA (`"cuda"` if available, else `"cpu"`). This ADR does not reverse that fleet-oriented choice; it makes the *override* authoritative. No regression for CUDA machines: with no env var set, every component still auto-selects CUDA exactly as today.
4. **`CODE_INDEXER_DEVICE` becomes a whole-stack kill-switch.** After this change, `CODE_INDEXER_DEVICE=cpu` makes embedder, reranker, summarizer, and eval all run on CPU — a single reliable guard that replaces the `CUDA_VISIBLE_DEVICES=""` workaround. Document it in the README as the supported way to force CPU-only operation.
5. **Correct `src/device.py`'s docstring** — the "single source of truth" claim, aspirational today, becomes accurate once all four components consult it.
6. **Explicitly out of scope:** no GPU-backend investigation (AMD/DirectML/ROCm are dropped entirely); no change to the `cloud/` harness; the stack default is *not* flipped to CPU (that remains ADR-024's settled decision). This ADR is purely about making the existing override cover the components it already claims to.

## Consequences

**Better:**
- One honest, documented device control. `CODE_INDEXER_DEVICE=cpu` actually produces a CPU-only run — CPU-safe **by construction**, not by remembering an unrelated CUDA env var.
- Closes the current, concrete hazard: on a machine where the GPU kernel-panics, the obvious safety setting now works.
- `src/device.py`'s "single source of truth" claim becomes true. The four components stop making four independent device decisions.

**Worse:**
- Touches the embedder's hot path (`_get_embed_model()`), which carries a load-bearing `max_seq_length` cap and lazy-singleton semantics — the change must preserve both exactly.
- Formalizes a second execution path (CPU vs CUDA) that must stay in parity, though that duality already exists de facto.

**Neutral:**
- Default behavior is unchanged for machines with a healthy GPU and no override set.
- Reranking stays off by default (ADR-007/009 measured decision); this only affects *what device* components use, never *whether* they run.
- No new dependencies; no cloud/offline-guarantee change.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Flip the whole-stack default to `"cpu"` (safer for the current crashing machine) | Reverses ADR-024's deliberate fleet-oriented auto-CUDA default and would need re-deciding for CI/other dev boxes. The authoritative override delivers the same local safety without the reversal. Kept as a fallback if the crash situation proves long-lived. |
| Leave it as-is and document `CUDA_VISIBLE_DEVICES=""` as the CPU guard | The env var that *looks* like the device control (`CODE_INDEXER_DEVICE`) silently doesn't govern the embedder — a footgun. Documenting a second, unrelated variable as the "real" switch entrenches the confusion instead of fixing it. |
| Original ADR-020: local AMD GPU path via DirectML / ROCm | Obsolete on all three premises (hardware replaced, need solved by the cloud T4 harness, local GPU unusable). Superseded by this ADR. |
| Per-component device config keys in `indexer.toml` | Over-configuration. Device is a runtime/hardware fact, not per-repo policy — the same reasoning ADR-024 used to reject a `[reranker].device` key. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] Thread `resolve_device()` into `src/core.py` `_get_embed_model()` (embedder), preserving the lazy singleton and the `max_seq_length` cap. Passes `device=resolve_device()` to the `SentenceTransformer` constructor; the `max_seq_length` cap and lazy-singleton semantics are untouched.
- [x] Thread `resolve_device()` into `src/summarizer.py` (default device + dtype keyed off the resolved device). **Both** classes fixed: `ChunkSummarizer` (in-process) and `IsolatedChunkSummarizer` (subprocess — the one the indexer actually uses). `device` now defaults to `None` → `resolve_device()` (an explicit `device=` still wins). `ChunkSummarizer`'s dtype is keyed off `self._device == "cuda"` (was raw `torch.cuda.is_available()`). `IsolatedChunkSummarizer`'s `dtype` stays an explicit `"float16"` knob — its documented CPU RAM-saving default, independent of device placement.
- [x] Verify `CODE_INDEXER_DEVICE=cpu` produces a fully CPU-only index run — proved on **CPU alone** in `tests/test_device.py` (6 new ADR-020 tests): the embedder receives `device="cpu"` under the override (and `"cuda"` when so forced), both summarizers resolve `_device="cpu"`, an explicit `device=` beats the override, and `ChunkSummarizer` dtype follows the resolved device (cpu→float32, cuda→float16). Model loads are faked/monkeypatched — no download, no GPU.
- [x] Update `src/device.py`'s docstring now that the "single source of truth" claim holds for all four components.
- [x] Document `CODE_INDEXER_DEVICE=cpu` in the README as the supported force-CPU control.
- [x] Close the superseded original branch `feature/adr-020-gpu-acceleration` and its `ADR-020-gpu-acceleration-amd.md` — local branch force-deleted 2026-07-17 (its only unmerged commit was the superseded AMD ADR); the remote ref was already gone and the old ADR file never reached master, so nothing else to remove.
- [x] Add the bidirectional cross-reference into ADR-024 (**Depended on by: ADR-020**) — done in the rescope commit (PR #25).

**Notes:**
<!-- Add dated comments as you go -->
- 2026-07-17: Rescoped from the AMD/DirectML/ROCm original. Trigger: while verifying ADR-024 on a spot T4 (PR #19), confirmed that `resolve_device()` governs only the reranker + eval, and that `CODE_INDEXER_DEVICE` does not reach the embedder (`core.py:66` passes no `device=`). Combined with the local GPU being unusable (kernel crashes), that makes the missing override coverage an active safety gap, not a cosmetic one. The old ADR's bulk-embedding-on-local-GPU rationale is fully superseded by the `cloud/` T4 harness, which already does that job (see also #21 on the harness's stale-index issue).
- 2026-07-17: Implemented (this branch). Verified the footgun on master before fixing — `resolve_device()` was imported only by `hybrid_retriever.py`, `reranker.py`, `tools/real_repo_eval.py`; `core.py:66` and both summarizer classes never consulted it. Surprise during implementation: there are **two** summarizer classes and the live indexing path uses `IsolatedChunkSummarizer` (subprocess), not `ChunkSummarizer` — so fixing only the in-process class (which the §Decision text names) would have left the actual indexing path still ignoring the override. Fixed both. Kept `IsolatedChunkSummarizer.dtype` as its explicit `"float16"` default (documented RAM optimisation, orthogonal to device). Status → `accepted`: §Decision fully implemented, no `Depended on by` obligations to resolve. Full suite green; no GPU touched (CPU-only tests).
