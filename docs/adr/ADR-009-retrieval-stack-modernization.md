# ADR-009: Retrieval Stack Modernization — Swap-In Engine Upgrades, Validated Against the Baseline

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-009-retrieval-stack-modernization`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **committed Wave-0 baseline** and the **fast CI subset** to validate each component swap as a measured lift, not a claim. No swap lands without a number beating the baseline.
**Depended on by:**
- ADR-014 *(planned — docs/adr-backlog.md)* — Adaptive Ranking learns weights over **this ADR's fusion stage** (the convex/weighted combination introduced in P3); it needs the fusion to be parameterized (tunable weights) rather than fixed RRF.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-009 bucket + build kit) and
> [modernization-stack-review.md](../modernization-stack-review.md) (Pillars 1–4). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

The retrieval pipeline is the engine's highest-traffic surface and its quality ceiling. The current stack —
`jina-v2` embeddings, AST chunking, dense FAISS + a cross-encoder reranker, fused with RRF — predates a wave
of code-retrieval advances catalogued in [modernization-stack-review.md](../modernization-stack-review.md):
better code-specialized embedders ([37] jina-code, [38] Qwen3), **late chunking** ([42]) and contextual
retrieval ([43]), sparse+dense fusion with score-normalized convex combination over raw RRF ([47]), and
stronger code rerankers ([40] CoRNStack, Qwen3-Reranker).

These are **swap-in component upgrades**: each replaces or augments one stage of the pipeline, and —
critically — **none touches `stable_id`, the DB schema, or the MCP tool surface.** The `stable_id` formula
is model-independent (`tests/test_stable_id.py` is untouched); only the *vectors* change. That property is
what makes this a single coherent bucket: it is engine-internal quality work with no contract blast radius.

The blocker is measurement. We have no way today to know whether `jina-code` actually beats `jina-v2` on
*our* corpus, or whether convex fusion beats RRF, or whether the reranker swap is worth its latency. That is
exactly what ADR-007 exists to provide — so this ADR is gated behind it and treats "beats the committed
Wave-0 baseline" as the acceptance test for every change.

## Decision

Modernize the retrieval pipeline as a set of **independently validated, config-driven component swaps**,
each measured against the ADR-007 baseline. Four pillars land as the core; late-interaction is an optional
research phase.

### §P1 — Embedder refresh

Replace `jina-v2` with a current code-specialized embedder. **Default: `jina-code-embeddings-1.5b`** —
lowest friction (same vendor, drop-in API), with **Qwen3-Embedding** as the higher-ceiling alternative if
the baseline shows it's worth the change. The embedder is **config-driven** via `indexer.toml`
`[embeddings]`, loaded in `src/core.py` (`MultiIndexManager`).

**This is a one-time reindex.** A new embedder usually changes vector dimensionality, which forces a FAISS
index rebuild (the index-file dimension is fixed at creation). The `dimension` field in `[embeddings]`
(currently `768`, flagged "must match the model") changes with the embedder and is part of this migration.
`stable_id`s are unchanged, so the rebuild is purely "recompute vectors for existing chunks" — documented as
a migration step, not a schema change.

**Mantra 1 (local-first).** All candidate models — `jina-code-embeddings-1.5b`, `Qwen3-Embedding`, and the
P4 rerankers — are HuggingFace weights run **locally** (downloaded once via `huggingface-cli`, then offline
inference). No cloud API or runtime network call is introduced; the offline guarantee is preserved.

### §P2 — Late chunking

Add a **late-chunking path** ([42]) in `src/ast_chunker.py`: embed the full document context first, then
derive chunk vectors from that context-aware representation, rather than embedding each chunk in isolation.
This improves cross-chunk coherence (a chunk's vector "knows" its surroundings).

**Mantra 4 safety — this is vectors-only, not re-segmentation.** Late chunking changes only *how a chunk's
vector is computed*; it must keep the **chunk boundaries identical** (same AST/sliding-window segmentation,
therefore the same `scope`). Because `scope` is unchanged, `stable_id` is untouched and existing indexes are
not orphaned — exactly like P1, the change is recompute-vectors-only. (A late-chunking variant that *re-cut*
boundaries would change `scope` and be index-invalidating; that is explicitly out of scope here.)

The LLM summarizer
(`src/summarizer.py`) is **demoted to optional** — late chunking captures much of what the summarizer was
compensating for, and keeping the summarizer mandatory is a cost we no longer need to pay by default.

### §P3 — Sparse signal + weighted fusion

Add a **BM25 sparse retriever** (`rank-bm25`) alongside dense retrieval in `src/hybrid_retriever.py`, and
replace raw RRF with a **score-normalized convex combination** ([47]) as the default fusion mode (RRF kept
as a fallback). Sparse signal catches exact-identifier and rare-token matches that dense retrieval blurs;
convex fusion with normalization lets the two signals be **weighted** — which is the hook ADR-014 later
learns. New `[retrieval]` config block controls fusion mode and weights.

### §P4 — Reranker option

Add a code-specialized reranker option ([40] CoRNStack / Qwen3-Reranker) selectable via `indexer.toml`
`[reranker]`. This is a pure config choice — the rerank stage interface is unchanged.

**Correction (2026-06-22).** The original framing — "keeping the current cross-encoder as the default" —
rested on a false premise. The "current cross-encoder" was `jinaai/jina-reranker-v2-base-code`, a
**non-existent model id**, and `HybridRetriever()` was constructed with no args. The load therefore failed
on every startup and the pipeline silently fell back to RRF — production reranking had **never run**. So the
honest default is **RRF-only with reranking off**, which is exactly the measured Wave-0 baseline (ADR-007),
not a placeholder. This pillar's first delivered slice is a **truthfulness fix** (see log): correct the id,
make `HybridRetriever` read `[reranker]` (model_id + an explicit `enabled` flag, default `false`), and wire
Qwen3-Reranker as a real opt-in via the shared `src/reranker.py` scorer. No quality claim is attached —
reranker lift on CoIR was neutral (cosqa) to negative (codefeedback-mt) under the ADR-007 harness, so it
stays off pending the internal-repo eval (ADR-008).

### §S2 — Late interaction (optional research phase)

ColBERT-style late-interaction (token-level multi-vector matching) is listed as an **optional research
phase**, not core. It carries a different index structure and storage profile; it ships only if a research
spike shows a baseline lift that justifies the complexity.

### §Validation contract

Every pillar is **off by default until it beats the Wave-0 baseline** on the ADR-007 harness, and every
pillar is **independently togglable** via config so a swap can be A/B'd and reverted without touching code.
"Modernization" here means "measured lift," never "newer is better."

### §Validation coverage caveat (current state, with remediation under consideration)

The acceptance test inherits ADR-007 §9's limits, and that must be stated honestly: the Wave-0 baseline
measures **semantic retrieval on the languages CoIR covers (Python, JS)** only. It does **not** measure
**C#/C++** or the **structural-graph (Traverse) layer**. Consequence: a swap that "beats the baseline" is
proven to help *measured* retrieval — but could **silently regress C#/C++ or the graph layer** and the
harness would not catch it.
- **Mitigation now:** treat the baseline pass as necessary-not-sufficient; for embedder swaps (P1), spot-check
  C#/C++ on the legacy smoke queries before committing to a reindex.
- **Planned (under consideration):** the internal-repo eval (ADR-007 §9 / ADR-008 §7) extends the acceptance
  test to C#/C++ and the structural layer; once it exists, "beats the baseline" should mean both scorecards.

## Consequences

**Better:**
- Each upgrade is a *measured* improvement against a committed baseline, not a vibe; reversible via config.
- No contract blast radius: `stable_id`, schema, and the MCP tool surface are untouched — this is purely
  engine-internal quality work.
- Convex weighted fusion (P3) is the parameterized hook ADR-014 needs; late chunking (P2) lets the LLM
  summarizer become optional, cutting indexing cost.
- Sparse signal recovers exact-identifier matches dense embeddings blur — a known weakness for code search.

**Worse:**
- P1 forces a **one-time full reindex** (vector-dim change → FAISS rebuild); must be documented and run
  deliberately.
- New dependencies: `rank-bm25` (sparse), new embedder weights (HF), possibly `einops`; the sparse index
  adds storage cost.
- More config surface (`[embeddings]`, `[retrieval]`, `[reranker]`) and more pipeline branches to test.
- Late interaction (S2), if pursued, brings a materially different and heavier index structure.

**Neutral:**
- The summarizer isn't deleted — demoted to optional, so corpora that benefit can re-enable it.
- Defaults are conservative (same-vendor embedder, cross-encoder reranker kept) so the safe path is the
  default and the ambitious swaps are opt-in behind measurements.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Adopt the newest embedder/reranker without benchmarking | Violates the whole point of ADR-007; "newer" ≠ "better on our corpus." Every swap must beat the baseline. |
| Qwen3-Embedding as the default | Higher ceiling but more friction (different vendor/runtime); made the *alternative*, promoted only if the baseline justifies it. Default is the low-friction same-vendor jina-code. |
| Replace RRF entirely with no fallback | Convex fusion can underperform on some query mixes; RRF retained as a fallback mode rather than burned. |
| Drop the LLM summarizer entirely | Late chunking covers much of its value but not all corpora; demote to optional rather than delete. |
| Late interaction (ColBERT) as core | Materially heavier index + storage; value unproven on our corpus. Kept as an optional research phase gated on a spike. |
| Change `stable_id` to encode the embedder | Unnecessary and harmful — IDs are deliberately model-independent so a reindex is vectors-only. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] P1: config-driven embedder load in `src/core.py`; `[embeddings]` block; FAISS rebuild path + documented one-time reindex. Validate vs Wave-0 baseline.
- [ ] P2: late-chunking path in `src/ast_chunker.py`; demote `src/summarizer.py` to optional. Validate.
- [ ] P3: BM25 retriever (`rank-bm25`) + score-normalized convex fusion in `src/hybrid_retriever.py`; `[retrieval]` block (fusion mode + weights). Validate.
- [~] P4: reranker option ([40]/Qwen3) via `[reranker]`. **Truthfulness slice DONE (2026-06-22)** — see note below; off by default, no quality claim. Quality validation still pending the internal-repo eval.
- [ ] Document the one-time reindex + FAISS dimension implications.
- [ ] (Optional) S2 late-interaction research spike; ship only on a measured lift.
- [ ] Resolve **Depended on by**: confirm the parameterized-fusion contract ADR-014 will learn over, before `accepted`.

**Notes:**
<!-- 2026-06-18: Highest-ROI engine work; no stable_id/schema/MCP changes. Defaults: embedder = jina-code-embeddings-1.5b; fusion = convex w/ normalization (fallback RRF); sparse = BM25 on; late chunking on, summarizer optional; reranker = cross-encoder default. P1 = one-time reindex (vector-dim → FAISS rebuild). Every pillar gated on beating the ADR-007 Wave-0 baseline. -->

**2026-06-22 — P4 truthfulness slice (no quality claim).** Stacked on the ADR-007 dense-baseline branch
because it reuses that branch's Qwen3 scorer. What shipped:
- The configured reranker id `jinaai/jina-reranker-v2-base-code` did not exist; `HybridRetriever()` ignored
  config entirely. Production reranking had been **silently RRF-only via repeated load failures** — graceful
  on the surface, but an accident dressed as a feature.
- Introduced `src/config.py` (first `indexer.toml` reader in `src/`; walks up from cwd) and `src/reranker.py`
  (canonical home for the `Qwen3Reranker` logit-scorer + `load_reranker()` factory). `tools/coir_eval.py` now
  imports the scorer from `src/reranker.py` — **one implementation**, no duplication.
- `HybridRetriever` now reads `[reranker]`: `model_id` (default `Qwen/Qwen3-Reranker-0.6B`) + an explicit
  `enabled` flag (**default `false`**). When off, `_load_reranker()` returns `None` without fetching a model
  and the pipeline returns the RRF-ranked top-10 — the *intentional, documented* default, not a fallback.
  When on, `load_reranker()` routes to the Qwen3 scorer or a CrossEncoder by id.
- Corrected the docstrings in `hybrid_retriever.py`, the `MCPServer` singleton comment, `src/CLAUDE.md`, and
  the `[reranker]` block in `indexer.toml`. Scope was deliberately reranker-only: the embedder (`core.py`)
  and summarizer (`summarizer.py`) still hardcode their ids — P1 owns migrating those.
- Verified: 85/85 tests pass; `HybridRetriever()` constructs with reranking off and fetches no model. NOT
  verified: any reranker quality lift (there is none on CoIR; that's why it's off). -->
