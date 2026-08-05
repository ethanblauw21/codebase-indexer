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
| Routines | 279 — 209 in programs (183 RLL, 22 ST, 4 FBD), 70 inside 61 AOI definitions |
| Tags | 2,326 declared, 97.9% controller-scoped, 412 aliases |
| Modules | **83** — only 61 carry a `Name` attribute |
| Operands | 31,304 |
| Write positions | 11,379, all resolving → 3,868 distinct write edges |
| Read positions | 16,111, of which 285 unresolved (98.2%) |
| JSR resolution | 156 / 156, program-scoped |
| AOI positional binding | **418 / 418** |

**AOIs hold 55% of the ladder logic.** Anything treating an AOI as an opaque instruction call discards
more than half the code. AOI logic is also hermetically encapsulated: zero references outside its own
parameters and local tags across all 2,156 AOI rungs.

Two figures in earlier revisions of this table did not survive being checked against the adapter, and
both are corrected above. They are left described rather than deleted because the way they failed is
the reusable part.

**"Tag resolution 25,986 / 25,986 = 100.000%" was never a fact about the adapter.** It was produced by
a third implementation — a one-off measurement script, neither the survey nor the shipping code — which
counted only *base operands at TAG positions*. The adapter also emits reads for identifiers found inside
array subscripts and inside expression operands, and those are where nearly all failures live. Measured
against the adapter: **285 of 16,111 read positions do not resolve (1.8%)**, split 222 expression
interiors, 56 subscript interiors, 7 base operands. Writes resolve at 11,379 / 11,379.

The expression-interior rate is 31% by itself, and that is a known imprecision rather than a defect:
every identifier inside a `CPT` or `FAL` expression is treated as a read, which sweeps up nested
mnemonics and symbolic constants along with real tag references. Narrowing it needs an expression
parser. It is recorded here so the number is not mistaken for extraction failure.

**"Write edges 9,110" was a survey figure presented as an adapter figure.** The adapter emits 11,379
resolved write positions. The deduplicated total, 3,868 distinct write edges, agrees exactly — which is
why the discrepancy survived: the number that got checked downstream was the one that was right.

Neither correction changes a conclusion in this ADR. Both were found by §5.3's cross-source audit, and
neither would have been found by looking at the output and finding it plausible.

The operand-role reclassification the old figure described is still real and still the reason the model
is right: an earlier model treated every operand as a tag reference, giving 26,582 operands and a 2.0%
failure rate, because 1,575 operands (5.0%) are not tag references at all — 986 literals, 220 expression
fragments, 156 routine names, 155 keywords, 58 labels. Those are not discarded; they are routed to the
correct edge type (a JSR target becomes a call edge, not a failed tag lookup). Classification got
*correct*; the resolver never got broader.

### §5.1 — Why the resolution rate is not circular, and what to monitor instead

A near-total resolution rate produced by removing 5.0% of the denominator deserves suspicion, so the
property that makes it meaningful is stated here rather than left to be inferred.

**Role classification is positional and independent of resolution outcome.** `Signature.role_at` is a
function of `(index, arity)` only. It never consults the tag registry, and the adapter assigns a role
*before* attempting resolution. Operand 1 of `GSV` is typed by its position, not by whether that string
happens to match a declared tag. So an operand cannot escape the denominator by failing to resolve —
the two decisions are made independently and in that order.

That independence is exactly what creates the failure mode worth naming: **if the role table is wrong,
misclassified operands leave the denominator rather than failing to resolve, and the rate stays high
while extraction silently degrades.** A metric designed to detect bad extraction is largely incapable of
falling under that condition.

So the rate alone is not the health signal. **The monitored number is the exclusion rate, currently
5.0% (1,575 of 31,304).** On a future corpus the thing to watch is exclusions moving away from 5.0%,
not resolution dropping. Resolution barely can drop; exclusions can drift, and drift is what a wrong
role entry looks like from the outside.

`tools/l5x_role_audit.py` is the check that makes this operational, and it is deliberately **not** one
of the retiring diagnostics — it needs a corpus of real controllers, so it can never become a fixture.
It asserts three predicates per operand position. The first two (a non-tag role must not hold declared
tags; a write position must resolve) were already being run informally. The third is new and exists
because the first two have a blind spot: **a position typed `TAG` that never actually holds a tag is
invisible to both.** That predicate found `GSV[1]` (§6). Its calibration matters — a *low tag fraction*
at a TAG position is not a defect, since `ADD(A,5,Dest)` legitimately puts a literal at index 1 and
literals are skipped harmlessly. Only undeclared names and CIP object names count. An earlier draft
flagged on tag fraction and produced 22 false alarms against the 1 real finding.

The table also carries a corpus-independent invariant, in `tests/test_l5x_adapter.py`: **every write
position must also be typed `TAG` at every arity it can take.** The adapter skips non-tag roles *before*
consulting `writes`, so a write position typed `KEYWORD` is not a wrong edge but a silently missing one.
That is precisely what shipped in `GSV`, and it is checked without a corpus because the corpus exercises
only 56 of the 72 table entries — the other 16 would otherwise go unchecked until a controller that used
them turned up.

The 7 nested-`ABS` artifacts are recorded rather than rounded away for the same reason. Nested
instruction calls inside `CPT`/`FAL` expressions are a category the scanner does not handle — it
matches every `MNEMONIC(` including nested ones — and 7 is the count *in this corpus*, not a bound.

### §5.2 — Corrections not yet asserted by any fixture

The `GSV` role bug was a correct fix verified against the survey that did nothing in the adapter. The
survey and the adapter are two implementations of the same parse, so a fix verified in one says nothing
about the other. That generalizes, and this is the resulting audit.

| Correction | Verified against | Guard | Teeth check |
|---|---|---|---|
| Bracket-depth operand splitting | synthetic unit cases + corpus delta | `array_subscript_2d` | ✔ |
| JSR program-scoping | survey | `jsr_same_program` | ✔ |
| Mnemonic alias table | fixture + teeth check | `mnemonic_alias` | ✔ |
| AOI positional binding | 418 corpus call sites | `aoi_definition` | ✔ |
| Alias → module I/O | survey | `alias_module_io` | ✔ |
| `SWPB` destination index 2 | survey, Rockwell docs, profiling | `write_position_swpb_btd` | ✔ |
| `BTD` destination index 2 | profiling | `write_position_swpb_btd` | ✔ |
| `FAL` destination index 4 + expression role | profiling | `fal_expression` | ✔ |
| `GSV` destination last + multi-arity roles | survey, then audit | `gsv_ssv_system` | ✔ |
| `GSV[1]` is a keyword, not a tag | role audit, predicate 3 | `gsv_ssv_system` | ✔ |
| `SSV` emits no write | profiling | `gsv_ssv_system` | ✔ |
| Nameless module handling | corpus counts | `module_nameless` | ✔ |
| Module address from the **upstream** port | corpus port profile | `module_nameless` | ✔ |
| Neutral text present in chunk body | corpus measurement | `test_l5x_adapter.py` | ✔ |
| AOI internals are not tier-1 chunks | corpus chunk counts | `test_l5x_adapter.py` | ✔ |
| Dropped elements are reported, not silent | the module bug | `test_l5x_adapter.py` | ✔ |
| Empty operand slots preserve position | corpus, 7 sites | `test_l5x_adapter.py` | **none possible** |

Every correction now has a guard, and each guard was verified to fail when its fix is reverted — a
fixture that scores 1.000 with and without the fix guards nothing. The teeth check is scripted rather
than argued: revert the fix in memory, re-score all eleven fixtures, and confirm the intended one and
only the intended one breaks.

**One row cannot be given teeth, and that is itself the finding.** Preserving empty operand slots is
correct — an omitted operand is a hole, and collapsing it renumbers everything after it — but reverting
it reproduces the corpus edge counts *exactly*. `GSV` and `SSV` are the only instructions here that
carry holes, and their role tuples are uniform keywords followed by a last-position destination, so a
shift happens to re-type every operand to what it already was. The fix is defensive against a table
that does not exist yet. It is asserted at the scanner instead, and flagged here rather than described
as a bug fix, because claiming it corrected edges would be false.

### §5.3 — Cross-source audit of the headline figures

§5.2 covers *corrections* with no fixture. It does not cover *measurements* resting on a single script
with nothing checking them, which is a different failure and the one that produced the module bug: 61
looked plausible against 61 AOIs, and two unrelated numbers agreeing was enough to stop anyone looking.

A valid second source must come from outside the code that produced the first number — an element
histogram, a different traversal, raw-text arithmetic, or arithmetic that has to come out a particular
way for structural reasons. Another function in the same module agreeing is not independent.

| Figure | Value | Checked against | Result |
|---|---|---|---|
| Modules | 83 | `iter()` vs `Modules.findall()`; ancestor-chain check | agree |
| Programs | 35 | whole-document element histogram | agree |
| AOI definitions | 61 | whole-document element histogram | agree |
| Routines | 279 | histogram + parent-map traversal (209 program / 70 AOI) | agree |
| Tags | 2,326 | whole-document element histogram | agree |
| Parameters | 2,280 | 2,402 elements − 61 AOIs × 2 implicit | agree |
| Local tags | 947 | whole-document element histogram | agree |
| Rungs | 3,935 | histogram vs parent-map ownership traversal | agree |
| Alias edges | 412 | raw `AliasFor="` string count over file bytes | agree |
| Tag scope split | 97.9% | parent-map traversal (2,276 controller / 50 program) | agree |
| Symbols | 6,011 | per-kind arithmetic closes exactly | agree |
| Edges | 13,316 | per-kind arithmetic closes exactly | agree |
| JSR call sites | 156 | raw `JSR(` regex count over rung text | agree |
| Write positions | 11,379 | — | **corrected**, see §5 |
| Read resolution | 285 unresolved | — | **corrected**, see §5 |
| Operands | 31,304 | *no independent source* | — |
| Distinct tags referenced | — | *no independent source* | — |
| Chunk counts | 2,784 | *equals chunkable-kind symbol count, not independent* | — |
| AOI positional binding | 418 / 418 | *survey only* | — |

**The blanks are the deliverable.** Operand totals have no second source because any recount uses the
same scanner; a genuinely independent one needs a second tokenizer, which is a rewrite rather than a
check. The AOI arity rule held at 418/418 but that was measured by the survey, so by the standard this
section applies it is unconfirmed against the adapter — `aoi_definition` guards the *behaviour* but not
the *rate*. Those three are where the next module-shaped bug is most likely sitting.

Two audit findings are worth carrying forward as method. The audit found **two wrong figures out of
thirteen checkable ones**, and in both cases the wrong number sat next to a right one that agreed with
everything downstream — 9,110 write positions beside a correct 3,868 deduplicated total, and a 100%
resolution claim beside genuinely resolving writes. A figure does not have to be load-bearing to be
wrong, and a wrong figure next to a right one is *harder* to catch, not easier.

The second: writing the audit surfaced a bug in the audit. Globbing `*.L5X` and `*.l5x` on a
case-insensitive filesystem returns every file twice, which silently doubled every count in the first
run of `tools/l5x_role_audit.py`. Checking tools need checking too.

### §5.4 — Module identity, I/O decodability, and the packing rule

**Nameless does not imply no I/O.** The earlier report inferred that the 25 connectionless modules were
"almost certainly" the 22 nameless ones. The counts never matched, and the cross-tabulation shows the
two properties are close to independent:

| | has connection | no connection |
|---|---|---|
| **has `Name`** | 45 | 16 |
| **no `Name`** | 13 | 9 |

13 of 22 nameless modules carry a connection and 16 of 61 named ones do not. Any decoding work that
inherited "nameless means no I/O" would have skipped 13 modules that do have I/O. This is why the
handoff asked for four cells instead of an inference.

**Identity keys on `Name` when present, and that is load-bearing.** `ParentModule` refers to its parent
*by name*, and in all 83 corpus modules the parent named is one that carries a `Name` — so keying named
modules on anything else would break every ownership edge. Nameless modules key on parent + parent port
id + **upstream** port address, which is unique within every file and never collides with a named
module's fqn. The upstream qualifier is not cosmetic: 18 modules expose two addressed ports, and in all
18 the upstream one is *second* in document order, so taking the first addressed port keyed them on the
downstream bus they provide rather than their own position in the tree. Uniqueness held anyway, by luck.

**Decodability: the export is sufficient, and no out-of-band catalog is needed.** All **58 of 58**
connections carry tag structure — member names and datatypes, 2 to 81 members. 44 declare an explicit
`InputSize` and `OutputSize`; 28 carry a `ConnectionPath`; 50 modules carry a `ConfigTag`. The survey
script's decodability verdict of 44/83 is pessimistic because it never inspects `ConnectionPath` or
`ConfigTag`, and because the connectionless modules are mostly sub-devices with no I/O of their own.

**The packing rule is wrong, and a decoder must not be built on it.** This is the check that mattered
most and it came back negative. Computing the expected assembly size from the member list and comparing
against the declared `InputSize`:

| Candidate rule | Agrees |
|---|---|
| BOOLs pack 32/word, others align to own width | 14 / 44 |
| BOOLs pack 8/byte, others align | 1 / 44 |
| every BOOL takes a byte | 1 / 44 |
| BOOLs pack 32/word, no alignment | 14 / 44 |

`InputSize` is in bytes, not 32-bit words — nothing matched under a word reading. But the useful result
is the *separation*, which is total and has no overlap: **every one of the 14 agreements has 1–15 BOOLs,
and every one of the 30 disagreements has 41–123.** The rule holds exactly when the BOOLs fit in a
single 32-bit word and fails whenever they span more than one. The residuals are not a constant offset
either — declared exceeds computed 10 times and falls below it 20 times — so this is not a header or
status word that could be added back.

So the partial-word case is right and the multi-word layout is wrong: a long BOOL run is not a
contiguous packed block in declaration order. Whatever the real rule is, a decoder written on the
assumed one would be confidently off by a few bits on exactly the assemblies that carry the most data,
which is worse than not decoding. **Capture work stays blocked on determining the real layout**, and
that needs either documentation or a known-good capture to calibrate against, not more of this corpus.

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
| A `Module` always has a `Name` | Corpus | **False.** 22 of 83 (26.5%) have none; they key on parent + port id + address |
| The 22 nameless ones might be nested in vendor blobs, making 61 correct | Ancestor-chain check | **False.** All 83 are direct children of `<Modules>` under `<Controller>`; `iter("Module")` and `Modules.findall("Module")` agree exactly. 83 is the inventory |
| A missing attribute means a malformed element worth skipping | Module count | **False, and the pattern is the finding.** `if not name: continue` silently dropped a quarter of the module hierarchy. A missing attribute is a case to handle or fail loudly on, never to skip quietly |
| The bracket bug explains the unresolved operands | Root-cause breakdown | **False.** It explained 36 of 556 (6.5%). The other 520 were role misclassification |
| `GSV[1]` is a tag, since a keyword typed as TAG merely fails to resolve | Profiling what operand 1 *names* | **False,** and the reasoning was sound but rested on a false premise. All 8 resolvable instances name a **Module**; the 6 matching a declared tag are that module's alias tag colliding by name. There was no read edge to lose, and typing it TAG emitted 6 false read edges plus 29 spurious failures |
| Checking non-tag roles and write positions audits the table | The `GSV[1]` bug surviving both | **False.** A position typed `TAG` that never holds a tag is invisible to both predicates. A third predicate was needed, and calibrating it on *undeclared names* rather than tag fraction is what made it usable — 1 real finding instead of 22 false alarms |
| 25 connectionless modules are "almost certainly" the 22 nameless ones | 2×2 cross-tabulation | **False.** 13 of 22 nameless modules have a connection; 16 of 61 named ones do not. Nameless does **not** imply no I/O, and inheriting that would have skipped 13 modules that have it |
| The first addressed `Port` gives a module's address | Port profile | **False.** 18 modules expose two; in all 18 the **upstream** port is second, so document order keyed them on the downstream bus they provide. Uniqueness held by luck |
| BOOLs pack into 32-bit words in declaration order in an I/O assembly | Computed vs declared `InputSize`, 44 connections | **False, with a clean boundary.** 14/44 agree and all have 1–15 BOOLs; 30/44 disagree and all have 41–123. Correct within one word, wrong across words. Residuals go both directions, so it is not a header. **Decoder work stays blocked** |
| The corpus is the second source for the ADR's figures | Cross-source audit (§5.3) | **False for two of thirteen.** "9,110 write positions" and "100.000% tag resolution" were both produced by non-adapter implementations and neither describes what ships. Both sat beside a correct figure that agreed downstream |
| An audit script is trustworthy because it is an audit | Its own first run | **False.** Globbing `*.L5X` and `*.l5x` on a case-insensitive filesystem doubled every count |

Two method notes worth carrying to the next adapter:

**Documentation is authoritative for semantics and unreliable for serialization order — hypothesis,
not established.** Documentation gives the Studio 5000 *faceplate / dialog field list*, which is not
necessarily the order of the neutral-text serialization. `BTD` and `FAL` are both instructions where
the editor groups operands differently from a flat argument list, which would explain why exactly those
two were wrong while the semantics they describe were correct.

If that mechanism is real, documentation is not unreliable in general — it is unreliable in one
predictable direction, and the rule for the next adapter is: **take semantics to the manual, take
serialization order to the corpus.** That is far more useful than "docs were wrong twice." Recorded as
a hypothesis because two instances is not a pattern; a third instruction with grouped faceplate
operands and a confirmed flat order would settle it.

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

**Chunk granularity is partly settled.** The half that was a category error is fixed: AOI parameters
and local tags are declarations internal to a definition, and no query's best answer is one of them in
isolation. They were 3,227 of 6,011 tier-1 chunks — more than half the index — and are now excluded,
taking the corpus to **2,784**. Symbols and edges are untouched; all 6,011 still reach the graph, since
chunking is downstream of `parse_file`.

The mechanism is adapter-supplied policy, not a special case in shared code: an adapter may declare
`chunkable_kinds`, and omitting it — which every other adapter does — means every symbol is chunkable.
Chunk counts for Python, TypeScript, JavaScript, C# and C++ were recorded before and after and are
byte-identical.

The half that is genuinely open stays open. **Whether a controller-scoped `tag` declaration should be a
tier-1 chunk could go either way**, so `tag` remains chunkable and waits for gold queries and a harness
run. Routine granularity is still reasoned from rung-length distributions and **has never been measured
against a query anyone actually asked**. Settling either on reasoning alone would repeat the mistake
that produced the write-position table.

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

**Expression interiors resolve at 69%.** Every identifier inside a `CPT` or `FAL` expression is treated
as a read, which sweeps up nested mnemonics and symbolic constants: 222 of 706 do not resolve, and they
are the bulk of the adapter's 285 unresolved reads. Narrowing it needs an expression parser rather than
a regex. Recorded so the figure is not mistaken for extraction failure.

**Three figures have no independent second source** (§5.3): operand totals, distinct tags referenced,
and the 418/418 AOI arity rule, which was measured by the survey rather than the adapter. These are the
most likely place for the next module-shaped bug.

**Class 1 I/O decoding is blocked on the packing rule** (§5.4), not on the export. The export carries
enough structure — 58 of 58 connections have member names and datatypes — but the assumed BOOL packing
rule fails on every assembly whose BOOLs span more than one 32-bit word. Determining the real layout
needs documentation or a known-good capture; more of this corpus will not settle it.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] Implement the L5X stub into real extraction — `src/adapters/l5x_adapter.py` plus the instruction model in `src/adapters/l5x_instructions.py`. Symbols for programs, routines, AOI definitions, parameters, local tags, tags and modules; edges for reads, writes, AOI and JSR calls, alias-to-module-I/O, and ownership.
- [x] Curated conformance fixtures — **11** hand-authored synthetic L5X under `tests/fixtures/conformance/l5x/`, scoring 1.000/1.000 on symbols and edges (81 symbols, 93 edges). Teeth verified by script: each fix is reverted in memory and the intended fixture — and only that one — must fail. Eight reversions checked, seven caught; the eighth is unguardable and is recorded as such in §5.2.
- [x] Guards the conformance harness cannot express — `tests/test_l5x_adapter.py`, 12 tests: chunk-body content, chunking policy, loud failure on dropped elements, operand-slot positioning, and corpus-independent operand-role table invariants.
- [x] `tools/l5x_role_audit.py` — the operand-role audit promoted to a permanent, corpus-facing check with a third predicate the informal version lacked. Exits non-zero on any prediction failure. **Not** one of the retiring `tools/l5x/` diagnostics; it can never become a fixture because it needs real controllers.
- [x] Adapter-declared `chunkable_kinds` (`src/adapters/base.py`, `src/ast_chunker.py`) — opt-in policy, absent on every other adapter, verified to leave Python/TS/JS/C#/C++ chunk counts byte-identical.
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
  **2,784 of the 6,011 symbols are tier-1 chunks**; the 3,227 AOI-internal declarations are graph
  nodes but not retrievable passages (§8).
- The `GSV[1]` fix removed 6 false read edges and 29 spurious resolution failures. Deduplicated edge
  counts are unchanged, because all 6 duplicated edges that already existed — which is exactly why a
  deduplicated total is a poor place to look for this class of bug.

<!-- 2026-06-18: Wave 3, best near-term differentiation. Default first target = IMPLEMENT the L5X stub (currently NotImplementedError, deferred ADR-003 §D4) + IEC 61131-3 ST parsing (no parser today). Reuses ADR-003 registry + ADR-017 tiers + ADR-008 conformance. Done when a new DSL adapter passes its conformance suite + tier table updated. Open: grammar availability per DSL. Effort M per DSL. -->

**Notes:**
<!-- 2026-06-18: Wave 3, best near-term differentiation. Default first target = IMPLEMENT the L5X stub (currently NotImplementedError, deferred ADR-003 §D4) + IEC 61131-3 ST parsing (no parser today). Reuses ADR-003 registry + ADR-017 tiers + ADR-008 conformance. Done when a new DSL adapter passes its conformance suite + tier table updated. Open: grammar availability per DSL. Effort M per DSL. -->
