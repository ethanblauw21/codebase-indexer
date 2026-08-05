# ADR-013: Domain-Specific / Industrial Language Adapters — Depth Where No Compiler Index Exists

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-013-domain-specific-industrial-adapters`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — needs the **adapter registry + `LanguageAdapter` Protocol** (the L5X stub already registers via it); each DSL adapter reuses this registration machinery unchanged.
- ADR-017 — needs the **tier model**; each DSL adapter registers as a tier (Tier-A once it has a conformance suite, a Tier-B/probe otherwise).
- ADR-008 — needs the **per-feature conformance machinery** (feature-tagged fixtures + precision/recall) as each new adapter's acceptance suite.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-013 bucket + build kit), suggestions S4.
> Citations `[n]` index [references-code-intelligence.md](../references-code-intelligence.md):
> [24] ESBMC-PLC, [25] IEC 61131-3 static analysis.

## Context

The depth-over-breadth thesis has a natural frontier the mainstream tools ignore: **domain-specific and
industrial languages** — PLC ladder/Structured Text (IEC 61131-3), HDL, and mapping/config DSLs — where **no
compiler-grade index exists** and general code tools simply give up. The project already **registers an L5X
adapter seam** (Rockwell PLC export XML) — but today it is a **deferred stub**: `l5x_adapter.py` raises
`NotImplementedError`, and its extraction design (rung chunking, tag edges, embedding) is deferred per
**ADR-003 §D4** pending an example corpus and gold queries. So the *interface* beachhead is proven (the
registry provably contains no tree-sitter assumption), but the *extraction* is unbuilt — and this ADR is what
turns that stub into a measured adapter. There is no tree-sitter grammar zoo for ladder logic, so the only
way in is a real adapter — and a real adapter with a conformance suite is precisely our moat.

The enabling machinery already exists: ADR-003's adapter registry + Protocol, ADR-017's tier model, ADR-008's
feature-tagged conformance suites. So *registering* a new industrial adapter is assembly on existing
infrastructure — but the DSL-specific parsing and the conformance corpus are **genuinely net-new work** (and,
for L5X, still entirely unbuilt). Not a new subsystem, but not free either. This is Wave 3, and the backlog
flags it as the **best near-term differentiation**.

## Decision

Add **first-class DSL / industrial-language adapters**, each registered as a tier (ADR-017) via the ADR-003
registry and gated by a curated conformance suite (ADR-008). Start by **implementing the registered L5X stub**
(and IEC 61131-3 Structured Text, which has no parser yet) into a measured adapter, before broadening to
other DSLs.

### §1 — Adapters follow the existing L5X pattern

New adapters live in `src/adapters/` following `l5x_adapter.py`, register in `src/adapters/__init__.py`, and
satisfy the `LanguageAdapter` Protocol unchanged. Parsing uses **tree-sitter where a grammar exists**, and
**`lxml` for XML-based DSLs** (L5X, PLCopen XML) where the format is XML rather than a tree-sitter language.
No new core machinery — these are adapters, same as any Tier-A language.

### §2 — Each adapter ships a conformance suite (ADR-008)

Every DSL adapter ships **feature-tagged conformance fixtures** declaring expected symbols/edges, and is
measured by ADR-008's precision/recall harness. This is what makes a DSL adapter *Tier-A-grade* rather than
a best-effort Tier-B probe: we can report measured accuracy on ladder logic the same way we do on Python.
The conformance suite *is* the support claim.

### §3 — First target: expand the IEC 61131-3 beachhead

Concretely, the first work **implements** the L5X stub into real extraction — symbol/edge extraction
(routines, tags, function blocks, call/use relationships) — and adds **IEC 61131-3 Structured Text** parsing
(no ST parser exists today), each with a curated conformance suite. [24] ESBMC-PLC and [25] (IEC 61131-3 static analysis) are the prior art for what structure is
extractable and meaningful in this domain. Subsequent targets (HDL, mapping/config DSLs) follow the same
registration + conformance recipe, gated by grammar/format availability (the main Open Question).

### §4 — Tier table updated per adapter

When a DSL adapter passes its conformance suite, the README tier+capability table (ADR-017 tier table; ADR-008 measured accuracy) is updated —
the industrial languages appear as measured, supported languages, which is the differentiation made
visible.

## Consequences

**Better:**
- Stakes out a **differentiation niche** competitors can't reach: provable structure for industrial DSLs
  where no compiler index exists — depth-over-breadth at its sharpest.
- High reuse on the *seam*: ADR-003 registry + ADR-017 tiers + ADR-008 conformance machinery + the
  `l5x_adapter.py` registration stub. The net-new work — DSL-specific parsing and the conformance corpus — is
  real (the L5X stub is unimplemented), but it rides a proven interface.
- Each adapter ships *measured* accuracy (ADR-008), so an industrial language is a real support claim, not a
  "we can open the file" claim.

**Worse:**
- Grammar/format availability varies wildly per DSL (the main Open Question); some DSLs have no grammar and
  need a hand-written or `lxml`-based parser.
- Domain expertise is required to author correct conformance fixtures — knowing what a *correct* ladder-logic
  edge is takes PLC knowledge, not just parser knowledge.
- Effort is M *per DSL*, and the long tail of DSLs is unbounded — scope discipline (start with the L5X
  beachhead) matters.

**Neutral:**
- Reuses the Protocol, registry, tier index, embeddings, and conformance harness untouched — each adapter is
  additive.
- Sits in Wave 3; sequenced by differentiation value, starting with the existing beachhead.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Route DSLs through the Tier-B generic `tags.scm` adapter | Most industrial DSLs have no tree-sitter grammar / no `tags.scm`; generic extraction would be empty or wrong. A fitting adapter is the only honest path. |
| Skip conformance suites for "exotic" DSLs | Then they're unverified claims — the exact breadth-without-proof failure the moat rejects. Conformance is non-negotiable. |
| Broaden to many DSLs at once | Unbounded long tail; better to deepen the proven L5X beachhead first and expand by differentiation value. |
| Build a generic XML-DSL extractor instead of per-DSL adapters | XML structure ≠ semantic structure; ladder logic and PLCopen XML need domain-specific symbol/edge meaning, not generic node extraction. |
| Treat DSL support as a separate product | It's the same engine and the same moat applied to a new domain; a fork would duplicate everything for no gain. |

## §5 — Measured corpus findings (L5X)

Measured on five whole-controller exports from live customer projects. That corpus is confidential and
is never committed; every figure here came through an aggregating script, never by reading a file.
Software revisions 34.05, 35.05 (×3) and 36.04; `SchemaRevision` 1.0 across all five, so the schema is
stable across those versions. All five are `TargetType="Controller"` with `ContainsContext="false"`.

| | |
|---|---|
| Rungs | 3,935 — 1,779 in program routines, **2,156 inside AOI definitions** |
| Routines | 209 in programs (183 RLL, 22 ST, 4 FBD) + 61 AOI definitions |
| Tags | 2,326 declared, 97.9% controller-scoped, 412 aliases |
| Modules | **83** — only 61 carry a `Name` attribute |
| Operands | 31,304 |
| Tag resolution | **25,986 / 25,986 = 100.000%** |
| Write edges | 9,110, all resolving |
| JSR resolution | 156 / 156, program-scoped |
| AOI positional binding | **418 / 418** |

**AOIs hold 55% of the ladder logic.** Anything treating an AOI as an opaque instruction call discards
more than half the code. AOI logic is also hermetically encapsulated: zero references outside its own
parameters and local tags across all 2,156 AOI rungs.

The 100% tag-resolution figure is only meaningful with its denominator stated. An earlier model treated
every operand as a tag reference, giving 26,582 operands and a 2.0% failure rate. Under the operand-role
model the denominator is 25,993 — 589 fewer — because 1,575 operands (5.0%) are not tag references at
all: 986 literals, 220 expression fragments, 156 routine names, 155 keywords, 58 labels. Those are not
discarded; they are routed to the correct edge type (a JSR target becomes a call edge, not a failed tag
lookup). The rate improved because the classification got *correct*, not because the resolver got
broader — resolution scope is unchanged.

## §6 — Falsifications

The tables in `l5x_instructions.py` are the least valuable part of this work. The valuable part is what
was assumed, what check was run against it, and what the check found. Every correction below came from
comparing a prediction against data. **None came from reading output and finding it plausible** — which
is why the write-position table was corrected four times rather than shipped wrong.

| Assumption | Check | Result |
|---|---|---|
| The destination operand comes first | Corpus | **False.** Rockwell is mixed: `OTE(Dest)`, `MOV(Src,Dest)`, `ADD(A,B,Dest)`, `GSV(...,Dest)` |
| Paren depth is enough to split operands | `MOV` arity histogram | **False.** 28 impossible three-operand `MOV`s were `Recipe[Row,Col]` splitting inside the subscript. 3 rung bodies, 36 operands, `MOV` + `COP` |
| `SWPB` writes operand 1 | Rockwell reference, then corpus | **False.** `SWPB(Source, Order Mode, Dest)` — 18 sites wrote to the mode keyword |
| `GSV` writes a fixed index 3 | Corpus arity histogram | **False.** 3- and 4-operand forms both exist; destination is *last* |
| `SSV` writes a tag | Corpus | **False.** It writes a CIP object; its last operand is a *source*. Would have emitted 5 false write edges |
| `BTD` destination is last (secondary docs) | Corpus operand-kind profile | **False.** Index 4 is always a literal; index 2 is always a declared tag |
| `FAL` destination is last (secondary docs) | Corpus operand-kind profile | **False.** Index 5 is always the expression; destination is index 4 |
| Fixed-position roles suffice | Systematic audit of all 144 positions | **False.** `GSV`/`SSV` multi-arity forms put the tag where a keyword was declared, and the adapter skips keyword operands *before* consulting writes — silently suppressing a real write edge |
| AOI binding is positional | 418 call sites | **True.** Arity rule `1 + required non-Enable parameters` held at 418/418 |
| A `Module` always has a `Name` | Corpus | **False.** 22 of 83 (26.5%) have none; sub-modules key on parent + port id + address |
| The bracket bug explains the unresolved operands | Root-cause breakdown | **False.** It explained 36 of 556 (6.5%). The other 520 were role misclassification |

Two method notes worth carrying to the next adapter:

**Secondary documentation describes the wrong thing.** Third-party sources give the Studio 5000 *dialog
field list*, which is not the neutral-text operand order. They were wrong for both `BTD` and `FAL`.
Rockwell's own docs site is JS-rendered and unfetchable; its literature PDFs need tooling not installed.

**Corpus operand-kind profiling beats documentation.** A destination must be a declared tag, while bit
offsets, lengths and mode keywords are literals or keyword strings. That asymmetry falsifies a wrong
ordering immediately, and it is how the two documentation errors were caught.

## §7 — Decisions and their reasoning

**The routine is the chunk unit; the rung is the addressable sub-unit.** Rungs run a median 74–126
characters — far too small to chunk on. A routine is roughly 1,500 characters, mirroring the
class-and-method shape the other adapters already use.

**The AOI definition is a first-class symbol.** It is closest in shape to a function with a typed
signature, and its logic routine chunks like any other. An AOI invocation is a call edge into it.

**Both mnemonic spellings are stored; there is no config flag.** `mnemonic` holds the canonical
spelling and feeds edges and stable IDs; `mnemonic_raw` holds what was written. *A future reader will
see an obvious missing knob here and want to add it. Do not.* A flag that changed the mnemonic feeding
edge generation would change stable IDs, so flipping it later would silently invalidate every ID in the
index with no schema bump and no loud failure — the same shape as the auto-destructive dimension
mismatch already flagged as a serious risk. Storing both fields gives the opt-out without the knob.

**Embedded text is assembled from prose, but the chunk carries neutral text verbatim.** This is a
departure from every other adapter, where source text *is* the embedding payload. `XIC(a)XIO(b)OTE(c)`
carries almost no natural language, so the embedding payload is comments, descriptions and names.
But only ~30% of rungs carry a comment, so a prose-only chunk would leave ~70% of the ladder unreachable
by any lexical query — and lexical matching is half of hybrid retrieval. Both go in the chunk.

**FBD is unsupported, explicitly.** Four routines across two files, under 2% of the corpus, and it needs
a block-and-wire graph rather than text extraction. **ST is unsupported for now** and is a scoped
follow-on: 22 routines here, plus standalone IEC 61131-3 per §3. ST is real text and would make a
genuinely better embedding payload than ladder, so it is deferred rather than rejected.

**A missing edge is preferable to a false one.** An unknown instruction emits reads and suppresses
writes. A missing edge makes a query return nothing, which the user notices; a false edge makes a query
return something wrong, which the user believes. This is why `SSV` deliberately has no write position.

## §8 — Open items

**Chunk granularity is not settled.** Chunking is currently per-symbol, so 2,326 tag declarations each
become a chunk against roughly 280 routines — an ~8× index inflation, and a tag declaration in isolation
is not a retrievable unit. Routine granularity is reasoned from rung-length distributions and **has
never been measured against a query anyone actually asked**. Changing it on reasoning alone would repeat
the mistake that produced the write-position table. Gated on a harness run once gold queries exist.

**Gold queries do not exist.** They need controls engineers, not more parsing. Extraction proceeds
without them; ranking, chunk assembly, and anything about the graph beyond emitting edges cannot.

**Incremental indexing granularity is mismatched.** One L5X file is one entire controller — the largest
here is 7.9 MB and 131,940 XML elements. The incremental indexer diffs at file granularity with MD5, so
any edit anywhere in a controller invalidates the whole thing. In a Python repo a file is a module; here
a file is closer to a repo. Recorded, not solved — it needs its own ADR.

**Seven of eight mnemonic alias pairs remain inference.** Only `MOVE` is semantically confirmed, from
the `EQ(t,0)MOVE(1,t)` default-value idiom plus an operand-kind profile identical to `MOV`'s. The
spelling split correlates with SoftwareRevision 36.04, but that is a sample of one file. One more v36
export would settle it cheaply.

**16 instruction signatures have no corpus evidence** and are labeled `verified=False`. All 16 have zero
occurrences here, so they cost nothing in this corpus but would matter on a different controller.

**Nested instruction calls are scanned twice** — once inside the enclosing operand and once as their own
match. Edges dedupe, so this is not a correctness problem, but it inflates operand counts and produced
the only 7 apparent resolution failures before they were traced.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] Implement the L5X stub into real extraction — `src/adapters/l5x_adapter.py` plus the instruction model in `src/adapters/l5x_instructions.py`. Symbols for programs, routines, AOI definitions, parameters, local tags, tags and modules; edges for reads, writes, AOI and JSR calls, alias-to-module-I/O, and ownership.
- [x] Curated conformance fixtures — 7 hand-authored synthetic L5X under `tests/fixtures/conformance/l5x/`, scoring 1.000/1.000 on symbols and edges. Verified they have teeth: disabling canonicalization drops `mnemonic_alias` and nothing else.
- [x] Registered via the existing `.L5X`/`.l5x` REGISTRY entries; `.L5X` added to `tools/conformance_eval.py`. **`lxml` was NOT added** — stdlib `xml.etree` was sufficient, and ADR-013 §1's preference for `lxml` is deliberately not exercised without a concrete need.
- [x] Schema: `READS` / `WRITES` / `ALIAS_OF` edge kinds plus `_migrate_edge_kinds`. It needs its own guard because `_migrate_edges` gates on `"resolved_target" in cols`, already true on every existing index, and it must run *last* of the edge migrations because it rebuilds the table.
- [ ] **IEC 61131-3 Structured Text parsing** — deferred, see §7. Not attempted.
- [ ] Update the README tier+capability table. Deferred until conformance is measured against something broader than self-authored fixtures.
- [ ] Subsequent DSL targets (HDL, mapping/config) by the same recipe.

**Notes:**

- The stub's `parse()` used to raise `NotImplementedError`, violating the Protocol contract in
  `adapters/base.py` that it return an empty `ParseResult`. That crashed indexing of any tree containing
  a `.L5X` file, and made the fixtures abort the run rather than score 0.00.
- Edge counts are not operand counts. `UNIQUE(source_fqn, target, kind)` collapses 9,110 write
  *positions* into 3,868 distinct write *edges*. A routine writing the same tag in twenty rungs is one
  edge. Correct for graph queries; do not read the drop as data loss.
- Corpus run: 5/5 files, 12.7 MB, no parse failures, zero instructions lacking a signature.
  **6,011 symbols and 13,316 edges** — 83 modules, 2,326 tags, 2,280 parameters, 947 local tags,
  279 routines, 61 AOIs, 35 programs; 5,148 reads, 3,868 writes, 3,639 owns, 412 alias_of, 249 calls.

<!-- 2026-06-18: Wave 3, best near-term differentiation. Default first target = IMPLEMENT the L5X stub (currently NotImplementedError, deferred ADR-003 §D4) + IEC 61131-3 ST parsing (no parser today). Reuses ADR-003 registry + ADR-017 tiers + ADR-008 conformance. Done when a new DSL adapter passes its conformance suite + tier table updated. Open: grammar availability per DSL. Effort M per DSL. -->

**Notes:**
<!-- 2026-06-18: Wave 3, best near-term differentiation. Default first target = IMPLEMENT the L5X stub (currently NotImplementedError, deferred ADR-003 §D4) + IEC 61131-3 ST parsing (no parser today). Reuses ADR-003 registry + ADR-017 tiers + ADR-008 conformance. Done when a new DSL adapter passes its conformance suite + tier table updated. Open: grammar availability per DSL. Effort M per DSL. -->
