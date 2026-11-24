# Three-tier index, data flow, retrieval pipeline, design decisions

Moved from src/CLAUDE.md, verbatim (source lines 117-150, 205-211). Read when working on this area.

## Architecture

### Three-Tier Index

Every indexed symbol is chunked and embedded at three granularities, stored in separate FAISS indexes under `.code-index/`:

| Tier | Token Budget | Index File | Granularity |
| ------ | ------------- | ------------ | ------------- |
| 1 (surgical) | ~500 | `tier1_surgical.faiss` | One chunk per AST symbol |
| 2 (component) | ~1500 | `tier2_component.faiss` | Module-level sliding window |
| 3 (architectural) | ~4000 | `tier3_architectural.faiss` | System-level sliding window |

The companion `graph.db` (SQLite, WAL mode) stores the symbol/edge graph for structural traversal. Chunk payloads are served from SQLite (`chunks` table) via an in-memory cache in `DocumentStore`.

### Data Flow

```src
Source files
  → ast_chunker.py     (tree-sitter AST → symbols + edges + references)
  → incremental_indexer.py  (MD5 change detection → stale removals + new embeddings)
  → core.py            (code embeddings, FAISS, token counting)
  → db.py              (SQLite: files, symbols, chunks, edges, symbol_references)
  → MCPServer.py       (MCP tools over the combined index)
```

### Retrieval Pipeline (RTR)

`hybrid_retriever.py` implements a three-step Retrieve → Traverse → Rerank pipeline:

1. **Semantic**: FAISS top-50 candidates from each of the three tiers, fused via Reciprocal Rank Fusion (RRF, k=60)
2. **Structural**: One-hop call-graph expansion via SQLite (bidirectional, cycle-guarded CTEs); edges corroborated against the import graph
3. **Reranking**: optional, **off by default** — returns the RRF-ranked top-10. When `[reranker].enabled = true`, `src/reranker.py` rescores candidates with the configured model (`Qwen/Qwen3-Reranker-0.6B`, a causal-LM yes/no scorer, or a sentence-transformers CrossEncoder by id), falling back to RRF on load/predict failure

`iterative_retriever.py` wraps this in multi-round loops with confidence-based early stopping and query enrichment from prior findings.
### Key Design Decisions

- **Stable FAISS IDs**: 60-bit deterministic IDs (file hash × offset) allow surgical removes without full rebuilds.
- **Dual persistence**: FAISS handles ANN speed; SQLite handles graph queries — they are complementary, not redundant.
- **Token budgeting**: Context packing in `core.py` respects LLM context windows and warns when truncating.
- **Import resolution**: `import_resolver.py` handles tsconfig path aliases, relative imports, barrel files, and extension inference so graph edges reflect real module boundaries.
- **Monster-line shredding**: `ast_chunker.py` detects and strips Base64 inline SVGs and similar noise before chunking.
