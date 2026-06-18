# Modernization Review: Bringing the Retrieval Stack to 2026 SOTA

> **Status:** review / recommendations (not committed work).
> **Date:** 2026-06-18
> **Goal:** keep the tool current "in form and function" — for each component of the *current* stack, what is the
> 2025–2026 state of the art, does it beat what we run, and is the upgrade low-risk and well-validated?
> **Companion to:** [references-code-intelligence.md](./references-code-intelligence.md) §J,
> [design-research-informed-improvements.md](./design-research-informed-improvements.md),
> [suggestions-future-directions.md](./suggestions-future-directions.md).

## TL;DR — ranked by return on effort

1. **Refresh the embedder (biggest, easiest win).** `jina-embeddings-v2-base-code` is a ~2023 model and is now
   clearly behind. Direct upgrades exist, including Jina's *own* successor. **High value, low risk.**
2. **Add Late Chunking (free context win).** You already do a *variant* of contextual retrieval (the summarizer
   appends an extraction before embedding). Late Chunking gets much of the same benefit with **no LLM cost** and
   works with the Jina models you already use. **High value, low risk.**
3. **Add a sparse/exact-match signal if you're dense-only.** Code retrieval benefits unusually much from exact
   identifier matching; dense-only leaves recall on the table. **High value, medium effort.**
4. **Upgrade fusion from raw RRF to weighted / convex combination.** Small, well-researched accuracy gain. **Med
   value, low risk.**
5. **Reranker: keep the cross-encoder; it's still a sound offline choice.** LLM listwise rerankers win on quality
   but are heavy for a local tool. **Optional.**
6. **Use Leiden over Louvain for communities (math reliability).** Already noted in ADR-006; restated here.

> **Strategic tie-in:** validate every change on **CoIR** (the standard code-IR benchmark). That both proves the
> upgrade *and* feeds the "measurable, provable accuracy" moat (Bucket B / ADR-007). Modernization and the moat
> are the same work if you benchmark.

---

## Pillar 1 — Code embedding model (most outdated; upgrade first)

**Current:** `jinaai/jina-embeddings-v2-base-code` (single dense vector, ~2023).

**2025–2026 SOTA (all evaluated on CoIR, the code-IR benchmark):**
- **Jina's own successors — `jina-code-embeddings-0.5b` / `1.5b`** (2025; *"Efficient Code Embeddings via
  Generation Models,"* arXiv:2508.21290). Claimed SOTA across 25 code retrieval benchmarks. **Lowest-friction
  upgrade — same vendor/ecosystem.**
- **Qwen3-Embedding** (arXiv:2506.05176) — 8B ranked #1 on MTEB-multilingual (incl. code); crucially ships a
  **matching reranker family**, so you could unify embedder + reranker under one model line.
- **CodeXEmbed / SFR** (arXiv:2411.12644) — 7B is best-averaged on CoIR; generalist multilingual code retrieval.
- **Voyage-code-3 / voyage-code-002** — strong but API-only (breaks the offline/local constraint — note, likely
  disqualified for this tool).
- **Qodo-Embed-1** — 1.5B (68.53 CoIR) beats some 7B models; efficiency-focused.
- **Granite Embedding R2** (IBM, arXiv:2508.21085) — another open option.

**Recommendation:** move to **`jina-code-embeddings-1.5b`** (drop-in-est, same family) or **Qwen3-Embedding**
(if you want one model line for embed + rerank). Keep it *local* (rules out Voyage). Re-embedding is a one-time
full reindex — but note `stable_id` is model-independent, so the ID formula is unaffected; only the vectors
change. **Validate on CoIR before/after.**

---

## Pillar 2 — Chunking & chunk-context (you're half-way to SOTA already)

**Current:** tree-sitter AST chunking at three granularities; `summarizer.py` appends an LLM extraction to a
chunk before embedding ("extraction over synthesis").

**Insight:** appending generated context before embedding *is* a homegrown variant of **Anthropic's Contextual
Retrieval** (Sept 2024) — you independently arrived at a SOTA idea. Two ways to modernize:

- **Late Chunking** (Jina; arXiv:2409.04701) — embed the *whole document/region* with a long-context model, then
  mean-pool token embeddings into chunk vectors. Each chunk vector then carries surrounding context **with no LLM
  call and no training**, and it works with Jina embeddings (which you already use). This is the cheaper,
  lower-variance alternative to the LLM-summarizer path — strong fit. *(arXiv:2409.04701)*
- **Anthropic Contextual Retrieval** — formalizes your summarizer approach; its headline result is that combining
  **contextual embeddings + contextual BM25 + rerank** drives the largest gains (motivates Pillar 3's sparse
  signal). Keep your summarizer where LLM-quality context matters; use late chunking as the cheap default.

**Recommendation:** adopt **Late Chunking** as the default context mechanism (free, uses current models); keep
the LLM summarizer as an optional high-quality tier. Cite cAST ([ref 11]) for the AST-structural-chunking basis.

---

## Pillar 3 — Fusion & the missing sparse signal

**Current:** Reciprocal Rank Fusion (RRF, k=60) across the three tiers; retrieval appears **dense-only (FAISS)**.
*(Verify: ADR-006 context mentions "dense+BM25 RTR" but `hybrid_retriever.py` as read is FAISS dense + structural
+ rerank. If BM25/sparse is genuinely absent, this pillar is the highest-value retrieval upgrade.)*

**2025–2026 SOTA:**
- **Dense+sparse hybrid beats dense-only**, especially for code (exact identifier/token matches matter). Add a
  **BM25** or **learned-sparse (SPLADE)** signal and fuse it in. Hybrid consistently raises recall in the
  literature.
- **Better-than-RRF fusion:** **Convex Combination** (normalized score blend) beat RRF in a head-to-head
  (Recall@5 0.726 vs 0.695). **Dynamic Alpha Tuning** (March 2025) and **Dynamic Weighted RRF** (Feb 2025) set
  per-query weights (the latter from tf·idf specificity, no LLM) for +2–7.5pp P@1/MRR.

**Recommendation:** (a) if dense-only, **add a sparse signal** — biggest single retrieval gain for code; (b)
replace raw RRF with **weighted RRF or convex combination** (requires score normalization — the one math caveat).
Per-query weighting ties directly to the S1 learned-feedback idea. RRF stays the robust fallback.

---

## Pillar 4 — Reranker (keep what you have; know the options)

**Current:** `jina-reranker-v2-base-code` cross-encoder (optional; ~500 MB; lazy-loaded).

**2025–2026 SOTA:** LLM **listwise** rerankers (RankZephyr, TourRank, Rank-K) generally outscore cross-encoders
on quality; **FIRST** (single-token listwise) and **Set-Encoder** make listwise cheaper; **Rank-DistiLLM** closes
the cross-encoder↔LLM gap. Empirical caveat: *"How Good are LLM-based Rerankers?"* (arXiv:2508.16757) shows the
gains are uneven and cost-sensitive.

**Recommendation:** for a **local, offline** tool, a fast cross-encoder is still the right default — a 7B listwise
reranker is heavy for a developer laptop. Two low-risk moves: (a) consider a **CoRNStack-trained** code reranker
(arXiv:2412.01007 — contrastive data tuned for code retrieval *and* reranking); (b) if you adopt Qwen3 for
embeddings, use the **matching Qwen3-Reranker** to unify the model line. Hold off on LLM listwise unless quality
demands justify the resource cost.

---

## Pillar 5 — Graph math reliability

**Current/planned:** Louvain community detection (ADR-006).

**Recommendation (restated):** prefer **Leiden** (Traag et al., 2019) where available — it fixes Louvain's
"badly connected communities" defect and guarantees well-connected, stable partitions. ADR-006 already keeps
Leiden as an optional path; for *math reliability* it should be the preferred backend when installable, Louvain
the fallback. Also fold in the competitor's **refinement step** (split <1% internal-density communities) noted in
the design proposal.

---

## How to validate (and feed the moat)

Adopt **CoIR — A Comprehensive Benchmark for Code Information Retrieval** (ACL 2025; arXiv:2407.02883;
github.com/coir-team/coir) as the standard scorecard. Run the current stack as the baseline, then measure each
change. This converts "modernization" into reproducible numbers — which is exactly the provable-accuracy posture
from [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md). Your existing `tools/eval_retrieval.py`
(MRR@5 / Hit@k) is the harness skeleton; point it at CoIR tasks.

## Priority table

| Pillar | Change | Value | Risk/Effort | Validate |
|--------|--------|-------|-------------|----------|
| 1 Embedder | jina-v2 → jina-code-1.5b or Qwen3 | High | Low (one reindex) | CoIR |
| 2 Chunking | add Late Chunking (keep summarizer optional) | High | Low | CoIR |
| 3 Fusion | add sparse (BM25/SPLADE) + weighted/convex fusion | High | Med (normalization) | CoIR |
| 4 Reranker | keep cross-encoder; CoRNStack/Qwen3 option | Med | Low | CoIR |
| 5 Graph | Leiden default + refinement step | Med | Low | ADR-006 tests |

**Bottom line:** the stack is well-architected but the *embedder is the dated part*; the chunking is accidentally
modern; fusion and the (likely missing) sparse signal are the next cheap wins. None of these touch `stable_id`,
the schema, or the MCP surface — they're swap-in component upgrades, all validatable on one public benchmark.
