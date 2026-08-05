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

The operand-role reclassification is real and is the reason the model is right: an earlier model treated
every operand as a tag reference, giving 26,582 operands and a 2.0% failure rate, because 1,575 operands
(5.0%) are not tag references at all — 986 literals, 220 expression fragments, 156 routine names, 155
keywords, 58 labels. Those are not discarded; they are routed to the correct edge type (a JSR target
becomes a call edge, not a failed tag lookup). Classification got *correct*; the resolver never got
broader.

### §5.0 — RETRACTED: "tag resolution 25,986 / 25,986 = 100.000%"

**There are three parsers in this project, and this claim was measured on the one nobody ships.**

That sentence is the finding. The survey under `tools/l5x/`, the adapter under `src/adapters/`, and — as
this audit discovered — a third one-off measurement script written to verify the resolution rate. Three
implementations of the same parse. **Every figure must state which one produced it, and a figure not
produced by the adapter is not a claim about the system.**

*The claim.* Earlier revisions of §5 reported tag resolution at 25,986 / 25,986 = 100.000%.

*Why it was believed.* It arrived with its denominator explained — the operand-role model excludes 5.0%
of operands as non-references — and with §5.1 stating the property that made it non-circular: role
classification is positional and independent of resolution outcome. That property is **true**, and it
was documented specifically so a reader would trust the number. The reasoning was sound and the number
was still wrong, which is the uncomfortable part.

*What falsified it.* §5.3's cross-source audit. Instrumenting the shipping adapter's own resolution
call sites showed 314 unresolved reads where the claim required zero. The measurement script counted
only **base operands at TAG positions**; the adapter additionally emits reads for identifiers inside
array subscripts and inside expression operands, and that is where essentially all failures live. The
100% was never false about what it measured. It was false about what it was presented as measuring.

*What replaced it.* **285 of 16,111 read positions unresolved — 98.2%**, split 222 expression interiors,
56 subscript interiors, 7 base operands. Writes resolve at 11,379 / 11,379. (285 rather than 314 because
the `GSV[1]` fix in §6 removed 29 of them.)

98.2% is a good number, and worth noting: it is roughly where the survey stood before any of this work.
The expression-interior rate is 69% on its own, which is a known imprecision rather than a defect —
every identifier inside a `CPT` or `FAL` expression is treated as a read, sweeping up nested mnemonics
and symbolic constants. Narrowing it needs an expression parser.

A second figure failed the same audit for the same reason. **"Write edges 9,110" was a survey number
presented as an adapter number**; the adapter emits 11,379 resolved write positions. The deduplicated
total, 3,868 distinct write edges, agreed exactly — which is precisely why it survived. The number that
got checked downstream was the one that was right.

Neither correction changes a decision in this ADR. Both were invisible to inspection, because a wrong
figure sitting next to a right one is *harder* to catch than a wrong figure standing alone.

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
shift happens to re-type every operand to what it already was. It is asserted at the scanner instead,
and flagged here rather than described as a bug fix, because claiming it corrected edges would be false.

**That row is unguardable *on this corpus*, not in principle**, and the distinction matters for anyone
reading seven-of-eight as a ceiling. An instruction with heterogeneous operand roles and an omitted
operand would be mis-typed immediately; this corpus simply contains no such instruction. A different
controller mix makes the row catchable. `tools/l5x_fixture_teeth.py` prints that caveat next to the
result so the number is not read as a permanent property of the adapter.

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
| AOI positional binding | 418 / 418 | **adapter-side counter** (was survey-only) | agree |
| Operands | 31,304 | *no independent source* | — |
| Distinct tags referenced | — | *no independent source* | — |
| Chunk counts | 2,784 | *equals chunkable-kind symbol count, not independent* | — |

**The blanks are the deliverable.** Operand totals have no second source because any recount uses the
same scanner; a genuinely independent one needs a second tokenizer, which is a rewrite rather than a
check.

The AOI arity rule was the one blank with consequences attached — **786 write edges depend on it** — so
it was closed rather than left open. The adapter now counts its own positional bindings and arity
mismatches (`aoi_calls_bound` / `aoi_calls_arity_mismatch`) and warns when a call site's operand count
is not predicted by `1 + required non-Enable parameters`, since those parameter edges are then absent.
Measured by the adapter rather than the survey, it holds at **418 / 418 with zero mismatches**. Same
number, now produced by the implementation that ships.

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

**The packing rule is UNDETERMINED — and the earlier "falsified, with a clean boundary" reading of this
same data is retracted.** The retraction is recorded because it failed in exactly the way §5.0 did, one
turn after §5.0 was written.

*The claim.* Computing assembly size from the member list and comparing against declared `InputSize`
gave 14 / 44 agreement under "BOOLs pack 32-to-a-word in declaration order, other members align to their
own width", 1 / 44 under two alternatives. The agreements and disagreements separated perfectly by BOOL
count: **every one of the 14 agreements had 1–15 BOOLs, every one of the 30 disagreements had 41–123.**
That was read as "the rule holds within one 32-bit word and breaks across word boundaries."

*Why it was believed.* A total separation with no overlap is a strong-looking signal, and the residuals
went both directions (declared exceeded computed 10 times, fell below it 20), which ruled out a constant
header and made "the multi-word layout is wrong" the natural reading.

*What falsified it.* Two follow-up checks, both cheap, both suggested rather than volunteered:

- **`InputSize` is a function of the connection datatype name, not of the member list.** Across 18
  distinct `AB:…` connection datatypes there are **zero** conflicts — every name maps to exactly one
  `(InputSize, OutputSize)` and exactly one member signature.
- **And it is emphatically not a function of the member list.** `AB:1408_0ED79BF4:I:0` has 17 members
  and declares 68 bytes. `AB:5000_DI16:I:0` has 52 and declares 68. `AB:5000_DO16_Diag:I:0` has 84 and
  declares 68. Three assemblies, member counts spanning 5×, identical declared size.

So `InputSize` is the **CIP connection buffer size — a per-family constant including protocol and status
overhead** — while the member list describes the *decoded tag structure*. They are different things, and
the comparison was category-confused. It could never have tested the packing rule. The 14 "agreements"
were coincidences where a small assembly happened to land near its family's buffer size, and the clean
boundary at 1–15 BOOLs was an artifact of exactly that.

A separate test against `ConfigTag` / `ConfigSize` — the attribute the survey never inspected, and which
lives on `ConfigTag` rather than on `Module`, which is why an earlier pass counted zero of them — also
mismatches on all 50. Several land at computed + 4 bytes, but not consistently enough to call a header.

*What replaced it.* **The packing rule is undetermined, not falsified.** Nothing in the export
independently states byte offsets, so nothing in this corpus can settle it. The export carries the
*semantics* — 58 of 58 connections give member names, datatypes and declaration order — and does not
carry the *layout*.

That inverts the sequencing. Layout comes from an EDS file or from observing real traffic, and a capture
from a live controller with known tag values yields the offsets empirically. **The tap comes before the
decoder, not after it.** Building a decoder first and validating it against a capture has the dependency
backwards; the capture is the only available source of the rule the decoder needs.

### §5.5 — Diagnostics are instruments, not verification artifacts

An earlier revision set a retirement condition for the diagnostics under `tools/l5x/`: they go once
every fact they established has a fixture asserting it in the adapter. **That condition is now met, and
it was the wrong condition. The diagnostics stay.**

The reasoning that produced it was that the surveys are duplicate implementations of the parse, which is
a real problem — the `GSV` fix that was verified in the survey and did nothing in the adapter is exactly
that problem, and §5.0 shows a third implementation doing the same thing on a larger scale. But the fix
for duplicate implementations is **to stop treating survey output as verification**, not to delete the
survey.

The evidence against retirement is this session. Three of the four things it turned up came from running
diagnostics against the corpus for reasons nobody anticipated: the module count from a survey run for an
unrelated purpose, the `InputSize` result from a packing check, and the two wrong figures in §5.0 and
§5.3 from an audit that existed to check something else. **Fixtures assert what you already know.
Diagnostics find what you do not.** Retiring the second because the first is complete trades the
discovery instrument for the regression net, and this ADR needed both.

So the standing rule, which generalises past L5X:

- **Diagnostics** (`tools/l5x/`) are exploratory instruments. They are never verification artifacts, and
  no fix is ever confirmed by their output. They are maintained as long as they earn it. **If they drift
  from the adapter, that is a finding, not a defect** — the drift is information about one of the two.
- **Checks** (`tools/l5x_role_audit.py`, `tools/l5x_fixture_teeth.py`) are a different category and run
  in CI. A role-table entry silently becoming wrong is precisely what they exist to catch, and it will
  not announce itself: a wrong role drops an operand out of the denominator rather than failing to
  resolve, so extraction degrades while every headline metric holds steady.
- **Fixtures** are the regression net and the only thing that confirms adapter behaviour.

The surveys are deliberately *not* in CI. Finding something new is not a pass/fail condition, and a
gate that fires on discovery would train people to silence it.

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
| BOOLs pack into 32-bit words in declaration order in an I/O assembly | Computed vs declared `InputSize`, 44 connections | **Untestable this way — see the next row.** The 14/44 agreement with a clean 1–15 vs 41–123 BOOL boundary looked decisive and was an artifact |
| `InputSize` describes the member list, so it can test a packing rule | Grouping by connection datatype name | **False, and it retracts the row above.** `InputSize` is a per-family constant: 18 datatype names, zero size conflicts, and assemblies of 17, 52 and 84 members all declare 68 bytes. It is the CIP connection buffer, not the decoded structure. The packing rule is **undetermined, not falsified**, and layout must come from an EDS file or a live capture |
| The corpus is the second source for the ADR's figures | Cross-source audit (§5.3) | **False for two of thirteen.** "9,110 write positions" and "100.000% tag resolution" were both produced by non-adapter implementations and neither describes what ships. Both sat beside a correct figure that agreed downstream |
| There are two implementations of this parse | The audit finding a third | **False.** The survey, the adapter, and a one-off measurement script. Every figure must name its producer, and a figure not produced by the adapter is not a claim about the system (§5.0) |
| An audit script is trustworthy because it is an audit | Its own first run | **False.** Globbing `*.L5X` and `*.l5x` on a case-insensitive filesystem returns every file twice and doubled every count. Caught only because the numbers looked wrong, not because the tool complained — a checking tool is just another implementation and gets audited like one |
| Diagnostics can retire once every fact they established has a fixture | This session | **False, and the condition is withdrawn (§5.5).** Three of four findings came from running diagnostics for unanticipated reasons. Fixtures assert what you know; diagnostics find what you do not. The real problem was treating survey output as verification, and the fix for that is not deleting the survey |

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

**Two figures have no independent second source** (§5.3): operand totals and distinct tags referenced.
The AOI arity rule, previously the third and the only one with consequences attached, is now measured
adapter-side at 418/418. The remaining two are the most likely place for the next module-shaped bug.

**Class 1 I/O decoding needs a capture before it needs a decoder** (§5.4). The export carries the
semantics — 58 of 58 connections give member names, datatypes and declaration order — and does not
carry the layout. `InputSize` cannot supply it, being a per-family connection-buffer constant rather
than a description of the member list. Byte offsets come from an EDS file or from observing real
traffic against known tag values, so the tap is the prerequisite rather than the validation step.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] Implement the L5X stub into real extraction — `src/adapters/l5x_adapter.py` plus the instruction model in `src/adapters/l5x_instructions.py`. Symbols for programs, routines, AOI definitions, parameters, local tags, tags and modules; edges for reads, writes, AOI and JSR calls, alias-to-module-I/O, and ownership.
- [x] Curated conformance fixtures — **11** hand-authored synthetic L5X under `tests/fixtures/conformance/l5x/`, scoring 1.000/1.000 on symbols and edges (81 symbols, 93 edges). Teeth verified by script: each fix is reverted in memory and the intended fixture — and only that one — must fail. Eight reversions checked, seven caught; the eighth is unguardable and is recorded as such in §5.2.
- [x] Guards the conformance harness cannot express — `tests/test_l5x_adapter.py`, 12 tests: chunk-body content, chunking policy, loud failure on dropped elements, operand-slot positioning, and corpus-independent operand-role table invariants.
- [x] `tools/l5x_role_audit.py` — the operand-role audit promoted to a permanent, corpus-facing check with a third predicate the informal version lacked. Exits non-zero on any prediction failure. A *check*, not a diagnostic (§5.5); it can never become a fixture because it needs real controllers.
- [x] CI job `l5x-table-integrity` — runs `l5x_fixture_teeth.py --check` and the adapter guards. Both are corpus-free. The surveys under `tools/l5x/` are deliberately **not** in CI: discovery is not a pass/fail condition. Verified the gate fails by injecting a correction with no guard and confirming exit 1.
- [x] Adapter-side AOI arity counters, closing the one audit blank with consequences attached (786 write edges). 418/418, zero mismatches, measured by the implementation that ships.
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
