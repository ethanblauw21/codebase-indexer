# Chunk-Shape Plan Review: B-026 and B-027

> **This review is advisory.** The plan (`docs/backlog.md`, B-026 and B-027, on branch
> `docs/backlog-chunk-shape` at 08b3eea) is **unchanged**, pending @edb's decisions.
> Five reviewers read it: the Architect (Opus), the Maverick, the Grounder and the Critic (Sonnet),
> and the Synthesis Guard (Opus). All were read-only. Date: 2026-09-25.
> 
> **Applied, 2026-09-25.** @edb accepted the findings; a second outside opinion agreed and found
> no conflicts. B-026 and B-027 were revised in `docs/backlog.md` and three standalone defects were
> filed: B-028 (ghost vectors, do first), B-029 (`chunker_version`) and B-030 (the MCP budget loop).
> This review describes the plan as it stood at 08b3eea.

## Executive synthesis

1. **A live data bug that none of the plan saw: ghost vectors.** When two symbols share an FQN,
   ingest adds a vector for each, but the database keeps only the last one's text. The orchestrator
   measured this on the built `store` indexes: tier-1 FAISS holds more vectors than there are chunk
   rows, **+1 on p-queue, +92 on zustand, +36 on click**. The summary index has the same surplus
   (p-queue 137 vectors against 136 rows, zustand 464 against 372). Those vectors resolve to another
   symbol's text, and they were present in every ADR-030 measurement.
   - The fix (dedupe `(tier, id)` before `add_with_ids`) is independent of everything else. It is
     the obvious first change.
   - The Synthesis Guard found this. The orchestrator confirmed it with a vector-count against
     row-count check.
2. **The Stage 1 pass gate cannot decide anything as written.** All five reviewers converge here:
   - "Beyond noise" is undefined.
   - "Report the largest skeleton part" has no threshold.
   - Five changes are bundled into one number, so a result cannot be attributed to any of them.
   - The file "whole" metric was dropped from the criteria.
   - The fix: a per-query gate on the named queries, plus a bound on how many queries flip; an
     ablation per change; and every criterion given a threshold.
3. **The diagnosis is partly wrong.** `pq-concurrency` has **no JSDoc**. It loses because of the
   getter/setter collision, not because of skeleton bloat. The three losing queries have at least
   two different mechanisms, so "JSDoc is the cause" has to be proven per query, with a cheap
   hand-edited probe on the CPU, before building.
4. **Existing indexes never see the fix.** Incremental re-indexing keys only on the file's
   `content_hash`, and there is no chunker version. Four of five reviewers flagged this.
   - The fix: a `chunker_version` in `index_meta` that **warns** and asks for an explicit rebuild.
   - It must not rebuild automatically. An automatic rebuild at MCP startup can hang the first
     search, or leave a half-built index marked as clean.
5. **Narrow the merge and drop `parent_id`.** Merge only contiguous siblings: accessor pairs and
   overload sets, which are adjacent (click's 36 `@overload`s are confirmed). zustand's 92
   collisions are *unrelated* test helpers, so they stay separate and only lose their ghost vectors.
   The parent should come from the `class_context` the parser already computes, stored as text if
   at all, and never as a stable id. Order the work as Stage 1 → B-027 → any change to the schema
   or `retrieve()`.

**New defects found along the way, with nothing yet filed:**
- **Ghost vectors** (item 1).
- **`#` methods would look dead.** `_CALL_QUERY` (`ts_adapter.py:59-65`) has no
  `private_property_identifier`, so once `#` members are indexed they would have no callers, and
  `find_dead_code` would call every one of them dead.
- **The MCP result budget stops at the first chunk that doesn't fit.** The loop in
  `MCPServer.py:100-111` uses `break`, so one oversized chunk hides everything ranked below it.
- **Docs could be orphaned.** Documented plain fields, and other members that are never extracted
  as symbols, would lose their doc if the doc-move rule is applied blindly.

Verified by the orchestrator in code: `_CALL_QUERY`, the `break`, `add_with_ids` without a dedupe,
and the vector and row counts. The Grounder's other claims were not re-run (see the process note in
its section).

## Go / No-Go gate (Synthesis Guard): CONDITIONAL GO

See the Synthesis Guard's section below for the full gate. In short:
- **Before either ADR is written:**
  - pin ADR-030 at e1e9491, or merge it;
  - a per-query p-queue baseline;
  - a mechanism for each losing query;
  - a numeric noise rule;
  - no report-only criteria;
  - scope set to TS and Python;
  - the merge, doc-move and parent rules written down.
- **Stage 1 ships only with:**
  - the `chunker_version` warning;
  - the id dedupe (`ntotal == rows`);
  - call edges for `#` members;
  - the fixture list;
  - the ablation arms;
  - an MCP Inspector run that includes a `#` FQN.

## Recommended revisions (synthesized)

1. **Split out the ghost-vector dedupe as its own small fix, and do it first.** Every later
   measurement depends on it, including a re-check of ADR-030's numbers.
2. **Re-scope B-026 Stage 1 to TS and Python.** C# and C++ get fixtures only, with their separate
   skeletonizers named as out of scope. Re-size it from M to L (the Maverick), or split it into
   separately measured arms.
3. **Rewrite the merge policy:** contiguous siblings only, with the implementation first; line range
   from min to max; a stated type precedence. Scattered collisions are not merged.
4. **Rewrite the doc-move rule:** a doc moves only when its member is emitted as a symbol; define
   how docs attach across decorators and blank lines; keep it in a single code path per adapter.
5. **Add `#` call and reference queries** to the same change.
6. **Add a `chunker_version`** with a warning and an explicit rebuild. Write the version only after
   a successful full build.
7. **Replace the pass criteria** with the per-query gate plus a flip bound, include file "whole",
   give the skeleton size a threshold, report the paired CI95 without gating on it, and add a
   code-list-only ablation arm per change.
8. **Stage 2:**
   - The parent comes from persisted `class_context` or `parent_fqn` text; there is no `parent_id`
     stable id.
   - Grouping is MCP-display-only until B-027's FQN decision.
   - Fix the budget loop (skip rather than stop; member before skeleton); give each skeleton part a
     body-only line range.
9. **B-027:** decide the FQN syntax for test names (spaces, quotes, dots), give free-floating
   comments a home, cost long-input summarization in GPU-minutes, and plan a version bump. The
   Maverick calls it two or three items; consider splitting it.
10. **Provenance:** name the harness (`gpu-crash-repro/*`, gitignored), gloss `store`, and log
    summary cache hits and misses per arm.

## Per-persona findings

## The Architect — structure, sequencing, alignment
### Constraints map
- **Identity is scope-hashed.** Vector id = md5(tier::file::scope). Any scope change forces a rebuild, and split symbols exist only as `<fqn>_part_N`. No vector or row ever has the bare `<fqn>` of a split symbol (`ast_chunker.py:258`).
- **One row per scope.** `UNIQUE(file_id, scope, tier)` forces one row per FQN per tier. That is why same-FQN symbols overwrite each other today, and any merge fix has to live inside this key.
- **The embedder reads 512 tokens.** It sees the first 512 tokens of each chunk: the header, then whatever text comes first. So whatever sits at the top of a chunk is what gets embedded.
- **Summaries cost GPU time.** Every text change invalidates its summary, because the cache is keyed on a hash of the chunk text. That is 16 to 67 GPU-minutes per full index, on a shared 8 GB laptop with scripted, gated runs.
- **Incremental re-indexing keys only on the file's `content_hash`** (`db.py:587`, `incremental_indexer.py:288`). There is no chunker or parser version, so a parser change never reaches unchanged files in an existing index. `schema_version` exists (`db.py:456`) but it is only a schema marker.
- **All measurement runs through the unmerged ADR-030 branch** (fusion k=60/0.5, a grader keyed on file and scope). The gold for the intent and file sets lives in a gitignored kit. The three repos are TS/TS/Python: no C#, no C++, and no arrow-field classes.
- **The grader matches gold by FQN suffix** and dedupes on (file, scope without _part_N). FQN spelling and rank order are both part of the grade.
- **Fixed rules:** a Stage 2 MCP change needs an MCP Inspector run. Every measurement must name its stack. A backlog item is not an ADR; the ADR is written on the build branch.

### Findings
- **[HIGH] Stage 2's "nearest enclosing extracted symbol" assumes something the code does not do.** The brief says TS never looks inside a function body. That is only true for `function_declaration`, `method_definition` and `const x = () =>`. The generic fallthrough (`ts_adapter.py`, the final `for child in node.children: walk(child, class_ctx)`) walks into anonymous arrows and function expressions passed as arguments. That is exactly how zustand's 92 duplicate helpers inside `it(() => …)` callbacks are extracted, and they have no extracted parent. **Fix:** define `parent_id = NULL` as a legal state before writing the Stage 2 ADR. Also accept that B-027's `describe`/`it` symbols will later give these helpers a parent, which changes the parent semantics under Stage 2.
- **[HIGH] `parent_id` points at an id that often does not exist.** The plan fills it with "the parent's stable id". A class skeleton over 500 tokens (PQueue has 7 parts) is stored only as `PQueue_part_1..7`, and tier is part of the hash. **Fix:** store the parent's FQN and resolve the parts at query time from the class FQN plus line ranges. Or say explicitly which part id `parent_id` means. Also say what happens when the parent is re-indexed and the member is not.
- **[HIGH] Merging same-FQN symbols conflates three different cases.** Getter/setter pairs and `@overload` sets are adjacent and form one concept. Redeclared test helpers (92 of 135 in zustand) are unrelated code in separate callbacks. Merging them gives one chunk with a discontiguous line range. That breaks the single `Lines:` header, and it breaks Stage 2's "pick the skeleton part from line ranges". The merged text also embeds as a blend. **Fix:** merge only adjacent same-parent collisions in Stage 1. Defer the test-callback collisions to B-027, where `describe`/`it` scoping gives them distinct FQNs anyway.
- **[HIGH] Stage 1 cannot say which change caused a result.** It bundles four parser changes plus per-part headers into one build. They touch every TS class member's text, so nearly every summary is regenerated. The pass bar (p-queue orig ≥ 0.537 from 0.511) is one or two query rank flips on about 25 queries, and "beyond noise" is never defined. A pass or fail will not show which change caused it. **Fix:** set a numeric noise band first (for example, per-query rank deltas and a flip count). Also run a CPU-only, code-list-only ablation (doc-move alone, then the rest) before paying for the summary rebuild.
- **[HIGH] Existing indexes will not pick up any of this.** Neither stage bumps a chunker version. Nothing in the pipeline tells an existing user index that parser output changed, so unchanged files keep old chunks while edited files get new ones. The result is a mixed-generation index, and a mixed-generation summary index. For the "others run it from GitHub" use case, that is silent inconsistency. **Fix:** add a `chunker_version` to `index_meta`, and trigger a full re-chunk when it changes, as part of Stage 1's ADR.
- **[MED] Moving docs onto members can recreate the `_part_N` problem one level down.** The JSDoc blocks with code examples that bloat the skeleton will now inflate member chunks past 500 tokens. That creates `PQueue.add_part_N`. Leading docs also fill the 512-token embedder window, so the body falls out of the code vector. **Fix:** report member part counts and the share of a member's text that is doc, next to "largest skeleton part". Consider putting doc text after the signature, or capping it.
- **[MED] Stage 2's open question decides whether Stage 2 can be measured, so settle it first.** If grouping happens in `retrieve()`, a skeleton at the head of each group takes a rank slot above the member. Symbol-gold MRR then drops by construction, roughly from 1/r to 1/(r+1). If grouping is MCP-only, "measure as in stage 1" measures nothing except tokens. **Fix:** decide now. The likely answer is MCP-only grouping plus a group-aware grader. Otherwise the schema column is built before anyone knows whether it is used.
- **[MED] "Do it in the shared helper" does not match the code.** C# and C++ have their own skeletonizers (`_cs_skeletonize`, `_cpp_skeletonize`). Attaching docs to `Symbol.text` happens where each adapter emits a symbol, not in `skeletonize`. And no eval repo covers C#, C++ or arrow fields. Those paths ship guarded only by fixtures. **Fix:** scope Stage 1 to TS (plus Python conformance), or state that C# and C++ are fixture-only with no retrieval claim.
- **[MED] Stacking on an unmerged branch.** Both B-026 stages and B-027 depend on `feature/adr-030-summary-index`. If ADR-030 changes (weight, dedupe key) or is dropped, every baseline number is void. **Fix:** merge ADR-030 or freeze it before Stage 1 branches, and pin the `store` build artifacts by commit and stack in the provenance.
- **[MED] The sequence should be Stage 1 → B-027 → Stage 2, not Stage 1 → Stage 2 → B-027.** B-027 changes test-file symbols (`describe`/`it`), parent relationships, and the file-level target that grouping must coexist with. Building grouping and `parent_id` before B-027 invites rework. The "depends on Stage 1 only" arrow is right, but the ordering of the stages is not.
- **[MED] The fix may not address the diagnosis.** The three p-queue losses come from `PQueue_part_N` in both the code list and the summary list. Stage 1 makes the skeleton smaller but repeats the class header on every part, which makes each part look more like "the class". Nothing shows that the losing parts lose because of JSDoc rather than because of class-level summaries. **Fix:** before building, run the three queries against a hand-edited `index.ts` chunk set (docs stripped, header repeated) to confirm the mechanism.
- **[LOW] Free-floating comments leave tier 1.** Region markers, license text and TODO blocks now exist only in the tier-2/3 slices, and B-027 plans to replace those slices with an outline. After B-027 these comments could exist nowhere. Record them as a B-027 constraint.
- **[LOW] `#` FQNs are a new spelling.** They should be checked against the suffix grader, the MCP tool arguments (symbol lookup by FQN), and edge targets (`owns`, calls). This is a grader and API contract change, not just a parser change.

Verdict: The diagnosis is sound and Stage 1's direction is right, but it has five fault lines. The plan relies on a parent invariant that is false in TS and a `parent_id` that often points at a non-existent id. It merges unrelated test helpers into one chunk. Stage 1 bundles changes it cannot tell apart against an undefined noise bar. And existing indexes will not pick up the new parser output. Fix those, and reorder to Stage 1 → B-027 → Stage 2, before either ADR is written.

## The Maverick — feasibility, bloat, over-engineering
- [HIGH] Stage 1 ships five unrelated parser edits (doc-move, `#` extraction, arrow-field extraction, FQN merge, per-part header) as one bundled change across four language adapters (TS, C#, C++ via shared helper, Python untouched) graded by a single pass/fail number. That is not "M" sized — it is 4-6 independent features each needing its own fixtures, each capable of moving the eval score independently, with no way to attribute a regression to one of the five once they land together. Re-cost: M (a few days) is realistic for docs-move alone; the full bundle with per-language fixtures, C#/C++ verification, and a 3-repo re-embed-and-regrade loop is a two-to-three week slog, not a checkbox — call it L, not M.
- [HIGH] There's no chunker-version invalidation anywhere in the schema (verified: `db.py` only tracks `schema_version` for DB structure; `incremental_indexer.py` gates re-parsing solely on MD5 `content_hash` of file bytes). Ship Stage 1's parser changes and every unchanged file in every existing index silently keeps its old skeletons/chunks forever — the eval will look great on freshly-built repos and be a lie for anyone who incrementally updates. This is a real defect the backlog item doesn't even mention needing a fix for; it needs a chunker-version stamp compared at read time, which is scope the "M" estimate doesn't cover.
- [HIGH] `parent_id` column (Stage 2) is solving a problem you already have the data for. `Symbol.class_context` is set in-memory during parsing (`ts_adapter.py:300,386` etc.) and FQNs are built as `class.member` (`build_fqn`), so parent is already derivable by string-splitting the FQN before the last separator, or by joining on `class_context` at query time — no schema change, no migration, no backfill script needed. Adding a stored column buys you: a migration for existing DBs, a second source of truth that can drift from the FQN if `class_context` logic ever changes, and — the part the backlog itself flags — ambiguity about which part-id a `_part_N` split symbol's parent should point to. This is textbook premature normalization: don't store a foreign key you can compute in one line at retrieval time for a single-consumer local tool.
- [MED] "Every skeleton part repeats the class header" (declaration through `{`, generics, base types) is presented as a small tweak but is actual bloat by design: a 7-part skeleton (like `PQueue_part_1..7`, the exact case motivating this backlog item) now repeats that header 7 times inside chunks whose whole point is to be small enough for the 512-token embedder window and cheap enough for GPU summarization. The item never re-measures the summarization cost after this change even though the summarizer is explicitly called "expensive (16-67 GPU-min per full index)" — adding tokens to every skeleton part on a 8GB laptop GPU that's shared with other CPU work is a real cost this plan hand-waves.
- [MED] Same-FQN merge conflates three very different situations under one mechanism: adjacent getter/setter pairs (fine, small, intentional), overload sets (fine-ish, but merging 36 `@overload` stubs into one chunk could itself blow past the tier-1 limit and re-trigger `_part_N` splitting, arguably worse than losing 42 near-duplicate overload signatures), and scattered test-helper redeclarations across different `describe`/test blocks in the same file (merging these produces a Frankenstein chunk of unrelated local functions under one FQN with no shared meaning — that's not "recovering lost data," that's manufacturing a confusing chunk). The plan treats "135 dropped symbols" as one number and one fix; it's at least two different problems needing different treatment, and the zustand 92-symbol case in particular needs its own accounting before claiming the merge is safe.
- [MED] B-027's "outline chunk per file" (imports + exported signatures) is scoped as an idea, not a plan, but it already smells of scope creep: "one summary per file vs today's summary per slice" is flagged as open, and the answer requires summarizing input longer than the model's context window — that's a real unsolved sub-problem (chunked summarization + merge, or truncation with quality loss) being waved through as a footnote in an "L"-sized item that also has to solve test-file `describe`/`it` semantics and god-file paging. Re-cost: this is two or three L items wearing a trenchcoat, not one.
- [MED] Stage 2's "group results under their class" competes directly with B-027's not-yet-designed file-level target for the same top-of-list real estate, and the backlog item admits Stage 2 depends on Stage 1 but says nothing about sequencing against B-027 despite B-027 formally depending on B-026 Stage 1 too. Building class-grouping now risks a second rework pass once file-outline chunks exist and want their own slot in the same ranked list — worth explicitly deferring Stage 2's retrieval-side grouping until B-027's shape is at least sketched, per the Architect's own note.
- [LOW] The "decide: does grouping happen in retrieve() or only in MCP formatting" question is left open but has an asymmetric cost that isn't priced: doing it in `retrieve()` changes what the eval grades, meaning Stage 1's already-established baseline numbers become non-comparable to Stage 2's, and the grader (`real_repo_eval.py:117`, dedupes on file+scope) may need its own changes to handle grouped output — that's implicit rework the plan doesn't budget for.
- [LOW] Single developer, local tool, GitHub for others to run — yet the plan adds a DB column, two new stub extraction paths, an FQN merge policy, and a parent-tracking scheme in one swoop for a codebase whose own author calls C#/C++ support unverified ("check C# and C++ before relying on this"). Ship Stage 1 for TS+Python first (where it's actually validated against eval data) and treat C#/C++ as a follow-up, rather than writing shared-helper code now for languages you haven't confirmed the walk semantics for.

Verdict: Reasonable problem diagnosis, but the plan is scoped optimistically — Stage 1's "M" bundles five separable changes with no attribution story, Stage 2's `parent_id` is a solved-in-FQN problem given a schema migration anyway, and the missing chunker-version invalidation is a silent correctness bug the plan doesn't even acknowledge needs fixing before any of this ships safely to incremental users.

## The Grounder — source verification
- [CONFIRMED] [HIGH] The TS walk descends into anonymous callbacks and extracts symbols with no parent. — `src/adapters/ts_adapter.py:403-404` (`for child in node.children: walk(child, class_ctx)` is the only fallthrough, reached for `call_expression`/`arguments`/`arrow_function` bodies since none of those types are matched explicitly). Reproduced directly: parsing `describe("s",()=>{it("d",()=>{function helper(){} class Inner{method(){}}})})` with `TypeScriptAdapter().parse()` yields `helper` (fqn `test.ts::helper`, class_context=None) and `Inner`/`Inner.method` as top-level symbols. — Confirms B-027's premise and matters directly for Stage 2: `parent_id` = "nearest enclosing extracted symbol" is well-defined only because such orphans get `class_ctx=None`, i.e. no parent, not an ambiguous one; the plan should say this explicitly rather than leave it implicit.
- [CONFIRMED] [HIGH] tree-sitter-typescript node types match the plan's every claim. — Verified by parsing a synthetic class with `tree_sitter_typescript` directly: `#baz()` → `private_property_identifier` (text includes the `#`); `private qux = () => {...}` → `public_field_definition` (the node type is `public_field_definition` regardless of the `private` modifier — a trap for anyone matching on node type alone); a leading `/** doc */` is a sibling `comment` node under `class_body`, not a child of the following `method_definition`. `_TS_NAME_TYPES` at `ts_adapter.py:101` omits `private_property_identifier`, so `_ts_decl_name` returns `None` for `#` members and they are silently dropped (defect #2 is real).
- [CONFIRMED] [HIGH] click's collisions are adjacent/overload, not scattered. — `grep -c "@overload"` over `click/src/click/*.py` returns exactly 36 (decorators.py:8, core.py:12, globals.py:2, shell_completion.py:4, _termui_impl.py:2, termui.py:5, types.py:3). Python's `@overload` stacks all overload signatures immediately above the implementation with the same name — structurally adjacent. Corroborates the merge premise for click.
- [CONFIRMED] [MED] zustand's collisions are scattered test-helper redeclarations, not adjacent. — Grep-counted repeated declaration names within single test files: `tests/basic.test.tsx` has `useBoundStore` 15×, `Component` 10×, `Counter` 5×; `tests/persistAsync.test.tsx` has `useBoundStore` 21×, `storage` 19×, `Counter` 12× (approximation, not AST-aware). Merging 15 same-named-but-unrelated helpers into one chunk would concatenate unrelated bodies into a single, likely oversized, incoherent chunk — a risk the plan does not address for the scattered case.
- [CONFIRMED] [HIGH] Incremental reindex has no chunker-version gate; it keys purely on content_hash. — `incremental_indexer.py:compute_diff()` (~278-298) classifies `modified` solely by `disk[p] != db_state[p]` (MD5 of file bytes). No `chunker_version`/`parser_version` exists; only an unrelated `schema_version` (`db.py:456-461`, ADR-025 §4). After Stage 1, an ordinary run will not reprocess unchanged files: old skeletons persist in every existing index until a file is edited or a full rebuild is forced. Invisible in eval (fresh builds), real for users.
- [UNCERTAIN] [MED] Whether moved JSDoc pushes member chunks over the 500-token tier-1 limit. — In p-queue `source/index.ts`: `setPriority` (36-line JSDoc + 7-line body, 396-438) = 1336 chars; `onError` (29-line JSDoc + 10-line body, 726-765, the `pq2-on-error` gold) = 1159 chars; `runningTasks` (31-line JSDoc + 13-line body, 923-966) = 1308 chars. ~290-335 tokens at 4 chars/token — under 500 with ~165-210 headroom, but fragile (emoji and code fences tokenize expensively). Separately: `PQueue.concurrency` (382-394) has NO JSDoc; the `pq-concurrency` loss is the getter/setter collision (defect #4), not JSDoc — so the plan conflates the mechanisms across its three named failing queries. Needs a token count, not a line count.
- [CONFIRMED] [MED] `skeletonize` is shared by TS and Python; C# and C++ have separate implementations. — `_treesitter.py:33`; called at `ts_adapter.py:303` and `python_adapter.py:111`. `csharp_adapter.py:150` `_cs_skeletonize` (used at 395) and `cpp_adapter.py:236` `_cpp_skeletonize` (used at 496) do not call the shared helper. "Do this in the shared helper so TypeScript, C# and C++ all get it" is false for C#/C++: they need their own patches — a second and third code path the plan does not flag.
- [CONFIRMED] [MED] Neither C# nor C++ adapters look inside a method/function body for nested declarations. — C#: `_handle_member` (`csharp_adapter.py:432+`) recurses only for `_TYPE_DECL_KINDS`; `method_declaration` appends and returns. C++: `_handle_fn_node` (`cpp_adapter.py:638-695`) appends and returns, never descending into `compound_statement`. Closes the plan's "check C# and C++" item in its favour.
- [CONFIRMED] [LOW] The grader's FQN suffix match is not broken by `#`. — `real_repo_eval.py:109-127`: plain `endswith`/`==` after stripping `_part_N`. The rejection of suffixed FQNs is independently CONFIRMED: `pq-concurrency` gold is `"source/index.ts::PQueue.concurrency"` (`fixtures/p-queue.jsonl:5`); a `:get`/`:set` suffix would fail every match form.
- [UNBACKED, partial] The count of "8 gold answers that name an accessor": only 1 (pq-concurrency) directly verified; the other 7 plausible but unconfirmed.

Verdict: The plan's grammar and code-location claims check out with unusual precision (down to click's 36 `@overload`s) — well-grounded, not vibes. The two real gaps are structural: (1) Stage 1 fixes `skeletonize` (TS/Python) but C#/C++'s forked skeletonizers are silently out of scope unless someone remembers; (2) Stage 1 does nothing for any existing index until a full rebuild, because incremental reindex has zero chunker-version awareness.

(Process note from the Grounder: it ran small read-only Python snippets against the installed tree-sitter grammar and the repo's TypeScriptAdapter.parse() to ground claims (a) and (c), despite the brief saying not to run Python. No files written; no index, eval or GPU run.)

## The Critic — semantics, definitions, logic
- [HIGH] "Beyond noise" in the Stage 1 pass criteria is never operationalized, even though the number exists two lines away in the source material. ADR-030 itself says, for the very n=15 file set the criteria reuse, "n = 15 is small; one query moves the file mean by up to 0.067" — and the codebase has a paired-CI95 helper (`tools/eval_common.py:84`, docstring: "a subsampled score is an interval, never false exactness") already used for exactly this kind of before/after comparison elsewhere in ADR-030 and ADR-019. B-026 quotes three bare decimals (orig 0.535, intent 0.555, file 0.811) and says "no set drops beyond noise" without a tolerance, a CI, or even the 0.067 figure that's sitting in the doc it depends on — so nobody, including the person running Stage 1, can actually adjudicate pass/fail from the text as written.
- [HIGH] The causal chain "3 queries lose to PQueue_part_N" → "JSDoc is the cause" is asserted more strongly than the evidence it's built on. ADR-030's own log calls this an open question, not a diagnosis: "now behind tier-1 PQueue_part_N class-body parts, which this knob does not touch. That is the class-skeleton question." B-026 then picks one specific mechanism (JSDoc bloat) out of several plausible ones (raw part count, stub density, generic keyword overlap, chunk length in general) with no ablation isolating JSDoc from the other three "defects" bundled into the same stage.
- [HIGH] Stage 1's own measurement can't attribute anything. It ships five independent changes at once — doc-to-member move, `#`-member extraction, arrow-field extraction/stubbing, same-FQN merge, and a new per-part class header — as a single before/after MRR delta. The "class header repeated on every part" change adds text back into the very skeleton parts the JSDoc move is meant to shrink, so a null or negative result can't tell you whether the JSDoc hypothesis was wrong or was cancelled out by the header. There is no per-change or leave-one-out variant in the "Measure" section for either stage.
- [HIGH] The third bullet of "Stage 1 passes when:" — "the size of the largest skeleton part is reported" — is not a criterion. It has no threshold, so it cannot fail. Listing a reporting obligation inside a pass/fail gate is a category error that lets Stage 1 "pass" no matter how bloated the worst skeleton part still is.
- [MED] Numeric provenance is unverifiable from what's checked in. `real_repo_eval.py:117`'s grader (`_norm`/`_matches`) does match the plan's description, but that same file's docstring shows it drives a completely different eval axis (ADR-019's graph/reranker/fusion A/B/C/D arms over a 5-repo, 148-query fixture set). The orig/intent/file numbers B-026 quotes are computed by a separate, gitignored, untracked harness (`gpu-crash-repro/…`) per ADR-030's own citations (`summary_store_eval.py`, `results_file_level.json`) — nobody without that private kit can reproduce or audit them, and the house rule ("measurement provenance must name the stack") is only half-satisfied: the grader is named, the harness that actually ran isn't.
- [MED] The arithmetic behind "3 queries carry the loss" is plausible but unverifiable as written, and the plan doesn't supply the one number needed to check it. p-queue's orig slice is never stated in the brief; I counted it directly (`benchmarks/real_repo/fixtures/p-queue.jsonl` = 24 lines, and 32+24+27 = 83, confirming n=24). At n=24, 0.511→0.537 is a total reciprocal-rank swing of 0.026×24 = 0.624, i.e. ~0.21/query average if exactly 3 queries moved — plausible for a few rank-1→rank-3ish shifts, but the text shows no per-query ranks, so a reader can't rule out that more than 3 queries actually changed (some up, some down, netting to "3 named losers").
- [MED] Several load-bearing terms are used but never defined precisely enough to implement or verify: "member" (static members? nested classes? TS `accessor`/index signatures — in or out of scope for the fix?); "leading doc comment" (what counts as "leading" when a TS decorator sits between the `/** */` and the method — `/** … */ @Input() foo(){}` — is the comment still "attached"? blank-line tolerance?); and "whole-file chunk" (B-026's chunking term) vs. the grader's "whole" (only-a-tier2/3-chunk match) are near-synonyms used in adjacent sections without ever being tied together, inviting conflation between "the chunk type" and "the grading bucket."
- [MED] "`store`" is used as an established noun ("ADR-030's `store` build") with no gloss in either file handed to me — it's ADR-030 vocabulary (a named build variant, distinct from `none`/`batched`) imported wholesale. Fine for someone who has the ADR open; not fine as a self-contained brief, and it reads confusingly close to a data-store/storage reference.
- [MED] The pass-criteria list quietly narrows scope without saying so: it checks file "any" (0.811) but drops file "whole" (0.500) entirely, even though the ADR that produced these numbers treats "whole" as the metric that actually proves tier-2/3 summaries (not tier-1 content) carry file questions. If Stage 1's symbol-level changes happen to move "whole" downward, the pass criteria as written would not catch it.
- [LOW] `file_chunk_weight` is a misleading name for what it does: per ADR-030 §3 it scales tier-2/3 chunks in the *code* ranking only — "its summary still counts in full" — not a blanket weight on "chunks belonging to a file" (which would include tier-1 symbol chunks too). A reader skimming the config key would reasonably guess the opposite of its actual scope.
- [LOW] "Nearest enclosing extracted symbol" (Stage 2's `parent_id`) is defined circularly around "extracted" — a symbol type that Stage 1 fails to extract (e.g. arrow-field class members before that fix ships, or any kind neither adapter walks into) silently reparents to a grandparent with no flag that a level was skipped, which will look like correct data rather than a known gap.

Verdict: The prose reads rigorous but the pass/fail gate is not decidable as written — no noise threshold where one already exists in the source ADR, an unablated five-change bundle measured as one number, and a causal story (JSDoc) that the ADR itself only ever called an open question.

## The Synthesis Guard — crash test and gate
### Crash test (risks the proposed fixes introduce)
- [HIGH] **The `chunker_version` fix (Architect, Maverick, Grounder) can hang the server or leave a half-built index.** If a version mismatch triggers a full re-chunk from `_ensure_indexes()` at MCP startup, a GitHub user's first `semantic_code_search` call blocks for minutes, or for GPU-hours with summaries on, and then times out. A run cut off mid-way leaves an index that mixes old and new chunks, and the version marker says it is clean. Prevent it: on mismatch, warn in the tool output header (not `isError`) and require an explicit full reindex. Write the new version to `index_meta` only after a fully successful build, and build aside then swap.
- [HIGH] **Moving docs can orphan them.** The rule "the doc leaves the skeleton and goes to its member" loses the doc from tier 1 when the member is never extracted as a symbol. That covers plain fields (`/** max */ readonly #concurrency: number`), index signatures, abstract members, TS overload signatures and decorated properties. p-queue documents fields as well as methods. Prevent it: a doc comment leaves the skeleton only if its node is emitted as a `Symbol` in the same parse. Add a fixture with a documented plain field.
- [HIGH] **Extracting `#` members without call edges makes them look dead.** `_CALL_QUERY` (`ts_adapter.py:59-65`) matches only `(member_expression property: (property_identifier))`, so `this.#tryToStartAnother()` produces no call edge. Once Stage 1 indexes the 21 `#` members, `find_dead_code` (`MCPServer.py:1431`) and blast-radius tools will report every one of them as unreferenced, which is a confidently wrong verdict for an agent. Prevent it: add `private_property_identifier` to the call and reference queries in the same change, with a fixture that asserts at least one caller.
- [HIGH] **Duplicate FQNs already leave ghost vectors, and a partial merge keeps them.** The ingest path (`incremental_indexer.py:606-673`) calls `add_with_ids` for every chunk without deduping ids. `IndexIDMap` accepts duplicates. So today the getter's vector and summary vector are in FAISS under the same id as the setter, while the DB and the doc store hold only the setter's text (`INSERT OR REPLACE`, `db.py:745`). The getter's vector can match a query and return the setter's text. The Architect's adjacent-only merge leaves this in place for the 92 scattered zustand cases. Prevent it: dedupe `(tier, id)` before `add_with_ids` for the code and summary indexes, log the count dropped, and add a test that asserts `ntotal == rows`.
- [HIGH] **A merged chunk still maps to one symbol row.** `symbols.fqn` is UNIQUE (`db.py:135`) and so is `symbol_types.symbol_fqn` (`db.py:229`). A merged getter and setter, or an overload set, still gets one line range and one type, so the getter's return type or the setter's parameter type is silently lost. Prevent it: the ADR defines the merged `start_line` as the min and `end_line` as the max, restricts merging to contiguous siblings, and names the type precedence. Add a unit test on the rows, not only on the chunk text.
- [MED] **Merged overload chunks can hide the implementation from the embedder.** Python `@overload` stubs come before the implementation. A 4 to 12 stub set plus the header fills the 512-token window, and the implementation body falls out of the code vector. Fix: put the implementation first and the stubs after it, and report member `_part_N` counts after the merge (the Architect's metric).
- [HIGH] **MCP output can drop the member it is meant to show.** `semantic_code_search` caps output at 4000 tokens and `break`s at the first chunk that does not fit (`MCPServer.py:100-111`). Members with docs moved onto them get bigger. If Stage 2 puts a skeleton part at the head of each group, one large head can end the loop before the member it introduces is printed, so the agent sees a class outline and a truncation note. Prevent it: show the member before the skeleton inside the budget, fall back to a header-only skeleton, change `break` to skip-and-continue, and measure the before/after token count and the number of returned results on a fixed query set.
- [MED] **The repeated header breaks part selection by line range.** Once every skeleton part repeats the class header, a part's `Lines:` range is no longer contiguous. Stage 2's "pick the part that holds the member's signature, known from line ranges" then matches every part, or part 1 only. Fix: store each part's body-only range separately from the header's range, and never emit a `Lines:` range that runs across both.
- [MED] **A nullable `parent_id` means two things after migration.** `ALTER TABLE ADD COLUMN parent_id` fills existing rows with NULL, and the Architect's fix also makes NULL the legal value for an orphan such as a helper inside `it(() => …)`. "Not yet migrated" and "no parent" then look the same. Fix: don't store a stable id at all (see conflicts below). If a column ships, stamp it by generation and fill it only when the file is re-chunked.
- [MED] **The cheap ablation may be biased.** A summary-frozen arm (reuse `store`'s summary index by stable id, swap only the code index) costs only CPU. But new scopes (`#` members, merged chunks) have no summaries in that arm, so it is biased against them. Report it as a code-list-only diagnostic, never as the Stage 1 pass result.
- [MED] **B-027's test-block FQNs will churn ids and grades.** Deferring the scattered collisions to B-027's `describe`/`it` scoping means every test-file symbol gets a new FQN, and so a new stable id and a full re-summarize of test files. Test names contain spaces, quotes and `.`, and those break `split(".")[-1]` (`hybrid_retriever.py:652`) and the grader's suffix match. The B-027 ADR must fix the escaping for test-name FQNs before any fixture gold uses them.
- [MED] **The baseline can change under Stage 1.** If ADR-030 merges with a different fusion weight or dedupe key, every `store` number is void. Pin `store` to commit e1e9491 plus the artifact paths and hashes, or merge ADR-030 first.
- [LOW] **Scoping to TS only, as recommended, leaves languages inconsistent.** C# and C++ keep their docs inside skeletons (their skeletonizers are separate: `_cs_skeletonize`, `_cpp_skeletonize`), while TS moves them. State this in the ADR and in the conformance fixtures, so nobody later reads the C# behaviour as a regression.
- [LOW] **`#` in FQNs is a public contract change.** Agents will pass `PQueue.#foo` to symbol-lookup tools. Add one Inspector call that uses a `#` FQN as an argument, and confirm lookups do not assume identifier characters.
- [LOW] **The summary cache only grows.** Every ablation arm adds rows to `chunk_summaries` keyed by text hash, and nothing evicts them. That is harmless for correctness, but a shared cache across arms means "regenerated summaries" can be silent cache hits from an earlier arm. Log hit and miss counts per arm in the provenance.

### Reviewer conflicts
- **Architect ("store the parent FQN, or say which part id") vs Maverick ("no column; split the FQN").** Maverick is right that no stable id should be stored, because parts and tiers make that id ambiguous. But splitting the FQN string is fragile: `Inner.method` inside a callback, nested classes, C++ `::`, and dotted test names from B-027. Ruling: persist `class_context` (or a `parent_fqn` text column) on `symbols`, which the parser already computes, and resolve parts at query time. Never store a stable id.
- **Architect ("nearest-enclosing is false in TS") vs Grounder ("well-defined because orphans get `class_ctx=None`").** They agree on the facts. Ruling: the Architect is right that the plan's sentence is false. The Grounder is right that the code's behaviour is coherent. The ADR must state NULL = no extracted ancestor as a legal state, and note that B-027 will change that state for test files.
- **Plan (merge all same-FQN symbols) vs Architect (adjacent only) vs Maverick (overloads can grow past the limit).** Ruling: the Architect is right. Merge only contiguous siblings: accessor pairs and overload sets. Scattered collisions stay unmerged, but their ghost vectors must be fixed now, which none of the reviewers caught.
- **Plan and Critic (the JSDoc story) vs Grounder (`pq-concurrency` is a collision with no JSDoc).** The Grounder's evidence is the strongest. The three losing queries have at least two different mechanisms, so the p-queue gate is judged per query and not as one mean.
- **Critic (use a paired CI) vs the need to decide at all.** A paired CI95 at n=24 will not rule out a delta of 0.026, so a gate based only on the CI can never pass or fail. Ruling: gate on per-query rank outcomes for the named queries plus a bound on how many queries flip. Report the CI; do not gate on it.
- **Maverick (the repeated header is bloat) vs Architect (it makes parts look more like the class) vs Plan (it adds context).** None of them measured it. Ruling: the header is its own ablation arm and is capped (declaration, generics and heritage only; no decorators, no doc).
- **Architect and Maverick (order Stage 1 → B-027 → Stage 2) vs Plan (Stage 1 → Stage 2 → B-027).** The Architect is right for anything touching the schema or `retrieve()`. MCP-only display grouping, using the persisted `class_context`, may ship before B-027 because it touches no ids and no grades.
- **Process note on the Grounder.** It ran Python despite the brief's ban. Its findings are credible but not reproduced by this reviewer. They must be reproduced in the ADR's fixtures before they are cited as evidence.

### Go / No-Go gate
**Before writing either ADR:**
1. ADR-030 is merged, or frozen at a named commit (e1e9491). The `store` artifact paths, grader commit, harness name (`summary_store_eval.py`) and stack are written into the backlog item.
2. A per-query baseline table exists for p-queue orig (all 24 queries, rank under `none` and under `store`). The number of queries that changed is stated, not assumed to be 3.
3. Each of the 3 losing queries has a named mechanism: `pq-concurrency` = collision; the other two confirmed as JSDoc or not by a hand-edited chunk probe on CPU.
4. A numeric noise rule is written: pass means each named query's rank is no worse than under `store`, no more than N (for example 2) queries lose 3 or more ranks per set, and the set means are reported with a paired CI95 (`eval_common.py:84`).
5. Pass criteria include file "whole" (0.500), and the largest skeleton part has a threshold. No criterion is report-only.
6. Stage 1's scope is written down: TS and Python only; C# and C++ get fixtures only, with no retrieval claim.
7. The merge policy is written down: contiguous siblings only; scattered collisions stay unmerged but their ids are deduped; min/max line range for a merged symbol; type precedence.
8. The doc-move rule is written down: a doc leaves the skeleton only when its member is emitted as a symbol, and decorator and blank-line attachment is defined.
9. Stage 2's grouping location is decided as MCP-only, and the parent is `parent_fqn`/`class_context`, not a stable id. The sequence is Stage 1 → B-027 → any `retrieve()` or schema change.

**Stage 1 ships only if:**
1. A `chunker_version` in `index_meta` is bumped. On mismatch the server warns in tool output and does not auto-rebuild. The version is written only after a full build succeeds (test: kill mid-build, then the marker is unchanged).
2. Ingest dedupes `(tier, id)` before `add_with_ids` for the code and summary indexes. A test asserts FAISS `ntotal` equals the DB chunk-row count per tier on all 3 repos.
3. `#` members have call edges: `_CALL_QUERY` covers `private_property_identifier`. A fixture asserts `this.#x()` gives a caller, and `find_dead_code` on `PQueue.#tryToStartAnother` does not say dead.
4. Fixtures pass for: a documented plain field (doc stays in the skeleton), a documented method (doc moves), a decorated method, a `#` method, an arrow field (extracted and stubbed), a getter/setter pair (one chunk, one row), Python `@property` setter and `@overload` (implementation first), and scattered redeclarations (not merged, no duplicate ids).
5. The ablation is reported: doc-move only, plus `#`/arrow, plus merge, plus header, at least as code-list-only arms on CPU, then one full summary-regenerated build. Summary cache hits and misses are logged per arm.
6. The gate from "before" items 4 and 5 passes against the pinned `store` (orig 0.535, intent 0.555, file 0.811 any / 0.500 whole), p-queue orig is at or above 0.537, and each named query's mechanism is shown fixed.
7. The report includes the largest skeleton part against its threshold, member `_part_N` counts before and after, and the doc share of member text.
8. The MCP Inspector run passes: tools listed, `semantic_code_search` returns `#` FQNs, a lookup by `#` FQN works, and an error comes back as `isError`.

**Stage 2 ships only if:**
1. B-027's ADR is written, at least its FQN and outline shape, or Stage 2 is limited to MCP display with no schema change and no `retrieve()` change.
2. No stable id is stored as a parent. The parent comes from `parent_fqn`/`class_context`, NULL means no extracted ancestor, and any migrated column tells "never filled" apart from "no parent".
3. The MCP budget loop skips an oversized chunk instead of stopping. The member is printed before its skeleton within the budget, with a header-only skeleton fallback. A test: for 10 fixed queries, every top-5 member present before grouping is still present after it.
4. Choosing a skeleton part uses the body-only range of each part. A test shows the member's signature part is the one chosen for a multi-part class (PQueue).
5. Tokens per result list and member coverage are reported before and after. The eval MRR is unchanged, which it must be if grouping is display only; any change is a bug.
6. The MCP Inspector run passes as in Stage 1, including an empty result and an oversized class.

**B-027 may start only if:**
1. Stage 1 has shipped and passed its gate, with its numbers recorded as B-027's baseline.
2. The FQN syntax for test blocks is decided, including how spaces, quotes, `.` and `::` in test names are handled. A check shows the fixture gold for zustand and p-queue test symbols still matches.
3. A home is named for free-floating comments (license, region, TODO) before the tier-2/3 slices are replaced, so they do not vanish from the index.
4. The rule for summarizing input longer than the model's window (page-and-merge or truncation) is decided and costed in GPU-minutes on the 8 GB card.
5. A chunker-version bump and the matching warning to users are planned, because test-file ids will all change.

Verdict: CONDITIONAL GO — Stage 1's direction is sound. No ADR should be written until the ghost-vector dedupe, `#` call edges, the doc-orphan rule, the versioned re-chunk and a decidable per-query gate are in the plan, and Stage 2 waits for B-027's FQN decision.

