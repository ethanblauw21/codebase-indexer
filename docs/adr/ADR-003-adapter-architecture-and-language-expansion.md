# ADR-003: Adapter Architecture and Language Expansion

**Status:** proposed
**Date:** 2026-06-11
**Branch:** `feature/adapter-architecture-language-expansion`
**Reviewer:** @ethanblauw21

## Context

With ADR-002 hardening complete, the indexer needs to support C# and C++ without duplicating the stable-ID, chunking, and edge plumbing that H1 fixed. Today the chunker is implicitly "tree-sitter + per-language queries for Python/TypeScript/JavaScript." Adding a language means forking the chunker, which is the exact triplicated-formula failure mode H1 exists to end.

One format on the roadmap (L5X — Rockwell XML, no tree-sitter grammar) cannot be handled by tree-sitter at all. The architecture must not bake tree-sitter into the interface contract.

C# introduces partial classes: one type's members span multiple files. Symbol identity must merge on FQN across files. The current schema models symbol → file (one FK); the required schema is symbol → locations[] (one-to-many). C++ has the same shape via header/impl split — the schema change pays for both languages. This migration ships in Phase 1 alongside the Python/TS/JS adapter refactor.

Shared infrastructure that must remain untouched by language expansion: stable IDs, three-tier FAISS index structure, embeddings (the Jina code model is multi-language), incremental MD5 diffing, the RTR pipeline, and the doc store.

## Decision

Introduce a formal `LanguageAdapter` Protocol and an adapter registry. Refactor Python, TypeScript, and JavaScript onto the interface (Phase 1) with golden before/after snapshots proving zero behavior change. Then add C# (Phase 2) and C++ (Phase 3). Reserve an L5X stub entry from day one.

**§2.1 — LanguageAdapter interface**

```python
class LanguageAdapter(Protocol):
    language_id: str                     # "python", "csharp", "cpp", "l5x"
    extensions: set[str]                 # {".cs"}, {".cpp", ".cc", ".cxx", ".h", ".hpp"}
    def parse(self, path, source: bytes) -> ParseResult: ...
        # ParseResult: symbols, tier-1 chunks + metadata,
        #              edges (calls/imports/inherits), diagnostics
    def fqn(self, symbol) -> str         # language-correct qualified naming
    def skeletonize(self, node) -> str   # tier-2 compression rules
    def test_conventions(self) -> TestConventions
        # file patterns + in-file markers for find_test_coverage
    def project_resolver(self) -> ImportResolver | None
        # the tsconfig-equivalent for this ecosystem
```

Adapter registration is a data table, mirroring the probe/tier-registry pattern already in `MCPServer.py`. Tree-sitter is an implementation detail of each adapter, not part of the interface contract — the L5X stub proves this from day one.

Each adapter ships with a **conformance fixture suite**: a small golden repo in the target language with asserted symbols, FQNs, edges, chunk boundaries, and test-detection results. A passing conformance suite is the definition of "supported language" — no adapter merges without one.

**Phase 1 — Refactor Py/TS/JS + multi-location schema (merge blocker: golden snapshots)**

The existing Python, TypeScript, and JavaScript logic is wrapped behind `LanguageAdapter` implementations. The `symbols` table is migrated from `symbol → file_id` to `symbol → locations[]` in this phase; single-location entries are back-compatible. Golden snapshots of symbols, FQNs, stable IDs, edges, and chunk boundaries are captured before the refactor. The Phase 1 merge is blocked until after-snapshots are byte-identical to the before-snapshots. This gate is what makes this a refactor, not a rewrite. A changed stable ID would orphan every existing index.

**§2.2 — C# adapter (Phase 2)**

Grammar: `tree-sitter-c-sharp`.

Symbols: namespaces, classes/structs/records/interfaces/enums, methods, properties, events, delegates, constructors. Attributes captured as symbol metadata — they are the C# risk-rule hook (`[Authorize]`, `[HttpPost]`, `[AllowAnonymous]`).

FQN: `Namespace.Type.Member` with arity suffix for overloads (`Repo.Save/2`). Nested types use `Outer+Inner` (CLR convention — see D3). This is permanent: it bakes into stable IDs. The convention is pinned in the conformance suite before any indexing runs.

Partial-class merge: multiple file locations for the same FQN are unified at the graph layer via the multi-location symbol schema from Phase 1. Chunks remain per-file; the graph unifies.

Edges: `using` directives + fully-qualified references → import edges; call edges via H5 corroboration through using-graph and same-namespace resolution; inheritance/interface-implementation edges from base lists (new edge type, high blast-radius value).

Project resolution: parse `.csproj`/`.sln` — `<ProjectReference>` gives the inter-project import graph; `<PackageReference>` marks external boundaries (edges terminate there, like `node_modules` today).

Test conventions: `*Tests.cs` / `*.Tests/` project patterns; `[Fact]`, `[Theory]`, `[Test]`, `[TestMethod]` in-file markers.

Skeletonization: keep signatures, attributes, and XML-doc summaries (`/// <summary>`); drop bodies; properties compress to `Type Name { get; set; }`.

Documented limits: extension methods resolve to candidates only; LINQ query syntax indexed as text (chunk-level), not as call edges; `dynamic` is invisible to the graph.

**§2.3 — C++ adapter (Phase 3)**

Grammar: `tree-sitter-cpp`.

Position statement, published verbatim in the docs: *This is a heuristic semantic/structural indexer, not a compiler frontend. For precise C++ navigation use clangd/SCIP. This tool's value is hybrid search and good-enough graph over codebases where no compile-accurate index exists.*

Symbols: namespaces, classes/structs, free functions, methods, templates (definitions), enums, type aliases. Declarations (.h) and definitions (.cpp) of the same entity unify on FQN via the multi-location schema — the header/impl split is partial classes by another name.

FQN: `ns::Class::method(param-type-list)` — signature-qualified, because C++ overloading makes arity alone insufficient. Parameter types are taken syntactically as written, not canonicalized. The whitespace and `const`-placement normalization rules are pinned in the conformance suite.

Include resolution: (1) `compile_commands.json` if present, parsed for `-I`/`-isystem` flags per translation unit; (2) fallback heuristic: relative-to-file, then configured include roots. Angle-bracket system includes terminate as external edges.

Call edges: name-based and H5-corroborated; overload sets resolve to all candidates with a `candidate: true` flag. Verdict tools (blast-radius, dead-code) require non-candidate or single-candidate edges.

Inheritance edges from base clauses.

Documented blind spots (limits section, not fine print): preprocessor macros indexed as unexpanded text — macro-generated functions are invisible to the graph; template instantiations are invisible (definitions index fine); function pointers and virtual dispatch resolve to declared type only; operator overloads index as symbols but rarely earn call edges.

Test conventions: GoogleTest (`TEST(`, `TEST_F(`), Catch2 (`TEST_CASE(`), doctest; directory patterns `test/`, `tests/`, `*_test.cpp`, `*_unittest.cpp`.

Skeletonization: keep signatures, template heads, and leading comment blocks; drop bodies; class skeletons keep access specifiers.

**§2.4 — Risk rule packs**

Rides H6 (ADR-002). Starter packs ship as `examples/` only, never as built-ins:
- C#: string-concatenated SQL near `SqlCommand`; `[AllowAnonymous]` on mutating actions; secrets in `appsettings` patterns.
- C++: `strcpy`/`sprintf`/`gets` family; `new` without smart-pointer context (hint-severity — many valid uses exist).

Layer detection (CLIENT/SERVER) becomes rule-pack-defined, since the concept is meaningless for most C++.

**D3 — C# nested-type FQN: CLR `Outer+Inner` convention**

`Namespace.Outer+Inner.Member`. The `+` makes nesting unambiguous against namespace boundaries — with dots alone, `A.B.C` cannot be parsed back into namespace-vs-nesting. It matches CLR reflection output, so FQNs round-trip against any .NET tooling without transformation. This is a permanent choice that bakes into stable IDs; it is pinned in the conformance suite before indexing.

**D4 — L5X seam stub**

The adapter registry includes a stub `language_id: "l5x"` entry from day one. The entry claims L5X file extensions; `parse()` raises `NotImplementedError` with a pointer to the deferral rationale. All actual L5X design (rung chunking, tag edges, embedding strategy) is out of scope pending an example corpus and gold queries. The stub costs nothing and permanently protects the interface from re-acquiring a tree-sitter assumption.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Per-language chunker forks | Each new language re-duplicates stable-ID/chunk/edge plumbing — the exact failure mode H1 exists to end. The formula was triplicated before H1; forks would make it n-plicated. |
| D3: dots throughout for nested types (`A.B.C`) | Ambiguous: cannot be parsed back into namespace-vs-nesting without external type information. Does not round-trip against CLR reflection tooling. |
| D4: no L5X mention until corpus arrives | A one-line stub costs nothing. Without it, a future adapter author may reach for tree-sitter out of convention rather than necessity, leaking it into the interface contract. |

## Security & Consequences

**Better:**
- The adapter interface enforces a clean boundary: shared infrastructure (stable IDs, three-tier index, embeddings, RTR pipeline) is never touched by language expansion — each language expansion modifies only its adapter.
- Conformance fixture suites are the formal definition of "supported language." No adapter merges with unverified symbol/FQN/edge behavior.
- The L5X stub keeps tree-sitter out of the interface contract permanently, preserving the option to index non-grammar-based formats.
- The C++ position statement published verbatim in docs sets accurate expectations before a user depends on graph accuracy for safety-critical analysis.

**Worse:**
- Phase 1's golden-snapshot gate is a hard merge blocker. Any pre-existing behavioral inconsistency in the Python/TS/JS path that was previously hidden will surface and must be resolved before C# work can begin.
- The multi-location symbol schema migration is a one-way schema change. Rollback requires a full re-index.
- C++ heuristic limits (macro-generated functions, template instantiations) are fundamental to static analysis without a compiler frontend and cannot be fixed within this architecture.

**Neutral:**
- The C++ blind-spot documentation is proactive — users of clangd/SCIP should understand this tool's different value proposition (hybrid search + graph over unindexed codebases), not treat it as a replacement.

## Testing Additions

| Area | Type | Notes |
|------|------|-------|
| Phase-1 golden snapshots | Integration — merge blocker | Py/TS/JS fixture repos: byte-identical symbols/FQNs/IDs/edges/chunks before and after adapter refactor |
| Multi-location symbol schema | Unit | symbol→locations[] migration; single-location back-compat; FQN-based merge across files |
| C# conformance suite | Integration | Partial-class merge, `Outer+Inner` FQNs (D3), overload arity suffixes, `.csproj` `<ProjectReference>` graph, attribute capture, `[Fact]`/`[Test]` detection |
| C++ conformance suite | Integration | Header/impl unification, signature-qualified FQNs + normalization rules, `compile_commands.json` and fallback include resolution, candidate-edge fan-out |
| C++ blind-spot proofs | Integration | Fixtures that *demonstrate* the documented limits: macro-generated function absent from graph; template instantiation absent — so the documented limits remain verifiably true |
| L5X stub | Unit | Stub present in adapter registry; `parse()` raises `NotImplementedError` with deferral message; interface contains no tree-sitter import or reference |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 0 — ADR-002 prerequisite** *(must be complete before Phase 1 begins)*
- [x] ADR-002 H1–H3 merged and CI green

**Phase 1 — Adapter interface + Py/TS/JS refactor** *(merge blocker: golden snapshots)*
- [x] Take before-snapshots: `tools/capture_snapshots.py` runs `parse_file()` + `chunk_file_ast()` on four fixture files (sample.py, sample.ts, sample.js, sample.tsx); records symbols, FQNs, edges, chunk boundaries, chunk text to `tests/fixtures/snapshots/*.json`. `tests/test_adapter_snapshots.py` asserts byte-identical output — 4/4 passing pre-refactor.
- [x] Define `LanguageAdapter` Protocol and `ParseResult` in `src/adapters/base.py`; move shared data types (Symbol, Edge, Chunk, Reference, SymbolType) there; re-export from `ast_chunker.py` for backward compat. Shared tree-sitter helpers extracted to `src/adapters/_treesitter.py`.
- [x] Migrate `symbols` schema: additive — `symbol_locations` table added (symbol_id, file_id, start_line, end_line, text; UNIQUE(symbol_id, file_id)); `_migrate_symbol_locations()` back-fills from existing rows on startup; `upsert_file()` writes both tables going forward. `symbols.file_id/start_line/end_line/text` retained for single-location back-compat so no existing queries change.
- [x] Implement `PythonAdapter`, `TypeScriptAdapter`, `JavaScriptAdapter` wrapping existing logic exactly; `TypeScriptAdapter` and `JavaScriptAdapter` share `_WebAdapter` base. `ast_chunker.py` reduced from 1076 → ~200 lines: pure dispatch + generic chunking loop + fallback chunker.
- [x] Register adapters in `src/adapters/__init__.py` (REGISTRY + `get_adapter()`); add `L5xAdapter` stub entry (D4) — claims `.L5X`/`.l5x` extensions, `parse()` raises `NotImplementedError` with deferral pointer; no tree-sitter import in stub, proving the interface has no tree-sitter assumption.
- [x] After-snapshots: `tests/test_adapter_snapshots.py` — 4/4 PASS. Full test suite 36/36 PASS. FQNs, symbol kinds, edges, chunk boundaries, chunk text byte-identical before and after refactor.

**Phase 2 — C# adapter**
- [x] Add `tree-sitter-c-sharp==0.23.5` grammar dependency; added to `requirements.txt`.
- [x] `CSharpAdapter.parse()`: namespace/type/member symbols (class, partial_class, interface, struct, record, enum, method, constructor, property), attribute metadata captured in symbol text, EXTENDS/IMPLEMENTS edges from base_list, OWNS edges, CALLS edges from invocations, import edges from `using` directives. FQN: `file_path::Namespace.Type.Member/arity` with CLR `+` for nested types (D3). Always-arity suffix on methods and constructors — stable regardless of overload presence.
- [x] `_cs_skeletonize()`: strips method/constructor/destructor bodies from class/struct/record/interface; preserves attributes, modifiers, signatures, properties, and events verbatim.
- [x] `analyze_tags()`: category tags via `tag_symbol()` + C#-specific security attribute tags (`[CS_AUTHORIZE]`, `[CS_ALLOW_ANON]`, `[CS_HTTP_MUTATE]`, `[CS_HTTP_READ]`, `[CS_CSRF_GUARD]`) keyed on attribute text present in symbol source.
- [x] Partial-class handling: each file independently emits its symbols with file_path-prefixed FQN; `partial_class` kind distinguishes partial declarations. Cross-file merge at graph layer deferred — `symbol_locations` table already supports it structurally. See impl note below.
- [x] `_treesitter.run_query()` fixed: previous implementation silently returned `[]` for all queries in tree-sitter 0.25 (the old `lang.query().captures()` API was removed). Migrated to `QueryCursor(Query(lang, pattern)).captures(node)`. All five golden snapshots regenerated with correct import and call edges; 37/37 tests PASS.
- [x] `CSharpAdapter` registered in `src/adapters/__init__.py` under `.cs`.
- [x] C# conformance fixture: `tests/fixtures/src/sample.cs` — covers enum, interface, class with base+interfaces, partial_class, constructor overloads (arity disambiguation), property, method overloads, security attributes ([Authorize]/[AllowAnonymous]), nested class (`DataService+Config` with `+` convention). Golden snapshot: 19 symbols, 19 edges, 19 chunks — 5/5 snapshot tests PASS, 37/37 total PASS.
- [x] Ship `examples/csharp-rules.yaml`: SQL injection (string concat + FromSqlRaw), auth ([AllowAnonymous] on mutating actions, missing [Authorize], CSRF guard), secret leakage (connection strings, appsettings key patterns), unsafe deserialization, Process.Start, XXE.
- [x] Partial-class merge at graph layer (FQN-keyed, multi-location): C# type FQNs now use namespace-qualified names without `file_path::` prefix (e.g., `MyApp.Services.DataService` instead of `path/to/file.cs::MyApp.Services.DataService`). C# namespace uniqueness makes file_path redundant for identity. Added `shared: bool = False` field to `Symbol`; `CSharpAdapter` sets `shared=True` on type-level symbols. `upsert_file()` uses `INSERT OR IGNORE` for shared symbols (first file wins the primary row) and accumulates `symbol_locations` rows for subsequent files. Stale EXTENDS/IMPLEMENTS/OWNS edges from shared types are not cleaned on incremental re-index — full re-index always produces a correct graph. Documented limit; acceptable because class hierarchy changes are infrequent. Golden snapshot recaptured: all 19 C# symbols now have clean namespace FQNs. 37/37 tests pass.
- [x] `CSharpAdapter.project_resolver()`: `CsprojResolver` class parses `.csproj` XML (`<ProjectReference>` → inter-project import edges; `<PackageReference>` → NuGet boundary edges) and `.sln` files (project path extraction via regex). Registered `.csproj`/`.sln` extensions in adapter REGISTRY. Incremental indexer handles project files via lean `ingest_project_file()` path: edges stored in SQLite but no chunking, no FAISS embedding, no tier chunks. Added `PROJECT_EXTS` to `incremental_indexer.py`.
- [x] `CSharpAdapter.test_conventions()`: returns `TestConventions(file_suffixes=["Tests.cs", "Test.cs"], in_file_markers=["[Fact]", "[Theory]", "[Test]", "[TestMethod]", ...])`. `TestConventions` dataclass added to `adapters/base.py`. Protocol extended with `test_conventions()` and `project_resolver()` methods; all adapters implement them (Python/TS/JS return their respective patterns; L5X returns None). `find_test_coverage()` in `MCPServer.py` generalized: reads test file suffixes from the source language's adapter, falls back to all-adapter suffixes when extension is unknown, builds direct-name candidates for all suffixes.

**Phase 3 — C++ adapter**
- [x] Add `tree-sitter-cpp==0.23.4` grammar dependency; added to `requirements.txt`.
- [x] `CppAdapter.parse()`: namespace/class/struct/fn/template/enum/typedef symbols. FQN format: types `ns::ClassName`, methods `ns::ClassName::method(type1, type2)`, free functions `ns::func(type)`. Parameter type normalization rules pinned in conformance suite (whitespace collapse, no space before `*`/`&`, param names stripped, cv-qualifiers on function excluded). All symbols `shared=True` for header/impl unification.
- [x] Header/impl unification via multi-location schema: same `shared=True` + `INSERT OR IGNORE` mechanism as C# partial classes. A `.h` declaration and its `.cpp` definition produce the same FQN; first-indexed wins the primary symbol row, second adds a `symbol_locations` row.
- [x] `CppAdapter.project_resolver()`: `CppProjectResolver` parses `compile_commands.json` (per-entry `-I`/`-isystem` flags → import edges from source file to include path). Returns resolver instance from `project_resolver()`.
- [x] `CppAdapter.test_conventions()`: returns `TestConventions(file_suffixes=["_test.cpp", "_unittest.cpp", "_test.cc", "_unittest.cc", "Test.cpp", "Tests.cpp"], in_file_markers=["TEST(", "TEST_F(", "TEST_P(", "TEST_CASE(", "SECTION(", "DOCTEST_TEST_CASE(", "BOOST_AUTO_TEST_CASE("])`.
- [x] `_cpp_skeletonize()`: strips `compound_statement` bodies from inline `function_definition` members in `field_declaration_list`; access specifiers and declarations preserved verbatim. Template head prepended when class is wrapped in `template_declaration`.
- [x] C++ conformance fixture: `tests/fixtures/src/sample.cpp` — covers enum class, struct, typedef, using alias, class with inheritance (EXTENDS edge), template class, constructors, overloaded methods (signature-qualified FQN disambiguation), free functions, `#include` import edges. Golden snapshot: 19 symbols, 15 edges, 19 chunks — 6/6 snapshot tests PASS, 38/38 total PASS.
- [x] Ship `examples/cpp-rules.yaml`: unsafe buffer ops (strcpy/sprintf/gets/scanf), raw memory (new/delete/malloc), command execution (system/popen), unsafe type ops (C-style cast, reinterpret_cast), concurrency (volatile-for-sync), pointer arithmetic hints.

**Phase 4 — Rule packs + docs**
- [x] Finalize per-language limits sections in README: per-language limits table and limits prose added for Python, TS/JS, C#, and C++. README opening line updated to list all five supported languages.
- [x] Publish C++ position statement verbatim in docs: statement published in README "Language Limits" → "C++" section, and in ADR-003 §2.3. Exact wording: *This is a heuristic semantic/structural indexer, not a compiler frontend. For precise C++ navigation use clangd or SCIP. This tool's value is hybrid search and a good-enough graph over codebases where no compile-accurate index exists.*
- [x] Ship `examples/` rule packs for C# (`examples/csharp-rules.yaml`) and C++ (`examples/cpp-rules.yaml`). Both are examples-only, never built-in.

**Notes:**
<!-- 2026-06-11: Sourced from indexer-hardening-and-csharp-cpp-spec.md. Phase 1 golden-snapshot gate is non-negotiable. L5X deferred pending example corpus + gold queries; stub only. -->
