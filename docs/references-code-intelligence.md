# References: Code Intelligence, Call-Graph Accuracy, and Structural Retrieval

> A working bibliography for the indexer's design rationale — especially the
> **depth-over-breadth / provable-accuracy** thesis (see [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md))
> and the [research-informed design proposal](./design-research-informed-improvements.md).
>
> **⚠️ Verification note.** Titles, venues, years, and URLs below were checked against search results and (where
> noted) the source PDFs. **Some author lists were taken from search summaries, not the publisher page** — these
> are marked *(authors: verify)*. Confirm every entry against the linked source before using it in anything
> formal (a paper, a README claim, an external doc). Treat this as a curated reading list, not a camera-ready
> bibliography.

---

## A. The competitor / foil

**[1] Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP.**
Martin Vogel, Falk Meyer-Eschenbach, Severin Kohler, Elias Grünewald, Felix Balzer. arXiv:2603.27277v1
[cs.SE], 28 March 2026. Code: https://github.com/DeusData/codebase-memory-mcp (MIT, v0.5.5).
https://arxiv.org/abs/2603.27277
- *Relevance:* the breadth-first system this project is positioned against (the `codebase-memory-mcp` named in
  ADR-004). 66 languages via generic Tree-Sitter extraction + confidence-scored edges; **83% vs 92% quality**,
  **~10× fewer tokens**, **2.1× fewer tool calls**. Its weakest result (**macro-heavy C, 0.58 vs 1.00**) and its
  fuzzy 0.30-confidence resolution fallback are the empirical evidence that generic breadth sacrifices accuracy.
  Full analysis in [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md).

---

## B. Call-graph accuracy — the academic spine of "claim less, prove it"

**[2] Total Recall? How Good Are Static Call Graphs Really?**
Dominik Helm et al. (OPAL group, TU Darmstadt). Proc. 33rd ACM SIGSOFT Int'l Symposium on Software Testing
and Analysis (**ISSTA 2024**). DOI: 10.1145/3650212.3652114.
PDF: https://www.opal-project.de/articles/TotalRecall@ISSTA24.pdf · https://dl.acm.org/doi/10.1145/3650212.3652114
- *Relevance:* **keystone citation.** Argues generic test suites inadequately assess accuracy and that
  systematically constructed, **ground-truth-verified** corpora (here: dynamic/executed call graphs) are
  required to substantiate any soundness claim. Directly supports the conformance-suite moat.
- *(authors: verify full list — search surfaced "Dominik Helm" as lead; OPAL group typically Helm, Kübler, Reif,
  Eichberg, Mezini.)*

**[3] Judge: Identifying, Understanding, and Evaluating Sources of Unsoundness in Call Graphs.**
Michael Reif, Florian Kübler, Michael Eichberg, Dominik Helm, Mira Mezini. Proc. 28th ACM SIGSOFT ISSTA
(**ISSTA 2019**). Introduced the **CATS** curated micro-benchmark.
https://dl.acm.org/doi/10.1145/3293882.3330555
- *Relevance:* the academic name for this project's approach — *"hand-crafted micro-benchmark suites … crafted
  to exercise individual language features … check if the resulting CG contains the expected calls."* That is the
  golden-snapshot conformance fixture suite, as a research method.

**[4] Systematic Evaluation of the Unsoundness of Call Graph Construction Algorithms for Java.**
*(authors: verify — likely Reif/Eichberg et al.; may be the extended/journal companion to [3].)*
https://www.researchgate.net/publication/330226529
- *Relevance:* corroborates that **no** mainstream tool (Soot, WALA, Doop) builds a sound call graph across all
  features — soundness is a spectrum to be measured, not a checkbox.

**[5] On the Soundness of Call Graph Construction in the Presence of Dynamic Language Features — A Benchmark and
Tool Evaluation.** Li Sui, Jens Dietrich, Amjed Tahir, George Fourtounis. APLAS 2018, Springer LNCS.
*(authors: verify.)* https://link.springer.com/chapter/10.1007/978-3-030-02768-1_4
- *Relevance:* benchmark + tool eval; dynamic features (reflection, dynamic dispatch) defeat static extraction —
  exactly the edges Codebase-Memory resolves at confidence 0.30–0.55.

**[6] Deblometer — micro-benchmark of 59 curated Java test cases with manually curated ground truth.**
*(citation to confirm — surfaced in search summary; verify authors/venue before citing.)*
- *Relevance:* precedent for **manually curated ground truth enabling precise precision/recall measurement** —
  the rigor model for per-language conformance.

---

## C. Resolution techniques (lifting accuracy without LLMs)

**[7] PyCG: Practical Call Graph Generation in Python.**
Vitalis Salis, Thodoris Sotiropoulos, Panos Louridas, Diomidis Spinellis, Dimitris Mitropoulos. ICSE 2021.
*(authors: verify.)* https://arxiv.org/abs/2103.00587
- *Relevance:* practical static (no-runtime) call-graph generation; reference point for what a careful
  language-specific resolver achieves vs. generic extraction.

**[8] Call Me Maybe: Enhancing JavaScript Call Graph Construction using Graph Neural Networks.**
*(authors: verify.)* arXiv:2506.18191. https://arxiv.org/abs/2506.18191
- *Relevance:* frames the **soundness vs. precision** trade-off via Rice's theorem; notes WALA is deliberately
  *"precise but incomplete"* — the academic form of "prefer unknown over a wrong edge."

**[9] An Empirical Study of Large Language Models for Type and Call Graph Analysis in Python and JavaScript.**
Empirical Software Engineering (Springer), 2025. DOI: 10.1007/s10664-025-10704-3.
*(authors: verify.)* https://link.springer.com/article/10.1007/s10664-025-10704-3
- *Relevance:* LLM-assisted type/call-graph analysis; useful if a future hybrid resolver pass is considered.

---

## D. Benchmarks & ground truth (the "prove it" layer)

**[10] An Execution-Verified Multi-Language Benchmark for Code Semantic Reasoning (TraceEval).**
Yikun Li, Jinfeng Jiang, Ting Zhang, Chengran Yang, Chenxing Zhong, Yin Yide, Leow Wen Bin, Eng Lieh Ouh,
Lwin Khin Shar, David Lo. arXiv:2605.11006 (2026). Code: https://github.com/yikun-li/TraceEval *(repo slug
shown as `TraceEva` in metadata — verify exact URL).* https://arxiv.org/abs/2605.11006
- *Relevance:* ground truth by **execution**, not human judgment — the strongest form of "prove it," and
  multi-language. Template for an execution-verified accuracy layer atop the conformance suite.

---

## E. Structural retrieval context (adjacent, from [1]'s reference list)

**[11] cAST: Enhancing Code Retrieval-Augmented Generation with Structural Chunking via Abstract Syntax Tree.**
Yilin Zhang, Xinran Zhao, Zora Zhiruo Wang, Chenyang Yang, Jiayi Wei, Tongshuang Wu. Findings of EMNLP 2025.
https://aclanthology.org/2025.findings-emnlp.430/
- *Relevance:* AST-structural chunking beats flat text chunking for retrieval — supports the structure-aware
  approach this indexer takes (`ast_chunker`).

**[12] RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph.**
Ouyang et al. ICLR 2025. https://arxiv.org/abs/2410.14684
- *Relevance:* repo-level code graphs boost agents by **+32.8% relative** on SWE-bench — independent evidence
  that structural retrieval helps, complementing this project's graph layer.

**[13] LoCoBench-Agent: An Interactive Benchmark for LLM Agents in Long-Context Software Engineering.**
Qiu et al. arXiv:2511.13998 (2025). https://arxiv.org/abs/2511.13998
- *Relevance:* documents the comprehension-vs-token-efficiency Pareto frontier the graph layer attacks.

---

## G. Future-direction sources (from [suggestions-future-directions.md](./suggestions-future-directions.md))

*Reranking / relevance feedback (S1):*
**[15] ReFIT: Relevance Feedback from a Reranker during Inference.** arXiv:2305.11744. *(authors: verify.)*
https://arxiv.org/abs/2305.11744
**[16] Incorporating Relevance Feedback for Information-Seeking Retrieval using Few-Shot Document Re-Ranking.**
arXiv:2210.10695. *(authors: verify.)* https://arxiv.org/abs/2210.10695
**[17] Modeling Relevance Ranking under the Pre-training and Fine-tuning Paradigm.** arXiv:2108.05652.
*(authors: verify.)* https://arxiv.org/abs/2108.05652

*Late interaction / multi-vector retrieval (S2):*
**[18] ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT.**
Omar Khattab, Matei Zaharia. SIGIR 2020. arXiv:2004.12832. https://arxiv.org/abs/2004.12832
**[19] ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction.** arXiv:2112.01488.
*(authors: verify.)* https://arxiv.org/abs/2112.01488
**[20] CITADEL: Conditional Token Interaction via Dynamic Lexical Routing for Efficient and Effective
Multi-Vector Retrieval.** arXiv:2211.10411. *(authors: verify.)* https://arxiv.org/abs/2211.10411

*Cross-repository / cross-service graphs (S3):*
**[21] LogicLens: Leveraging Semantic Code Graph to Explore Multi-Repository Large Systems.** arXiv:2601.10773
(2026). *(authors: verify — closest prior art to S3.)* https://arxiv.org/abs/2601.10773
**[22] LIDL: LLM Integration Defect Localization via Knowledge Graph-Enhanced Multi-Agent Analysis.**
arXiv:2601.05539 (2026). *(authors: verify.)* https://arxiv.org/abs/2601.05539
**[23] CKGFuzzer: LLM-Based Fuzz Driver Generation Enhanced by Code Knowledge Graph.** arXiv:2411.11532.
*(authors: verify.)* https://arxiv.org/abs/2411.11532
*(See also [1] Codebase-Memory and [12] RepoGraph above.)*

*Industrial / DSL static analysis (S4):*
**[24] ESBMC-PLC: Formal Verification of IEC 61131-3 Ladder Diagram Programs Using SMT-Based Model Checking.**
arXiv:2606.15461 (2026). *(authors: verify.)* https://arxiv.org/abs/2606.15461
**[25] Static Code Analysis of IEC 61131-3 Programs: Comprehensive Tool Support and Experiences from
Large-Scale Industrial Application.** Prähofer et al. *(authors/venue: verify.)*
https://www.researchgate.net/publication/307551694

---

## H. Merkle trees — drift detection & verifiable vector search (from [merkle-tree-drift-handling.md](./merkle-tree-drift-handling.md))

*Verifiable vector search (integrity, not drift — see eval doc §2a):*
**[26] VeriANN — Practical and Verifiable Encrypted Vector Search for Retrieval-Augmented Generation.**
IACR ePrint 2026/923. Client verification reduced to a single hash check against a published Merkle root.
https://eprint.iacr.org/2026/923
**[27] Providing Authentication and Integrity in Outsourced Databases using Merkle.** UC Berkeley CS261 reading.
https://people.eecs.berkeley.edu/~raluca/cs261-f15/readings/merkleodb.pdf
**[28] A Survey of Optimized Merkle Tree Structures for Query Authentication.** IJSAT 2025.
https://www.ijsat.org/papers/2025/3/6844.pdf

*Change detection / file sync (the applicable foundation — eval doc §2b):*
**[29] sync-mht — Fast incremental file transfer using Merkle-Hash-Trees.** Haskell package; O(log n) folder
comparison. https://hackage.haskell.org/package/sync-mht
**[30] Filesystem embedded Merkle trees.** US Patents 11,741,067 and 11,704,295.

## I. More-supportive structures & agentic-drift framing (from [merkle-tree-drift-handling.md](./merkle-tree-drift-handling.md) §7)

*Versioned / diff-optimized structures:*
**[31] Prolly Trees (Probabilistic B-trees).** Engineering construct from Noms / Dolt — B-tree × Merkle-tree
hybrid with history-independence, fast diffs, structural sharing. *Not a peer-reviewed paper — cite the docs.*
https://docs.dolthub.com/architecture/storage-engine/prolly-tree · vector index on prolly trees:
https://www.dolthub.com/blog/2024-10-08-how-to-build-a-vector-index-with-prolly-trees/
**[32] FastCDC: A Fast and Efficient Content-Defined Chunking Approach for Data Deduplication.** Wen Xia et al.
*(authors: verify.)* USENIX ATC 2016. Sub-file change localization via content-defined boundaries.
https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia

*Human↔AI drift / agentic collaboration (the novelty vein):*
**[33] Codified Context: Infrastructure for AI Agents in a Complex Codebase.** arXiv:2602.20478 (2026).
*(authors: verify — closest prior art to a tiered, git-driven drift detector.)* https://arxiv.org/abs/2602.20478
**[34] Scaling Human-AI Coding Collaboration Requires a Governable Consensus Layer.** arXiv:2604.17883 (2026).
*(authors: verify.)* https://arxiv.org/abs/2604.17883
**[35] Human-AI Synergy in Agentic Code Review.** arXiv:2603.15911 (2026). *(authors: verify.)*
https://arxiv.org/abs/2603.15911

## J. Stack modernization — 2026 SOTA per component (from [modernization-stack-review.md](./modernization-stack-review.md))

*Code embedding models & benchmark:*
**[36] CoIR: A Comprehensive Benchmark for Code Information Retrieval Models.** ACL 2025. arXiv:2407.02883.
Code: github.com/coir-team/coir. The standard code-IR scorecard. https://arxiv.org/abs/2407.02883
**[37] Efficient Code Embeddings via Generation Models** (jina-code-embeddings 0.5b/1.5b). arXiv:2508.21290.
*(authors: verify.)* https://arxiv.org/abs/2508.21290
**[38] Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation Models.** arXiv:2506.05176.
Unified embed + rerank family. https://arxiv.org/abs/2506.05176
**[39] CodeXEmbed: A Generalist Embedding Model Family for Multilingual and Multi-task Code Retrieval.**
arXiv:2411.12644. *(authors: verify.)* https://arxiv.org/abs/2411.12644
**[40] CoRNStack: High-Quality Contrastive Data for Better Code Retrieval and Reranking.** arXiv:2412.01007.
*(authors: verify.)* https://arxiv.org/abs/2412.01007
**[41] Granite Embedding R2 Models.** IBM. arXiv:2508.21085. https://arxiv.org/abs/2508.21085

*Chunk-context techniques:*
**[42] Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models.** Jina AI.
arXiv:2409.04701. https://arxiv.org/abs/2409.04701
**[43] Anthropic — Introducing Contextual Retrieval** (Sept 2024). Contextual embeddings + contextual BM25 +
rerank. https://www.anthropic.com/news/contextual-retrieval

*Reranking SOTA:*
**[44] How Good are LLM-based Rerankers? An Empirical Analysis of State-of-the-Art Reranking Models.**
arXiv:2508.16757. *(authors: verify.)* https://arxiv.org/abs/2508.16757
**[45] RankZephyr: Effective and Robust Zero-Shot Listwise Reranking is a Breeze!** arXiv:2312.02724.
*(authors: verify.)* https://arxiv.org/abs/2312.02724
**[46] FIRST: Faster Improved Listwise Reranking with Single Token Decoding.** arXiv:2406.15657 / 2411.05508
(reproduction). *(authors: verify.)* https://arxiv.org/abs/2406.15657

*Fusion beyond RRF:*
**[47] Dense–Sparse Hybrid Retrieval & fusion comparisons** (Convex Combination vs RRF; Dynamic Alpha Tuning,
2025; Dynamic Weighted RRF, 2025). *(survey/benchmark cluster — verify individual citations.)*
https://arxiv.org/abs/2604.01733
**[48] Leiden — From Louvain to Leiden: Guaranteeing Well-Connected Communities.** Traag, Waltman, van Eck.
*Scientific Reports* (2019). https://www.nature.com/articles/s41598-019-41695-z

---

## K. Codebase-indexing systems & hybrid vector+graph architectures

> **⚑ FLAG FOR THE NEXT AGENT — pull these down and write a reference doc.**
> The three entries below were triaged from a 2026-06-18 paper search but **not yet read in full** —
> only abstracts / search summaries. Next pass: **fetch the PDFs, read them, and create a thematic
> deep-dive under `docs/`** in the style of [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md)
> and [modernization-stack-review.md](./modernization-stack-review.md) (suggested name:
> `docs/study-hybrid-indexing-systems.md`). Extract *actionable* takeaways tied to ADRs, not summaries:
> - **[49] HybridCode** → validates the vector+graph thesis; mine its adaptive query-routing for the RTR
>   pipeline / `iterative_retriever` (when to traverse vs. rerank). Differentiator to record: it uses an
>   LLM-inferred graph + cloud DBs (Qdrant/Neo4j) — this project uses **EXTRACTED** edges, local, deterministic.
> - **[50] Persistent vs. Ephemeral** → the measured baseline for the accuracy moat: **+33.94% relative Exact
>   Match** from persistent cross-file context. Pull the exact test setup + metric for **ADR-007 (eval harness)**
>   and **ADR-008 (measured conformance)**; corroborates **ADR-016 (persisted symbol tree)**.
> - **[51] AI Agent over SaaS codebase** → take the queue-based ingestion-pipeline *shape* for very large
>   corpora → **ADR-012 (cross-repo/cross-service graph)**; ignore the cloud stack (OpenSearch /
>   `text-embedding-3-large`), which is counter to the local/offline stance.
>
> **⚠️ Metadata below is from search summaries — verify authors, venue, year, and URL against the source PDF before any formal use.**

**[49] HybridCode: A Dual-Database Framework for Intelligent Codebase Analysis and Article Generation.**
V.S.N.L. Yarramallu, R.J. Gangireddy, et al. 2025 5th Asian Conference (IEEE Xplore), 2025.
*(authors/venue/URL: verify — ieeexplore.ieee.org.)*
- *Relevance:* the closest architectural twin in the search — vector (Qdrant + Cohere embeddings) **and** graph
  (Neo4j), with an adaptive query processor that "dynamically chooses the best retrieval strategy, combining
  vector similarity with graph traversal." Independent prior art for the hybrid vector+graph thesis; its routing
  idea is a candidate for `iterative_retriever`. Contrast to record: LLM-inferred graph + cloud DBs vs. this
  project's EXTRACTED edges, local and deterministic.

**[50] Persistent vs. Ephemeral: A Comparative Analysis of Codebase Indexing in AI Programming Tools.**
M.T. Khan, D. Yadav, K. Kumar, J. Sharma, F. Siddiqui, et al. *Tejas Journals.*
*(year/venue/URL: verify — tejasjournals.com PDF.)*
- *Relevance:* most on-point of the set. Persistent (embedding-based FAISS/HNSW) indexing maintains durable,
  queryable codebase representations for cross-file context; reports **+33.94% relative Exact Match** from that
  context — a citable, measured benefit for the persistent-index + provable-accuracy thesis. Feeds ADR-007/008
  and corroborates ADR-016.

**[51] AI Agent for Conversational Q&A over a SaaS Codebase using Large Language Models.**
O. Cherednichenko, D. Sytnikov, N. Romankiv, et al. CEUR Workshop Proceedings, 2025.
*(authors/URL: verify — ceur-ws.org PDF.)*
- *Relevance:* a queue-based ingestion pipeline for indexing very large SaaS codebases at scale → ADR-012. Uses
  OpenSearch + `text-embedding-3-large` (1024-dim) — cloud-first, so take the *pipeline shape*, not the stack
  (counter to the local/offline constraint).

---

## F. Theory

**[14] Rice's theorem** (H. G. Rice, 1953). Any non-trivial semantic property of programs is undecidable.
- *Relevance:* the theoretical reason soundness *and* completeness are jointly unachievable for call graphs →
  every tool must choose a trade-off → choosing **precision** (and saying so) is principled, not a limitation.

---

### Cross-references
- Competitor deep-dive: [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md)
- Thesis write-up: [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md)
- Improvement roadmap: [design-research-informed-improvements.md](./design-research-informed-improvements.md)
- Related ADRs: ADR-005 (versioning/self-healing), ADR-006 (graph analytics), ADR-017 (tiered languages, renumbered from ADR-004); §K ties to ADR-007/008 (eval + measured conformance), ADR-012 (cross-repo), ADR-016 (persisted symbol tree)
