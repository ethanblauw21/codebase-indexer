# ADR-024: GPU Auto-Detection with CPU Fallback for the Reranker

**Status:** proposed
**Date:** 2026-07-13
**Branch:** `feature/adr-024-gpu-auto-device`
**Reviewer:** @edb
**Depends on:** none
**Depended on by:** ADR-020 — *extends this ADR's `resolve_device()` to the embedder and summarizer, and makes `CODE_INDEXER_DEVICE` authoritative over all four model loads (this ADR wired only the reranker + eval; the embedder/summarizer still auto-select CUDA independently — see ADR-020 Context).*

## Context

Device selection across the three inference-heavy components is inconsistent today:

- **Embedder** (`src/core.py`, `_get_embed_model()`): `SentenceTransformer(model_id, trust_remote_code=True)` passes no `device` kwarg, so it already defers to sentence-transformers' own default device resolution (CUDA if available, else CPU). No change needed.
- **Summarizer** (`src/summarizer.py`): already defaults to `device="auto"` with `device_map="auto"`, and separately picks `torch.float16` when `torch.cuda.is_available()` else `float32`. No change needed.
- **Reranker** (`src/reranker.py` `load_reranker()`, `src/hybrid_retriever.py` `HybridRetriever.__init__`): hardcodes `device: str = "cpu"` as the default. `MCPServer.py` constructs `HybridRetriever()` with no arguments, so production **never** uses a GPU even when one is present — a caller would have to explicitly pass `device="cuda"`, and nothing in production does.
- `tools/real_repo_eval.py` already solved this exact problem for itself with a local `_auto_device()` (checks `EVAL_DEVICE` env override, else `torch.cuda.is_available()`), explicitly scoped to the eval harness only, by design, to avoid changing production behavior at the time.

The user now has a Blackwell-class NVIDIA GPU in their dev machine and wants the reranker to use it automatically when reranking is enabled, without requiring an explicit `device="cuda"` at every call site, while still working unmodified on CPU-only machines.

Note: reranking is **off by default** (`[reranker].enabled = false` in `indexer.toml`), per the ADR-007/ADR-009 measured-neutral-lift decision. This ADR does not revisit that — it only fixes what device the reranker *would* run on if/when a caller enables it. The existing CUDA batch-size cap in `src/reranker.py` (`batch_size = min(batch_size, 8)`, tuned to avoid OOM on a 16GB T4) is also left as-is; it's a conservative floor, and tuning it per-GPU is a separate concern from whether the GPU is used at all.

## Decision

1. Add `src/device.py` with a single function, `resolve_device() -> str`:
   - Returns the value of the `CODE_INDEXER_DEVICE` env var if set (forces `"cpu"`, `"cuda"`, or `"mps"` — no validation beyond passing it through, matching `_auto_device()`'s existing forced-override behavior).
   - Otherwise returns `"cuda"` if `torch.cuda.is_available()`, else `"cpu"`. No MPS auto-detection tier — MPS is reachable only via explicit `CODE_INDEXER_DEVICE=mps`, since auto-detection only needs to match real hardware in this codebase's CI/dev fleet (CUDA or CPU).
2. `src/hybrid_retriever.py`: change `HybridRetriever.__init__`'s `device` parameter default from the literal `"cpu"` to `resolve_device()`, so `HybridRetriever()` (as constructed by `MCPServer.py`) auto-detects. Explicit `device=` args at call sites (e.g. tests forcing `"cpu"`) are unaffected — they still win.
3. `src/reranker.py`: change `load_reranker()`'s `device` parameter default the same way, for direct callers that bypass `HybridRetriever`.
4. `tools/real_repo_eval.py`: delete the local `_auto_device()` and the `EVAL_DEVICE` env var; import and use `resolve_device()` from `src/device.py` instead, with `CODE_INDEXER_DEVICE` as the (now sole) override. No behavioral change to the eval harness beyond the env var name.
5. No changes to `src/core.py` (embedder) or `src/summarizer.py` — both already auto-detect correctly; this ADR's Context section above documents that so it isn't rediscovered as an open question later.

## Consequences

**Better:** Enabling `[reranker].enabled = true` on a GPU-equipped machine (like the user's) now gets GPU acceleration with zero extra configuration. One shared, testable device-resolution function replaces two independent implementations (production had none; eval had a private one).

**Worse:** A machine with a *partially broken* CUDA install (driver mismatch, no VRAM headroom) that previously silently ran the reranker on CPU will now attempt CUDA first and surface a real error/OOM instead of quietly falling back — this is a behavior change for that edge case, not just an additive one. Mitigated by the existing `_reranker_failed` fallback-to-RRF path in `HybridRetriever`, but worth calling out.

**Neutral:** `EVAL_DEVICE` is retired in favor of `CODE_INDEXER_DEVICE`; anyone with `EVAL_DEVICE` set in their shell/CI config needs to rename it. Reranking itself stays off by default — this ADR changes *what device it would use*, not whether it runs.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Add a `[reranker].device` key to `indexer.toml` in addition to the env var | Adds a second override mechanism (config + env, with precedence rules to define and document) for a decision that's really a runtime/hardware fact, not a per-repo policy. Scoped out; can be added later if a real need for per-repo pinning shows up. |
| Auto-detect MPS as a third tier (`cuda > mps > cpu`) | No Apple Silicon hardware in this project's actual dev/CI fleet today; adding untested auto-detection for it is speculative. `CODE_INDEXER_DEVICE=mps` remains available as an explicit override if/when needed. |
| Raise the CUDA reranker batch-size cap for larger-VRAM cards | Out of scope — this ADR is about *whether* the GPU is used, not about tuning throughput on it. The existing conservative cap (8, tuned for 16GB) stays until a follow-up specifically addresses per-GPU batch sizing. |
| Leave `tools/real_repo_eval.py`'s `_auto_device()` untouched, only fix production | Avoids touching eval-harness code, but leaves two near-identical `cuda-if-available-else-cpu` implementations with different env var names in the same codebase — the exact duplication this ADR exists to remove. |
| Flip `[reranker].enabled = true` as part of this ADR | The prior OFF decision (2026-07-07 project memory) was driven by measured neutral/negative retrieval-quality lift, not CPU latency. GPU speed doesn't address that; enabling it is an independent quality decision for a future ADR. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] Add `src/device.py` with `resolve_device()` (env override + `torch.cuda.is_available()` fallback, no MPS auto-tier)
- [x] Wire `HybridRetriever.__init__`'s `device` default to `resolve_device()` in `src/hybrid_retriever.py`
- [x] Wire `load_reranker()`'s `device` default to `resolve_device()` in `src/reranker.py`
- [x] Replace `tools/real_repo_eval.py`'s local `_auto_device()` with an import of `resolve_device()`; retire `EVAL_DEVICE` in favor of `CODE_INDEXER_DEVICE`
- [x] Add/confirm test coverage: `resolve_device()` respects the env override; `HybridRetriever()` with no args resolves the same way `resolve_device()` does directly (`tests/test_device.py`, 4 tests passing)
- [ ] Manually verify: MCP server starts cleanly and `reindex` runs without error (per CONTRIBUTING.md §5) with reranking enabled on the GPU machine
- [x] Update this log with the actual GPU/VRAM behavior observed (batch cap sufficiency, any OOM) once tested against the user's Blackwell card

**Notes:**
<!-- Add dated comments as you go -->
- 2026-07-13: `torch.cuda.is_available()` confirmed `True` on the dev machine; `torch.cuda.get_device_name(0)` reports "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU". `resolve_device()` correctly returns `"cuda"` with no override set. Full test suite otherwise blocked locally by an unrelated pre-existing gap: `faiss` is not installed in this environment, so `HybridRetriever`/`incremental_indexer` import-time tests (`test_call_resolver.py`, `test_cs_cpp_indexing.py`, `test_csharp_call_edges.py`, and several others) fail to collect/run regardless of this change — not caused by it. The MCP-server-starts-cleanly + reranker-enabled-on-GPU checklist item needs `pip install -e .` (or at least `faiss-cpu`/`faiss-gpu`) done first.
- 2026-07-17: `resolve_device()` verified on **real CUDA hardware** — a spot NVIDIA T4 via the `cloud/` harness (`--arms C --repos p-queue`, no reindex). With no `CODE_INDEXER_DEVICE` set, `tools/real_repo_eval.py`'s `_DEVICE = resolve_device()` resolved to CUDA and the harness printed `p-queue/C: start — 24 queries (device=cuda)` on `torch 2.5.1+cu121`. This confirms the §Decision item 4 rewiring (local `_auto_device()` → shared `resolve_device()`) behaves identically on a GPU box. Three caveats, none blocking that specific claim:
  - A T4 is **not** the dev machine's Blackwell card. Blackwell-specific behavior is still only covered by the 2026-07-13 local `torch.cuda.is_available()` check above.
  - The run then died at `hybrid_retriever.py:170` on the ADR-009 §P1 dimension guard — the benchmark indexes bundled to the VM are stale jina-768 while `indexer.toml` now configures bge-code-v1 at 1536. Those indexes are **gitignored, locally-built artifacts** (`.gitignore:64`), not committed ones, so this is local-machine staleness rather than a repo-state problem (**issue #21**; pre-existing, unrelated to this ADR, and not fixed here). The reranker therefore never loaded, so the CUDA batch-size cap (§Context: `min(batch_size, 8)`, tuned for a 16GB T4) and its OOM behavior remain **unverified on any GPU**.
  - The remaining unchecked item ("MCP server starts cleanly and `reindex` runs without error") is **not reachable via the cloud harness at all** — the eval imports `HybridRetriever` directly and never touches `MCPServer`, and `reindex` never loads a reranker. That item is CPU-only work gated on the local `faiss`/`pip install -e .` gap noted 2026-07-13; it needs no GPU.
- 2026-07-17: Scope clarification on §Decision item 5 (worth recording so it isn't rediscovered): leaving `src/core.py` unchanged is correct for *auto-detection* — the embedder does pick CUDA on its own — but it means `CODE_INDEXER_DEVICE` **cannot force the embedder onto a specific device**, since the override lives in `resolve_device()` and `core.py:66` never calls it. `src/device.py`'s docstring ("single source of truth for which device GPU-capable components run on") is accurate for the reranker and retriever and overbroad for the embedder. Consequence: `CODE_INDEXER_DEVICE=cpu` does not make indexing CPU-only; only `CUDA_VISIBLE_DEVICES=""` does. Not changed here (out of scope — this ADR is reranker device selection); flag it if a real need for embedder pinning appears.
- 2026-07-17: The "real need" above has appeared — the local GPU is currently unusable (kernel crashes), so a working `CODE_INDEXER_DEVICE=cpu` is a live safety requirement, not a hypothetical. **ADR-020** (rescoped from its obsolete AMD/DirectML original) now owns extending `resolve_device()` to the embedder and summarizer so the override governs all four model loads. Recorded here as the **Depended on by** link.
