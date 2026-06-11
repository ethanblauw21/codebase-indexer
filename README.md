# Codebase Indexer

A local code intelligence engine that indexes Python, TypeScript, and JavaScript codebases into a hybrid semantic search system, then exposes query capabilities through a **Model Context Protocol (MCP) server** for use with AI assistants.

## What it does

Instead of grepping for strings, the Codebase Indexer:

- Parses source code with **tree-sitter** to extract real AST symbols (functions, classes, interfaces)
- Embeds every symbol at **three granularities** using `jinaai/jina-embeddings-v2-base-code`
- Stores embeddings in **FAISS** and symbol relationships in **SQLite**
- Serves **10 AI-facing MCP tools** so any compatible assistant (Claude Code, Continue.dev, etc.) can query it

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
  → core.py                (Jina-Code embeddings, FAISS, token counting)
  → db.py                  (SQLite: files, symbols, chunks, edges, symbol_references)
  → MCPServer.py           (MCP tools over the combined index)
```

### Retrieval Pipeline (RTR)

`hybrid_retriever.py` implements a three-step **Retrieve → Traverse → Rerank** pipeline:

1. **Semantic** — FAISS top-50 candidates from tier-1
2. **Structural** — One-hop call-graph expansion via SQLite (bidirectional CTEs, cycle-guarded)
3. **Reranking** — `jina-reranker-v2-base-code` CrossEncoder (optional; falls back to FAISS scores)

`iterative_retriever.py` wraps this in multi-round loops with confidence-based early stopping and query enrichment from prior findings.

## Installation

```bash
pip install -r indexer/requirements.txt
```

Or install manually:

```bash
pip install faiss-cpu sentence-transformers transformers \
    "tree-sitter>=0.21" tree-sitter-python tree-sitter-typescript tree-sitter-javascript \
    "mcp[cli]" watchdog numpy textual rich
```

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
| `reindex` | Maintenance | Rebuild the index (full or incremental) with git-staleness detection |

## Project Structure

```
indexer/
├── requirements.txt
└── src/
    ├── MCPServer.py            # MCP server entry point — all 10 tools
    ├── core.py                 # Embeddings (Jina-Code), FAISS management, token counting
    ├── ast_chunker.py          # tree-sitter AST → symbols, edges, chunks
    ├── db.py                   # SQLite schema and queries (WAL mode)
    ├── incremental_indexer.py  # MD5-based change detection and rebuild logic
    ├── hybrid_retriever.py     # Retrieve → Traverse → Rerank pipeline
    ├── iterative_retriever.py  # Multi-round retrieval with early stopping
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
- **Lazy CrossEncoder** — The 500 MB `jina-reranker-v2-base-code` model loads only on the first `investigate_architecture` call, so the MCP handshake completes instantly.

## Languages Supported for Indexing

- Python (`.py`)
- TypeScript (`.ts`, `.tsx`)
- JavaScript (`.js`, `.jsx`)
