# Graph Report - .  (2026-06-18)

## Corpus Check
- Corpus is ~43,650 words - fits in a single context window. You may not need a graph.

## Summary
- 697 nodes · 1456 edges · 35 communities (30 shown, 5 thin omitted)
- Extraction: 71% EXTRACTED · 29% INFERRED · 0% AMBIGUOUS · INFERRED: 417 edges (avg confidence: 0.55)
- Token cost: 66,269 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Adapter Base Types & Protocol|Adapter Base Types & Protocol]]
- [[_COMMUNITY_Retrieval & Document Store|Retrieval & Document Store]]
- [[_COMMUNITY_Textual TUI App|Textual TUI App]]
- [[_COMMUNITY_C++ Adapter|C++ Adapter]]
- [[_COMMUNITY_MCP Server & Code-Analysis Tools|MCP Server & Code-Analysis Tools]]
- [[_COMMUNITY_C Adapter|C# Adapter]]
- [[_COMMUNITY_ADR Hardening & Expansion Decisions|ADR Hardening & Expansion Decisions]]
- [[_COMMUNITY_CodeDB Query Layer|CodeDB Query Layer]]
- [[_COMMUNITY_Embeddings & Tokenization|Embeddings & Tokenization]]
- [[_COMMUNITY_Incremental Indexing Pipeline|Incremental Indexing Pipeline]]
- [[_COMMUNITY_SymbolChunk Store Access|Symbol/Chunk Store Access]]
- [[_COMMUNITY_Stable FAISS ID Utilities|Stable FAISS ID Utilities]]
- [[_COMMUNITY_Diff Computation & Stable-ID Tests|Diff Computation & Stable-ID Tests]]
- [[_COMMUNITY_C Sample Fixtures|C# Sample Fixtures]]
- [[_COMMUNITY_C++ Sample Fixtures|C++ Sample Fixtures]]
- [[_COMMUNITY_Import Resolver (TSJS)|Import Resolver (TS/JS)]]
- [[_COMMUNITY_Stable-ID Formula Tests|Stable-ID Formula Tests]]
- [[_COMMUNITY_Governance & Project Docs|Governance & Project Docs]]
- [[_COMMUNITY_Adapter Snapshot Testing|Adapter Snapshot Testing]]
- [[_COMMUNITY_Chunk Summarizer (LLM)|Chunk Summarizer (LLM)]]
- [[_COMMUNITY_LanguageAdapter Contract|LanguageAdapter Contract]]
- [[_COMMUNITY_Python Sample Fixture|Python Sample Fixture]]
- [[_COMMUNITY_Isolated Summarizer Worker|Isolated Summarizer Worker]]
- [[_COMMUNITY_Category Tagger|Category Tagger]]
- [[_COMMUNITY_CodeDB WriteCache Ops|CodeDB Write/Cache Ops]]
- [[_COMMUNITY_TUI MCP Backend|TUI MCP Backend]]
- [[_COMMUNITY_Call-Graph DB Layer|Call-Graph DB Layer]]
- [[_COMMUNITY_TypeScript Sample Fixture|TypeScript Sample Fixture]]
- [[_COMMUNITY_AST Chunker|AST Chunker]]
- [[_COMMUNITY_DB Schema Migrations|DB Schema Migrations]]
- [[_COMMUNITY_JavaScript Sample Fixture|JavaScript Sample Fixture]]
- [[_COMMUNITY_TSX Sample Fixture|TSX Sample Fixture]]
- [[_COMMUNITY_Recursive File Search|Recursive File Search]]
- [[_COMMUNITY_Monster-Line Shredding|Monster-Line Shredding]]

## God Nodes (most connected - your core abstractions)
1. `CodeDB` - 56 edges
2. `ParseResult` - 43 edges
3. `Symbol` - 42 edges
4. `TestConventions` - 41 edges
5. `Edge` - 38 edges
6. `Reference` - 38 edges
7. `node_text()` - 37 edges
8. `SearchScreen` - 29 edges
9. `Node` - 25 edges
10. `HybridRetriever` - 25 edges

## Surprising Connections (you probably didn't know these)
- `ndarray` --uses--> `DocumentStore`  [INFERRED]
  tools/eval_retrieval.py → src/core.py
- `test_snapshot_matches_golden()` --calls--> `chunk_file_ast()`  [INFERRED]
  tests/test_adapter_snapshots.py → src/ast_chunker.py
- `test_to_faiss_ids_dtype()` --calls--> `to_faiss_ids()`  [INFERRED]
  tests/test_stable_id.py → src/stable_id.py
- `test_to_faiss_ids_empty()` --calls--> `to_faiss_ids()`  [INFERRED]
  tests/test_stable_id.py → src/stable_id.py
- `test_to_faiss_matrix_dtype()` --calls--> `to_faiss_matrix()`  [INFERRED]
  tests/test_stable_id.py → src/stable_id.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **All Language Adapters Implement LanguageAdapter Protocol** — adr_adr_003_adapter_architecture_and_language_expansion_python_adapter, adr_adr_003_adapter_architecture_and_language_expansion_web_adapter, adr_adr_003_adapter_architecture_and_language_expansion_csharp_adapter, adr_adr_003_adapter_architecture_and_language_expansion_cpp_adapter, adr_adr_003_adapter_architecture_and_language_expansion_l5x_stub, adr_adr_003_adapter_architecture_and_language_expansion_language_adapter_protocol [EXTRACTED 1.00]
- **H1-H3 Merge Blockers for ADR-003 Phase 0** — adr_adr_002_pre_expansion_hardening_stable_id_module, adr_adr_002_pre_expansion_hardening_atomic_persistence, adr_adr_002_pre_expansion_hardening_tier_fusion, adr_adr_003_adapter_architecture_and_language_expansion_adapter_architecture [EXTRACTED 1.00]
- **Per-Project Risk Rule Packs (H6 Externalization)** — examples_firebase_rules_firebase_ruleset, examples_csharp_rules_csharp_ruleset, examples_cpp_rules_cpp_ruleset, adr_adr_002_pre_expansion_hardening_externalize_rules [EXTRACTED 1.00]

## Communities (35 total, 5 thin omitted)

### Community 0 - "Adapter Base Types & Protocol"
Cohesion: 0.07
Nodes (59): build_fqn(), Edge, ParseResult, Language adapter base types and Protocol.  Symbol, Edge, Chunk, Reference, Symbo, Build a file-scoped FQN: `file_path::ClassName.member` or `file_path::name`., File naming patterns and in-file markers that identify test code for a language., A named code entity extracted from a source file., A directed dependency from a symbol (or file) to another identifier. (+51 more)

### Community 1 - "Retrieval & Document Store"
Cohesion: 0.05
Nodes (38): FileSystemEventHandler, IterativeRetriever, DocumentStore, MultiIndexManager, In-memory chunk-payload cache backed by the SQLite `chunks` table.      On sta, HybridRetriever, _is_fqn(), hybrid_retriever.py — Retrieve-Traverse-Rerank pipeline for the Code Intelligenc (+30 more)

### Community 2 - "Textual TUI App"
Cohesion: 0.05
Nodes (19): App, ComposeResult, Highlighted, ModalScreen, Screen, Selected, Submitted, ToolDef (+11 more)

### Community 3 - "C++ Adapter"
Cohesion: 0.11
Nodes (33): _cpp_skeletonize(), CppAdapter, _extract_param_type(), _extract_params_text(), _find_inner_id(), _find_param_name(), _get_base_types(), _get_fn_declarator() (+25 more)

### Community 4 - "MCP Server & Code-Analysis Tools"
Cohesion: 0.09
Nodes (39): get_adapter(), Return the adapter for the given file extension, or None if unsupported., analyze_framework_tags(), Analyse a source file for architectural framework markers.     Returns ([], {}), embed(), Generates code-native embeddings via Jina., analyze_blast_radius(), _analyze_risks() (+31 more)

### Community 5 - "C# Adapter"
Cohesion: 0.14
Nodes (18): _base_types(), _cs_skeletonize(), CSharpAdapter, CsprojResolver, _decl_name(), _is_partial(), _param_count(), _qualified_or_identifier() (+10 more)

### Community 6 - "ADR Hardening & Expansion Decisions"
Cohesion: 0.08
Nodes (31): H2: Atomic Persistence / Retire doc_store.json, H5: Call-Edge Corroboration, H6: Externalize Risk Rules, ADR-002 Pre-Expansion Hardening, H7: Packaging and Model Provisioning, H4: Watchdog Reload Guard, H1: stable_id.py Extraction, H3: Tier-2/3 RRF Fusion (+23 more)

### Community 7 - "CodeDB Query Layer"
Cohesion: 0.07
Nodes (11): CodeDB, Thin wrapper around a SQLite connection for the Code Intelligence Engine.      U, Return file paths that import `canonical_path`, checking resolved_target, Return FQNs of symbols owned by `fqn` (OWNS edges where source=fqn)., Return FQNs of symbols that own `fqn` (OWNS edges where target=fqn)., How many reference locations exist for this bare symbol name., All reference locations for `symbol_name`, ordered by file+line., Batch reference count lookup keyed by FQN (using bare symbol name).         Used (+3 more)

### Community 8 - "Embeddings & Tokenization"
Cohesion: 0.12
Nodes (19): SentenceTransformer, embed_batch(), _get_embed_model(), pack_context_safely(), ndarray, Pack reranked chunks into a strict token budget for 8B-parameter local models., Generates code-native embeddings in optimized batches.     Provides a 5x-15x sp, TokenizerManager (+11 more)

### Community 9 - "Incremental Indexing Pipeline"
Cohesion: 0.17
Nodes (20): DocumentStore, ImportResolver, Index, get_stale_ids(), ingest_file(), ingest_project_file(), main(), md5_file() (+12 more)

### Community 10 - "Symbol/Chunk Store Access"
Cohesion: 0.21
Nodes (13): Chunk, Embeddable text segment with provenance and dependency metadata.     Supports di, Cursor, Chunk, Symbol, SymbolType, Chunk, Edge (+5 more)

### Community 11 - "Stable FAISS ID Utilities"
Cohesion: 0.11
Nodes (18): ndarray, stable_id.py — Deterministic FAISS vector IDs and FAISS dtype utilities.  Single, Convert a list of Python ints to a FAISS-safe int64 array.      FAISS dtype cont, Stack 1-D embedding vectors into a 2-D float32 C-contiguous matrix.      FAISS d, to_faiss_ids(), to_faiss_matrix(), On Windows, numpy's default integer type is int32.     np.array([1, 2, 3]) → int, A 60-bit stable ID must round-trip through to_faiss_ids without truncation. (+10 more)

### Community 12 - "Diff Computation & Stable-ID Tests"
Cohesion: 0.18
Nodes (14): compute_diff(), Three-way comparison between disk state and the SQLite `files` table.      SQLit, _MockDB, Tests for stable_id.py — the single source of truth for FAISS vector IDs and dty, TIER_NUM and TIER_NAME must be exact inverses of each other., Minimal CodeDB stand-in for compute_diff tests., Hash comparison must catch content changes even when mtime is identical., test_compute_diff_all_new() (+6 more)

### Community 13 - "C# Sample Fixtures"
Cohesion: 0.17
Nodes (9): AllowAnonymous, Authorize, List, DataServiceBase, IDataService, MyApp.Services, Task, Config (+1 more)

### Community 14 - "C++ Sample Fixtures"
Cohesion: 0.14
Nodes (15): Cache, _capacity, clear, get, put, maxRetries, timeout, _cfg (+7 more)

### Community 15 - "Import Resolver (TS/JS)"
Cohesion: 0.16
Nodes (8): ImportResolver, import_resolver.py — Canonical import resolution for the Code Intelligence Engin, Parse an index.ts barrel file and return its re-exported symbol names.         R, Expand a tsconfig path alias to an absolute path, or return None., Try `base` with each known extension, then as a directory index.         Returns, Resolves raw TypeScript/JavaScript import specifiers to canonical     repo-relat, Read tsconfig.json compilerOptions.paths and return a dict mapping         alias, Return a canonical repo-relative POSIX path for `specifier` imported         fro

### Community 16 - "Stable-ID Formula Tests"
Cohesion: 0.14
Nodes (14): Deterministic 60-bit FAISS vector ID.      Formula: int(md5(f"{tier_name}::{file, stable_id(), Same inputs must always produce the same output., Swapping tier/path/scope must produce a different ID (field separator matters)., Reference formula for golden tests.  Must NOT be changed when stable_id.py     i, stable_id() must produce the same output as the pinned reference formula., All stable IDs must be non-negative and below the signed int64 ceiling., Formula uses 15 hex chars = 60 bits; result must fit. (+6 more)

### Community 17 - "Governance & Project Docs"
Cohesion: 0.21
Nodes (12): ADR Template, ADR-001 Engineering Governance Standards, Bug Report Issue Template, src/CLAUDE.md Repo Guidance, ADR Lifecycle, Change Classification (Minor/Major), Merge-Commits-Only Strategy, Feature Request Issue Template (+4 more)

### Community 18 - "Adapter Snapshot Testing"
Cohesion: 0.24
Nodes (10): parse_file(), Parse a source file and return all symbols, edges, references, and type     anno, Golden snapshot tests for the LanguageAdapter refactor (ADR-003 Phase 1).  These, _serialize_chunks(), _serialize_parse(), test_snapshot_matches_golden(), capture(), main() (+2 more)

### Community 19 - "Chunk Summarizer (LLM)"
Cohesion: 0.18
Nodes (8): ChunkSummarizer, summarizer.py — Optional LLM-based chunk summarization for embedding augmentatio, Lazy-loaded instruct LLM that augments chunk text with factual extractions     b, Return one extraction string per code chunk.          Empty strings are returned, ProcessPoolExecutor initializer — called once when the worker process     starts, Run one summarization batch inside the worker process.     Returns a list of ext, _worker_batch(), _worker_init()

### Community 20 - "LanguageAdapter Contract"
Cohesion: 0.18
Nodes (7): LanguageAdapter, Contract for all language-specific parsers.      Shared infrastructure (stable I, Parse source bytes and return symbols, edges, references, symbol types., Return (file_tags, fqn_tags).         file_tags propagate to every chunk in this, Return test identification conventions (file suffixes + in-file markers)., Return a project-level dependency resolver, or None if not applicable., Protocol

### Community 21 - "Python Sample Fixture"
Cohesion: 0.18
Nodes (7): DataProcessor, format_output(), Sample Python module for golden snapshot testing., Load records for the given key., Transform records into a summary., Render data as a formatted string., Processes raw data records.

### Community 22 - "Isolated Summarizer Worker"
Cohesion: 0.28
Nodes (5): NamedTuple, DiffResult, IsolatedChunkSummarizer, Drop-in replacement for ChunkSummarizer that runs the LLM in a dedicated     chi, Return one extraction string per code chunk, same contract as         ChunkSumma

### Community 23 - "Category Tagger"
Cohesion: 0.31
Nodes (8): classify_query(), _leading_comment(), category_tagger.py — Keyword-based semantic category tagging for code chunks.  A, Return the first few lines of a chunk — usually docstring or inline comments., Return category tags for a symbol based on its name and opening comment lines., Return category tags likely relevant to a natural-language query string., tag_symbol(), _tokenize()

### Community 24 - "CodeDB Write/Cache Ops"
Cohesion: 0.22
Nodes (3): Clear all cached graph traversal results after any write to edges., Remove a file and all its symbols, chunks, and outgoing edges.         ON DELETE, Persist (text_hash, summary) pairs. OR IGNORE — first write wins.

### Community 25 - "TUI MCP Backend"
Cohesion: 0.31
Nodes (8): Path, call_tool(), extract_files(), extract_results(), get_file_chunks(), _get_server(), Parse tool output into [{file, score}] preserving per-block score association., set_project_root()

### Community 26 - "Call-Graph DB Layer"
Cohesion: 0.32
Nodes (4): CallGraphNode, _normalise_edge_kind(), db.py — SQLite persistence layer for the Code Intelligence Engine.  Schema (six, Bidirectional call-graph traversal rooted at `fqn`.         Results are cached p

### Community 27 - "TypeScript Sample Fixture"
Cohesion: 0.25
Nodes (3): ProcessFn, Record, DataService

### Community 28 - "AST Chunker"
Cohesion: 0.38
Nodes (6): chunk_file_ast(), fallback_token_chunker(), AST-based code chunker using tree-sitter.  Primary API ----------- parse_file(fi, AST-guided chunker.  One Chunk per extracted Symbol; oversized symbols are     s, Token-based line chunker with a monster-line shredder for huge inlined blobs., _symbol_rich_text()

## Knowledge Gaps
- **30 isolated node(s):** `Language`, `ndarray`, `Path`, `Param`, `maxRetries` (+25 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LanguageAdapter` connect `Adapter Base Types & Protocol` to `C++ Adapter`, `LanguageAdapter Contract`, `C# Adapter`?**
  _High betweenness centrality (0.009) - this node is a cross-community bridge._
- **Why does `CodeDB` connect `CodeDB Query Layer` to `CodeDB Write/Cache Ops`, `Symbol/Chunk Store Access`, `Call-Graph DB Layer`, `DB Schema Migrations`?**
  _High betweenness centrality (0.009) - this node is a cross-community bridge._
- **Why does `get_adapter()` connect `MCP Server & Code-Analysis Tools` to `Adapter Base Types & Protocol`?**
  _High betweenness centrality (0.009) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `CodeDB` (e.g. with `Chunk` and `Symbol`) actually correct?**
  _`CodeDB` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `AST-Style Scope Recovery: converts anonymous_part_X to a navigable label.     T`, `CRITICAL: Use this tool FIRST before using standard file reading or grep.     U`, `Use this tool to find duplicate or mathematically similar code across the projec` to the rest of the system?**
  _202 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Adapter Base Types & Protocol` be split into smaller, more focused modules?**
  _Cohesion score 0.07286288009179576 - nodes in this community are weakly interconnected._
- **Should `Retrieval & Document Store` be split into smaller, more focused modules?**
  _Cohesion score 0.053208137715179966 - nodes in this community are weakly interconnected._