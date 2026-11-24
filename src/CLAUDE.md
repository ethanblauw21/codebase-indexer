# CLAUDE.md

Guidance for Claude Code in this repository.

## What This Is

A local code intelligence engine that indexes codebases (Python, TypeScript/JavaScript incl. Apps Script `.gs`, C#, C++, Rockwell L5X; see `src/adapters/`) into a hybrid FAISS + SQLite store and exposes query capabilities through an MCP (Model Context Protocol) server. Packaged with `pyproject.toml` (entry points in the root `CLAUDE.md`); there is no web server or build pipeline.

## Rules to preserve when editing

- **A code default must equal the value shipped in `indexer.toml`** (`tests/test_config_drift.py` enforces it, ADR-026 section 8). Model IDs for the embedder, reranker and summarizer are all read from `indexer.toml` at runtime; `[summarization].enabled` is the real on/off gate. Reranking is **off by default** (`[reranker].enabled = false`); the RRF top-10 is the measured baseline (ADR-007), not a fallback.
- **Changing the embedder is a one-time reindex** (ADR-009): update `model_id` and `dimension` together, delete `.code-index`, rerun `code-indexer`. `core.py` refuses to load an index whose dimension no longer matches. Model download commands: `docs/agents/models-and-embedder.md`.
- **`src/scan_policy.py` is the single answer to "does the indexer look at this file?"** (ADR-026 section 2); `incremental_indexer.scan_disk()` and `MCPServer._CodeChangeHandler._is_relevant` both call it. It is a **leaf module** (`os`, `dataclasses`, `functools`, `fnmatch`, `config` only): never import a sibling `src` module into it. `is_indexable()` is chunk+embed source; `is_scannable()` adds `.csproj`/`.sln`/`compile_commands.json` (edges only), and the watchdog uses the second.
- **`[ignore]` semantics**: `extra_*` keys add to defaults; bare `dirs`/`root_dirs`/`extensions`/`files` replace them; both spellings of one knob raises; `!name` drops one default. Defaults live in `scan_policy.py` and are deliberately **not** copied into `indexer.toml` (a bare key would freeze the gate). Credential file patterns (`.env*`, `*credentials*.json`, key/pem files) are never scanned. Defaults and rationale: `docs/agents/scan-gate-defaults.md`.
- **`what_writes` does no retrieval** (ADR-013): exact answer from resolved `writes` edges. Do not "unify" it with `trace_data_flow`; routing an exact relation through the ranked surface can only lose writers. Add a language's data-flow idioms to its adapter (`data_flow_rules()`), not to the tool. `get_symbol_source` never serves the stored copy of a symbol (ADR-050). Tool docstrings are written for AI consumption: they say *when* to call the tool. Details: `docs/agents/mcp-tools.md`.
- Seventeen MCP tools, by intent: Search (`semantic_code_search`, `find_similar_code`); Lookup (`get_file_outline`, `get_symbol_source`, `changed_symbols`); Impact (`analyze_blast_radius`, `detect_pattern_violations`); Tracing (`trace_data_flow`, `investigate_architecture`, `what_writes`); Discovery (`find_test_coverage`, `find_dead_code`, `find_unabstracted_collection_reads`, `map_module_communities`, `verify_candidate_edges`); Maintenance (`reindex`, `index_status`).
- **Stable FAISS IDs** (60-bit, file hash x offset) allow surgical removes; FAISS and SQLite are complementary, not redundant.

Architecture (three-tier index, data flow, retrieval pipeline, key decisions): `docs/agents/architecture.md`.
