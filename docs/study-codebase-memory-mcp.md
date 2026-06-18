# Study: Codebase-Memory — Tree-Sitter Knowledge Graphs for LLM Code Exploration via MCP

> **Source:** Vogel, Meyer-Eschenbach, Kohler, Grünewald, Balzer.
> *Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP.*
> arXiv:2603.27277v1 [cs.SE], 28 March 2026.
> Code & data: https://github.com/DeusData/codebase-memory-mcp (MIT, release v0.5.5)
>
> **Why this matters here:** This paper builds essentially the same thing this repo is reaching toward — a
> persistent, queryable knowledge graph over code, exposed to LLM agents. It is the closest published
> benchmark for the "graph beats grep for structural questions" hypothesis. Read it as a design reference
> and a source of empirical targets, not gospel (see *Caveats*).

---

## 1. One-paragraph summary

LLM coding agents normally explore code by reading files and grepping — cheap per call, but it scales badly:
input tokens dominate agentic cost, and text search can't follow transitive relationships ("what breaks if I
change this function?") without many iterations. Codebase-Memory replaces raw-file retrieval with a
**pre-materialized knowledge graph**: it parses 66 languages with Tree-Sitter, stores typed nodes/edges in a
single SQLite file, and exposes **14 structural query tools over MCP**. Across 31 real repositories it hits
**83% answer quality vs. 92%** for a file-exploration agent, while using **~10× fewer tokens** and **2.1× fewer
tool calls**, and is **>100× faster** per query. The headline trade-off: graphs win decisively for *structural*
questions; file exploration still wins when the task needs *actual source lines* the graph deliberately doesn't store.

---

## 2. The problem (Introduction)

- Text-based exploration scales poorly: a typical session is dozens of tool calls and hundreds of thousands of
  tokens before the agent understands enough to answer a structural question.
- Root cause is a **mismatch**: agents operate on unstructured text, but developer questions are inherently
  structural — call graphs, dependency chains, module boundaries, impact analysis.
- Following transitive references iteratively costs tokens *and* risks lost context ("lost in the middle").
- Production agentic workloads report **per-task LLM costs of several dollars** on non-trivial repos, even with caching.
- Existing structural tools (Code Property Graphs, CodeQL) are powerful but heavyweight — specialized DBs and
  query languages not designed for LLM consumption.
- **Thesis:** treat code structure as a first-class, queryable graph exposed via lightweight, agent-native MCP
  tools, with zero-infrastructure deployment.

**Stated contributions:**
1. A graph architecture combining Tree-Sitter parsing (66 languages), a multi-phase parallel build pipeline,
   6-strategy call resolution, and Louvain community detection — all in one SQLite file, zero external deps.
2. An MCP interface of 14 typed structural query tools with sub-millisecond query latency.
3. A head-to-head evaluation across 31 languages: ~10× lower token cost, 2.1× fewer tool calls, competitive
   quality (83% vs. 92%), plus analysis of where graphs win and where file exploration is still needed.

---

## 3. Related work landscape (Section 2)

Useful as a reading list of the adjacent field. Grouped:

| Area | Representative systems | Takeaway |
|---|---|---|
| Structural code analysis | Program Dependence Graphs, Code Property Graphs, CodeQL, Tree-Sitter | Powerful but heavyweight; Tree-Sitter is the de-facto fast/incremental parser (>100 grammars). AST-based chunking (cAST) improves retrieval. |
| Code retrieval for LLMs | RepoCoder (iterative BM25), DocPrompting, RACG survey | Repo-level understanding is hard; structural retrieval named a "key frontier." |
| Code graphs for retrieval | GraphCoder (ASE'24), CodexGraph (NAACL'25), KGCompass (58.3% SWE-bench Lite), RepoGraph (+32.8% on SWE-bench, ICLR'25) | Graphs measurably boost agents. |
| Graph-guided navigation | LocAgent (ACL'25), GraphCodeAgent, RANGER, Prometheus, Repository Intelligence Graph | Active 2025 research line. |
| Token efficiency | LLMLingua / LLMLingua-2 (up to 20× prompt compression), "Lost in the middle," LoCoBench-Agent | Confirms the comprehension-vs-efficiency Pareto tradeoff this work attacks. |
| Coding agents | SWE-bench, SWE-Agent, AutoCodeRover, Agentless, OpenHands | These optimize *agent strategy*; Codebase-Memory is **orthogonal** — it optimizes the *retrieval layer* and can combine with any of them. |

**Claimed differentiation:** MCP as a standard interface (any MCP agent), SQLite for zero-dependency deployment,
incremental live sync, and 66-language support in a single binary.

---

## 4. System design (Section 3)

### 4.1 Architecture — three stages
1. **Parse** — walk Tree-Sitter ASTs across 66 languages; extract definitions (functions, methods, classes,
   interfaces, enums, types with signatures, return types, receivers, decorators, complexity, export status),
   call sites, imports (8 language-specific parsers + a generic fallback), references, trait implementations.
   For **Go/C/C++**, augment with **LSP-style type resolution** (handles receivers, pointer indirection,
   package-qualified identifiers).
2. **Build** — multi-phase pipeline with parallel (pthreads) worker pools writing to per-worker in-memory
   graph buffers, merged then flushed to SQLite with deferred index creation.
3. **Serve** — MCP server exposing 14 typed tools over standard tool-call semantics.

Implementation notes: **single statically linked C binary, zero runtime dependencies**; vendors 66 Tree-Sitter
grammars as C source; runs on macOS/Linux/Windows. A **background file watcher** uses adaptive polling +
**XXH3 content hashing** to trigger incremental re-indexing of only changed files. All state in one SQLite file (WAL).

### 4.2 Graph schema (property graph)
- **Node types:** Project / Package / Folder (dir structure); File / Module (filesystem); Function / Method /
  Class / Interface / Enum / Type (AST); Route (framework detection).
- **Edge types:** CALLS, HTTP_CALLS, ASYNC_CALLS (invocation); IMPORTS; CONTAINS_*/DEFINES/DEFINES_METHOD
  (structural nesting); IMPLEMENTS; HANDLES (route handler); INHERITS, DECORATES (OOP); USES_TYPE, USAGE
  (symbol ref); THROWS, READS, WRITES (side effects); CONFIGURES; TESTS, FILE_CHANGES_WITH (semantic/git
  co-change); MEMBER_OF (community membership).
- **Cross-service edges:** HTTP_CALLS / ASYNC_CALLS discovered by matching HTTP routes/call-sites across
  services (6 framework extractors: Python, Go, Java/Spring, Kotlin/Ktor, Express.js, Laravel) with confidence
  scoring 0.0–1.0 — lets a microservice mesh be one graph.

### 4.3 Multi-pass pipeline (6 phases, one SQLite transaction)
Phases 1–4 write to an in-memory buffer (`cbm_gbuf_t`, hash maps keyed by qualified name/label/ID); temporary
sequential IDs are remapped to real SQLite row IDs on flush. Phase 6 runs after index creation.

| Phase | Output |
|---|---|
| 1. Structure | — |
| 2. Extraction | File discovery; Project/Package/Folder/File nodes + containment edges |
| 3. Resolution | Parallel definition extraction (pthreads pool) → Function/Method/Class/Interface/Enum nodes |
| 4. Enrichment | Type nodes; decorator tags; FunctionRegistry |
| 5. Flush | Parallel call/usage/semantic resolution → CALLS, IMPORTS, USAGES, USES_TYPE, IMPLEMENTS, INHERITS, DECORATES edges; TESTS edges, HTTP route matching, config linking, git co-change edges; bulk INSERT with deferred index creation |
| 6. Post-index | Louvain communities, XXH3 file hashes |

### 4.4 Call resolution — 6-strategy cascade (the core linking problem)
Resolving `pkg.Func` to a real graph node. A `FunctionRegistry` indexes by qualified name (exact) and simple
name (reverse index). Prioritized cascade with confidence scores:

1. **Import map (0.95)** — split callee `prefix.suffix`; resolve prefix via file's import map; exact match.
2. **Import map suffix (0.85)** — fallback suffix match against import-resolved module paths.
3. **Same module (0.90)** — prefix callee with enclosing file's module name; exact match.
4. **Unique name (0.75)** — simple name in reverse index; accept if exactly one project-wide candidate.
5. **Suffix match (0.55)** — among multiple candidates, pick by suffix + import-distance (nearest module wins).
6. **Fuzzy (0.30–0.40)** — last-resort string similarity.

> Strategies **1–3 resolve ~80% of calls** in well-structured codebases; 4–6 handle cross-module / dynamic dispatch.

**LSP-style hybrid type resolution** (Go/C/C++): dedicated passes after Tree-Sitter extraction build a per-file
`TypeRegistry` (+ stdlib stubs) and a `Scope` for bindings, then evaluate each call-site receiver's type
bottom-up (scope lookup, field/method lookup with base-class/embedded traversal, return-type propagation,
reference/pointer/alias simplification). C++ adds namespace resolution, template parameter defaults, and
retroactive resolution of pending template calls. Resolved calls carry the fully-qualified callee and bypass
the string cascade → higher-confidence edges.

### 4.5 MCP tool interface — 14 tools, four categories
| Category | Tools |
|---|---|
| Indexing | `index_repository` (build/update), `index_status` (poll progress) |
| Analysis | `list_projects`, `delete_project`, `search_graph` (symbol search), `trace_call_path` (directional call-chain, configurable depth), `query_graph` (Cypher-like traversals), `ingest_traces` (runtime traces), `detect_changes` (git diff impact), `get_graph_schema`, `get_architecture` |
| Code | `get_code_snippet`, `search_code` (full-text), `manage_adr` |

Every tool returns structured JSON. `query_graph` is a Cypher-like language; `trace_call_path` does directional
(inbound/outbound) tracing.

### 4.6 Incremental sync
On each FS event: XXH3 hash the file, compare to stored hash; if changed, delete + re-parse its nodes/edges,
update hash, recompute affected Louvain communities. XXH3 chosen for **~30 GB/s** throughput (collision
resistance not a security requirement here).

### 4.7 Community detection — Louvain
Modularity optimization to partition the call graph into functional communities. Two iterated phases:
(a) **local moving** — each node greedily joins the neighbor community maximizing modularity gain
`Q = w_in − γ·k_i·tot/(2m)` (γ=1.0 resolution); (b) **refinement** — communities with <1% internal density are
split by ejecting weakly-connected members. **Converges in ~3–5 iterations.** Operates on CALLS / HTTP_CALLS /
ASYNC_CALLS edges → Community nodes + MEMBER_OF edges consumed by `get_architecture`.

### 4.8 Security hardening (a notably deep section)
Threat model: MCP servers run with the host agent's full permissions but are installed as opaque third-party
binaries — a supply-chain risk amplified because agents invoke tools without per-call approval.

- **Code-level:** `cbm_validate_shell_arg()` rejects metacharacters/backticks/substitution before any `popen`;
  SQLite **authorizer callback blocks ATTACH/DETACH**; `get_code_snippet` uses `realpath()` containment vs path
  traversal; tests built with ASan + UBSan; 15-min ASan soak test.
- **8-layer CI audit suite (per commit):** (1) static allow-list audit of dangerous libc calls
  (system/popen/fork/execvp); (2) binary string audit (only GitHub API + localhost URLs allowed); (3) network
  egress monitoring via `strace` (only localhost/DNS/GitHub release API); (4) install path validation (blocks
  ~/.ssh, ~/.gnupg, ~/.aws); (5) smoke-test hardening (clean shutdown, no residual processes); (6) graph-UI
  asset audit (blocks external domains/trackers/iframes; HTTP server binds 127.0.0.1, locked CORS); (7) MCP
  robustness: 23 adversarial JSON-RPC payloads (malformed JSON, SQL injection, shell injection, path traversal,
  ReDoS, oversized inputs) — none may crash/hang; (8) vendored dependency integrity via SHA-256 over all 72
  vendored files.
- **Release pipeline (draft→verify→publish):** Sigstore cosign signing + SLSA provenance + CodeQL gating;
  VirusTotal (70+ engines, zero-tolerance, ≥60 engines must report) + Windows Defender + ClamAV; OpenSSF
  Scorecard gate; SHA-256 checksums + CycloneDX SBOM + user-verifiable `gh attestation verify` / `cosign verify-blob`.

---

## 5. Evaluation & study data (Section 4) — the empirical core

Four dimensions: head-to-head benchmark, qualitative win/loss analysis, system performance, community adoption.

### 5.1 Head-to-head benchmark — setup
- **12 standardized question categories** (hub detection, caller ranking, dependency manifests, full call-chain
  tracing, etc. — see table below).
- **31 languages**, each tested against **one real OSS repo**, ranging from **78 nodes (HCL/Terraform)** to
  **49,398 nodes (Python/Django)**.
- Two agents, identical questions, **same LLM backend (Claude Opus 4.6)**:
  - **MCP Agent** — Codebase-Memory's 14 tools.
  - **Explorer Agent** — conventional file-reading + grep.
- Graded by the **first author** vs. manually-derived reference answers. Continuous 0–1 scale;
  **PASS ≥ 0.80, PARTIAL 0.40–0.79, FAIL < 0.40**.

Benchmark question categories → primary tool:

| Q# | Category | Primary tool |
|---|---|---|
| 1 | Indexing | `get_graph_schema` |
| 2–3 | Discovery / pattern matching | `search_graph` |
| 4 | Code retrieval | `get_code_snippet` |
| 5 | Code search | `search_code` |
| 6 | Call tracing | `trace_call_path` |
| 7–8 | Graph query | `query_graph` |
| 9–10 | OOP analysis | `query_graph` |
| 11–12 | File operations | `get_architecture` |

### 5.2 Headline results

| Metric | MCP Agent | Explorer Agent | Difference |
|---|---|---|---|
| **Quality score** | **0.83** | **0.92** | MCP = 90% of Explorer |
| **Tool calls / question** | **2.3** | **4.8** | **2.1× fewer** |
| **Tokens / question** | — | — | **~10× fewer** |
| **Query latency** | **<1 ms** | **10–30 s** | **>100× faster** |

### 5.3 Where each approach wins (qualitative)
- **MCP/graph wins** on **hub detection & caller ranking** in **19 of 31 languages** — queries that follow
  pre-materialized edges.
  - Strongest on **functional languages (Haskell, OCaml, Elixir)** → quality gap narrows to **1%**.
- **Explorer/file wins** for **full source context** in **16 of 31 languages**, and for exhaustive call-site
  grep (**10/31**) — line-level tasks the graph deliberately doesn't store.
  - **Weakest MCP result: macro-heavy C — 0.58 vs. 1.00**, because macros aren't represented in the AST.
- **Why the speed gap:** MCP resolves via pre-computed lookups (BFS via SQL recursive CTE: **0.3 ms**); Explorer
  rediscovers structure each query (grep → read → parse → repeat), multiplying calls/tokens linearly with
  codebase size. **The graph pays indexing cost once (6 s for 49K nodes) and amortizes across all queries.**

### 5.4 Comparison vs. alternative paradigms

| Feature | Embedding/RAG | Repo-Map | Graph+LLM | **Ours** |
|---|---|---|---|---|
| Languages | 10–30 | 100 | 8–14 | **66** |
| Structural queries | No | No | Yes | **Yes** |
| Infrastructure | Vector DB | None | Neo4j | **SQLite** |
| Persistence | Yes | No | Yes | **Yes** |
| Embedding model | Yes | No | Some | **No** |
| Tokens / query | (high) | (low) | ~5K | **~1K** |
| Auto-sync | — | Manual | Manual | **Yes** |
| License | varies | Apache | mixed | **MIT** |

*(Table 7 in the paper is somewhat mangled by PDF extraction; values above are the legible cells.)*

### 5.5 System performance (Apple M3 Pro, macOS)

| Operation | Time |
|---|---|
| Fresh index — 49K nodes / 196K edges (Django) | **6 s** |
| Fresh index — Linux kernel, 2.1M nodes / 4.9M edges (28M LOC, 75K files) | **~3 min** |
| Incremental re-index | **1.2 s** (~4× faster than full) |
| Cypher query (relationship traversal) | **<1 ms** |
| BFS call-path tracing (depth 5) | **0.3 ms** |
| Name search (regex) | **<10 ms** |
| Dead-code detection | **150 ms** |

### 5.6 Community adoption (proxy for relevance)
Within **4 weeks of release (Feb 25, 2026):** **900+ GitHub stars, ~100 forks.** Referrers: Reddit (1,288 views),
LinkedIn (441), direct (869). Auto-detected by **10 coding agents** (Claude Code, Codex CLI, Gemini CLI, Zed,
VS Code, …).

---

## 6. Discussion (Section 5)

- **Clear division of labor:** graph retrieval for cross-file structural queries (hub detection, caller ranking,
  dependency traversal); fall back to file exploration for source-level / exhaustive-pattern tasks. **Optimal
  architecture is a hybrid.**
- **Structural retrieval as a paradigm:** complements embedding-based RAG. Embeddings excel at *semantic
  similarity*; graph traversal excels at *relational* queries. AST-chunking results corroborate that
  structure-aware approaches beat flat text retrieval for code.
- **Supply-chain trust in MCP ecosystems:** argues MCP servers requesting elevated host permissions need an
  automated, zero-tolerance verification baseline (their Section 3.8) — currently rare in OSS dev tools.

### 6.1 Threats to validity (important for how much to trust the numbers)
- **Internal:** single LLM backend (Claude Opus 4.6); **graded by the first author** (no blind/independent
  raters); PASS/PARTIAL/FAIL thresholds chosen pragmatically.
- **External:** **31 languages but only one repo each**; macro-heavy languages weak; **no systematic comparison
  vs. embedding-RAG, ctags/LSP, or other graph systems** (left as future work).
- **Construct:** **static structure only** — no runtime behavior, reflection, or dynamic dispatch; `query_graph`
  default ceiling of 100,000 rows may undercount huge codebases; all perf on a single machine (M3 Pro).

### 6.2 Future work
Controlled study across SWE-bench with ablations; hybrid structural+semantic retrieval; multi-repo dependency
tracking; LLM-generated graph summaries at function/module level; and applying the approach to **DSLs in health
informatics** (e.g. FHIRconnect openEHR↔FHIR mapping) — a nod to the authors' Charité/medical-informatics background.

---

## 7. Conclusion (Section 6)
Treating code structure as a first-class queryable graph (vs. text to be searched) delivers order-of-magnitude
efficiency with competitive accuracy. The multi-phase Tree-Sitter pipeline + 6-strategy resolution + Louvain
communities yields a sub-millisecond-queryable graph exposed to any MCP agent with no infra. One static C
binary scales from small projects to the Linux kernel (2.1M nodes in 3 min). ~10× lower token cost, 2.1× fewer
tool calls across 31 languages; plus an MCP supply-chain verification pipeline; early adoption confirms demand.

---

## 8. Relevance & takeaways for *this* project

> The indexer/graphify work in this repo targets the same problem space (filesystem → knowledge graph →
> queryable for an agent). Concrete things worth borrowing or watching:

1. **Hybrid is the design conclusion, not graph-only.** Keep grep/file-read fallback for source-level questions;
   route structural questions to the graph. The 83% vs 92% gap is *entirely* about line-level retrieval the
   graph chooses not to store.
2. **Confidence-scored, prioritized call resolution** (their 6-strategy cascade) is a clean model for turning
   raw symbol references into real edges — and the insight that ~80% resolve via the top 3 cheap strategies is a
   good optimization guide.
3. **Content-hash incremental re-indexing** (XXH3, only re-parse changed files, recompute only affected
   communities) is the right pattern for a live file-watcher — ~4× cheaper than full rebuilds here.
4. **SQLite (one file, WAL) over a graph DB** is a defensible zero-infra choice; recursive CTEs gave them
   0.3 ms BFS. No Neo4j needed.
5. **Louvain communities → an `get_architecture`-style summary** is a cheap way to give an agent a high-level map.
6. **Benchmark methodology to copy (and improve on):** standardized question categories × many repos, PASS/
   PARTIAL/FAIL grading, measuring tokens + tool-calls + latency, not just quality. **But** fix their weaknesses:
   use blind/independent grading, multiple repos per language, and compare against embedding-RAG.
7. **Targets to beat / match:** ~1K tokens/query, <1 ms structural queries, 6 s to index ~50K nodes.

### Caveats before treating numbers as ground truth
- Single grader = the first author; single LLM backend; one repo per language; static-only; single hardware.
  The *direction* (graph = far cheaper, slightly lower quality on structural Qs) is credible; the *exact*
  percentages are soft.

---

## 9. Cross-reference to this project's planned ADRs

> **Headline:** this paper *is* the competitor already named in **ADR-004**: "breadth-first competitors
> (e.g. `codebase-memory-mcp`) claim 150+ [languages] by vendoring tree-sitter grammars and doing generic,
> unverified extraction." The GitHub repo (DeusData/codebase-memory-mcp) and the paper are the same project.
> So this isn't just related research — it's a published, benchmarked description of the exact system the
> indexer's tiering strategy is positioned against. Read §9 as competitive intelligence.

### 9.1 ADR-004 (Tiered Language Support) — the paper is the foil, and its data backs the moat

- **Same mechanism, no tiers.** Codebase-Memory's 66-language breadth comes from generic Tree-Sitter
  extraction with **confidence-scored edges (0.0–1.0)** via its 6-strategy cascade — structurally the same
  idea as ADR-004's **Tier-B `GenericTreeSitterAdapter` + `candidate=True` edges**. The difference is
  presentation: the paper treats all 66 languages as **equal** ("66 languages" flat claim), whereas ADR-004
  publishes a *spectrum* (A proven / B best-effort / C text) and tells the user which they queried. ADR-004's
  positioning line — "we tell you which you're querying" vs. an "undifferentiated 150-languages claim" — lands
  directly on this paper.
- **Their data validates ADR-004's central fear.** The paper's worst result is **macro-heavy C: 0.58 vs 1.00,
  "because macros are not represented in the AST."** That is exactly the *confidently-wrong / can't-prove-it*
  failure mode ADR-004 cites to justify confidence-tagging instead of a flat support claim. Their fix is a
  confidence number on the edge; ADR-004's is the `candidate` flag + verdict-tool gating ("insufficient —
  candidate-only"). **ADR-004's stance is the stronger version of what their own benchmark exposes.**
- **Convergence on the honesty principle.** Their per-edge confidence ≈ ADR-004's candidate contract ≈
  ADR-006's EXTRACTED-only provenance. Three independent expressions of the same "don't assert structure you
  can't prove" rule.
- **⚠️ Fact-check flag:** the paper claims **66 languages**, but ADR-004 attributes **"150+"** to
  `codebase-memory-mcp`. Either the ADR cited marketing/an older number, or conflated it with another tool.
  Worth correcting the ADR to "66 (claimed)" so the comparison is defensible.

### 9.2 ADR-005 (Chunk Versioning + Self-Healing) — two re-index axes; ours is the one they lack

- **Content-hash incremental: they have it, we should confirm we do.** The paper re-indexes on an **XXH3
  content-hash** change (file changed → reparse that file, recompute only affected Louvain communities, ~4×
  faster than full). That is the *content-drift* axis.
- **Version-drift: ADR-005 is purely additive over the paper.** ADR-005's `recheck` triggers on
  **adapter-method-version** drift (`generic-go/v1` → `go/v1`), not content. The paper has **no analog** — it
  has no notion of a chunker version or of reprocessing unchanged files because the *extractor* improved. This
  is a genuine indexer advantage, not a port.
- **Quality scoring: the paper has none — and it cost them.** Codebase-Memory has **no structural/coherence
  scoring loop**. It discovered its C weakness (0.58) only through *manual benchmark grading by the first
  author*. ADR-005's `get_flagged_summary()` would have surfaced "C: N flagged / M chunks" automatically and
  fed the ADR-004 promotion backlog. **The self-healing scorer is a differentiator the published competitor
  lacks**, and the paper is the proof-by-absence that it's worth building.

### 9.3 ADR-006 (Graph Analytics) — independent validation + concrete details to borrow

This is the closest overlap, and the paper *confirms* ADR-006's bets rather than contradicting them:

| ADR-006 decision | What the paper does | Verdict |
|---|---|---|
| Louvain community detection (seeded NetworkX, optional Leiden) | Louvain modularity, γ=1.0, converges in **3–5 iterations** | Convergent — same algorithm, same family |
| Betweenness centrality → god-object scoring | Betweenness for "bridge"/hub detection | Independent validation of the god-node idea |
| `map_module_communities` MCP tool + DSM viz | `get_architecture` tool fed by Community/MEMBER_OF | Convergent tool shape (we chose DSM to stay visually distinct) |
| EXTRACTED edges only (provenance honesty) | Runs Louvain on CALLS/HTTP_CALLS/ASYNC_CALLS, but **includes low-confidence (0.30–0.55) fuzzy-resolved edges** | **Ours is higher-fidelity** — their communities ingest fuzzy edges; ADR-006 deliberately doesn't |
| `EDGE_WEIGHTS` per kind (calls 1.0, imports 0.6, …) | Binary edge inclusion (call-type edges only), **not weighted by kind** | ADR-006 is more nuanced |

- **Borrowable detail #1 — Louvain refinement step.** The paper adds a refinement phase ADR-006 doesn't
  mention: communities with **<1% internal density are split by ejecting weakly-connected members**. Cheap,
  improves cluster quality, worth adding to `graph_analytics.py`.
- **Borrowable detail #2 — recompute only affected communities on incremental update.** The paper recomputes
  *only the affected* community assignments on a file change, not the whole partition. ADR-006 is analysis-only
  (recomputes on demand); if community results are ever cached, adopt their incremental recompute.
- **Borrowable detail #3 — aggregation guard.** Their `query_graph` caps at 100K rows and they aggregate large
  graphs; ADR-006 already has `DSM_MAX_NODES=1500` → community-aggregated matrix. Same instinct, validated.

### 9.4 The indexer's biggest structural advantage the paper inadvertently documents

The paper's **own conclusion (§5.1) is that the optimal architecture is a hybrid** — graph for structural
queries, file/text fallback for source-level — and it lists hybrid structural+semantic retrieval as **future
work** it hasn't built. Codebase-Memory ships graph + full-text (`search_code`) but **"Embedding model: No"**
(their Table 7). **This indexer already runs a hybrid dense + BM25 RTR pipeline alongside the graph** — i.e. it
already *is* the architecture the paper concludes is optimal and defers. That is the single sharpest
positioning point to extract from this study: the published competitor's roadmap endpoint is the indexer's
current baseline.

### 9.5 Empirical targets worth adopting as benchmarks

If/when the indexer runs its own eval, these are the published numbers to match or beat:
- **~1K tokens/query**, **<1 ms** structural queries, **0.3 ms** depth-5 BFS, **6 s** to index ~50K nodes,
  **1.2 s** incremental re-index.
- And the paper's **benchmark methodology** (12 question categories × many repos, PASS/PARTIAL/FAIL grading,
  measuring tokens + tool-calls + latency, not just quality) is a ready-made template — but **fix its validity
  holes**: blind/independent grading, multiple repos per language, and a three-way graph-vs-RTR-vs-hybrid
  comparison the paper never ran.

### 9.6 Out-of-scope but noted: supply-chain security

The paper's Section 3.8 (8-layer CI audit, Sigstore/SLSA/CodeQL, multi-engine AV gating) sets a concrete
supply-chain bar for MCP servers that request host permissions. Not part of the gap-filler, but a useful
reference if ADR-001/002 governance ever extends to release verification for a distributed binary.
