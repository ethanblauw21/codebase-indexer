# ADR-009: Retrieval Stack Modernization — Swap-In Engine Upgrades, Validated Against the Baseline

**Status:** accepted (2026-07-02) — P1/P3/P4 implemented + merged, all flags off by default. §P3 convex fusion is settled (rejected — stays `rrf`). §P4 reranker is **settled — NOT enabled**: the 2026-07-06 power rerun (ADR-019, n=148) showed C−B passes public clauses 1 & 2 (CI excludes 0, no per-language regression), but the 2026-07-07 private contamination-free slice (clause 3) **FAILED** — pooled C−B CI includes 0 and TypeScript regresses on clean code, so the private slice *disagrees* with the public verdict. Under the all-three-clauses bar (public-enable / private-disagree → default off), `[reranker].enabled` **stays `false` by settled verdict**. The public win was partly a contamination artifact concentrated in TS (zustand outlier); the Python lift is real. See §P4.
**Date:** 2026-06-18
**Branch:** `feature/adr-009-retrieval-stack-modernization`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **committed Wave-0 baseline** and the **fast CI subset** to validate each component swap as a measured lift, not a claim. No swap lands without a number beating the baseline.
**Depended on by:**
- ADR-014 *(planned — docs/adr-backlog.md)* — Adaptive Ranking learns weights over **this ADR's fusion stage** (the convex/weighted combination introduced in P3); it needs the fusion to be parameterized (tunable weights) rather than fixed RRF.
- ADR-019 *(real-repo retrieval eval)* — **operationalizes the §P3 (fusion) and §P4 (reranker) enable decisions** this ADR could not settle on CoIR. Recorded in §P3/§P4: convex fusion **rejected outright** (D−B negative in all 5 languages); reranker C−B was positive-but-underpowered at n=42 (2026-07-01) and, after the **2026-07-06 power rerun (n=148)**, now **passes both public clauses** — still off pending the private slice (clause 3), the sole remaining gate. Both flags stay off *today*, but the reranker's path to `enabled` is now down to one contamination-free check.

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

**Status (2026-06-22) — plumbing done, swap deferred.** The config-driven plumbing is implemented:
`src/core.py` reads `model_id` / `max_seq_length` / `dimension` from `[embeddings]` (was hardcoded), and
`MultiIndexManager` refuses to load an index whose dimension ≠ the configured embedder (the explicit reindex
guard). Per the validation contract the **default stays `jina-v2` (768)** — proven — so behavior is unchanged
until a swap is measured. The actual embedder swap is a **deferred operator step**: set `model_id` + `dimension`
to `jina-code-embeddings-1.5b`, delete `.code-index`, reindex, then validate dense vs the committed Wave-0
baseline. **Caveat for that run:** `jina-code-embeddings-1.5b` is a Qwen2.5-Coder-based *decoder* embedder, not
a BERT like jina-v2 — it likely wants task-specific query/document **instruction prefixes**, so a naive
`SentenceTransformer.encode` may underperform its ceiling. Check the model card's prompt usage before
concluding it lost to the baseline. (CoIR validation needs no code change — the harness already reads
`[embeddings].model_id`.)

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

**Correction (2026-06-22).** The "convex as the default, RRF as fallback" framing is inverted to honor this
ADR's own **validation contract**: convex ships as a config *option* with `fusion_mode = "rrf"` the **default**
until a measured lift proves it — the same discipline applied to the reranker in §P4 (newer is not enabled
until it beats the baseline). Convex flips to default only when the ADR-007 harness shows it wins (see log).

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

**Real-repo eval result (2026-07-01, ADR-019).** The reranker was finally measured on real code — 5
languages, 42 hand-authored queries, the real `HybridRetriever(reranker_enabled=True/False)`, paired lift
**C−B**. Pooled: **MRR@10 +0.078 ±0.120, NDCG@10 +0.058 ±0.098** — positive means, but the 95% CI includes
zero and one target language regresses (js / p-queue −0.129). Under the §Validation contract this **FAILS**
the enable bar, so **`[reranker].enabled` stays `false`.** Crucially this is *not* a rejection like CoIR's:
the lift is **positive on 4 of 5 languages** and materially so on TypeScript (+0.264) and C# (+0.182) — the
**first positive reranker signal** the project has produced. Read it as "probably helps, underpowered at
n=42," not "doesn't help." Tightening that CI (a larger query set; investigate the lone p-queue regression)
is the path to a definitive verdict. *(Same eval rejected convex fusion §P3 — negative in all 5 languages.)*

**Power-rerun update (2026-07-06, ADR-019, n=148).** That path was taken. The C−B arm was re-run on an
expanded, dip-weighted fixture set (41→148 queries, weighted toward the two languages that had dipped).
Pooled: **MRR@10 +0.1405 (95% CI [+0.076, +0.205]), NDCG@10 +0.1174 (CI [+0.065, +0.169])** — **both CIs now
exclude zero (clause 1 PASS)**, and **every language's mean lift is positive (clause 2 PASS)**: the n=82
interim dips (js −0.014, cpp −0.019) flipped to **+0.032 / +0.132** once the set was large enough, confirming
they were sampling noise. So the reranker now **clears both public clauses that blocked at n=42** — the flip
is purely statistical power, not a code change. **It is still not enabled**, and deliberately so: §Validation
clause 3 (private contamination-free slice) has **not** been run, and the bar requires all three. `[reranker].enabled`
**stays `false`** pending that slice — which the rerun promotes from "moot" to **the single remaining gate**.
**Authoritative confirmation (2026-07-06, GPU).** The clean single-process full-run was since executed on a
spot T4 in GCP (all 148 queries through arm C on-GPU, one process). `verdict()`'s own printed scorecard:
**mrr@10 +0.1405 ±0.0641 (CI>0 ✓), ndcg@10 +0.1174 ±0.0520 (CI>0 ✓) → PASS (public)** — *identical to the
reconstructed values to 4 dp*, so the pooled-variance reconstruction is now confirmed exact, not merely
"exact modulo rounding." Caveat (2) below is thereby **resolved**. Residual honesty notes: the authoritative
run's `git_sha` is `unknown` (the cloud bundle isn't a git checkout, so the result file doesn't self-stamp its
commit); and zustand (+0.378) is an outlier leaning the pooled magnitude, though the four other languages are
all positive without it.

**Clause 3 — private-slice verdict (2026-07-07, ADR-019 §6): FAIL. Reranker decision SETTLED — stays off.**
The contamination-free control was authored (clean-room repos: `quanta` Python + `relay` TypeScript, post-cutoff
by construction) and run through arms B/C (44 queries; git-ignored, numbers-only). `verdict()`'s own output:
**`[reranker].enabled` → FAIL (§6 private)** — pooled C−B **mrr@10 +0.0920 ±0.1070 (CI includes 0 ✗),
NDCG@10 +0.0760 ±0.0815 (CI includes 0 ✗), no-regression ✗ (typescript)** → **the private slice DISAGREES
with the public verdict.** Per the §Validation rule (all three clauses required; public-enable / private-disagree
→ **default off and investigate**), `[reranker].enabled` **stays `false`** — no longer "pending", now a *settled*
verdict. The disagreement is language-split and diagnostic: **cleanroom-py C−B +0.1867 ±0.1335 (excludes 0 — a
real Python lift) vs cleanroom-ts −0.0325 ±0.1622 (negative).** This retroactively indicts the public pooled
pass, which leaned on the **zustand +0.378 TS outlier** whose fixtures target public types/interfaces
(maximally contamination-exposed); on *clean* TypeScript the reranker lift goes negative. So the §Context
contamination worry materialized — inverted: contamination **inflated** the public TS lift rather than
compressing it. Net reading: the reranker genuinely helps Python but not clean TypeScript, and its headline
public win was partly a memorization artifact. Two follow-ups are logged but **do not block** (the flag is off
regardless): (a) grow the private slice (n=44 is thin, split 25/19) to de-noise the TS negative; (b) per-language
reranking (on for Python, off for TS) — a new ADR, since `[reranker].enabled` is a single global flag today.

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

- [~] P1: config-driven embedder load in `src/core.py`; `[embeddings]` block; FAISS rebuild path + documented one-time reindex. **Plumbing + reindex guard DONE (2026-06-22); candidate reselected to `BAAI/bge-code-v1` + config staged (2026-07-07)** — see notes below. Remaining: T4 reindex + dense-arm validation vs the committed jina baseline (then a separate `max_seq_length`→4096 experiment).
- [ ] P2: late-chunking path in `src/ast_chunker.py`; demote `src/summarizer.py` to optional. Validate.
- [x] P3: BM25 retriever (`rank-bm25`) + score-normalized convex fusion in `src/hybrid_retriever.py`; `[retrieval]` block (fusion mode + weights). **Implemented + wired DONE (2026-06-22); validation DONE (2026-07-01) — convex REJECTED on CoIR AND on the ADR-019 real-repo eval (negative in all 5 languages, incl. the exact-identifier queries BM25 was meant to win), stays off (`rrf`).** See log below.
- [x] P4: reranker option ([40]/Qwen3) via `[reranker]`. **Truthfulness slice DONE (2026-06-22); quality validation DONE (2026-07-01, ADR-019); power rerun DONE (2026-07-06, n=148); private slice / clause 3 DONE (2026-07-07) → FAIL.** **Decision SETTLED — `[reranker].enabled` stays `false`.** At n=148 the public C−B lift passed clauses 1 & 2 (mrr@10 +0.1405, CI excludes 0), but the private contamination-free slice (clause 3) FAILED — pooled C−B CI includes 0 (+0.0920 ±0.1070) and TypeScript regresses on clean code, so it *disagrees* with the public verdict. All-three-clauses bar → default off. Split is diagnostic: clean Python +0.187 (real), clean TS −0.033 (public win was a contamination artifact, zustand outlier). Follow-ups (non-blocking): grow the slice; per-language reranking = new ADR. See §P4 clause-3 note.
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

**2026-06-22 — memory-hygiene pass (surfaced running the reranker scorecard on a 16 GB machine).** The
dense+reranker run appeared to hang for ~1 h; investigation found peak memory swap-thrashing on the larger
corpora. Root cause was redundant multi-GB arrays held alongside FAISS's own copy. Fixed by freeing them
promptly: `del mat` after `index.add()` and `shards.clear()` after `np.vstack` in `tools/coir_eval.py`
(plus `gc.collect()` between subtasks); `del` of per-batch tensors in `src/reranker.py`; `del vec_matrix,
id_array` after the FAISS add in `src/incremental_indexer.py` (benefits the production indexer too); and HF
Arrow-buffer frees in `tools/coir_prepare.py`. Pure memory hygiene — no change to any output. The two
`src/` edits (`reranker.py`, `incremental_indexer.py`) are why this pass is recorded against ADR-009.

**2026-06-22 — P3 sparse + convex fusion (implemented, off by default).** On `feature/adr-009-bm25-fusion`,
stacked on the P4 truthfulness branch (both touch `hybrid_retriever.py`). What shipped:
- `src/fusion.py` (new) — the single home for the tokenizer + `minmax_norm` + `convex_fuse`, shared by the
  production retriever and the eval harness so "convex fusion" means the same thing in both.
- `src/hybrid_retriever.py` — reads `[retrieval]` (`fusion_mode` + `dense_weight`/`sparse_weight`). When
  `convex`, builds a `BM25Okapi` index over the in-memory chunk corpus at construction and fuses the union of
  dense-RRF and BM25 top-k via `convex_fuse`; degrades to RRF if `rank-bm25` is missing or the corpus is empty.
- `indexer.toml [retrieval]` (default `fusion_mode = "rrf"`, weights 0.7/0.3) + `rank-bm25` in requirements.
- `tools/coir_eval.py` — new `dense+sparse` config (the §validation arm): BM25 over the CoIR corpus, dense via
  FAISS (same tie-breaking as the dense baseline — critical, CoIR's duplicate docs otherwise shift MRR by tie
  order alone), convex-fused, graded paired vs dense with CIs.
- **Directional result (cosqa, n=500):** sparse lift mrr@10 = **−0.034 ± 0.028** (CI excludes 0) — a small
  but real regression, the *expected* worst case (NL→code paraphrase, where lexical match is least helpful).
  This is one subtask; the full sweep (CSN/stackoverflow, where exact identifiers matter) is pending and will
  decide whether convex ever flips to default. Verified: 85/85 tests; production convex path constructs,
  builds BM25, and ranks correctly (graceful RRF fallback on an empty index).
- **Known caveat (flagged, not addressed):** convex scores are in [0,1] vs RRF's ~0.01–0.05, so the
  structural-expansion thresholds and `_CATEGORY_BOOST` (tuned for the RRF scale) may need retuning when convex
  is enabled in production. Acceptable while it's off-by-default and validated on the flat CoIR corpus; the
  internal-repo eval (ADR-019) is where the production-scale interaction gets measured.

**2026-06-22 — P1 embedder plumbing (config-driven; swap deferred to an operator run).** On
`feature/adr-009-embedder-refresh` (sibling of P3 off the P4 truthfulness branch — touches `core.py`, not
`hybrid_retriever.py`, so no conflict). What shipped:
- `src/core.py` now reads `[embeddings]` (`model_id`, `max_seq_length`, `dimension`) via `src/config.py`
  instead of hardcoding jina-v2/512/768. The embedder load, the tokenizer, the empty-vector dims, and
  `MultiIndexManager`'s default dimension are all config-driven. Defaults preserve the jina-v2 stack exactly,
  so an unconfigured repo is byte-identical to before.
- **Reindex guard:** `MultiIndexManager.load_or_create` raises if an existing FAISS index's dimension ≠ the
  configured dimension — turning a silent corrupt-state into a loud "delete `.code-index` and reindex" error.
- Docs: `[embeddings]` comments + `src/CLAUDE.md` now explain the one-time reindex; the candidate model is
  `jina-code-embeddings-1.5b`.
- **Deferred (the operator run, when home):** set `model_id` + `dimension` to the new model, delete
  `.code-index`, reindex, then validate dense vs the committed Wave-0 baseline (or just run
  `coir_eval --config dense` after pointing `[embeddings]` at the new model — re-embeds CoIR under a new
  cache tag, no code change). Heed the instruction-prefix caveat in §P1 before judging the result.
- Verified: 85/85 tests; defaults resolve to jina-v2/768/512; the reindex guard fires on a dimension change.

**2026-06-23 — P1 validation is impractical on CPU; deferred to ADR-020 (GPU).** Measured
`jina-code-embeddings-1.5b` (Qwen2 decoder, 1536-dim) embedding on this CPU at **~1 s/doc** (~64–67 s per
batch of 64): cosqa alone ≈ ~6 h, the full CoIR core set (452,082 docs) ≈ **~5.5 days** (a floor). A 1.5B
decoder is ~9–10× the params of jina-v2, so the CPU path cannot run the embedder-swap validation at a usable
cadence. The model loads cleanly via SentenceTransformer (its own pooling config; 1536-dim normalized), but
the harness still applies no task **instruction prompts**, so a CPU run would also be a quality *floor*, not
a verdict. **Decision: defer the actual P1 swap/validation behind a GPU path — see [[ADR-020]]** (AMD RX
6700 XT via DirectML / WSL2+ROCm). The P1 *plumbing* above stays committed and reversible; only the
multi-day embed run waits on GPU. The 1.5b weights are already in the HF cache for when it resumes.

**2026-07-07 — P1 candidate reselected (bge-code-v1) + config staged; GPU path is now the GCP spot T4.**
A deep-research sweep of early-2026 code embedders (20 sources, 25 claims adversarially verified) moved the
P1 default off `jina-code-embeddings-1.5b`. New pick: **`BAAI/bge-code-v1`** ("CodeR", Qwen2.5-Coder-1.5B
backbone) — it tops CoIR at **81.77** NDCG@10 vs jina-code-1.5b's 79.04 and the jina-v2 baseline's 59.56,
*and* it is **Apache-2.0** (jina-code is CC-BY-NC) and loads via plain SentenceTransformer. Notably open-weight
now beats the API ceiling (Voyage-code-3 78.53), so self-hosting costs no accuracy. The GPU path also changes:
the **GCP spot NVIDIA T4** (proven by the ADR-019 n=148 reranker run, `cloud/`) supersedes the AMD RX 6700 XT /
DirectML plan noted 2026-06-23 — a T4 at fp16 (~3 GB weights) collapses the ~5.5-day CPU embed to hours.
Config **staged (not yet validated)** on this branch: `indexer.toml [embeddings]` → `model_id = BAAI/bge-code-v1`,
`dimension = 1536`, `max_seq_length = 512` (held for a clean A/B — swap only the embedder), and a new
`query_instruct` field. `src/core.py:embed()` now wraps QUERIES as `<instruct>{query_instruct}\n<query>{text}`
(bge-code-v1 needs a query-side instruction); DOCUMENTS get no prefix, so `embed_batch()` (the indexing path)
is untouched — the reindex stays a config swap. **Pending operator run:** reindex the real_repo corpora on the
T4, then run the ADR-019 **dense arm** (bge-code-v1 vs the committed jina baseline) on our five languages
(JS/TS, Python, C++, C#) — self-reported CoIR has no per-language C++/C# breakdown, so the real-repo eval is
the verdict. Revert `model_id`+`dimension`+`query_instruct` to the jina pair if the dense arm does not confirm.

**2026-07-07 — P1 VERDICT: `bge-code-v1` CONFIRMED, promoted to default.** Ran the reindex + dense arm on the
spot T4 (VM `adr019-eval-20260707-105031`, single-process on-GPU, `max_seq_length` held at 512 for a clean A/B).
The 768→1536 dim jump rebuilt cleanly — no dimension-guard trip, `shape=(N, 1536)` confirmed. Paired dense arm B,
same 148 queries, bge-code-v1 vs the committed jina-v2 baseline:

| Repo | Lang | mrr@10 (jina → bge) | Δ mrr@10 | ndcg@10 (jina → bge) | Δ ndcg@10 |
| --- | --- | --- | --- | --- | --- |
| click | python | 0.445 → 0.512 | +0.067 | 0.505 → 0.592 | +0.086 |
| p-queue | javascript | 0.606 → 0.585 | −0.021 | 0.645 → 0.637 | −0.007 |
| serilog | c# | 0.355 → 0.552 | **+0.197** | 0.447 → 0.620 | **+0.173** |
| spdlog | c++ | 0.359 → 0.380 | +0.021 | 0.432 → 0.460 | +0.029 |
| zustand | typescript | 0.273 → 0.309 | +0.036 | 0.427 → 0.459 | +0.032 |
| **pooled** | **n=148** | **0.401 → 0.464** | **+0.062 (+15.6%)** | **0.484 → 0.549** | **+0.065 (+13.4%)** |

bge wins **4 of 5 languages** on both metrics; pooled lift **+0.062 mrr@10 / +0.065 ndcg@10**. The lone dip
(p-queue, −0.021 mrr) sits well inside its ±0.17 CI — noise, not a regression. **C# is the standout** (+0.197,
the one repo whose gap clears its own ±0.12 CI) — exactly where jina-v2 was weakest. The +22 CoIR headline
compressed to ~+0.06 on this stack (expected: different corpus, small-n private eval, contamination caveat), but
the direction is unanimous and the pooled effect is real. **Decision: `bge-code-v1` is the committed P1 default**
(`indexer.toml [embeddings]` already carries it). Result: `benchmarks/real_repo/bge_code_v1_denseB.jsonl`.

**FOLLOW-UP (separate experiment, T4) — raise `max_seq_length` 512 → ~4096.** The 512 cap is a legacy
CPU-OOM workaround (O(L²) attention crashed the process on ~4000-token inputs). Consequence today: tier-2
(~1500 tok) and tier-3 (~4000 tok) chunks are **truncated to 512** before embedding — their vectors represent
only the opening ~1/3 and ~1/8 of the chunk. The T4 makes full-length embedding feasible. The meaningful
ceiling is **~4096** (fully embeds tier-3), not the model's 32k (no chunk is that long — dead headroom).
Run this **after** the embedder A/B settles, as an independent variable, to measure whether fully-embedded
large chunks lift tier-2/3 (component/architectural) retrieval. One dial at a time so lift stays attributable.

**2026-07-01 — P3 full-sweep result: convex fusion REJECTED on CoIR (stays off).** Completed the `dense+sparse`
sweep across all four measurable subtasks. The two large corpora (CSN-python/js) were CPU-bound on the pure-Python
BM25 full-corpus scan (~7.5 s/query at 280k docs → a multi-day full run), so a seeded query sample was added to
the sparse arm — `[eval].sparse_sample_queries` (default 500, seed 13), mirroring the reranker's feasibility
sampling; the lift is measured paired on the sampled queries so its CI stays tight. Paired sparse lift (fused vs
dense, same queries):

| Subtask | n | mrr@10 lift (95% CI) | ndcg@10 lift (95% CI) | verdict |
| --- | --- | --- | --- | --- |
| cosqa | 500 | −0.034 ± 0.028 | −0.035 ± 0.027 | significant regression |
| stackoverflow-qa | 1994 | −0.008 ± 0.009 | −0.009 ± 0.007 | ~neutral (slightly neg) |
| CodeSearchNet-python | 500 (of 14918) | −0.004 ± 0.015 | −0.003 ± 0.013 | neutral |
| CodeSearchNet-javascript | 500 (of 3291) | **−0.051 ± 0.022** | −0.042 ± 0.018 | significant regression |

The decisive exact-identifier case came back against the thesis: CSN-python is a wash, CSN-javascript is a real
regression (recall@1 −0.064 ± 0.031). **No subtask shows positive lift**, so P3's validation contract (positive
paired lift, CI excluding 0) is met nowhere. `fusion_mode` stays `"rrf"` — the measured Wave-0 default.
- **Scope of the rejection:** this is CoIR (NL→code queries), which under-exercises BM25's actual strength
  (literal identifier / rare-token lookup). Weights are the untuned 0.7/0.3 default (weight-learning is ADR-014).
  So this rejects convex *on CoIR at the default weights*, not forever — the fair sparse test is the literal-query
  arm of the internal-repo eval (ADR-019). The code stays shipped-but-off so ADR-014/ADR-019 can revisit it
  without re-implementing.
