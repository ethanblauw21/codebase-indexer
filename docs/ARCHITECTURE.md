# Codebase Indexer — Architecture Guide

> A full walkthrough of what every part of this repo does, what it controls, why it was
> designed that way, and what remains open. This is a **snapshot** (2026-07-08, branch
> `fix/adr-008-adapter-extraction-gaps`); the `Status:` lines in `docs/adr/` are the
> living source of truth for any decision recorded here.

---

## 1. Orientation: what this is

A **local code-intelligence engine**. It reads a codebase, turns it into a searchable
*hybrid* index — semantic vectors **plus** a structural call/type graph — and exposes that
intelligence to AI assistants through a **Model Context Protocol (MCP) server**. The point
is to give an AI coding assistant a real understanding of a repo instead of `grep`.

### The problem it solves

An assistant working in your codebase has two bad options: read everything (blows the
context window) or `grep` for strings (misses semantic meaning and structural
relationships). This engine sits in between, answering questions like *"what breaks if I
change this function?"*, *"where is this pattern violated?"*, *"trace this data's
lifecycle,"* and *"show me the shape of this whole module graph."*

### The two halves that make it "hybrid"

This is the central design idea; everything else hangs off it.

| Half | Backed by | Answers | Strength |
|------|-----------|---------|----------|
| **Semantic** | FAISS vector index (bge-code / Jina embeddings) | "what code *means* this?" | fuzzy, intent-based recall |
| **Structural** | SQLite graph (symbols, edges, references) | "what *calls / owns / extends* this?" | exact, relational precision |

Neither alone is enough. FAISS gives fuzzy recall but no notion of "caller." SQLite gives
exact relationships but can't do "find code that feels like an auth check." The product is
the **fusion** of the two — which is why the retrieval pipeline is Retrieve (vectors) →
Traverse (graph) → Rerank.

### The strategic thesis: depth over breadth

The project deliberately supports **5 languages** (Python, TypeScript, JavaScript, C#, C++)
and tries to make its extraction *provably* accurate — rather than claiming 30 languages
badly. The positioning is explicit: *this is a heuristic structural indexer, not a compiler
frontend.* For compile-accurate C++ you'd use clangd/SCIP; this tool's value is good-enough
graph + semantic search over repos where no compile-accurate index exists.

### How the pieces fit

```
                         ┌─────────────────────────────────────┐
   your source code  ─►  │  INDEXING                            │
                         │  adapters/ (tree-sitter per-lang)    │
                         │  → ast_chunker → incremental_indexer │
                         └───────────────┬─────────────────────┘
                                         │ symbols, edges, chunks
                        ┌────────────────┴───────────────┐
                        ▼                                 ▼
                 ┌─────────────┐                   ┌─────────────┐
                 │ core.py     │                   │ db.py       │
                 │ embeddings  │                   │ SQLite      │
                 │ → FAISS     │                   │ graph       │
                 │ (3 tiers)   │                   │             │
                 └──────┬──────┘                   └──────┬──────┘
                        └───────────────┬─────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │ hybrid_retriever (RTR)         │
                        │ + iterative_retriever          │
                        │ + graph_analytics              │
                        └───────────────┬───────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │ MCPServer.py — 11 AI-facing    │
                        │ tools (spoken to by Claude etc)│
                        └───────────────────────────────┘
```

### Two governance systems wrapped around the code

What makes this repo unusual for its size (~7k lines of `src`) is the scaffolding around it:

- **22 ADRs** (`docs/adr/ADR-001`…`ADR-022`) — every architectural decision is written down,
  numbered, and cross-referenced.
- **An evaluation culture** — `tests/` (conformance fixtures), `tools/` (eval harnesses),
  `benchmarks/` (recorded results), and `cloud/` (GPU eval on a VM). Claims get *measured*.

Mental model: **`src/` is the engine; the ADRs are the steering log; the eval harness is the
dynamometer.**

---

## 2. The indexing pipeline: source → symbols, edges, chunks

The front half of the engine. Five files turn raw text into the two artifacts everything
downstream consumes: **embeddable chunks** and a **structural graph**.

### The data model (`adapters/base.py`)

Every stage passes these language-neutral dataclasses around:

- **`Symbol`** — a named entity (function/class/method/interface). Carries `fqn`
  (`path::Class.member`), `kind`, line span, raw `text`. `shared=True` flags symbols that can
  span files (C# partial classes).
- **`Edge`** — a directed relationship: `import | call | owns | extends | implements |
  provides_context | consumes_context`. `resolved_target` is filled for imports at parse
  time; for calls later (see `call_resolver`).
- **`Chunk`** — an embeddable text segment with provenance. Becomes a FAISS vector.
- **`Reference`** — an exact usage site of a symbol (line-level).
- **`SymbolType`** — return type / params / async-ness (mostly TS).
- **`ParseResult`** — the NamedTuple every adapter returns: `(symbols, edges, references,
  symbol_types)`.

The vocabulary is **language-neutral** — a Python method and a C++ template both come out as
`Symbol`+`Edge`. That is what lets one downstream pipeline serve five languages.

### Step 1 — Adapters do language-specific extraction

`ast_chunker.parse_file()` is a thin dispatcher: it looks at the file extension, asks the
registry (`adapters/__init__.py`) for the matching adapter, and calls `adapter.parse()`.
Unsupported extension → empty `ParseResult`, never an exception.

### Step 2 — `ast_chunker` turns symbols into three tiers of chunks

- **`chunk_file_ast()`** — the tier-1 "surgical" chunker. **One chunk per AST symbol.** For
  each symbol it builds a *rich text* wrapper (`_symbol_rich_text`): a header (`File:`,
  `Entity: <fqn> (kind)`, `Tags:`, `Type:`, `Lines:`) then `Code:`. That header is what makes
  the embedding semantic rather than lexical.
- **`fallback_token_chunker()`** — line-oriented sliding window with overlap, used for tiers
  2/3, unsupported files, and oversized symbols. Notable feature: the **monster-line
  shredder** — if one line exceeds the whole token budget (a Base64-inlined SVG, a minified
  bundle) it is token-sliced instead of corrupting a chunk.

The three tiers (surgical 500 / component 1500 / architectural 4000 tokens) are defined once
in `stable_id.py::TIER_CONFIGS`. Only tier-1 is AST-aware; tiers 2/3 are sliding windows over
the whole file.

### Step 3 — `stable_id.py`: the ID scheme that makes incremental updates possible

`stable_id(tier, path, scope)` = `int(md5(f"{tier}::{path}::{scope}")[:15], 16)` — a
**deterministic 60-bit** FAISS vector ID. Because the ID is a pure function of
`(tier, path, scope)`, **IDs are never stored** — they're recomputed on demand from SQLite
columns, which is what enables *surgical removes* (purge a file's vectors by re-deriving their
exact IDs, no full rebuild). Two constraints, pinned by golden tests:

- **60 bits, not 64** — FAISS `idx_t` is *signed* int64; 64-bit MD5 prefixes would go negative
  and mis-route. 15 hex chars stays under 2^63.
- **The Windows int32 trap** — `to_faiss_ids()` / `to_faiss_matrix()` are the *only* sanctioned
  way to build FAISS arrays, because numpy defaults to int32 on Windows and would throw a SWIG
  TypeError.

Changing this formula invalidates every index on disk; `tests/test_stable_id.py` is the guard.

### Step 4 — `call_resolver.py`: making the call graph traversable (ADR-021)

Adapters emit `CALLS` edges whose target is just the **bare callee name** (`enqueue`) — they
can't know which of the repo's five `enqueue`s you meant. Left alone, the graph-traversal step
has nothing to resolve against and the graph layer becomes a **retrieval no-op** (a real
discovered bug, ADR-019).

`resolve_call_edges()` runs once after all files are indexed and fills `resolved_target` **only
when it can prove the target is unique**, in priority order: (1) unique repo-wide → (2) unique
in the caller's own file → (3) unique among imported files → else NULL. It is
**precision-first** (a wrong edge is worse than a missing one), so ambiguous receiver-typed
calls are left for ADR-011. It recomputes *every* run, so a resolution is demoted back to NULL
if a newly-added symbol makes the name ambiguous.

### Step 5 — `incremental_indexer.py`: the orchestrator

`run_incremental()` is the conductor, designed to do the least work and survive crashes:

- **MD5 change detection** — walks the disk, MD5s each file's bytes (64 KiB blocks, constant
  memory), and three-way-diffs against the `content_hash` column: `new / modified / deleted`.
  MD5 over SHA-256 because it's ~3× faster and this isn't a security check. Unchanged files skip
  embedding entirely.
- **Crash-safe ordering** — compute diff → get stale IDs from SQLite *before* mutating → purge
  FAISS (in-memory) → delete from SQLite (per-file atomic) → re-index → *then* persist FAISS. If
  it dies mid-run, deleted rows are simply absent next run and re-index cleanly.
- **Per-file ingest** (`ingest_file`) chunks all three tiers, embeds, `add_with_ids`, upserts
  SQLite, with an optional summarizer pass (cache-checked by text-hash).
- **Project descriptors** (`.csproj/.sln/compile_commands.json`) go through
  `ingest_project_file` — edges only, no chunks/vectors.

**Live state to know:** `INDEXABLE_EXTS` recently gained C#/C++ extensions (their source was
previously never chunked, only their project descriptors). And `IGNORE_DIRS` still omits
`venv`/`site-packages`/`__pycache__` — a latent bug on repos with an in-tree virtualenv.

### Open levers

- **ADR-011 — high-precision call resolution**: the receiver-typed gap in `call_resolver` is the
  biggest accuracy lever left in the graph.
- **The `IGNORE_DIRS` venv gap** — small, latent, known.
- **ADR-016 — persisted symbol tree**: symbols are currently re-derived.

---

## 3. The embedding & storage core: `core.py` + `db.py`

Where the two halves of the hybrid physically live. FAISS (via `core.py`) for vectors; SQLite
(via `db.py`) for the graph. Complementary, not redundant.

### `core.py` — embeddings, FAISS management, token budgeting

1. **The embedder (config-driven, lazy, ADR-009).** Read from `indexer.toml` `[embeddings]`,
   with the historical Jina stack as default. Settled: **bge-code-v1 is the validated default**
   (+0.062 mrr@10). Design touches:
   - **Lazy singleton** — the model loads on *first embed call*, not import, so the summarizer's
     worker processes don't each load a 3 GB model, and the MCP handshake stays instant.
   - **The 512-token cap is a crash guard.** Self-attention memory is O(L²); a 4000-token chunk
     needs ~9 GB of tensors and dies with an uncatchable Windows SEH exception. 512 keeps peak
     memory <1 GB. (4096 was tried, no eval change, reverted.)
   - **Asymmetric query/document paths** — `embed_batch()` (indexing, no prefix) vs `embed()`
     (query, optional `<instruct>` prefix for embedders like bge-code).

2. **`MultiIndexManager`** — one `IndexFlatIP` (exact inner-product; the corpus is small enough
   that exact beats approximate) wrapped in an `IndexIDMap` per tier. The **reindex guard**
   raises loudly if an on-disk index's dimension doesn't match the configured embedder — the
   safety net for the embedder swap (recompute-vectors-only, stable_ids unchanged).

3. **`DocumentStore`** — an in-memory `{faiss_id → chunk metadata}` cache hydrated from SQLite.
   FAISS stores only vectors+IDs; this turns a returned ID back into `{file, scope, text,
   tags}`. (Formerly a `doc_store.json`; SQLite is now authoritative and the JSON is
   auto-retired.)

4. **`pack_context_safely`** — packs reranked chunks into a hard token budget. **Structural**
   chunks whose score is within 10% of the top *semantic* score are promoted ahead of
   lower-ranked semantic chunks, so architectural context isn't crowded out by volume. Appends a
   `[CONTEXT TRUNCATED]` warning when it drops chunks.

### `db.py` — the structural graph (SQLite, WAL)

**Schema (7 tables):**

| Table | Holds | Purpose |
|-------|-------|---------|
| `files` | path, `content_hash`, indexed_at | change-detection authority |
| `symbols` | fqn (unique), kind, name, class_context, span, text | every extracted entity |
| `symbol_locations` | (symbol, file) → span | multi-location symbols (C# partial, C++ header/impl) |
| `chunks` | scope, tier, text, tags | chunk payloads |
| `chunk_summaries` | md5(text) → summary | LLM summary cache, survives renames |
| `edges` | source_fqn, target, kind, `resolved_target` | the dependency graph |
| `symbol_references` | name, fqn, line, ref_kind, context | usage sites for find-refs + density |
| `symbol_types` | return_type, params, async/gen | TS type metadata |

PRAGMAs are tuned for a read-heavy embedded workload: **WAL** (concurrent reads during a write),
64 MB cache, `MEMORY` temp store, 256 MB mmap.

**Centerpiece — `_CALL_GRAPH_SQL`:** a single recursive CTE doing bidirectional call-graph
traversal. Three UNION arms (anchor, `calls` forward, `called_by` backward). It traverses on
`COALESCE(resolved_target, target)` — precise resolved FQN when `call_resolver` filled it, bare
name otherwise — and carries a `char(31)`-delimited `visited` string as a cycle guard. This one
query is the entire "Traverse" step.

Edge-kind normalization: adapters emit lowercase (`call`); the DB canonicalizes to uppercase
(`CALLS`) via `_EDGE_KIND_MAP`. Migrations (`_migrate_edges`, `_migrate_symbol_locations`) show
the schema self-upgrades on open — no external migration tool.

### Open levers

- **ADR-016 — persisted symbol tree** (`symbol_locations` is groundwork).
- **Config migration is partial** — the summarizer still hardcodes its model id; only the
  reranker + embedder + retrieval read `indexer.toml`.
- **ADR-022 — graph-neighbor scoring** — the active lever for making the structural half
  influence ranking.

---

## 4. The retrieval pipeline: Retrieve → Traverse → Rerank

Where the two halves fuse. `hybrid_retriever.py` is the heart; `fusion.py`, `reranker.py`, and
`iterative_retriever.py` support it.

### The output type: `RetrievedChunk`

Two fields carry hybrid signal beyond `score`:

- **`source`** — `"semantic"` (from FAISS) vs `"structural"` (from the graph walk).
- **`corroborated`** — `False` when a `CALLS` edge isn't backed by an `IMPORTS` edge. Unverified
  graph edges still *inform* retrieval, but the verdict tools (blast-radius, dead-code) exclude
  them.

### Step 1 — Retrieve (semantic, multi-tier RRF)

`_semantic_search` embeds the query once, searches **all three FAISS tiers**, and fuses them
with **Reciprocal Rank Fusion** (`score += 1/(60 + rank)`). RRF is used because the three tiers
have different score distributions; fusing on **rank position** is scale-free. Tier-1 hits (which
carry FQNs) become graph seeds.

**Fusion fork (ADR-009 §P3):** default `fusion_mode="rrf"` (dense only). `"convex"` also builds an
in-memory **BM25** index and combines via `fusion.py::convex_fuse`. Sparse matters because the
tokenizer keeps identifiers whole (`snake_case`/`camelCase` stay single tokens), so BM25 catches
exact-identifier matches dense embeddings blur. `convex_fuse` min-max-normalizes each signal
then does a weighted sum whose ratio is the hook ADR-014 will later learn. Off by default until
it beats the Wave-0 baseline.

### Step 2 — Traverse (structural expansion)

`_expand_structurally_budgeted` is a **budgeted beam search** over the call graph: seed the
frontier with the top-5 semantic hits, expand highest-scoring nodes via the SQLite CTE, keep the
top-5 neighbors by a hop-decayed score (×0.7/hop), label each `corroborated`, and stop at 20 new
nodes or when scores decay below threshold. Designed for **bounded latency** (depth 1, beam 5,
budget 20).

The `graph_enabled` toggle: production leaves it on; the ADR-019 eval flips it off to measure
**arm A (semantic) vs arm B (semantic+graph)** — the paired lift B−A that isolates what Traverse
earns.

### Step 3 — Rerank (optional, off by default)

- **Reranker OFF (default):** returns the RRF-ranked pool with a `_CATEGORY_BOOST` nudge for
  category-matching chunks. This is the *measured Wave-0 baseline*, not a degraded fallback.
- **Reranker ON:** `reranker.py` loads either a sentence-transformers `CrossEncoder` or a
  `Qwen3Reranker` (a causal LM read as a yes/no classifier via the "yes"/"no" token logit), both
  behind one `.predict()` surface. Hard-won detail lives here: left-padding is mandatory (logits
  read at the final position), the attention mask is required, and a CUDA batch cap prevents a
  9 GB logits-tensor OOM that would silently fall back to RRF. When on, the final score is a
  composite: cross-encoder + density + locality + category bonuses.

**Settled verdict:** reranker lift on CoIR was neutral/negative, so `[reranker].enabled` stays
false. Every failure mode falls back to RRF with an identical return type.

### The wrapper — `iterative_retriever.py`

Runs the RTR pipeline up to N rounds, accumulating evidence. Each round enriches the query with
the top-3 FQNs found so far, excludes explored FQNs, and checks a **confidence** stop (ratio of
5th-best to top score ≥ 0.85). Powers `investigate_architecture`.

### Design spine

Every expensive or unproven stage is **off by default and measured before it's trusted**:
reranker off (neutral lift), convex fusion off (until it beats baseline), graph expansion with a
built-in ablation. The defaults are the honest measured baseline.

### Open levers

- **ADR-022 — graph-neighbor retrieval scoring** (the answer to "graph is inert under RRF").
- **ADR-014 — usage-driven adaptive ranking** (learns the fusion weights).
- **Sparse tokenizer v1 → subtokens** (`fusion.py` flags splitting identifiers as future work).

---

## 5. `MCPServer.py`: the AI-facing surface and its 11 tools

1,740 lines, the largest file. Built on **FastMCP**; each `@mcp.tool()` function becomes a tool
in Claude Code / Continue.dev.

### The docstring *is* the API

MCP ships each tool's docstring to the LLM as its description, so those docstrings are **prompt
engineering aimed at the calling model** (`semantic_code_search` literally opens with "CRITICAL:
Use this tool FIRST before... grep"). Getting them right is as important as the retrieval code.

### Lifecycle: instant handshake, lazy everything

`main()` calls `start_watchdog()` then `mcp.run()`. Indexes load lazily — `_ensure_indexes()`
builds FAISS/doc-store on the first tool call, not at startup.

### The 11 tools

| Tool | Category | What it does |
|------|----------|--------------|
| `semantic_code_search` | Search | multi-tier FAISS + RRF |
| `find_similar_code` | Search | duplicates/callers of a snippet |
| `analyze_blast_radius` | Impact | sorts the repo into Origin / Direct Dependents / Primitives / Parallel Implementations via directional import filtering |
| `detect_pattern_violations` | Impact | files that match a pattern semantically but lack the enforced symbols |
| `trace_data_flow` | Tracing | full lifecycle of a data symbol |
| `investigate_architecture` | Tracing | full iterative RTR + risk analysis → Markdown report |
| `find_test_coverage` | Discovery | tests covering a file/symbol |
| `find_dead_code` | Discovery | any consumers? (uses the corroborated graph) |
| `find_unabstracted_collection_reads` | Discovery | enforce "reads of X go through Y" |
| `map_module_communities` | Discovery | whole-graph analytics + DSM |
| `reindex` | Maintenance | full/incremental rebuild + hot-swap |

### Live reindex: watchdog → debounce → hot-swap

1. **`start_watchdog`** — a `watchdog` Observer (kernel-pushed `ReadDirectoryChangesW` on
   Windows, zero polling). Filters events to indexable files, mirroring `scan_disk`'s logic.
2. **`_ReindexDebouncer`** — collapses a burst of events into a single reindex after 3s of
   silence, preventing concurrent `run_incremental()` calls from racing the SQLite lock.
3. **`_reload_indexes`** — **build-then-swap**: construct new objects outside the lock (slow I/O
   doesn't block in-flight tool calls), then acquire the lock only for the reference swap. No
   downtime.

### Architectural tension worth knowing

There are effectively **two retrieval implementations.** `hybrid_retriever.py` (§4) is the
clean, tested pipeline — but only `investigate_architecture` uses it. The other search tools
reach directly into `t1_index`/`doc_store` and re-implement RRF and import-filtering inline. This
is historical: those tools predate the `hybrid_retriever` extraction, so newer graph/resolution
work hasn't reached them.

### Open levers

- **Unify the tools onto `hybrid_retriever`** — the highest-leverage refactor here; back-ports
  resolved-edge traversal + corroboration to all tools and deletes duplicated inline RRF/regex.
- **`analyze_blast_radius`'s regex import detection** could become `db.get_importers_resolved()`.
- **No per-tool smoke tests** — the tools are only exercised end-to-end.

---

## 6. Graph analytics: community detection, god-objects, the DSM

The `map_module_communities` engine — the one place that analyzes the **shape of the whole
graph**. Three files (ADR-006), inspired by Graphify but independently reimplemented over this
engine's own extracted edges.

### Clean layering

`graph_analytics.py` imports **only `networkx`** — never FAISS, the embedder, or the MCP server.
Same for the report/viz layers. This keeps analytics unit-testable in isolation and portable to
a future Rust engine. It's the architectural style the rest of the codebase aspires to.

### The engine (`graph_analytics.py`)

`analyze()` runs a pipeline over `db.get_graph_edges()`:

1. **Build a weighted graph.** Edge weights: `CALLS`=1.0 (strongest), `EXTENDS`/`IMPLEMENTS`=0.8,
   down to `OWNS`=0.4 and `*_CONTEXT`=0.2 (excluded from coupling). The code documents an
   **ADR-006 deviation**: the draft assumed lowercase kinds + a `contains` edge; the real
   `db.py` vocabulary is uppercase with `INSTANTIATES`/`*_CONTEXT` — the code maps to reality.
2. **Detect communities.** Louvain on the weighted undirected projection, **seeded**
   (`GRAPH_SEED = 20260618`) for byte-stable reports, then an A1 refinement pass that splits
   low-density communities.
3. **Centrality.** Betweenness computed **unweighted** — the edge weights are *similarities, not
   distances*, so feeding them to a shortest-path betweenness would invert the meaning. Above
   5,000 nodes it uses k-sampled approximate betweenness.
4. **God-object detection** — the centerpiece. A symbol's owned methods must **sprawl across ≥3
   distinct coupling communities**. Span is measured on a **separate coupling-only subgraph**,
   not the all-edge clustering, so a class's own `OWNS` edges can't cluster its methods together
   and let it **self-mask its span**. Ranking = `0.5·betweenness + 0.3·fan_in + 0.2·span`;
   betweenness/fan-in only *rank*, they don't *gate*.
5. **Optional split suggestions**, each stamped `HEURISTIC — unverified`.

### The renderers

- **`graph_report.py`** — Markdown output. Community labels filled here (heuristic, no LLM,
  deterministic), keeping the engine UI-agnostic. Every report leads with an
  `EXPLORATORY_DISCLAIMER` ("not an accuracy claim") and closes with an `AUDIT_NOTE` ("only
  EXTRACTED edges — no INFERRED or LLM-derived edges participated").
- **`graph_viz.py`** — the **Design Structure Matrix**: an N×N adjacency matrix, community-banded.
  God-objects = dense full row/column; healthy communities = bright diagonal blocks;
  cross-community coupling = off-diagonal cells. Constraints: **one self-contained static file, no
  web server, no runtime CDN** (a tiny inlined vanilla-JS canvas renderer). Above 1,500 nodes it
  aggregates to community×community. Writes `.code-index/architecture_matrix.html`.

### Open levers

- **ADR-015 / S5 — interactive web explorer** (the DSM is explicitly its single-file precursor).
- **Edge-confidence gating** — `detect_communities` is designed to gate on a confidence floor once
  ADR-008 produces per-edge confidence.
- **Leiden backend** (optional `[leiden]` extra) — higher quality than Louvain, falls back
  gracefully.

---

## 7. The adapter architecture: how languages plug in

The breadth axis, and the current active work area. The contract: **shared infrastructure never
changes when you add a language** (ADR-003, ADR-017).

### The contract: `LanguageAdapter` Protocol

A `@runtime_checkable Protocol` (duck-typed, not inheritance) with four methods: `parse`,
`analyze_tags`, `test_conventions`, `project_resolver`. The registry (`__init__.py`) is a flat
extension→instance dict; `get_adapter(ext)` is the only entry point. Adding a language is local:
write the adapter, register extensions, add conformance fixtures. **"A passing conformance suite
is the definition of 'supported language.'"**

### The reference adapter: `PythonAdapter`

Compiles the tree-sitter Python grammar once, uses declarative `.scm` queries for imports/calls,
and a recursive `walk()` emitting `Symbol`s and `Edge`s (`extends` from superclass lists, `call`
from call sites, `owns` from class→method). Careful correctness choices (skipping `metaclass=`
kwargs, reducing `base.Mixin` to `Mixin`) are exactly what the conformance harness pins.

### Shared toolbox: `_treesitter.py`

`node_text` (byte-slice → UTF-8, error-tolerant), `run_query` (tree-sitter 0.25+ QueryCursor,
returns `[]` on any failure — graceful degradation), and `skeletonize` — renders a class with
method bodies replaced by `...` stubs so a class embedding captures its interface, not a wall of
implementation.

### `project_resolver`: the C#/C++ project layer

Python returns `None`; C#/C++ have a project layer above source files. `CsprojResolver` parses
`.csproj/.sln` for reference edges; `CppProjectResolver` parses `compile_commands.json` for
include paths. This is why `incremental_indexer` has a separate `ingest_project_file` path (edges
only) and routes those files by *filename*, not extension.

### The tiering model (ADR-017)

| Tier | What | Guarantees | Cost |
|------|------|------------|------|
| **A** | Hand-written adapter (Py, TS/JS, C#, C++) | full FQNs, resolved edges, golden suite | high |
| **B** | one `GenericTreeSitterAdapter` driven by each grammar's `tags.scm` | **symbols only; every edge `candidate=True`** | low |
| **C** | text fallback (`fallback_token_chunker`) | chunks, no graph | zero |

The honesty mechanism is `Edge.candidate`: **every Tier-B edge is `candidate=True` by
construction** because `tags.scm` gives name-based references with no resolution. Same trust-flag
idea as `corroborated`. Defaults `False`, so Tier-A is unaffected. A defined **B→A promotion
path** exists (§9).

### The `L5xAdapter` stub

44 lines that only `raise NotImplementedError`, but registered for `.L5X` from day one. Its
purpose: L5X (Rockwell PLC XML) has **no tree-sitter grammar**, so the stub proves the Protocol
contains no tree-sitter assumption — a future implementer "cannot accidentally reach for
tree-sitter out of convention." A deliberately-broken stub defending an abstraction boundary.

### Open levers

- **Finish ADR-008 extraction gaps** (the current branch): C++ template/namespace, C#
  partial-class edges.
- **ADR-017 Tier-B is designed but not built** (`GenericTreeSitterAdapter` + `registry.yaml` +
  grammar-pinning manifest) — the breadth expansion lever.
- **ADR-011 — high-precision call resolution** feeds the Tier-A promotion path.
- **ADR-013 — domain-specific industrial adapters** (where L5X eventually gets designed).
- **C# `interface_impl_gap`** — a documented Tier-A limitation (first-base heuristic mislabels a
  second interface as `extends`); a real target for type resolution.

---

## 8. Conformance & extraction accuracy (ADR-008)

The measurement layer that turns "provably accurate" into a number — the "1.00 / 1.00" README
table. Tooling: `tools/conformance_eval.py` + fixtures under `tests/fixtures/conformance/`.

### Two harnesses, don't confuse them

- **Snapshot tests** (`test_adapter_snapshots.py`) guard against **drift** (today == golden).
  Catches regressions, proves nothing about correctness.
- **Conformance** (ADR-008) measures **correctness** against independently hand-authored ground
  truth of what a correct extractor *should* produce.

### The metric

For `symbols`, `edges_all`, and the headline `edges_call`:

```
precision = correct emitted ÷ all emitted    (are the edges we assert real?)
recall    = correct emitted ÷ ground truth   (did we find the edges that exist?)
```

`edges_call` is broken out because call-edge P/R is "the number the depth-over-breadth thesis
stands on."

### The rule that makes it honest

**`expected.json` is authored from source semantics — never by copying parser output.** Echoing
the parser guarantees a meaningless 1.0. A fixture that legitimately scores below 1.0 is a real,
honest signal of an adapter gap. This is the entire game.

### The subtle heart: `normalize_fqn` + collision invariant

Fixtures strip the checkout-specific path prefix to stay portable, which is a minefield:

- **C# `/arity`** — member FQNs are `Namespace.Type.Member/2` where `/2` is overload arity. Naive
  `basename` splits on `/` and collapses every method to its arity digit, masking recall misses.
- **C++ `::`** — a *namespace* separator, not a path separator. Naive stripping collapses the
  namespace off every FQN.

So `normalize_fqn` only treats `::`/`/` as a path delimiter when the left side actually looks
like a filesystem path. And a **key-distinctness invariant** flags any case where two distinct
raw keys normalize to the same key — because a collision doesn't just undercount, it *masks a
recall miss behind a surviving sibling*. The harness distrusts its own arithmetic.

### Known gaps: measuring failure without hiding it

`validate_known_gap` lets a fixture encode the **correct** ground truth (so the score legitimately
drops below 1.0) plus a `known_gap` block that **must** carry a `reason` and a `ref` to the
documenting ADR/section, or the harness raises. The ruler is calibrated to catch real
limitations and then honestly reports catching them.

### CLI & CI

- `--write-baseline` → freeze numbers to `benchmarks/conformance/baseline.json`
- `--check-baseline` → regression gate (exit 1 on drop) — what CI runs
- `--write-readme` → regenerate the README table between the `CONFORMANCE:START/END` markers

Runs in ~5s with **no GPU** (extraction only), so it's an always-on gate.

### The README caveat is the ethos

> "a row is 'precision/recall on the fixtures we wrote,' never a language's true precision."

A measured claim with its scope stapled to it.

### Open levers

- **Grow the fixture corpus** (parallelized by the `conformance-fixture-author` subagent).
- **ADR-008 §4/§5 blocked** on ADR-003/017 (edge-confidence, deeper tiers).
- **Close documented known-gaps** by fixing the adapter, not the ground truth.
- **Wire JavaScript into the baseline** (it's in the extension map but not the README table).

---

## 9. Retrieval evaluation & the benchmark harness

The heavier measurement system — the retrieval-quality counterpart to §8. Three ADRs converge:
**ADR-007** (CoIR), **ADR-019** (real-repo eval), **ADR-009** (the decisions both adjudicate).

### Two harnesses

- **`coir_eval.py` (ADR-007)** — indexes CoIR's *own public corpus* and grades against CoIR
  qrels. The standardized, comparable external benchmark (Wave-0 baseline). Never touches the
  repo's `.code-index` (would score ~0).
- **`real_repo_eval.py` (ADR-019)** — drives the **real `HybridRetriever`** against actual
  indexed repos, grading against hand-authored gold fixtures.

Both share metric/CI/baseline code (`eval_common.py`) so metrics mean the same thing in both.

### Ablation arms isolate each decision

| Arm | graph | rerank | fusion | = production config |
|-----|-------|--------|--------|---------------------|
| A | off | off | rrf | semantic only |
| B | on | off | rrf | **today's default** |
| C | on | on | rrf | `[reranker].enabled=true` |
| D | on | off | convex | `fusion_mode="convex"` |

Three **paired lifts** (same queries per arm, so noise cancels; each mean carries a 95% CI):

- **graph = B − A** (what Traverse earns; CoIR structurally can't measure it)
- **reranker = C − B** (drives `[reranker].enabled`)
- **sparse = D − B** (drives `fusion_mode`)

The arms *are* the production pipeline with toggles flipped — the `HybridRetriever` constructor
exposes `graph_enabled` / `reranker_enabled` / `fusion_mode` specifically so the eval can flip
them without forking the code.

### Engineering for long runs

Sharded resumable embeddings (`coir_eval`, 20k-doc shards survive an interrupted 11h run),
flushed per-query progress logs (`real_repo_eval`, `tail -f`-able), and append-only, deduped,
**git-SHA-stamped** baselines so every number is traceable to the commit that produced it.

### The private slice (ADR-019 §6)

A git-ignored `repos.private.toml`, merged when present, lets private/licensed eval repos extend
coverage while the public baseline stays reproducible. This is what de-noises clean-TS results.

### The cloud runbook

`cloud/` is a **self-deleting spot-GPU eval pipeline**. The reranker arm is the bottleneck (~2-3h
CPU, minutes on a T4). `launch_eval.ps1` + `startup.sh` launch a spot T4 that pulls a bundle from
GCS, runs the eval, uploads results, and **deletes itself**. The one failure mode (an orphaned
GPU VM) is designed out three ways: self-delete on completion, spot-preemption DELETES, and a hard
`--max-run-duration=10800s` backstop — plus a $10 budget alert. The runbook tells you to grep the
serial console for `device=cuda` to prove the GPU is actually doing the reranking (a silent CPU
fallback would invalidate the run).

### `benchmarks/` — recorded evidence

The append-only output of everything above (`baseline.jsonl`, `real_repo/*.jsonl` including
`real_repo_authoritative.jsonl`, plus investigation `.log` files). Lets a decision be re-examined
without re-running the job.

### Current verdicts

- **embedder → bge-code-v1 validated & promoted**
- **reranker → off** (neutral/negative lift; the public win was partly a TS contamination
  artifact; the Python lift is real)
- **graph → inert under RRF** (B−A ≈ 0; the B−A gate is formally dropped, graph recorded as
  rerank-only, deferred to ADR-022)
- **convex/sparse → rejected** (negative in all 5 languages)

### Open levers

- **ADR-022 — graph-neighbor retrieval scoring** (make B−A positive).
- **Reranker verdict parked, not closed** (per-language reranking + larger private slice).
- **Wire `real_repo_tripwire.py` into CI** (fast smoke gate alongside the conformance gate).

---

## 10. The governance layer: 22 ADRs and `docs/`

The decision-recording system that makes every non-obvious choice legible. ADR-001's purpose.

### The ADR

A numbered, dated markdown file: **Status / Date / Branch / Reviewer / Depends on / Depended on
by**, then **Context → Decision → Consequences → Alternatives Considered → Implementation Log**.
Two features make it more than boilerplate:

- **Consequences are three-way** (`Better / Worse / Neutral`) — you must write down what gets
  worse.
- **The Implementation Log is a living diary** — records deviations and pivots during
  development, which is why the ADRs are trustworthy about reality (e.g. the graph_analytics
  "ADR-006 deviation" that the code and ADR both admit).

### The status line carries the verdict

Statuses aren't rubber stamps — they're verdicts with evidence. ADR-009's status records the
exact reranker eval (n=148, which clauses passed, that the public win was a zustand TS outlier).
The status line *is* the paper trail from the eval harness (§9) to the toggle defaults (§4).

### The cross-reference standard (ADR-001, 2026-06-18 amendment)

**Every dependency link must be recorded in *both* ADRs, forward and back, in the same PR.** A
one-directional link is a defect. Downstream "Depends on" names the exact artifact it waits for;
upstream "Depended on by" names each consumer and must resolve those obligations before
`accepted` — closing the loop when context is freshest.

### The 22 ADRs as a map

| Status | ADRs | What |
|--------|------|------|
| **accepted/merged** | 001, 002, 003, 006, 007, 009, 019, 021 | governance, hardening, adapters, graph analytics, both eval harnesses, call resolution — everything in §§2-9 |
| **proposed** | 004, 005, 008, 010-018, 022 | CI observability, self-healing, conformance-completion, call-precision, cross-repo graph, industrial adapters, adaptive ranking, web explorer, tiered languages, graph-scoring |

The accepted set *is* the working engine; proposed is the roadmap. ADR-008 is still `proposed`
even though its harness is built, because the ADR includes edge-confidence/verdict-gating that
isn't done. Note a **gap at ADR-020** (019 → 021) — no file present; worth reconciling.

### `docs/adr-backlog.md` and the research corpus

The backlog is the planning index that grouped scattered ideas into "one bucket = one coherent
decision" and assigned numbers. `docs/` also holds the thinking the ADRs distilled:
positioning (`prior-art-depth-over-breadth`, `design-research-informed-improvements`),
competitive analysis (`study-codebase-memory-mcp`), technical deep-dives
(`merkle-tree-drift-handling`, `modernization-stack-review`,
`conformance-fixture-conventions`), and agent tooling
(`agent-prompt-code-intelligence-architect`). `.github/` completes it: `CONTRIBUTING.md`
(ADR-001's single source of truth) + issue/PR templates + `workflows/ci.yml`.

### Open levers

- **Reconcile the ADR-020 gap.**
- **Promote ADR-008 toward `accepted`** (edge-confidence + verdict-gating).
- **Refresh the backlog** (predates 016-022).
- **CI-enforce the cross-reference invariant** (lint for missing reciprocal links).

---

## 11. The supporting cast & generated artifacts

### `indexer.toml` + `config.py`

The one file a *user* edits. `config.py` walks up from cwd to find it, returns `{}` if absent.
Four sections: `[indexer]` (paths, risk rules), `[embeddings]` (the swap surface — bge-code-v1
default with full inline justification + revert instructions), `[reranker]` (off default), and
`[retrieval]` (fusion mode + weights). The comments encode the hard constraint: changing the
embedder = update `dimension` + delete `.code-index` + rebuild. Known drift: no `[summarizer]`
section — the summarizer still hardcodes its model id.

### `summarizer.py`

Optional LLM embedding augmentation. At index time an instruct-tuned local LLM (Qwen2.5-Coder,
1.5B default) generates a structured extraction that's **appended** (not prepended — keeps code's
lexical tokens at the front so exact-match recall is unaffected). "Strangle the prompt":
extraction over synthesis (list facts you can see, don't infer) cuts hallucinations;
`temperature=0`. Summaries are cached in `chunk_summaries` by `MD5(chunk_text)`, survive renames.
Runs in an isolated worker process (which is why `core.py`'s embed model is a lazy singleton).

### `category_tagger.py`

Coarse keyword classifier (`[CAT_AUTH]`, `[CAT_PERSIST]`, `[CAT_NETWORK]`, `[CAT_PARSE]`, …).
`tag_symbol` at index time, `classify_query` at retrieval time. Output is the `_CATEGORY_BOOST`
nudge in the reranker. Deliberately a cheap deterministic prior, not a model.

### `import_resolver.py`

Populates `Edge.resolved_target` for imports: tsconfig path aliases, relative imports, barrel
files, extension inference. Non-repo specifiers return `None` (fall back to raw string). Without
it the import graph would store literal strings instead of topology, and corroboration (§4) would
have nothing precise to match.

### `tui/` — standalone terminal UI (928 lines)

A **Textual** app that's a separate human-facing front-end to the same engine (an alternative to
MCP). `tools.py` (catalog), `backend.py` (bridge), `app.py` (UI, with colored score bars).
Launch with `python -m tui`. Same engine, two faces: human-via-TUI, AI-via-MCP.

### `RecFileSearch.py` — the odd one out

55 lines that find the largest file on a drive. Not imported by anything, has its own `__main__`,
scans whole drives. Doesn't fit the architecture — a candidate to move to `tools/` or delete.

### The generated `.code-index/`

Everything the engine produces at runtime (git-ignored): the three `.faiss` indexes, `graph.db`
(WAL, with `-wal`/`-shm` sidecars), and `architecture_matrix.html` (the DSM). The sibling
`.code-index.jina-bak/` is the backup of the prior Jina index (physical residue of the embedder
swap); `cloud/last-vm.txt` is residue of the last GPU eval run. These untracked files are
generated evidence, not source.

---

## 12. The through-line

The architecture is best understood as **one honest engine wrapped in two proof systems and a
decision log:**

- **The engine** (§§2-7): source → adapters → chunks+graph → FAISS+SQLite → RTR → 11 MCP tools.
  A hybrid of fuzzy semantic recall and exact structural precision, with every risky feature
  defaulting to *off*.
- **Proof system #1 — extraction** (§8): the conformance scorecard that makes "we extract
  accurately" a measured, honestly-scoped number.
- **Proof system #2 — retrieval** (§9): the CoIR + real-repo ablation harness (and its
  self-deleting GPU) that adjudicates every on/off toggle with paired-lift CIs.
- **The decision log** (§10): 22 cross-linked ADRs whose status lines carry the verdicts and
  their receipts.

The recurring disposition — visible in the 60-bit ID guard, the `corroborated`/`candidate` trust
flags, the L5X defensive stub, the "author ground truth from semantics not parser output" rule,
the ablation toggles, and the ADR status lines that admit a win was a contamination artifact —
is that **the project systematically distrusts its own cleverness and refuses to overclaim.**
For a tool whose purpose is making a codebase legible and trustworthy to an AI, that discipline
isn't incidental — it *is* the product.

### The biggest open levers

1. **ADR-022 — make the graph pay** (B−A ≈ 0 today; call resolution landed, scoring is next).
2. **Unify the MCP tools onto `hybrid_retriever`** (back-ports resolved edges + corroboration).
3. **Build ADR-017 Tier-B** (`GenericTreeSitterAdapter` — the breadth play, honestly labeled).
4. **Close ADR-008 to `accepted`** (edge-confidence + verdict-gating on the shipped scorecard).
5. **Housekeeping**: the `IGNORE_DIRS` venv gap, the summarizer config-migration, the ADR-020
   numbering gap, and `RecFileSearch.py`'s home.
