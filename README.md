# Codebase Indexer

A local code intelligence engine that indexes Python, TypeScript, JavaScript, C#, and C++ codebases into a hybrid semantic search system, then exposes query capabilities through a **Model Context Protocol (MCP) server** for use with AI assistants.

## What it does

Instead of grepping for strings, the Codebase Indexer:

- Parses source code with **tree-sitter** to extract real AST symbols (functions, classes, interfaces)
- Embeds every symbol at **three granularities** using `BAAI/bge-code-v1`
- Stores embeddings in **FAISS** and symbol relationships in **SQLite**
- Serves **11 AI-facing MCP tools** so any compatible assistant (Claude Code, Continue.dev, etc.) can query it

## Architecture

### Three-Tier Index

Every symbol is embedded at three chunk sizes, each stored in a separate FAISS index under `.code-index/`:

| Tier | Token budget | FAISS file | Granularity |
|------|-------------|------------|-------------|
| 1 — Surgical | ~500 | `tier1_surgical.faiss` | One chunk per AST symbol |
| 2 — Component | ~1,500 | `tier2_component.faiss` | Module-level sliding window |
| 3 — Architectural | ~4,000 | `tier3_architectural.faiss` | System-level sliding window |

At query time, all three tiers are searched and results are fused with **Reciprocal Rank Fusion (RRF)** for consensus ranking.

### Data Flow

```
Source files
  → ast_chunker.py         (tree-sitter AST → symbols + edges + references)
  → incremental_indexer.py (MD5 change detection → stale removals + new embeddings)
  → core.py                (bge-code-v1 embeddings, FAISS, token counting)
  → db.py                  (SQLite: files, symbols, chunks, edges, symbol_references)
  → MCPServer.py           (MCP tools over the combined index)
```

### Retrieval Pipeline (RTR)

`hybrid_retriever.py` implements a three-step **Retrieve → Traverse → Rerank** pipeline:

1. **Semantic** — FAISS top-50 candidates from tier-1
2. **Structural** — One-hop call-graph expansion via SQLite (bidirectional CTEs, cycle-guarded)
3. **Reranking** — `Qwen/Qwen3-Reranker-0.6B` cross-encoder. Off by default
   (`[reranker].enabled = false`); when disabled the fused retrieval scores stand.

`iterative_retriever.py` wraps this in multi-round loops with confidence-based early stopping and query enrichment from prior findings.

## Installation

```bash
pip install -r indexer/requirements.txt
```

Or install manually:

```bash
pip install faiss-cpu sentence-transformers transformers \
    "tree-sitter>=0.21" tree-sitter-python tree-sitter-typescript tree-sitter-javascript \
    "mcp[cli]" watchdog numpy textual rich "networkx>=3.0"
```

> Optional: `pip install leidenalg python-igraph` enables the higher-quality Leiden
> community-detection backend for `map_module_communities`. Without it the engine uses
> NetworkX's built-in Louvain — no functionality is lost.

> Use `faiss-gpu` instead of `faiss-cpu` for CUDA GPU acceleration.

## Running the MCP Server

```bash
cd indexer/src
python MCPServer.py
```

The server starts instantly (indexes load lazily on first tool call) and optionally starts a **file watchdog** that triggers incremental reindex on source file changes.

### First-time indexing

On first run no index exists. Call the `reindex` MCP tool from your AI assistant:

```
reindex()                          # full reindex (first time or after major refactors)
reindex(changed_files_only=True)   # incremental (after routine edits)
```

### Connecting to Claude Code

Add to your MCP config (`~/.claude/claude_mcp_config.json` or `.mcp.json` in your project):

```json
{
  "mcpServers": {
    "codebase-indexer": {
      "command": "python",
      "args": ["/absolute/path/to/indexer/src/MCPServer.py"]
    }
  }
}
```

## MCP Tools Reference

| Tool | Category | Description |
|------|----------|-------------|
| `semantic_code_search` | Search | FAISS + RRF search across all three tiers |
| `find_similar_code` | Search | Find duplicates and callers of a code snippet |
| `analyze_blast_radius` | Impact | Map callers, dependents, and parallel implementations of a symbol |
| `detect_pattern_violations` | Impact | Find files that should follow a pattern but don't |
| `trace_data_flow` | Tracing | Full lifecycle of a data symbol (definitions → producers → consumers) |
| `investigate_architecture` | Tracing | Full RTR pipeline with Markdown report and architectural risk analysis |
| `find_test_coverage` | Discovery | Locate Vitest test files covering a source file or symbol |
| `find_dead_code` | Discovery | Determine if a symbol has any consumers in the codebase |
| `find_unabstracted_collection_reads` | Discovery | Enforce "reads of X must go through Y" patterns |
| `map_module_communities` | Discovery | Whole-graph structural view: community detection + god-object analysis + a DSM visualization |
| `reindex` | Maintenance | Rebuild the index (full or incremental) with git-staleness detection |

### Architecture mapping — `map_module_communities`

Where the other tools answer questions about a *single* symbol, `map_module_communities`
analyzes the **shape of the whole graph**. It reads the stored symbol/edge graph and runs
**Louvain community detection** + **betweenness centrality** over it (pure
[NetworkX](https://networkx.org/) — no LLM, deterministic under a fixed seed), then returns:

- a **community map** — natural module clusters, each with a heuristic label and a raw cohesion score;
- **god-objects** — high-coupling chokepoints whose owned members sprawl across many coupling
  communities (the "this class has grown too large" signal), ranked by a composite score;
- a **Design Structure Matrix (DSM)** written to `.code-index/architecture_matrix.html` — a
  self-contained, single-file interactive matrix (no web server, no CDN) where god-objects
  show up as dense rows/columns and healthy communities as bright diagonal blocks.

```
map_module_communities()                      # descriptive report + DSM (default)
map_module_communities(target_path="src/")    # scope analysis to a subtree
map_module_communities(suggest_splits=True)   # also emit heuristic module-split proposals
```

`suggest_splits=True` additionally proposes how an over-large class might decompose, grouping
its members by coupling community. These proposals are **descriptive heuristics**, not
recommendations — every one is stamped `[HEURISTIC — unverified]` and should be validated
against the real call graph before acting. The whole report is explicitly **exploratory**: it
maps the EXTRACTED graph honestly but is not a verified accuracy claim (a measured quality bar
is tracked separately).

> **Attribution.** This capability is **inspired by [Graphify](https://github.com/safishamsi/graphify)**
> (Safi Shamsi, MIT) — specifically its demonstration of community detection + betweenness
> "god-node" analysis over a code graph. It is **reimplemented natively** over this engine's own
> EXTRACTED edges (no Graphify source is used), and the DSM is a deliberately different visual idiom
> from Graphify's force-directed node-link graph. See `docs/adr/ADR-006`.

## Project Structure

```
indexer/
├── indexer.toml               # Per-repo configuration (embedder, retrieval, eval, ignores)
├── requirements.txt
├── benchmarks/                # Eval deliverables: baselines, qrels, fixtures (ADR-007/019)
├── cloud/                     # GPU eval VM launcher + startup script
├── docs/
│   ├── ARCHITECTURE.md        # Full architecture walkthrough
│   ├── adr/                   # Architecture Decision Records — the living source of truth
│   └── adr-backlog.md         # Proposed/queued decisions
├── examples/                  # Sample per-language rule configs
├── scripts/                   # Benchmark launchers (run from the repo root)
├── tests/
├── tools/                     # Eval + CI harnesses (coir_eval, conformance_eval, …)
└── src/
    ├── MCPServer.py            # MCP server entry point — all 11 tools
    ├── config.py               # Locates and parses the per-repo indexer.toml
    ├── core.py                 # Embeddings (bge-code-v1), FAISS management, token counting
    ├── ast_chunker.py          # tree-sitter AST → symbols, edges, chunks
    ├── adapters/               # Per-language extraction adapters (ADR-003)
    │   ├── base.py             #   Adapter interface + shared tree-sitter helpers
    │   └── …                   #   python, ts, csharp, cpp, l5x
    ├── db.py                   # SQLite schema and queries (WAL mode)
    ├── stable_id.py            # The 60-bit compound-key hash every symbol identity uses
    ├── call_resolver.py        # Baseline CALLS-edge resolution (ADR-021)
    ├── graph_analytics.py      # Community detection + centrality + god-objects (ADR-006)
    ├── graph_report.py         # Markdown report rendering for map_module_communities
    ├── graph_viz.py            # Design Structure Matrix (DSM) HTML visualization
    ├── incremental_indexer.py  # MD5-based change detection and rebuild logic
    ├── hybrid_retriever.py     # Retrieve → Traverse → Rerank pipeline
    ├── iterative_retriever.py  # Multi-round retrieval with early stopping
    ├── fusion.py               # Sparse tokenization + score-normalized fusion (ADR-009)
    ├── reranker.py             # Cross-encoder rescoring — off by default
    ├── import_resolver.py      # tsconfig path aliases, barrel files, relative imports
    ├── category_tagger.py      # Symbol classification
    ├── summarizer.py           # Tier-3 summary generation
    ├── RecFileSearch.py        # File search utilities
    └── tui/                    # Textual terminal UI
        ├── app.py
        ├── backend.py
        └── tools.py
```

The `.code-index/` directory (FAISS indexes, `graph.db`, `doc_store.json`) is generated at runtime and is git-ignored.

## Key Design Decisions

- **Stable FAISS IDs** — 60-bit deterministic IDs (file hash × offset) allow surgical removes without full rebuilds.
- **Dual persistence** — FAISS handles ANN speed; SQLite handles graph queries. They are complementary, not redundant.
- **Token budgeting** — Context packing in `core.py` respects LLM context windows and warns when truncating.
- **Monster-line shredding** — `ast_chunker.py` detects and strips Base64 inline SVGs and similar noise before chunking to prevent embedding corruption.
- **Import resolution** — `import_resolver.py` handles tsconfig path aliases, relative imports, barrel files, and extension inference so graph edges reflect real module boundaries.
- **Lazy cross-encoder** — When the reranker is enabled, `Qwen/Qwen3-Reranker-0.6B` (~1.2 GB) loads only on first use rather than at startup, so the MCP handshake completes instantly.

## Languages Supported for Indexing

| Language | Extensions | Symbols extracted | Graph edges |
|----------|-----------|------------------|-------------|
| **Python** | `.py` | module, class, function, method, decorated function | import, call, owns, extends |
| **TypeScript** | `.ts`, `.tsx` | class, interface, function, arrow function, type alias, React component | import, call, owns, extends, context |
| **JavaScript** | `.js`, `.jsx` | class, function, arrow function, React component | import, call, owns, extends, context |
| **C#** | `.cs`, `.csproj`, `.sln` | namespace, class, interface, struct, record, enum, method, constructor, property | import (using), call, owns, extends, implements |
| **C++** | `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hxx` | namespace, class, struct, enum, function, method, constructor, template, typedef, using alias | import (#include), call, owns, extends |

### Language Limits

These are known, documented constraints — not bugs. They reflect the fundamental difference between a heuristic structural indexer and a compiler frontend.

#### Python
- Dynamic attribute assignment (`self.x = ...` in `__init__`) is not indexed as a symbol — only statically declared class attributes are.
- `__getattr__` / `__getattribute__` overrides make attribute resolution invisible to the graph.
- Decorators are captured as symbol metadata but decorator-generated methods (e.g. `@property`, `@staticmethod`) are not separately indexed as symbols.

#### TypeScript / JavaScript
- Extension methods (prototype augmentation) resolve to candidates only.
- Dynamic `require()` and `import()` with non-literal paths are not resolved.
- Type-narrowed dispatch (discriminated unions) produces candidate call edges, not single-target edges.
- `tsconfig.json` path aliases are resolved; `node_modules` edges terminate at the package boundary.

#### C#
- Extension methods resolve to candidates only (no receiver-type inference without full type resolution).
- LINQ query syntax is indexed as text at chunk level, not as call edges.
- `dynamic` is invisible to the graph.
- Cross-file partial-class EXTENDS/IMPLEMENTS/OWNS edges are not cleaned on incremental re-index; full re-index always produces a correct graph.

#### C++

> **Position statement:** This is a heuristic semantic/structural indexer, not a compiler frontend. For precise C++ navigation use clangd or SCIP. This tool's value is hybrid search and a good-enough graph over codebases where no compile-accurate index exists.

- **Preprocessor macros:** macro-generated functions are invisible to the graph. Only source-visible function definitions and declarations are indexed.
- **Template instantiations:** template *definitions* index correctly; instantiations (`std::vector<int>`) are invisible.
- **Function pointers and virtual dispatch:** call edges resolve to the declared type only, not the runtime type.
- **Operator overloads:** indexed as symbols but rarely earn call edges (call sites use operator syntax, not a function name).
- **Header/impl unification:** relies on FQN identity across files. If a declaration and definition use different parameter spellings (e.g. `const string&` vs `const std::string&`) they will produce different FQNs and not unify. Normalize includes in your codebase to prevent this.

## Extraction Accuracy (ADR-008)

<!-- CONFORMANCE:START -->
| Language | Symbols P/R | Edges P/R | Call edges P/R | Fixtures |
|----------|-------------|-----------|----------------|----------|
| python | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 | 6 |
| typescript | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 | 6 |

_Measured on hand-authored feature fixtures (Tier-A adapters), not an exhaustive corpus — a row is "precision/recall on the fixtures we wrote," never a language's true precision (ADR-008 §7). Regenerate with `python tools/conformance_eval.py --write-readme`._
<!-- CONFORMANCE:END -->

## Documentation

**Decisions.** `docs/adr/` holds the Architecture Decision Records. The `Status:` line in
each ADR is the source of truth for anything this README summarizes — where the two
disagree, the ADR is right. `docs/adr-backlog.md` indexes what is proposed but undecided,
and `docs/adr/ADR-000-template.md` is the starting point for a new one. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the process.

**Orientation.** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the long-form walkthrough
of every part of the engine and why it is shaped that way — start there for depth this
README intentionally skips. It is a dated snapshot, not a living document.

**Research and position work.** These informed the ADRs but are *not* committed work; each
states its own non-binding status in its header.

| Document | What it is |
|---|---|
| [prior-art-depth-over-breadth.md](docs/prior-art-depth-over-breadth.md) | The fewer-languages / provable-accuracy thesis underpinning ADR-004 |
| [references-code-intelligence.md](docs/references-code-intelligence.md) | Working bibliography behind the design rationale |
| [study-codebase-memory-mcp.md](docs/study-codebase-memory-mcp.md) | Study of the Codebase-Memory paper (tree-sitter KGs over MCP) |
| [design-research-informed-improvements.md](docs/design-research-informed-improvements.md) | Sourced improvement opportunities mapped onto this codebase |
| [modernization-stack-review.md](docs/modernization-stack-review.md) | Review of the retrieval stack against 2026 SOTA |
| [merkle-tree-drift-handling.md](docs/merkle-tree-drift-handling.md) | Evaluation of a Merkle tree for drift detection |
| [suggestions-future-directions.md](docs/suggestions-future-directions.md) | Menu of candidate directions from a full codebase audit |
| [agent-prompt-code-intelligence-architect.md](docs/agent-prompt-code-intelligence-architect.md) | Agent prompt used to drive the ADR authoring pass |
