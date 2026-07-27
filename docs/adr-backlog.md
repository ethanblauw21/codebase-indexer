# Proposed ADR Backlog — Buckets from the Research Docs

> ## ⚠️ Frozen 2026-07-27 — superseded by `backlog.md` + `roadmap.md`
>
> This file did three jobs at once — **intake** (the buckets), **sequencing** (the waves), and
> **per-ADR build kits** — and that is precisely why the ADR set drifted into a backlog. Nine of the
> buckets below became `proposed` ADR files that were never built, and nothing in the process could
> ever remove one again.
>
> The three jobs are now split:
> **wants → [`backlog.md`](./backlog.md)** · **order → [`roadmap.md`](./roadmap.md)** ·
> **decisions → [`adr/`](./adr/)**. See [`README.md`](./README.md#working-lists) for the rule.
>
> **This file is history, and history is the only job it still has — edit it never.** It is kept, not
> deleted, for two things a summary would destroy: the **build kits** below (deps, touch-points,
> default decisions and citations per ADR, still accurate as research) and the **traceability table**
> mapping every research idea to its bucket. Live ADRs cite it as their source of record.
>
> **Read the wave plan below as of 2026-06-18, not as of today.** Waves 0 and 1 are complete —
> including two component rejections the harness earned — and Waves 2 and 3 were never started.
> [`roadmap.md`](./roadmap.md#closed-waves) carries the current state; where the two disagree, the
> roadmap wins.

> **Status:** planning index (not ADRs themselves). Groups the actionable ideas scattered across the 2026-06-18
> research docs into coherent, independently-decidable ADR buckets, with dependencies and priority.
> **Numbering is now assigned, not provisional.** ADRs **007–015** (the buckets below) have been drafted as
> `proposed` and live in `docs/adr/`; cross-references are recorded bidirectionally per the standard below.
> Existing ADRs: 001–002 + 003 (committed), 004/005/006 (proposed). **ADR-016** (*Persisted Symbol Tree*,
> deferred stub extending ADR-005) took the next free slot after a numbering collision — it is sequenced by a
> trigger, not a wave (see Recommended path).
> **Inputs:** `design-research-informed-improvements.md`, `suggestions-future-directions.md`,
> `merkle-tree-drift-handling.md`, `modernization-stack-review.md`, `prior-art-depth-over-breadth.md`,
> `study-codebase-memory-mcp.md`, `references-code-intelligence.md`.

## How these were grouped

One bucket = one coherent architectural decision that could ship as a unit. Ideas were clustered by *what
subsystem they change* and *what they depend on*, then ordered so prerequisites come first. Small enhancements to
already-proposed ADRs are listed as **amendments**, not new ADRs, to avoid fragmentation.

---

## Cross-reference standard (REQUIRED for multi-ADR changes)

**Every dependency link between two ADRs must be recorded in *both* ADRs — forward and back — and kept in sync in
the same PR.** A one-directional link is incomplete and must be treated as a defect.

- **In the dependent (downstream) ADR — "Depends on":** name the upstream ADR *and the exact artifact, decision,
  or confirmation it needs* from it (e.g. "ADR-008 — needs the `Edge.confidence` field and the verdict-gating
  contract"). This tells an implementor to **wait**, and tells them *what* they are waiting for, before starting.
- **In the prerequisite (upstream) ADR — "Depended on by / Downstream obligations":** reciprocally name each
  downstream ADR *and what that ADR will need from this one*. When the upstream ADR's implementation completes,
  the implementor **must resolve those listed obligations** — answer the open questions, confirm the contracts —
  as part of closing it, rather than leaving the downstream implementor to rediscover them later.

**Why both directions:** (1) downstream can sequence and wait correctly; (2) upstream proactively closes the loop
at completion, when the context is freshest and cheapest to answer — not at the downstream's implementation time.

The dependency diagram and traceability table below are the *index*; the paired "Depends on" / "Depended on by"
entries inside each ADR are the *source of truth*. (Recommended: add both fields to `ADR-000-template.md` so this
is structural, not a convention people forget.)

---

## The buckets (proposed new ADRs)

### ADR-007 — Evaluation & Benchmark Harness  ·  *foundational, do first*
**Why a bucket:** both the accuracy moat and the modernization work are unprovable without a scorecard. This is
the shared substrate.
**Scope:** adopt **CoIR** (code-IR benchmark) as the standard; extend `tools/eval_retrieval.py` (MRR@5/Hit@k) to
report retrieval *and* extraction metrics + tokens/tool-calls/latency; baseline the current stack. Fix the
competitor's validity holes (blind grading, multiple repos/language).
**Sources:** design-doc A6/A7; modernization (CoIR §validate); study §9.5.
**Depends on:** nothing. **Unlocks:** ADR-008, ADR-009.

### ADR-008 — Measured Conformance & Edge Confidence  ·  *the moat*
**Why a bucket:** turns "we claim accuracy" into reported, reproducible precision/recall — the depth-over-breadth
thesis made real. *(This is the bucket the design doc tentatively called "ADR-007"; renumbered behind the
harness it depends on.)*
**Scope:** B1 precision/recall conformance reporting; B3 curated feature-exercising micro-benchmarks; B2 per-
language/per-tier accuracy table (README); B5 "prefer-unknown" as a measured, tunable confidence-threshold
policy; A3 confidence scores on edges (boolean `candidate` → graded `confidence`). B4 execution-verified ground
truth is a **Phase 2** (heavier).
**Sources:** design-doc Bucket B + A3; prior-art-depth-over-breadth (whole doc).
**Depends on:** ADR-007 (harness), builds on ADR-004 (tiers). **Pairs with:** ADR-011.

### ADR-009 — Retrieval Stack Modernization  ·  *highest ROI engine work*
**Why a bucket:** swap-in component upgrades to the retrieval pipeline; none touch `stable_id`/schema/MCP surface.
**Scope:** P1 embedder refresh (jina-v2 → jina-code-1.5b or Qwen3); P2 Late Chunking (keep LLM summarizer
optional); P3 add sparse/BM25 signal + weighted/convex fusion over raw RRF; P4 reranker option (CoRNStack /
Qwen3-Reranker, keep cross-encoder default); S2 late-interaction (ColBERT-style) as an **optional research
phase**.
**Sources:** modernization-stack-review (Pillars 1–4); suggestions S2.
**Depends on:** ADR-007 (to validate each change). **Note:** P1 is a one-time reindex.

### ADR-010 — Content-Addressed Drift Detection & Incremental Reindexing
**Why a bucket:** a reconciliation + drift-alarm layer in front of the existing diff pipeline.
**Scope:** Merkle (or Prolly-tree) state structure; mandatory `(mtime,size)` gate; git-SHA reconciliation
(§6.8); 3-tier check (mtime heartbeat / vital pre-flight / full backstop) with the **vital set auto-derived from
ADR-006 centrality**; root-hash drift alarm for human↔AI drift; optional FastCDC for sub-file localization.
**Sources:** merkle-tree-drift-handling (all sections); design-doc A2.
**Depends on:** extends ADR-005 (self-healing/versioning); vital tier depends on ADR-006.

### ADR-011 — High-Precision Call Resolution (Hybrid Type Resolution)
**Why a bucket:** the *mechanism* that earns the precision ADR-008 measures.
**Scope:** A4 LSP-style hybrid type-resolution passes for receiver-typed languages (Go/C/C++), with a correctness
gate that emits `unknown` rather than a wrong edge; emits graded-confidence edges (shared with ADR-008 A3).
**Sources:** design-doc A4; references §B–C (Total Recall, PyCG, the 34%→76% finding).
**Depends on:** ADR-004 Tier-A promotion path. **Pairs with:** ADR-008.

### ADR-012 — Cross-Repository & Cross-Service Graph
**Why a bucket:** scale the graph beyond one repo, with provable cross-service edges.
**Scope:** S3 multi-repo linked index; A5 cross-service HTTP/async edges resolved via *verifiable* contracts
(OpenAPI/proto/manifests), marked `candidate` when only heuristic.
**Sources:** suggestions S3; design-doc A5; study (competitor's HTTP_CALLS); references [21] LogicLens (prior art
to differentiate from).
**Depends on:** edge-confidence from ADR-008/011. **Caution:** LogicLens is close prior art — differentiate on
provability.

### ADR-013 — Domain-Specific / Industrial Language Adapters
**Why a bucket:** a differentiation niche (existing L5X adapter is the beachhead); depth-over-breadth applied
where no compiler index exists.
**Scope:** S4 first-class DSL/industrial adapters (IEC 61131-3 ladder/ST, HDL, mapping/config DSLs), each with a
curated conformance suite (reuses ADR-008 machinery).
**Sources:** suggestions S4; references [24][25] (ESBMC-PLC, IEC 61131-3 static analysis).
**Depends on:** ADR-004 (tiers) + ADR-008 (conformance). Mostly reuses existing adapter machinery.

### ADR-014 — Usage-Driven Adaptive Ranking  ·  *research-grade*
**Why a bucket:** closes the loop — learn from which results agents actually use.
**Scope:** S1 log retrieval outcomes as implicit relevance; tune RRF/boost weights and/or LoRA-adapt the
reranker; per-query weighting (Dynamic Alpha Tuning style).
**Sources:** suggestions S1; modernization P3 (per-query fusion).
**Depends on:** ADR-007 (harness), ADR-009 (fusion). **Note:** the most novel; potential original paper.

### ADR-015 — Local Graph & Retrieval Explorer (UI)  ·  *not research*
**Why a bucket:** make the structural output human-legible (only a TUI exists today).
**Scope:** S5 zero-config local web explorer — community map (ADR-006), interactive blast-radius/call-path, RTR
retrieval playground. Honor the "no web server / no build pipeline" constraint (static bundle over the index).
**Sources:** suggestions S5.
**Depends on:** ADR-006 (communities) for the map view.

---

## Amendments to existing ADRs (not new ADRs)

- **Amend ADR-006 (Graph Analytics):** P5 make **Leiden** the preferred backend (math reliability) with Louvain
  fallback; A1 add the **Louvain/Leiden refinement step** (split <1% internal-density communities); A2 incremental
  community recompute (or house in ADR-010). Sources: modernization P5; design-doc A1/A2.
- **Amend ADR-005 (Versioning/Self-Healing):** ADR-010's content-hash drift layer is the natural companion;
  standardize the MD5/SHA-256 split onto one hash (XXH3) while there.
- **Amend ADR-004 (Tiers):** fix the "150+" → "66 (claimed)" competitor figure; A3 graded confidence is the
  evolution of the `candidate` boolean.

---

## Recommended path — waves, gates, deliverables

```
ADR-007 (Harness)  ──┬──>  ADR-008 (Measured Conformance) ──> ADR-013 (DSL adapters)
                     │            ▲
                     │            └── ADR-011 (Hybrid Resolution) ──> ADR-012 (Cross-repo)
                     └──>  ADR-009 (Retrieval Modernization) ──> ADR-014 (Adaptive Ranking)

ADR-006 (existing) ──> ADR-010 (Drift Detection)  and  ADR-015 (UI)

ADR-005 (existing) ──> ADR-016 (Persisted Symbol Tree)   ⟂ DEFERRED — promote only on a 2nd consumer
```

Execute in waves; each wave has an exit **gate** that must pass before the next starts. Do the **amendments**
opportunistically whenever you're already editing the relevant file (they're cheap).

**Wave 0 — Foundation.** `ADR-007`. *Why first:* nothing else is provable without it. *Deliverable:* baseline
numbers for the current stack committed to the repo. **Gate:** `tools/eval_retrieval.py` runs CoIR + emits
MRR@10/NDCG@10/Recall@k + tokens/tool-calls; baseline checked in.

**Wave 1 — ROI + moat (parallelizable).** `ADR-009` (modernization) and `ADR-008`+`ADR-011` (conformance + the
resolver that earns the precision). *Why here:* 009 is the cheapest quality win; 008/011 build the differentiation.
Also fold in **amend-ADR-004** (the "150+"→"66" fix + graded confidence) since 008/011 touch `Edge`. **Gate:**
CoIR shows measurable lift over the Wave-0 baseline **and** a per-language precision/recall table is published.

**Wave 2 — Robustness.** `ADR-010` (drift). Fold in **amend-ADR-005** (hash standardization). **Gate:** cold-start
reconciliation works, git-checkout causes no re-hash storm, root-hash drift alarm fires on out-of-band edits.

**Wave 3 — Reach / research / UX (pick by need).** `ADR-013` (DSL adapters — best near-term differentiation),
`ADR-012` (cross-repo), `ADR-014` (adaptive ranking — research), `ADR-015` (UI). Fold in **amend-ADR-006** (Leiden
+ refinement) when building the UI's community view. **Gate:** per-ADR (see kits).

**Deferred (trigger-gated, not wave-gated).** `ADR-016` (Persisted File Symbol Containment Tree) extends
`ADR-005` (existing): ADR-005 derives a symbol's containment parent *on the fly* for coherence scoring; ADR-016
persists that tree as a first-class asset. It is a **deferred stub** — *not started, not yet designed* — and is
promoted **only when a second consumer needs the persisted tree** (structural tier-2 = class + its methods,
outline-as-tier-3, or RTR graph navigation). It is listed here so the seam has a home in the order, but it is
sequenced by a **trigger, not a wave exit**: do *not* schedule it into a wave. **Trigger/gate:** a feature beyond
ADR-005 coherence requires the tree → then design schema (`parent_symbol_id` vs closure table) + incremental sync.
**Caution:** any tier-2/tier-3 *boundary* redefinition changes `scope` and therefore the FAISS `stable_id` — an
**index-invalidating** change needing a planned full reindex, which is exactly why it is deferred rather than
bundled into ADR-005.

### New-dependency summary (everything the path introduces, at a glance)

| ADR | New Python packages | New models | New repo artifacts |
|-----|--------------------|-----------|--------------------|
| 007 | `coir-eval` or HF `datasets` | — | `benchmarks/`, `[eval]` in `indexer.toml` |
| 008 | none (stdlib) | — | feature-tagged fixtures; `Edge.confidence` |
| 009 | `rank-bm25` (sparse); maybe `einops` for new embedder | jina-code-embeddings-1.5b **or** Qwen3-Embedding (+ Qwen3-Reranker) | rebuilt FAISS indexes (dim change); `[retrieval]` config |
| 010 | `xxhash`; optional `fastcdc` | — | `.code-index/merkle.json` (or `dir_hashes` table); `[drift]` config |
| 011 | none (uses tree-sitter) | — | per-language resolution pass module |
| 012 | `prance`/`openapi-spec-validator`, `protobuf` | — | multi-project schema; cross-service extractor |
| 013 | per-DSL grammars (tree-sitter or `lxml` for XML DSLs) | — | new adapters + conformance fixtures |
| 014 | `scikit-learn` (weight tuning); optional `peft` (LoRA) | — | feedback-log table; tuned-weights file |
| 015 | none (stdlib `http.server`) | — | `src/web/` static bundle |
| 016 *(deferred)* | none | — | `parent_symbol_id` column (or closure table) on `symbols`; **index-invalidating if tier-2/3 boundaries change** |

> Already installed (don't re-add): `faiss-cpu`, `sentence-transformers`, `transformers`, `tree-sitter` (+ grammars),
> `mcp[cli]`, `watchdog`, `numpy`, `textual`, `rich`. ADR-006 already adds `networkx` (+ optional `leidenalg`/`igraph`).

---

## ADR build kits — concrete deps per ADR

> Each kit is what an ADR author needs so the research is *done* and writing is assembly. Verify exact file paths
> against the current tree; line references are from the 2026-06-18 audit. Citations are the `[n]` entries in
> [references-code-intelligence.md](./references-code-intelligence.md).

### ADR-007 — Evaluation & Benchmark Harness
- **Prereq ADRs:** none.
- **Deps:** `coir-eval` (or pull CoIR via HF `datasets`).
- **Touches:** `tools/eval_retrieval.py` (extend beyond the fixed 10-query set), new `benchmarks/` dir, new
  `[eval]` block in `indexer.toml`.
- **Decisions (recommended default):** metrics = MRR@10 + NDCG@10 + Recall@{1,5,10} + tokens/tool-calls/latency;
  CoIR subset = code-retrieval + text↔code tasks matching our 5 languages; grading = automated vs CoIR qrels (no
  human). Note: this is the **retrieval** arm; the **extraction** precision/recall arm lives in ADR-008.
- **Cite:** [36] CoIR; study §9.5; design-doc A6/A7.
- **Done when:** current stack baselined, numbers committed; a fast subset runnable in CI.
- **Open Qs:** which CoIR subtasks are representative; how to project the 3-tier index onto CoIR's flat corpus.
- **Effort:** M.

### ADR-008 — Measured Conformance & Edge Confidence
- **Prereq ADRs:** 007 (harness pattern), 004 (tiers).
- **Deps:** none (reuses fixtures).
- **Touches:** `tests/test_adapter_snapshots.py` (extend to precision/recall), `tests/fixtures/` (add
  feature-tagged micro-benchmarks declaring expected symbols/edges), `src/adapters/base.py` (`Edge` gains
  `confidence: float | None`), `src/db.py` (edge write threads confidence), `src/MCPServer.py` (verdict tools gate
  on a confidence floor), `README.md` (accuracy table).
- **Decisions (default):** precision = correct emitted edges ÷ emitted; recall = correct emitted ÷ ground-truth;
  verdict confidence floor = 0.5; ground truth = hand-authored fixtures (Phase 1), execution-verified (Phase 2).
- **Cite:** [2] Total Recall, [3] Judge/CATS, [6] Deblometer; prior-art-depth-over-breadth (whole doc).
- **Done when:** per-language precision/recall reported; verdict tools return "insufficient — candidate-only"
  below the floor; README table auto-generated.
- **Open Qs:** authoring ground-truth edges at scale; threshold tuning.
- **Effort:** M (Phase 1).

### ADR-009 — Retrieval Stack Modernization
- **Prereq ADRs:** 007 (to validate every change).
- **Deps:** `rank-bm25` (sparse signal); new embedder weights (HF). **`stable_id` is model-independent** — only
  vectors change; the ID formula and `tests/test_stable_id.py` are untouched.
- **Touches:** `src/core.py` (embedder load + vector dim → FAISS rebuild; `MultiIndexManager`), `indexer.toml`
  (`[embeddings]`, `[reranker]`, new `[retrieval]`), `src/ast_chunker.py` (+ late-chunking path),
  `src/hybrid_retriever.py` (add sparse retriever + fusion mode), `src/summarizer.py` (demote to optional).
- **Decisions (default):** embedder = `jina-code-embeddings-1.5b` (lowest friction, same vendor); fusion = convex
  combination w/ score normalization (fallback RRF); sparse = BM25 on; late chunking = on, LLM summarizer optional.
- **Cite:** [37] jina-code, [38] Qwen3, [40] CoRNStack, [42] late chunking, [43] contextual retrieval, [47]
  fusion; modernization-stack-review (Pillars 1–4).
- **Done when:** CoIR beats the Wave-0 baseline; embedder swap is config-driven; the one-time reindex is documented.
- **Open Qs:** FAISS index-file implications of a vector-dim change; sparse-index storage cost.
- **Effort:** M–H.

### ADR-010 — Content-Addressed Drift Detection
- **Prereq ADRs:** extends 005; vital tier needs 006.
- **Deps:** `xxhash` (XXH3 leaves); optional `fastcdc` (sub-file localization). Prolly Trees have no mature Python
  lib → **default to a Merkle tree, list Prolly as a future option** (merkle doc §7).
- **Touches:** `src/incremental_indexer.py` (`scan_disk`→merkle build; front-end `compute_diff`), new
  `.code-index/merkle.json` (or `dir_hashes` table in `db.py`), `src/MCPServer.py` (startup reconciliation +
  drift-alarm tool), git via `subprocess`.
- **Decisions (default):** structure = Merkle (Prolly later); hash = XXH3; `(mtime,size)` gate mandatory; git-SHA
  reconciliation on when `.git` present; vital set auto-derived from ADR-006 centrality, fallback `vital_paths`
  glob in `indexer.toml`.
- **Cite:** merkle-tree-drift-handling (all); [29] sync-mht, [31] Prolly, [32] FastCDC, [33] Codified Context.
- **Done when:** incremental run skips unchanged subtrees; cold-start reconciliation works; checkout storm gone;
  root-hash drift alarm fires.
- **Open Qs:** Merkle vs Prolly final call; persistence location; is CDC granularity worth it now.
- **Effort:** M.

### ADR-011 — High-Precision Call Resolution
- **Prereq ADRs:** 004 (Tier-A promotion path); pairs with 008.
- **Deps:** none new (tree-sitter already present).
- **Touches:** `src/adapters/cpp_adapter.py` (and a Go adapter when added), `src/adapters/base.py` (shares
  `Edge.confidence` with 008), a new per-language type-resolution pass module.
- **Decisions (default):** correctness gate — emit `unknown`, never a wrong resolved target; graded confidence
  per resolution strategy (mirror the competitor's 6-strategy scores).
- **Cite:** [2] Total Recall, [7] PyCG, the 34%→76% type-inference finding; design-doc A4.
- **Done when:** call-edge resolution rate rises on Go/C/C++ fixtures **with precision held** (measured by 008).
- **Open Qs:** how far to push C++ templates; Go/C adapters aren't Tier-A yet.
- **Effort:** H.

### ADR-012 — Cross-Repository & Cross-Service Graph
- **Prereq ADRs:** 008/011 (edge confidence).
- **Deps:** `prance` / `openapi-spec-validator`, `protobuf` (contract parsing).
- **Touches:** `src/db.py` (multi-project schema — Project nodes already exist), `src/import_resolver.py`, new
  cross-service extractor, `src/MCPServer.py` (multi-repo selection).
- **Decisions (default):** cross-service edges are `candidate` unless a parsed contract (OpenAPI/proto/manifest)
  verifies them.
- **Cite:** [21] LogicLens (**differentiate** — close prior art), study (competitor HTTP_CALLS), [12] RepoGraph.
- **Done when:** two linked repos queryable; cross-service edges carry provenance/confidence.
- **Open Qs:** cross-repo symbol identity; differentiation from LogicLens.
- **Effort:** H.

### ADR-013 — Domain-Specific / Industrial Adapters
- **Prereq ADRs:** 004 (tiers), 008 (conformance).
- **Deps:** per-DSL grammars (tree-sitter where available; `lxml` for XML DSLs like L5X / PLCopen XML).
- **Touches:** `src/adapters/` (new adapters following `l5x_adapter.py`), `src/adapters/__init__.py` (registry),
  `tests/fixtures/` (conformance fixtures).
- **Decisions (default):** first target = expand the existing **L5X / IEC 61131-3 Structured Text** beachhead.
- **Cite:** [24] ESBMC-PLC, [25] IEC 61131-3 static analysis; suggestions S4.
- **Done when:** new DSL adapter passes its conformance suite; tier table updated.
- **Open Qs:** grammar availability per DSL.
- **Effort:** M per DSL.

### ADR-014 — Usage-Driven Adaptive Ranking  *(research-grade)*
- **Prereq ADRs:** 007 (harness), 009 (fusion).
- **Deps:** `scikit-learn` (weight tuning); optional `peft` (LoRA reranker adaptation).
- **Touches:** `src/hybrid_retriever.py` (learned weights), new feedback-log table in `src/db.py`,
  `src/MCPServer.py` (capture which results were used).
- **Decisions (default):** start with label-free pseudo-relevance / weight tuning before any LoRA fine-tune.
- **Cite:** [15] ReFIT, [16], [17]; modernization P3 (Dynamic Alpha Tuning).
- **Done when:** tuned weights beat static RRF on a CoIR/usage holdout.
- **Open Qs:** define "used"; offline training cadence.
- **Effort:** M–H.

### ADR-015 — Local Graph & Retrieval Explorer (UI)  *(not research)*
- **Prereq ADRs:** 006 (communities for the map).
- **Deps:** none heavy — static HTML/JS reading the index; optional stdlib `http.server`.
- **Touches:** new `src/web/` bundle reading `graph.db` + FAISS; reuse ADR-006's DSM renderer.
- **Decisions (default):** static bundle, no framework (honors the no-build constraint).
- **Cite:** n/a (UI).
- **Done when:** local explorer renders community map + blast-radius + an RTR retrieval playground.
- **Open Qs:** exposing FAISS query to the browser without a long-running server.
- **Effort:** M–H.

---

## Traceability — every idea → its bucket

| Idea (source doc) | Bucket |
|---|---|
| A1 Louvain refinement, A2 incremental recompute, P5 Leiden | amend ADR-006 (A2 also ADR-010) |
| A3 edge confidence | ADR-008 / ADR-011 |
| A4 LSP-style hybrid resolution | ADR-011 |
| A5 cross-service edges, S3 cross-repo | ADR-012 |
| A6 harness, A7 perf targets, CoIR | ADR-007 |
| B1 precision/recall, B2 accuracy table, B3 micro-benchmarks, B5 prefer-unknown policy | ADR-008 |
| B4 execution-verified ground truth | ADR-008 Phase 2 |
| S1 usage reranking | ADR-014 |
| S2 late interaction | ADR-009 (optional phase) |
| S4 DSL/industrial adapters | ADR-013 |
| S5 web UI | ADR-015 |
| P1 embedder, P2 late chunking, P3 sparse+fusion, P4 reranker | ADR-009 |
| Merkle/Prolly drift, 3-tier, git reconciliation, FastCDC, drift alarm | ADR-010 |

## Deferred / out of scope
- **Supply-chain release verification** (study §9.6 — SBOM, signing, AV gating): only relevant if a distributed
  binary ships; revisit then.
- **Verifiable retrieval (VeriANN-style Merkle proofs):** only if the index is ever served remotely.
