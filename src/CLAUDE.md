# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A local code intelligence engine that indexes Python/TypeScript/JavaScript codebases into a hybrid FAISS + SQLite store and exposes query capabilities through an MCP (Model Context Protocol) server. It is a standalone Python utility — there is no web server, build pipeline, or package manifest.

## Reference Documents

- **Contribution Standards:** Read `CONTRIBUTING.md` before starting any new work — defines minor vs major classification, branch naming, ADR requirements, bug/feature tracking, and PR process.
- **Docs map:** `docs/README.md` — what every document in `docs/` is for.

## Working Lists

Three documents, three jobs — do not let one hold another's content (`CONTRIBUTING.md` §4):

- `docs/backlog.md` — **wants.** Problems and requests, `B-NNN`, with a source and a date.
- `docs/roadmap.md` — **order.** Sequencing and dependency edges. Start here to find what's next.
- `docs/adr/` — **decisions.** One committed solution per file, plus its Implementation Log.

**A backlog item asserts a problem; an ADR asserts a solution you are building.** New wants go to the
backlog, never to a new `proposed` ADR on `master`.

## Working Style

- For non-trivial new features or significant design decisions, use `/grill-plan` to produce an ADR before implementing.
- Major changes are any changes to `src/`. They require a branch, an ADR, and a PR. See `CONTRIBUTING.md`.
- **The ADR is born on its branch** — created in the branch's first commit, `accepted` when it reaches `master`. Nothing is ever proposed on `master`.

## Installation

Preferred (development install — exposes `code-indexer` and `code-indexer-serve` entry points):

```bash
pip install -e .
```

Or install dependencies only (no entry points):

```bash
pip install -r requirements.txt
```

## Running

```bash
code-indexer          # rebuild the index (incremental by default)
code-indexer-serve    # start the MCP server with watchdog auto-reindex
```

Or directly:

```bash
python src/MCPServer.py
python src/incremental_indexer.py
```

## Tests

```bash
python -m pytest tests/ -v
```

Tests live in `tests/`. The `pyproject.toml` sets `pythonpath = ["src"]` so imports work without installing the package.

Key test files:
- `tests/test_stable_id.py` — golden fixtures for the 60-bit ID formula; diff categorization; dtype contracts

## Retrieval Eval

```bash
python tools/eval_retrieval.py [--index-dir .code-index] [--verbose]
```

Compares tier-1-only baseline vs. three-tier RRF fusion across a fixed 10-query set. Reports MRR@5 and Hit@{1,3,5}. Exits 1 if three-tier degrades quality (surface; do not revert silently — per ADR-002 H3). Requires a built index; run `code-indexer` first.

## Models

All models are downloaded automatically by HuggingFace on first use. To pre-download before going offline (recommended for CI or air-gapped environments), use `huggingface-cli` (ships with `huggingface_hub`, a transitive dep of `sentence-transformers`):

```bash
huggingface-cli download jinaai/jina-embeddings-v2-base-code   # embedder (~300 MB)
huggingface-cli download jinaai/jina-reranker-v2-base-code     # reranker (~500 MB, optional)
huggingface-cli download Qwen/Qwen2.5-Coder-1.5B-Instruct      # summarizer (~3 GB, optional)
```

Model IDs live in `indexer.toml` (`[embeddings]`, `[reranker]`, `[summarization]`) and are read from config at runtime: the **embedder** (`src/core.py`, ADR-009 §P1 — `model_id` + `max_seq_length` + `dimension`) and the **reranker** (`src/config.py` → `HybridRetriever`). The **summarizer** (`summarizer.py`) still hardcodes its id (not yet migrated). The reranker and summarizer are both optional — the indexer degrades gracefully without them. **Reranking is off by default** (`[reranker].enabled = false`): the retriever returns the RRF-ranked top-10, which is the measured Wave-0 baseline (ADR-007), not a fallback.

**Changing the embedder is a one-time reindex** (ADR-009 §P1): a new model usually has a different vector `dimension`, so update `model_id` + `dimension` together, delete `.code-index`, and rerun `code-indexer`. `stable_id`s are model-independent, so it is recompute-vectors-only; `core.py` refuses to load an index whose dimension no longer matches the configured embedder.

The pre-download `huggingface-cli` line for the reranker above lists the configured model; the prior `jinaai/jina-reranker-v2-base-code` id was a non-existent model and has been replaced by `Qwen/Qwen3-Reranker-0.6B`.

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
  → core.py            (Jina-Code embeddings, FAISS, token counting)
  → db.py              (SQLite: files, symbols, chunks, edges, symbol_references)
  → MCPServer.py       (MCP tools over the combined index)
```

### Retrieval Pipeline (RTR)

`hybrid_retriever.py` implements a three-step Retrieve → Traverse → Rerank pipeline:

1. **Semantic**: FAISS top-50 candidates from each of the three tiers, fused via Reciprocal Rank Fusion (RRF, k=60)
2. **Structural**: One-hop call-graph expansion via SQLite (bidirectional, cycle-guarded CTEs); edges corroborated against the import graph
3. **Reranking**: optional, **off by default** — returns the RRF-ranked top-10. When `[reranker].enabled = true`, `src/reranker.py` rescores candidates with the configured model (`Qwen/Qwen3-Reranker-0.6B`, a causal-LM yes/no scorer, or a sentence-transformers CrossEncoder by id), falling back to RRF on load/predict failure

`iterative_retriever.py` wraps this in multi-round loops with confidence-based early stopping and query enrichment from prior findings.

### MCP Tools (MCPServer.py)

Thirteen AI-facing tools grouped by intent:

- **Search**: `semantic_code_search`, `find_similar_code`
- **Impact**: `analyze_blast_radius`, `detect_pattern_violations`
- **Tracing**: `trace_data_flow`, `investigate_architecture`
- **Discovery**: `find_test_coverage`, `find_dead_code`, `find_unabstracted_collection_reads`, `map_module_communities`, `verify_candidate_edges`
- **Maintenance**: `reindex`, `index_status`

`verify_candidate_edges` (ADR-023) reports edge-aware three-state verdicts over
candidate (name-based / unresolved) call edges. `index_status` (ADR-025) reports
index freshness — `last_verified_at`, `last_indexed_commit` vs HEAD, and files whose
content changed within a window — for agents and downstream context hubs.

`map_module_communities` (ADR-006) is the whole-graph structural view: it reads the
SQLite edge graph, runs Louvain community detection + betweenness centrality over it
(`src/graph_analytics.py`, pure NetworkX), and renders a community map + god-object
report (`src/graph_report.py`). Inspired by Graphify (MIT); reimplemented natively
over our EXTRACTED edges. Output is explicitly exploratory — its quality bar is
deferred to ADR-008.

Tool docstrings are intentionally written for AI consumption — they describe *when* to call the tool, not just what it does.

### Key Design Decisions

- **Stable FAISS IDs**: 60-bit deterministic IDs (file hash × offset) allow surgical removes without full rebuilds.
- **Dual persistence**: FAISS handles ANN speed; SQLite handles graph queries — they are complementary, not redundant.
- **Token budgeting**: Context packing in `core.py` respects LLM context windows and warns when truncating.
- **Import resolution**: `import_resolver.py` handles tsconfig path aliases, relative imports, barrel files, and extension inference so graph edges reflect real module boundaries.
- **Monster-line shredding**: `ast_chunker.py` detects and strips Base64 inline SVGs and similar noise before chunking.
