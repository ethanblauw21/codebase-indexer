# L5X Retrieval Requirements — What Controls Engineers Actually Look For

Findings from structured elicitation with practicing controls engineers, derived for ADR-013 (L5X
adapter). This is the *requirements* half of the gold-query gate recorded in ADR-013 §8: the half
that describes what engineers search for and what shape the answer takes. It is **not** the gold-query
set itself, and it does not close that gate.

**Redaction.** Engineers answer with the equipment, device tags, customer projects and colleagues they
actually worked with, so the raw responses are customer data under the same rule as the corpus itself.
They live in `benchmarks/real_repo/private/`, gitignored at `.gitignore:78`, following the ADR-019 §6
private-slice precedent. This document carries only the derived findings, with identifiers removed.
Do not re-introduce them here.

> **Status: direction, not settlement.** Session 1 was **n = 1**, and the respondent was also the
> author of this tool. The plan calls for 2–3 engineers and 30–50 fixtures minimum to learn anything,
> ~100+ to settle a decision. Nothing below settles a decision. See [Confidence](#confidence).

## Method

The elicitation instrument deliberately does not ask engineers to invent queries — engineers asked for
queries invent plausible ones. It asks about the last concrete time instead: what were you looking
for, what do you check first on a support call, what do you read in unfamiliar code, what did you have
to ask a colleague, and how did you know the answer was right. The last question is designed to
produce the `gold` array; if it cannot be answered crisply, the query is unscorable and does not enter
the set.

## The five findings

### F-1 — Some queries are answered by a tag declaration, not a routine

An engineer looking for a named piece of process equipment goes to the controller tag list and filters
it. The answer is **a set of controller-scoped `tag` declarations**. No routine is involved.

This bears directly on the open question in ADR-013 §8, which leaves `tag` chunkable and says only a
gold query can settle it. This is the first query anyone has actually asked whose honest best answer
is a tag declaration, and it argues for keeping `tag` chunkable.

**The interesting part is the failure mode, not the success.** The search took three attempts, because
one machine appears under a long name, a short name, and an initialism. Dense retrieval bridges that
only when the tag `Description` carries the long form — and the corpus has descriptions on **1,564 of
2,326 tags (67.2%)**. On the undescribed third there is nothing to embed but the identifier, and the
tool degrades to the substring filter Studio 5000 already provides. The honest experiment is to run
such a query against equipment whose tags fall in that third, rather than assuming the win.

### F-2 — The daily fault-tracing questions are one-hop lookups, not graph traversals

The first move on a support call is one of three, and all three have the same shape:

| Entry point | Question | Answer unit |
|---|---|---|
| Alarm number from the operator alarm screen | what conditions could set this alarm | the rung(s) writing the alarm bit |
| Device tag | what activates this device | the rung(s) writing the device output |
| Process step number | what advances this step | the rung(s) writing the step transition |

**This corrects a prediction in the elicitation plan.** The plan expected these and classified them as
the trap — "what writes this tag" as a `graph-only` query, unscorable under the shipped RRF default
per ADR-019, to be captured as requirements for future graph work rather than as retrieval fixtures.

That conflated two operations. *What writes X* is a **one-hop exact lookup**: return the positions
where X occupies a destination operand. The adapter already resolves writes at **11,379 / 11,379** and
emits **3,868 write edges**. It is a deterministic filter over a relation that already exists.
Nothing is ranked, so RRF hop-decay — the mechanism that prevented ADR-019's graph-only fixtures from
registering a B−A lift — never applies.

The interaction model engineers described is explicitly iterative: find what drives the tag, then
repeat from there. **One hop at a time, with a human pruning between hops.** That also disposes of the
fan-out hazard ADR-013 records — one corpus file has a tag written 177 times, and a naive automatic
backward trace reaches whole-controller fan-out within two hops. A human pruning each hop never gets
there. Multi-hop automatic slicing remains out of scope and nothing here asks for it.

**Scope boundary.** Engineers distinguish enumerating the candidate conditions from identifying which
one is currently true; the second requires being online with the PLC. A static index can do the first
and can never do the second. The first is also the half performed on the phone without a laptop
connected, which is where the time is lost.

**Secondary contribution:** the plan required fixtures whose honest answer is a single rung and had no
source for them. F-2 supplies three classes.

### F-3 — First contact with an unfamiliar controller is a structural profile, not a search

Before searching anything, engineers triage: how many routines, how many rungs in each, whether
routine names are granular or monolithic, whether tags carry descriptions, whether tags are named
consistently, whether rungs carry comments. Only then do they open the routine they need.

Every one of those quantities is already computed by the Phase-0 survey — 209 program routines
(183 RLL / 22 ST / 4 FBD) across 35 programs; rungs per routine median 4, mean 9.9, p90 29, max 68;
1,168 of 3,935 rungs commented (29.7%); 1,564 of 2,326 tags described (67.2%), 910 UDT-typed.

**This is a report, not a retrieval feature, and it is gated on nothing.** Not gold queries, not
ranking, not the graph.

The one dimension not currently measured is naming quality, which is a judgment call. Reporting the
distribution — token counts, separator conventions, prefix families — and letting the reader judge is
more honest than scoring it.

### F-4 — Silent AOI drift is a real, recurring, mechanically detectable failure

The stated problem was cross-archive search: *which past project has an example of this device?* That
is worth noting on its own, because **it is a corpus scope nothing has been built for.** Every
ADR-013 measurement to date is within-file — whole-controller exports, in-file resolution, JSR targets
scoped to the owning program. Search across an archive of controllers is a different problem and
should not be assumed to fall out of the existing work.

**But the cost was not the search.** It was finding several copies of one Add-On Instruction at the
*same declared revision* with *different logic*, with no way to tell the master from a local
customization — no revision bump, no comment recording the change, no naming convention marking a
fork.

This is mechanically detectable with what the adapter already extracts. An `AddOnInstructionDefinition`
is a first-class symbol with a typed signature whose logic routine is chunked like any other. Hashing
the normalized definition body yields: *N controllers declare this AOI at revision R; here are the K
distinct implementations, grouped.* No embeddings, no ranking, no gold queries, no graph traversal —
a group-by over an existing extraction.

Design constraints:

- Hash the **normalized** body. Whitespace, attribute order and BOM all vary, and ADR-013 already
  flags BOM as a stable-ID hazard.
- Hash the **signature separately from the body.** A changed parameter list is a different and worse
  problem than a changed rung; one combined hash loses that.
- AOIs hold **2,156 of 3,935 rungs — 55% of all ladder logic** in the corpus. Drift here is not a
  corner case.
- 12 of 61 definitions are never invoked in-file. *Dead in this controller* is a third state alongside
  master and fork, and is probably worth reporting.
- Declared revision is a **claim, not evidence.** The entire finding is that the claim was false.

**This needs a conformance fixture, not a gold query:** two synthetic AOIs, same name, same revision,
one rung different, asserting the report separates them.

### F-5 — Relevance across programs is a directional equipment profile, and it did not yield a gold array

The question designed to produce the `gold` array **did not produce one**, and the plan's own rule
applies: what cannot be answered crisply cannot be scored. Recorded as a miss rather than
reinterpreted into a fixture.

What it produced instead is a relevance criterion over *whole programs*, with two parts:

1. **Provenance** — did I write it, or someone whose work I trust.
2. **Degree of difference**, counted in equipment. Reuse of a past program as a template was judged by
   comparing counts per equipment class: pumps at one stage, chemical feeds, tanks at another stage,
   and a treatment stage present in the target and absent from the template.

**The asymmetry is the sharp part.** Expanding a template — needing three of something where it has
two — is duplicate-and-tweak. Contracting one is delete-and-verify-nothing-dangled, and is
substantially worse. So the similarity function is **not** `|difference|`: a candidate with *more* of
a thing than required outranks one with *fewer*. A naive distance metric gets this backwards on half
the axes.

Relevance is also **sometimes satisfied by a set** rather than a single best match — one program
covering some axes, a second covering the rest. The current harness scores a single `gold` array per
query and has no shape for that.

**The requested feature, and how much of it is mechanical.** Engineers want an executive summary
attached to each archived program — a domain tag plus a table of equipment counts — so that archive
search does not depend on remembering what colleagues built recently.

The counts are largely derivable: **AOI instance counts are the equipment bill of materials.** The
corpus has 418 AOI call sites, all with resolved arity (418/418 against the
`1 + count(Required params excluding EnableIn/EnableOut)` rule — the strongest measured result in
ADR-013). Three instantiations of a pump AOI is three pumps, read off the call sites with no NLP and
no dependency on naming or description coverage.

Three parts are **not** mechanical, and promising them would be dishonest:

- **Equipment classes with no dedicated AOI.** A vessel may be a UDT-typed tag or a program rather
  than an AOI instance. The fallbacks — tag-type counts, program names — are naming-dependent, which
  is F-1's unsolved problem again.
- **The domain tag.** Nothing in an L5X export identifies the process domain. That is human-supplied
  metadata applied at archive time.
- **Provenance.** L5X owner/edit metadata is inconsistent and records who last *exported*, not who
  authored the logic. Criterion 1 above likely needs the archive system, not the parser.

## What this unblocked — built 2026-08-26

Three items followed from these findings, **none gated on gold queries, ranking, or the graph.** All
three are built. What each one measured on the corpus is below, and two of the measurements are
findings in their own right.

### 1. Rung-level provenance — the gap that had to be closed first (F-2)

A `writes` edge names the **routine**, and the rung number was being read for chunk text and then
discarded before edges were emitted. `self.references` was initialized, returned, and never appended
to: there was no rung-level provenance in the index at all. With routines at a median of 4 rungs but
p90 29 and max 68, "it is written in this routine" stops being an answer well before the tail.

The scan now threads the rung through and emits a `Reference` per resolved read, write and call.
**16,703 references on the corpus — 5,848 WRITE, 10,293 READ, 562 CALL, and zero rungs without a
usable number.** Symbols and edges are byte-identical to what this ADR already records (6,011 and
13,316, same per-kind breakdown), which is the check that makes it a pure addition.

Two things to know about the stored value. `line` carries the **rung number**, not a source line —
an L5X export is one XML document per controller and its line numbers mean nothing to an engineer,
while `[12]` is already how the chunk body addresses a rung; `ref_kind` is what tells a consumer
which it is holding. And the rung number is a **positional coordinate, not an identity**: inserting a
rung renumbers every rung after it, so it is good for "go look here" and must never feed a stable ID.

### 2. `what_writes(tag)` — the one-hop lookup (F-2)

An MCP tool reading resolved `writes` edges straight out of the graph, served by the existing
`idx_edges_target_kind`. No embedder, no ranking, no reranker: the result is complete rather than
top-k. It reports each writing routine with its rung numbers, flags aliases as hardware-driven, warns
when more than one routine writes a tag (last write in scan order wins), and ends by naming the next
hop rather than taking it.

**It was deliberately not built on `trace_data_flow`.** That tool is hardcoded to Firebase and
TypeScript idioms and detects producers by regex over ranked search results — the path ADR-019 showed
cannot reliably surface structural nodes. For a relation resolved at 11,379/11,379, ranking could only
lose writers.

### 3. Controller profile (F-3)

`tools/l5x_controller_profile.py`, with a `--redact` mode that placeholders every name so output is
safe to paste. Size, routine-type split, rungs-per-routine distribution, share of ladder inside AOIs,
description and comment coverage, tag-naming shape, and the largest routines with a monolith flag.
Naming quality is reported as a distribution and deliberately not scored.

**Finding: description coverage is far more uneven than the pooled figure suggests.** The corpus-wide
67.2% is an average over controllers ranging from **25.7% to 88.7%**. F-1 predicts the tool degrades
to substring matching where descriptions are absent, so this is not a cosmetic spread — it says
retrieval quality will vary by controller more than by query, and the pooled number hides exactly
that.

### 4. AOI drift detection (F-4)

`tools/l5x_aoi_drift.py`, also with `--redact`. Signature and logic are hashed separately — a changed
parameter list breaks every call site positionally and is a worse finding than a changed rung, and one
combined hash could not tell you which you had. Comments are excluded from the logic hash but a
comment-only difference is still reported.

**Finding: the reported incident is not an isolated one. 13 of 34 distinct AOI names carry drift
across just 5 controllers — 11 logic drift, 2 signature drift**, against 2 consistent and 19 appearing
only once. In the worst case one AOI is declared at a single revision in three controllers with **75
parameters in two of them and 83 in the third.** Since AOI call sites bind positionally, that is not a
cosmetic difference: a call site copied between those two projects is silently mis-slotted.

This does not affect extraction — the adapter binds against each file's own definition, so the
418/418 arity result stands. It affects **reuse**, which is what the engineer was doing when it cost
them time.

## Still not built

The program facet table (F-5) is partly mechanical via AOI instance counts and partly requires
human-supplied metadata. It should not be started until the mechanical/manual split above is
accepted, or it will silently over-promise.

## What remains gated

**The gold-query set is still empty, and everything ADR-013 §8 gates on it is still gated:** ranking,
chunk assembly, the `tag`-chunkability decision, and any claim that routine-as-chunk-unit is right.
F-1 argues for tag chunkability and F-2 supplies rung-level answer units, but arguing is not
measuring — settling either on reasoning alone would repeat the mistake that produced the
write-position table.

The follow-up that yields the most per unit of engineer time: for two or three specific recent support
calls, capture the entry identifier **and the routine and rung that turned out to be the answer.**
That is three scorable fixtures in the query class the daily workflow actually uses. The remaining
follow-ups are recorded in the private file.

Also still outstanding, unchanged and unrelated to this session: one more `SoftwareRevision 36.04`
export, which would settle whether the mnemonic spelling split is a version behaviour or an authoring
habit; and one fragment export (`ContainsContext="true"`), which the adapter refuses loudly and has
never actually seen.

## Confidence

**n = 1, and the respondent authored this tool.** That is the load-bearing caveat. F-1 landed exactly
on ADR-013's open `tag`-chunkability question and F-2 landed exactly on a relation the adapter already
extracts at 100% resolution. Those are either genuine convergence or an author unconsciously answering
toward what the tool can already do, and a single self-interview cannot distinguish the two.

The distinguishing test is cheap: if an engineer with no stake in the codebase independently reaches
for the one-hop write lookup as their first move on a support call, F-2 is real. If they do not, F-2
was the author describing his own design back to himself. **Weight F-2 accordingly until a second
respondent is interviewed.**

F-3 and F-4 are less exposed to this bias — F-3 is a description of behaviour that precedes any tool,
and F-4 is a documented incident that cost multiple people time before this project existed.

Answers were given in one pass with no follow-up probing. Where they are thin — F-5's missing gold
array above all — that is partly an artifact of the format rather than the respondent.
