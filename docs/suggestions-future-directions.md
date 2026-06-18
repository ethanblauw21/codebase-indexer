# Suggestions: Future Directions

> **Status:** ideas / discovery document (not an ADR; not committed work). A menu of five candidate directions —
> improvements, extensions, polishes, and UIs — derived from a full audit of the codebase on 2026-06-18.
> **Companion to:** [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md),
> [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md),
> [design-research-informed-improvements.md](./design-research-informed-improvements.md),
> [references-code-intelligence.md](./references-code-intelligence.md).

## Audit recap (what we're building on)

A local, offline-first code-intelligence engine: tree-sitter AST extraction → three-tier Jina-Code FAISS
embeddings + a SQLite symbol/edge graph → 10 MCP tools for AI assistants. ~8,500 LOC, mature: stable 60-bit
FAISS IDs, RRF tier fusion, a Retrieve→Traverse→Rerank pipeline with optional cross-encoder reranking, iterative
retrieval with early stopping, MD5 incremental indexing, import resolution, optional LLM summarization, a Textual
TUI, conformance snapshot tests, and a retrieval-eval harness. Strategic identity: **proven structural accuracy
over breadth** (see the depth-over-breadth thesis). Five Tier-A language adapters plus an existing **L5X (PLC
ladder-logic) adapter** — an unusual industrial/DSL beachhead.

**Standing gaps the suggestions target:** retrieval is evaluated only on a fixed 10-query set; indexing is
single-repo; the graph is static-only; the embedder is a single fixed model with no usage/relevance feedback;
visualization is limited to the planned DSM HTML.

---

## The five suggestions

Each tagged with category, the gap it closes, a sketch of the approach, rough effort, and **whether it warrants
an academic-literature search** (🔬 = research-grade; 🎨 = product/UX, no paper needed).

### S1 — Usage-driven learned reranking (relevance feedback loop) · 🔬 · Improvement
- **Gap:** the RTR reranker is a fixed, generic cross-encoder; RRF weights and the `_CATEGORY_BOOST` are
  hand-tuned constants. Nothing learns from which retrieved chunks the agent *actually used*.
- **Idea:** log retrieval outcomes (which `RetrievedChunk`s the agent cited / led to an accepted edit) as
  implicit relevance signal, then (a) tune RRF/boost weights and (b) optionally fine-tune or LoRA-adapt the
  reranker on this in-repo signal. A pseudo-relevance-feedback variant needs no labels at all.
- **Why it fits:** turns the eval harness from a static gate into a closed loop, and compounds the
  "measurably better" moat. Offline-friendly (training stays local).
- **Effort:** Med–High. **Research-worthy:** yes — learning-to-rank, relevance feedback, click/usage models.

### S2 — Late-interaction / multi-vector retrieval for code (ColBERT-style) · 🔬 · Extension
- **Gap:** every chunk is a single dense Jina vector; single-vector embeddings lose token-level signal on long
  or structurally dense code, and recall on rare identifiers suffers.
- **Idea:** add a late-interaction retrieval path (token-level multi-vector, MaxSim scoring) as an alternative
  or complement to single-vector FAISS, fused into the existing RRF step. Evaluate against the current tier-1
  baseline with the existing MRR@5/Hit@k harness.
- **Why it fits:** directly attacks retrieval *quality*, the other half of the accuracy story (extraction is the
  graph side; this is the embedding side). Plugs into RRF cleanly.
- **Effort:** High (index format + scoring + memory cost). **Research-worthy:** yes — late interaction, dense
  code retrieval, code embedding models.

### S3 — Cross-repository / monorepo-spanning graph with proven cross-service edges · 🔬 · Extension
- **Gap:** one `.code-index/` per repo; no cross-repo identity or cross-service linking. The competitor ships
  `HTTP_CALLS`/`ASYNC_CALLS` cross-service edges — but as *inferred*, low-confidence edges.
- **Idea:** support indexing multiple repos into a linked graph, with cross-repo edges resolved through
  *verifiable* signals (shared package manifests, OpenAPI/proto contracts, explicit dependency declarations) —
  and marked `candidate` when only heuristic. Honors the depth-over-breadth rule: cross-service edges only when
  provable.
- **Why it fits:** matches a real need (microservices/monorepos) and lets us beat the competitor on its own
  feature by being *correct* where they are merely *present*.
- **Effort:** High. **Research-worthy:** yes — repository-level / multi-repo code graphs, dependency analysis,
  cross-service call-graph construction.

### S4 — Domain-specific & industrial DSL adapters (lean into L5X / controls) · 🔬 · Extension
- **Gap:** the L5X (PLC ladder-logic) adapter is a one-off; general-purpose tools ignore industrial/DSL code
  entirely, yet that's exactly where *no* compiler-accurate index exists — the depth-over-breadth sweet spot.
- **Idea:** treat DSLs/industrial languages as a first-class tier-A target (IEC 61131-3 / ladder & structured
  text, HDL, build/config DSLs, mapping DSLs). Each gets a curated conformance suite — the same provable-accuracy
  story, applied where competitors have nothing. (The competitor paper itself flags health-informatics DSLs as a
  frontier; controls/automation is the analogous, underserved one.)
- **Why it fits:** a genuine differentiation niche aligned with an engineering-domain user base, and a natural
  extension of existing code (adapters + conformance + candidate edges all transfer).
- **Effort:** Med per DSL (grammar + adapter + fixtures). **Research-worthy:** yes — DSL/PLC static analysis,
  code intelligence for low-resource languages.

### S5 — Interactive local graph & retrieval explorer (web UI) · 🎨 · UI / Polish
- **Gap:** the only interactive surface is the Textual TUI; ADR-006 produces a static DSM HTML but nothing for
  exploring the graph, communities, blast radius, or running/visualizing retrieval queries.
- **Idea:** a local, zero-config web explorer (served from the existing index, no external deps in the spirit of
  the project's "no web server / no build pipeline" constraint — a single static bundle reading the SQLite/FAISS
  outputs) showing: community map (ADR-006), interactive blast-radius/call-path, and a retrieval playground that
  visualizes RTR stages (semantic hits → structural expansion → rerank).
- **Why it fits:** makes the engine's unique structural output legible to humans, not just agents; high
  demo/adoption value.
- **Effort:** Med–High. **Research-worthy:** no (pure UX) — excluded from the literature search.

---

## Supporting academic literature

> Targeted search run 2026-06-18 for the research-grade suggestions (S1–S4). S5 (web UI) is UX and is
> intentionally excluded. Full bibliographic entries (with author-verification flags) are in
> [references-code-intelligence.md](./references-code-intelligence.md) §G; URLs repeated here for convenience.
> **Verification note:** titles/URLs checked against search results; author lists not fully verified — confirm
> before formal citation.

### For S1 — Usage-driven learned reranking
- **ReFIT: Relevance Feedback from a Reranker during Inference** — arXiv:2305.11744. Optimizes the retriever's
  query representation at inference using the reranker's output; the closest published analog to a feedback loop
  over our RTR pipeline. https://arxiv.org/abs/2305.11744
- **Incorporating Relevance Feedback … Few-Shot Document Re-Ranking** — arXiv:2210.10695.
  https://arxiv.org/abs/2210.10695
- **Modeling Relevance Ranking under the Pre-training and Fine-tuning Paradigm** — arXiv:2108.05652.
  https://arxiv.org/abs/2108.05652
- **Gap = opportunity:** the literature is dominated by *text/passage* IR; relevance feedback driven by **agent
  usage signals on code** is underexplored. S1 is the most novel of the five — strong original-paper potential.

### For S2 — Late-interaction / multi-vector retrieval for code
- **ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT** — Khattab &
  Zaharia, SIGIR 2020. arXiv:2004.12832. The foundational late-interaction (MaxSim) model. https://arxiv.org/abs/2004.12832
- **ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction** — arXiv:2112.01488 (residual
  compression — addresses the multi-vector storage cost that would otherwise bite our FAISS budget).
  https://arxiv.org/abs/2112.01488
- **CITADEL: Conditional Token Interaction via Dynamic Lexical Routing** — arXiv:2211.10411 (efficiency for
  multi-vector retrieval at scale). https://arxiv.org/abs/2211.10411
- **Gap = opportunity:** late interaction is proven for prose; **late interaction specialized for source code**
  (identifier-level MaxSim, AST-aware token weighting) is largely open — a credible paper if paired with our
  MRR@5/Hit@k harness.

### For S3 — Cross-repository / cross-service graph
- **LogicLens: Leveraging Semantic Code Graph to Explore Multi-Repository Large Systems** — arXiv:2601.10773
  (2026). **Almost exactly this suggestion** — read first; differentiate on our *provable/candidate-edge*
  discipline. https://arxiv.org/abs/2601.10773
- **Codebase-Memory** [ref 1] — cross-service `HTTP_CALLS`/`ASYNC_CALLS` via framework route matching (inferred,
  low-confidence — the contrast point). https://arxiv.org/abs/2603.27277
- **RepoGraph** [ref 12] — repository-level code graph, +32.8% on SWE-bench. https://arxiv.org/abs/2410.14684
- **LIDL: LLM Integration Defect Localization via Knowledge Graph-Enhanced Multi-Agent Analysis** —
  arXiv:2601.05539 (cross-component defect localization over a code KG). https://arxiv.org/abs/2601.05539
- **CKGFuzzer: LLM-Based Fuzz Driver Generation Enhanced by Code Knowledge Graph** — arXiv:2411.11532 (a code-KG
  application, useful for framing graph value). https://arxiv.org/abs/2411.11532

### For S4 — Domain-specific / industrial DSL adapters (PLC focus)
- **ESBMC-PLC: Formal Verification of IEC 61131-3 Ladder Diagram Programs Using SMT-Based Model Checking** —
  arXiv:2606.15461 (2026). First open-source formal verifier with native IEC 61131-3 LD (PLCopen XML) support —
  validates that PLC/ladder is an active, underserved analysis frontier (directly relevant to the existing L5X
  adapter). https://arxiv.org/abs/2606.15461
- **Static Code Analysis of IEC 61131-3 Programs: Comprehensive Tool Support and Experiences from Large-Scale
  Industrial Application** — Prähofer et al. *(authors: verify)*. Confirms PLC static-analysis tooling is rare;
  uses call-graph + data-flow + pattern matching — the techniques this engine already has.
  https://www.researchgate.net/publication/307551694
- **Framing analog:** the Codebase-Memory paper itself flags **health-informatics DSLs (FHIRconnect)** [ref 1
  §5.5] as a frontier; industrial-controls DSLs are the parallel, underserved niche — the depth-over-breadth
  thesis applied where competitors have nothing.

---

## Quick read for prioritization

| # | Category | Effort | Research-grade | Strategic fit |
|---|---|---|---|---|
| S1 | Improvement | Med–High | 🔬 (most novel) | Compounds the "measurably better" moat; closes the eval loop |
| S2 | Extension | High | 🔬 | Upgrades retrieval quality (embedding side of accuracy) |
| S3 | Extension | High | 🔬 (prior art exists: LogicLens) | Beats competitor on correctness; real monorepo need |
| S4 | Extension | Med/DSL | 🔬 | Genuine differentiation niche; reuses all existing machinery |
| S5 | UI / Polish | Med–High | 🎨 (no paper) | Makes structural output human-legible; demo/adoption value |

**If picking one to pursue as original research:** S1 (usage-driven relevance feedback for code) is the least
crowded. **If picking one for near-term product differentiation:** S4 (industrial/DSL adapters) reuses the most
existing code and has the clearest "competitors have nothing here" story.
