# ADR-035: The Embedder Loads in bf16 on a GPU That Supports It

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-035-bf16-embedder`
**Reviewer:** @edb
**Backlog:** [B-031](../backlog.md#b-031) — the embedder loads in fp32 and fills the 8 GB card on its own
**Depends on:** none
**Depended on by:** [ADR-028](./ADR-028-central-model-host.md) — its gate "bf16 embedder landed" is this ADR. The host loads the embedder through `core`, so it gets the same precision.

## Context

`core._get_embed_model` loads `BAAI/bge-code-v1` (1.5B parameters, Qwen2.5-Coder backbone) with no
dtype, so it loads in fp32, about 6.2 GB. On the 8 GB RTX PRO 1000 that leaves no room for ADR-027's
1 GB reserve or for the summarizer. When memory runs out, Windows' WDDM pages to system RAM and runs
about 50 times slower, with no error.

Everything measured since 2026-09-24 already ran in bf16:
- The eval kit patches `core.SentenceTransformer` to pass `torch_dtype=bfloat16`, for index and
  queries alike.
- ADR-034's MCP Inspector wrapper does the same.

So bf16 is the precision the retrieval numbers in ADR-027 to ADR-034 describe. Production is the one
path that still loads fp32.

## Decision

1. **`[embeddings].dtype`**, default `"auto"`, read by `core.embed_dtype()`:
   - `"auto"`: bf16 when the resolved device is CUDA and `torch.cuda.is_bf16_supported()` is true,
     otherwise fp32. The CPU stays fp32, because bf16 matmuls are slower than fp32 on most CPUs, and a
     GPU without bf16 support (pre-Ampere) would emulate it.
   - `"float32"`, `"bfloat16"` or `"float16"` forces that precision. Any other value raises, naming
     the setting.
2. `_get_embed_model` passes the result as `model_kwargs={"torch_dtype": ...}` and logs it on the
   load line, for example `device=cuda, dtype=bfloat16`.
3. The setting is registered in `indexer.toml` and in the config drift test (ADR-026).

**Existing indexes are not rebuilt.** An fp32-built index queried with bf16 queries mixes precisions.
Verification 1 measures what that costs, and the log records it.

## Consequences

**Better:**
- The embedder takes about 3 GB instead of 6.2, so it fits beside the summarizer's reserve. That
  unblocks ADR-028.
- Production matches the stack every retrieval number since ADR-027 was measured on.
- Embedding is faster on the GPU.

**Worse:**
- bf16 keeps 8 bits of mantissa. Vectors differ slightly from fp32 ones, so an fp32-built index gets
  queries at a different precision until it is rebuilt.
- One more config key.

**Neutral:** the CPU path is unchanged. `CODE_INDEXER_DEVICE=cpu` still gives fp32 everywhere.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| fp16 by default | Half precision has a narrower exponent range than bf16, and Qwen2-family models are published and tested in bf16. fp16 stays available as an explicit value. |
| bf16 always, CPU too | Slower on most CPUs, and it would change the CPU path that CI and the kill switch rely on. |
| 8-bit or 4-bit quantization (bitsandbytes, NVIDIA Model-Optimizer) | It changes vectors more, needs a new dependency, and would need a full re-baseline of every gate. bf16 already fits. |
| Leave it and patch in the kit | The kit already does this, and it is why production and the measurements disagree. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `core.embed_dtype()` and the `model_kwargs` pass-through; the dtype in the load log line
- [ ] `[embeddings].dtype` in `indexer.toml`, and the drift test
- [ ] Tests: auto picks bf16 on CUDA with support, fp32 on CPU and without support; explicit values; a bad value raises
- [ ] Verification 1 (GPU): memory after load, bf16 against fp32 cosine on real chunks, and whether an fp32 index queried in bf16 changes retrieval
- [ ] MCP Inspector: the server starts and `semantic_code_search` answers with the bf16 embedder
- [ ] Resolve **Depended on by**: confirm to ADR-028 that its gate is met

**Notes:**
<!-- 2026-09-25: branch cut from master (1df2b79). -->
