# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A local code intelligence engine that indexes Python/TypeScript/JavaScript codebases into a hybrid FAISS + SQLite store and exposes query capabilities through an MCP (Model Context Protocol) server. It is a standalone Python utility — there is no web server, build pipeline, or package manifest.

## Reference Documents

- **Contribution Standards:** Read `CONTRIBUTING.md` before starting any new work — defines minor vs major classification, branch naming, ADR requirements, bug/feature tracking, and PR process.

## Working Style

- For non-trivial new features or significant design decisions, use `/grill-plan` to produce an ADR before implementing.
- Major changes are any changes to `src/`. They require a branch, an ADR, and a PR. See `CONTRIBUTING.md`.

## Running the MCP Server

```bash
python src/MCPServer.py
```

Dependencies are in `requirements.txt`. Install with:

```bash
pip install -r requirements.txt
```

No formal test or lint commands are configured. Before merging, manually verify the MCP server starts and `reindex` completes without error.

- `faiss-cpu` (or `faiss-gpu`)
- `sentence-transformers`
- `transformers`
- `tree-sitter` + `tree-sitter-python`, `tree-sitter-typescript`, `tree-sitter-javascript`
- `mcp` (FastMCP)
- `watchdog` (optional, for auto-reindex)
- `numpy`

## Architecture

### Three-Tier Index

Every indexed symbol is chunked and embedded at three granularities, stored in separate FAISS indexes under `.code-index/`:

| Tier | Token Budget | Index File | Granularity |
| ------ | ------------- | ------------ | ------------- |
| 1 (surgical) | ~500 | `tier1_surgical.faiss` | One chunk per AST symbol |
| 2 (component) | ~1500 | `tier2_component.faiss` | Module-level sliding window |
| 3 (architectural) | ~4000 | `tier3_architectural.faiss` | System-level sliding window |

The companion `graph.db` (SQLite, WAL mode) stores the symbol/edge graph for structural traversal. `doc_store.json` caches full document payloads for fast MCP lookup.

### Data Flow

```src
Source files
  → ast_chunker.py     (tree-sitter AST → symbols + edges + references)
  → incremental_indexer.py  (MD5 change detection → stale removals + new embeddings)
  → core.py            (Jina-Code embeddings, FAISS, token counting)
  → db.py              (SQLite: files, symbols, chunks, edges, symbol_references)
  → MCPServer.py       (MCP tools over the combined index)
```

### Retrieval Pipeline (RTR)

`hybrid_retriever.py` implements a three-step Retrieve → Traverse → Rerank pipeline:

1. **Semantic**: FAISS top-50 candidates from tier-1
2. **Structural**: One-hop call-graph expansion via SQLite (bidirectional, cycle-guarded CTEs)
3. **Reranking**: `jina-reranker-v2-base-code` CrossEncoder (500 MB, optional; falls back to FAISS scores)

`iterative_retriever.py` wraps this in multi-round loops with confidence-based early stopping and query enrichment from prior findings.

### MCP Tools (MCPServer.py)

Ten AI-facing tools grouped by intent:

- **Search**: `semantic_code_search`, `find_similar_code`
- **Impact**: `analyze_blast_radius`, `detect_pattern_violations`
- **Tracing**: `trace_data_flow`, `investigate_architecture`
- **Discovery**: `find_test_coverage`, `find_dead_code`, `find_unabstracted_collection_reads`
- **Maintenance**: `reindex`

Tool docstrings are intentionally written for AI consumption — they describe *when* to call the tool, not just what it does.

### Key Design Decisions

- **Stable FAISS IDs**: 60-bit deterministic IDs (file hash × offset) allow surgical removes without full rebuilds.
- **Dual persistence**: FAISS handles ANN speed; SQLite handles graph queries — they are complementary, not redundant.
- **Token budgeting**: Context packing in `core.py` respects LLM context windows and warns when truncating.
- **Import resolution**: `import_resolver.py` handles tsconfig path aliases, relative imports, barrel files, and extension inference so graph edges reflect real module boundaries.
- **Monster-line shredding**: `ast_chunker.py` detects and strips Base64 inline SVGs and similar noise before chunking.
