# ADR-008: Measured Conformance & Edge Confidence — Turning the Accuracy Claim into a Reported Number

**Status:** accepted (2026-07-17) — Phase 1 built and merged across PRs #13, #15, #17, #18, #26: the extraction precision/recall scorecard (`tools/conformance_eval.py`, §1–§3) plus C#/C++ fixtures, and §4/§5 graded `Edge.confidence` with a tunable `EDGE_CONFIDENCE_FLOOR`. The harness drove two real Python adapter fixes (extends edges, aliased imports) — py+ts now score 1.00/1.00. **Still open (checkboxes below, not blockers):** the precision/recall-vs-floor curve, and B4 execution-verified ground truth (Phase 2). *Status corrected 2026-07-27 — was `proposed` for ten days after merge.*
**Date:** 2026-06-18
**Branch:** `feature/adr-008-measured-conformance-edge-confidence`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **harness pattern** (fixture → run → metric → committed baseline) so the extraction precision/recall arm is a sibling of the retrieval arm, not a parallel invention.
- ADR-003 — introduces the **`Edge.candidate: bool` field** (§2.3, for C++ overload sets); this ADR **evolves** that boolean into a graded `confidence: float`.
- ADR-017 — needs the **language-tier model** (Tier-A fitting adapter vs Tier-B generic fallback) so precision/recall can be reported per tier; ADR-017 §3 is the `candidate` field's second consumer (Tier-B edges).
- ADR-023 — installs the **boolean three-state verdict + safe-direction rule** (ADR-017 §7) in the verdict tools. §4 here *reparameterizes that rule from the boolean to a confidence floor* (`instead of the boolean`), so **ADR-023 must land first**; this ADR then swaps the gating key.
**Depended on by:**
- ADR-011 *(planned — docs/adr-backlog.md)* — High-Precision Call Resolution emits **graded-confidence edges using the `Edge.confidence` field defined here** (shared field, A3), and its correctness is *measured by this ADR's* precision/recall harness. **Pairs with** this ADR.
- ADR-012 *(planned)* — Cross-Repository/Cross-Service Graph marks cross-service edges with the **`Edge.confidence`** introduced here.
- ADR-013 *(planned)* — DSL/industrial adapters reuse this ADR's **per-feature conformance machinery** (feature-tagged fixtures + precision/recall) as their acceptance suite.
- ADR-006 *(graph-analytics)* — its community detection will, once edges carry confidence, **gate on a confidence floor** (ADR-006 §Context, A3 coupling); until then ADR-006 is EXTRACTED-only.
- ADR-007 *(retrieval harness)* — its planned **internal-repo eval** (ADR-007 §9 — the complement that covers C#/C++ and the structural-graph layer CoIR cannot) reuses this ADR's **feature-tagged fixture + precision/recall machinery**.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-008 bucket + build kit) and
> [prior-art-depth-over-breadth.md](../prior-art-depth-over-breadth.md) (whole doc). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

The depth-over-breadth thesis is the project's moat: support fewer languages, but *prove* the
structure. Today "prove" stops at a passing conformance suite — a binary, per-adapter gate. We can say "the
C++ adapter passes its fixtures," but we cannot say "our call-edge extraction is 0.92 precision / 0.78
recall on Python." A competitor can *claim* 66 languages (the corrected figure — see the language-count
correction in `adr-backlog.md`; their own README's "150+" is unsubstantiated); we want to **report a number
they cannot**, because reporting it requires the conformance machinery they don't have.

The research (prior-art-depth-over-breadth, whole doc; [2] Total Recall, [3] Judge/CATS, [6] Deblometer)
converges on the same point: in code intelligence, a *wrong* edge is worse than a *missing* one, and the
only credible accuracy story is measured precision/recall against ground truth — paired with a policy of
**preferring `unknown` over a confident-but-wrong answer**. Two things are missing to make that real:

1. **A measurement harness for extraction** — precision/recall over edges and symbols, per language and per
   tier, not a pass/fail snapshot. This is the *extraction* sibling of ADR-007's *retrieval* harness.
2. **Graded edge confidence — evolving the `candidate` boolean.** The `Edge.candidate: bool` flag is
   introduced upstream (ADR-003 §2.3, for C++ overload sets; consumed by Tier-B in ADR-017 §3) — a binary
   "name-based / unresolved" marker, enough to firewall unverified edges from verdicts but too coarse to
   *tune* a prefer-unknown policy or to let ADR-011's resolver express "0.9 sure" vs "0.4 sure." This ADR
   **evolves** that boolean into a graded `confidence: float | None`, additive on the real `Edge` (which is
   `source_fqn / target / kind / resolved_target` + `candidate`). *(Neither field is in the code yet —
   `candidate` lands with ADR-003/017; this is its Phase-2 graduation to a measurable float.)*

## Decision

Build the **extraction accuracy scorecard** to ADR-007's pattern, and evolve edges from a binary `candidate`
flag to a graded `confidence`. Publish a per-language / per-tier precision/recall table, and make the
"prefer unknown" stance a **measured, tunable confidence-threshold policy** rather than a slogan.

### §1 — Precision/recall conformance reporting (B1)

Extend the conformance suite from pass/fail to measured:
- **Precision** = correct emitted edges ÷ all emitted edges.
- **Recall** = correct emitted edges ÷ ground-truth edges.
- Reported **per language** and **per tier** (A vs B), because the thesis is precisely that Tier-A precision
  is high; a blended number would hide that. Symbols get the same treatment where ground truth exists.

Ground truth in Phase 1 is **hand-authored fixtures** (below). Execution-verified ground truth (B4) is the
heavier **Phase 2**, deferred.

### §2 — Feature-tagged micro-benchmarks (B3)

Add small fixtures, each **tagged with the language feature it exercises** (e.g. `python/decorators`,
`cpp/overload-set`, `ts/generics`) and declaring its **expected symbols and edges**. These are the
ground-truth source for §1. They are curated and feature-exercising — a deliberate counter to a large,
shallow, unlabeled corpus: we measure *what we claim to handle*, feature by feature, so a regression points
at a specific capability.

### §3 — The published accuracy table (B2)

A **per-language / per-tier precision/recall table**, auto-generated from §1 and committed to `README.md`.
This is the artifact the competitor cannot produce: not "N languages supported," but "here is the measured
precision and recall on each, refreshed by the harness." Auto-generation is required — a hand-maintained
table rots and becomes another unverifiable claim.

### §4 — Graded edge confidence (evolving `candidate: bool`)

Evolve the upstream `Edge.candidate: bool` (introduced by the **ADR-003 §2.3 amendment**, consumed by Tier-B
in ADR-017 §3) into a graded `Edge.confidence: float | None` on `src/adapters/base.py`:
- `None` → not scored / not applicable (default; additive).
- `1.0` → fully resolved / verified (the old `candidate=False`; e.g. a resolved import or an ADR-011 exact
  resolution).
- `(0, 1)` → graded; a Tier-B name-match or an ADR-011 partial resolution lands here.
- The legacy `candidate=True` maps onto **below the verdict floor** (§5) — semantics preserved, expressiveness
  gained.

Because `candidate` and `confidence` arrive on the same real `Edge` (`source_fqn / target / kind /
resolved_target` + the new field) and `candidate` ships first (ADR-003 amendment / ADR-017), this is a
**field migration with a defined mapping** (`candidate=True → below floor`, `False → ≥ floor / 1.0`), not a
from-scratch addition. `src/db.py` threads `confidence` through the edge write/read path (additive column,
default preserving). `src/MCPServer.py` verdict tools gate on the confidence floor instead of the boolean —
the same safe-direction rule **ADR-017 §7** establishes, now parameterized by a tunable threshold.

### §5 — "Prefer-unknown" as a measured, tunable policy (B5)

The "emit `unknown` rather than a wrong edge" stance becomes a **confidence-threshold policy** with a
default floor of **0.5**: below the floor, verdict tools return "insufficient — candidate-only" rather than
asserting. Because §1 measures precision/recall, the floor is **tunable against data** — we can show the
precision/recall trade-off as the floor moves, instead of asserting a single hard-coded behavior. This is
the moat made operational: the policy is a dial with a measured curve behind it.

### §6 — Phase 2 (deferred): execution-verified ground truth (B4)

Hand-authored fixtures (§2) are Phase 1. **Execution-verified** ground truth — deriving true edges from
actually running code / instrumented traces — is heavier and is explicitly Phase 2. Listed so it isn't
re-litigated, not committed here.

### §7 — Coverage & limits (current state, with remediation under consideration)

Stating what the number does *not* cover is part of "correctness over breadth" (Mantra 2) — the accuracy
table must never be read as a language's true precision. These are honest current-state limits, each with a
planned remediation under consideration.
- **Coverage is bounded by fixture authorship, not adapter capability (current state).** The table reports
  precision/recall only for languages and features that have an **authored fixture** (§2). A feature with no
  fixture is simply absent — so a row is "measured on the fixtures we wrote," never "the adapter's true
  precision on all of language X." Ground truth is hand-authored and deliberately feature-by-feature: a
  curated probe of *what we claim to handle*, not an exhaustive corpus.
  *Planned (under consideration):* grow the feature-tagged fixture corpus per language as adapters mature.
- **C#/C++ and the deeper layers appear only when fixtures exist.** Until fixtures cover them, those
  languages — and the structural-graph behaviour ADR-007 §9 also defers to the shared internal-repo eval —
  are unscored.
  *Planned (under consideration):* Phase 2 execution-verified ground truth (§6) for edges too costly to
  hand-author, feeding the same per-language table.
- **Labelling rule (now).** Published rows are labelled "measured on authored fixtures, {language/feature}" —
  never an unqualified "precision of the X adapter."

## Consequences

**Better:**
- The accuracy claim becomes a **committed, reproducible number** (per-language precision/recall) — the
  depth-over-breadth thesis made measurable and the one thing breadth-first competitors structurally cannot
  match.
- Graded confidence lets downstream ADRs (011 resolver, 012 cross-service) express *how* sure an edge is,
  and lets ADR-006 community detection eventually gate on a floor instead of a boolean.
- "Prefer unknown" stops being a slogan and becomes a tunable dial with a measured precision/recall curve.
- Reuses ADR-007's harness shape and ADR-004's safe-direction verdict logic — small net-new surface.

**Worse:**
- Authoring hand-ground-truth edges at scale is real labor (an Open Question); the feature-tagged approach
  bounds it but does not eliminate it.
- Evolving `Edge.candidate: bool` → `Edge.confidence: float | None` touches `base.py`, `db.py`, and the
  verdict tools; once `candidate` ships (ADR-003 amendment / ADR-017), a backfill maps `candidate=True →
  below floor` and `False → ≥ floor`. Both fields are additive, so this is a field migration with a defined
  mapping, not a destructive schema change.
- Threshold tuning is now a live knob — power, but also a parameter that must be documented and defended so
  it isn't quietly changed to flatter the numbers.

**Neutral:**
- The retrieval arm (ADR-007) and this extraction arm stay separate scorecards by design.
- Phase-2 execution-verified ground truth is deferred behind an explicit seam, not abandoned.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep pass/fail conformance | Yields a binary "passes," never a precision/recall number; can't be put on a README as a comparative claim. |
| Stop at the binary `candidate: bool` (ADR-003/017) without grading it | The boolean is enough to firewall verdicts, but too coarse to tune a prefer-unknown policy or carry ADR-011's graded resolution — it can't express "0.9 vs 0.4 sure." This ADR evolves it to a graded float for exactly that reason. |
| Confidence-scored *verdicts* surfaced to users (e.g. "radius 4.2 @ 0.7") | Less actionable than "3 verified + 5 to review." Confidence lives on the *edge* and drives gating; the user-facing verdict stays VERIFIED/ADVISORY/INSUFFICIENT. |
| One blended accuracy number | Hides per-language/per-tier variance — the exact thing the thesis needs visible. |
| Hand-maintained README accuracy table | Rots immediately and becomes another unverifiable claim; the table must be auto-generated from the harness. |
| Execution-verified ground truth now | Heavier (instrumentation/trace harness); correctly deferred to Phase 2 so Phase 1 ships. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 1 — measured conformance + graded confidence**
- [x] Evolve `Edge.candidate: bool` into `Edge.confidence: float | None` on `src/adapters/base.py`; mapping `candidate=True → below floor (0.25)`, `False → ≥ floor (1.0)` via `effective_confidence()`; threaded through `src/db.py` edge write/read (additive `confidence REAL` column + `_migrate_edge_confidence`) **and the recursive call-graph CTE** (`MAX(confidence)` alongside `MIN(candidate)`). **DONE 2026-07-17** — the upstream `Edge.candidate` field landed via ADR-017 P1 (#22) and ADR-023's three-state verdict via #23, unblocking this.
- [x] Verdict tools in `src/MCPServer.py` gate on the confidence floor (default 0.5) via the single `_caller_evidence` chokepoint (all four verdict tools route through it), reusing the ADR-017 §7 safe-direction rule. **DONE 2026-07-17** — behaviour-preserving under the derived mapping; a producer-graded candidate edge ≥ floor now correctly reads as verified.
- [x] Feature-tagged micro-benchmark fixtures in `tests/fixtures/conformance/` declaring expected symbols + edges. **DONE 2026-07-07 — first batch: 6 Python + 6 TypeScript (Tier-A), ground truth authored from source semantics (NOT parser echo — the integrity rule), path-normalized so fixtures are checkout-portable.**
- [x] ~~Extend `tests/test_adapter_snapshots.py`~~ → **DEVIATION: dedicated `tools/conformance_eval.py` (scorer, micro-averaged P/R for symbols / all-edges / call-edges) + `tests/test_conformance.py` (regression gate vs committed `benchmarks/conformance/baseline.json`).** Kept separate from the snapshot test on purpose: snapshots guard *drift* (golden==current), this measures *correctness* (vs independent ground truth); merging them muddies both. **DONE 2026-07-07 — 14 tests pass in ~5s, no GPU/model.**
- [x] Auto-generate the per-language precision/recall table into `README.md` (idempotent `<!-- CONFORMANCE:START/END -->` markers; `--write-readme`). **DONE 2026-07-07.**
- [ ] Show the precision/recall-vs-floor curve so the prefer-unknown threshold is a measured dial. **UNBLOCKED (2026-07-17) but not yet built** — the floor is now a single tunable constant (`EDGE_CONFIDENCE_FLOOR`) the §1 harness can sweep; the sweep/plot itself is a follow-up. Meaningful once ADR-011 emits real graded values (today only the coarse mapping populates confidence, so the curve is a step function).
- [ ] Resolve **Depended on by** obligations: confirm the `Edge.confidence` contract + verdict-floor semantics for ADR-011/012, the conformance machinery for ADR-013, and the floor for ADR-006, before `accepted`.

**2026-07-07 — harness immediately drove two adapter fixes (PR #13).** The scorecard's two named Python recall gaps were fixed in `src/adapters/python_adapter.py`: `extends` edges for subclassing (inheritance now reaches the graph layer, matching TS) and aliased-import capture (`import json as j`). Python all-edges recall **0.929 → 1.000**; full suite 120 passed, no snapshot drift; baseline + README regenerated. Also added a fast, model-free `conformance-scorecard` CI job (`--check-baseline`). This is the loop working as designed: build the ruler, measure, fix, re-measure.

**2026-07-07 — Phase-1 first slice (§1–§3) landed; §4/§5 blocked, not abandoned.** Buildable-now scope shipped: the ground-truth fixture format, the scorer, the committed baseline + pytest gate, and the auto-generated README table. Zero compute cost (tree-sitter parse only — the whole suite runs in ~5s on CPU, no embedder). **First measured numbers** (micro-averaged over the authored fixtures): Python — symbols **1.00/1.00**, call-edges **1.00/1.00**, all-edges **1.00/0.93**; TypeScript — **1.00/1.00** across symbols, edges, and call-edges. Precision is 1.00 everywhere: the adapters emit nothing semantically wrong on these fixtures. The two honest recall gaps are real, named adapter limitations surfaced by independent ground truth: (1) the Python adapter drops **aliased imports** (`import json as j`); (2) it emits no **`extends` edge** for subclassing (`class Dog(Animal)`) — whereas the TS adapter emits both `extends` and `implements`, which is why TS all-edges recall is 1.00. §4 (graded `Edge.confidence`) + §5 (tunable floor) are **blocked** on the upstream `Edge.candidate` field (ADR-003/017), which is not in the code — so they are explicitly deferred, not skipped. Deferred within Phase 1 too: C#/C++ fixtures (add once py/ts is proven) and per-tier splitting (all current fixtures are Tier-A). Files: `tools/conformance_eval.py`, `tests/test_conformance.py`, `tests/fixtures/conformance/{python,typescript}/*`, `benchmarks/conformance/baseline.json`, README table.

**2026-07-08 — C# conformance batch + harness hardening (Minor; branch `feature/adr-008-csharp-conformance`).** Classification: **Minor** per CONTRIBUTING §1 — touches `tools/`, `tests/`, `docs/`, `benchmarks/` only, **no `src/`** (the two adapter gaps below are encoded as ground truth, deliberately *not* fixed in `src/`, which would be a separate Major/ADR change). C# is now proven to parity with py/ts. **7 C# fixtures authored** (5 clean + 2 known-gap): `classes`, `inheritance`, `namespaces_usings`, `records_structs_enums`, `nested_types` (clean); `interface_impl_gap`, `filescoped_namespace` (known-gap). **Clean-set C# = 1.00/1.00** on symbols, all-edges, and call-edges; baseline + README regenerated to include the csharp row (5 clean fixtures). Conformance tests 14 → 47; full suite **153 passed** in ~10s, still no GPU/model.

- **Finding 1 — harness defect (`normalize_fqn`), fixed.** C# member FQNs are `Namespace.Type.Member/arity` with no `::`; the old normalizer fell through to `os.path.basename`, which splits on `/` and collapsed **every** C# method/constructor to its arity digit (`Compute/0` and `Build/0` both → `('0','method')`). This produced a *meaningless 1.00* that undercounts and masks recall misses (a dropped method hides behind a same-arity sibling). Never bit py/ts (they use `file::symbol`). Fixed to basename only genuine file paths; py/ts scores unchanged; pinned by a targeted regression test **and** a new key-distinctness invariant.
- **Finding 2 — adapter bug (undocumented), encoded as `known_gap`.** Under a **file-scoped namespace** (`namespace X;`, the .NET 6+ default) the adapter drops the namespace qualifier — symbols come out `Account` instead of `Ledger.Account`. Root cause: `_walk` treats `file_scoped_namespace_declaration` as if members were its children, but the tree-sitter grammar makes them *siblings*, so they are walked with `ns=None`. Since the FQN is the symbol's identity (bakes into stable IDs, ADR-003 D3), this zeroes out matching. Captured as the `filescoped_namespace` known-gap fixture with correct qualified ground truth. **Adapter fix is out of scope here (Major/`src/` → its own ADR/issue); flagged for filing.** The second gap, `interface_impl_gap`, is the *documented* base-list extends/implements limit (csharp_adapter docstring).
- **Harness hardening (§1 machinery).** (a) `known_gap` fixture metadata with a **required** `reason` + `ref`; (b) two **disjoint aggregates** — the clean set gates the committed baseline, the known-gap set is reported honestly and **never gates on a sub-1.0**, so a documented gap can't dilute or inflate the gated number; (c) **unexpected-pass alert** — a known-gap fixture that scores 1.0 (gap closed) fails `--check-baseline`; (d) **key-distinctness invariant** — any two distinct ground-truth or adapter-output keys collapsing to one normalized key fails CI, making the Finding-1 defect class structurally un-reintroducible. All three gates (baseline regression, key collision, unexpected pass) are wired into `--check-baseline`.
- **Docs.** New `docs/conformance-fixture-conventions.md` (symbol model + field-scoping rationale, FQN/arity notation + the same-name/same-arity overload-collision caveat, known-gap semantics, authoring integrity rule). Added the missing reciprocal cross-reference **ADR-003 → `Depended on by: ADR-008`** (the C# FQN D3 convention).
- **Still deferred:** C++ fixtures (next batch — adapter + `_LANG_BY_EXT` already wired); §4/§5 remain blocked on the upstream `Edge.candidate`/`confidence` field (ADR-003/017 unbuilt). Files: `tools/conformance_eval.py`, `tests/test_conformance.py`, `tests/fixtures/conformance/csharp/*`, `benchmarks/conformance/baseline.json`, `docs/conformance-fixture-conventions.md`, `docs/adr/ADR-003-*.md`, README table.

**2026-07-08 — C++ conformance batch + `normalize_fqn` C++-namespace fix (Minor; branch `feature/adr-008-cpp-conformance`, stacked on the C# branch).** Classification: **Minor** per CONTRIBUTING §1 — `tools/`, `tests/`, `docs/`, `benchmarks/` only, **no `src/`** (the adapter bug below is encoded as ground truth, not fixed here). C++ now proven to parity with py/ts/C#. **Authored by the `conformance-fixture-author` subagent** (first live use of the ADR-008 delegation agents); the sub-1.0 fixture was triaged by `conformance-gap-investigator`. **6 C++ fixtures** (5 clean + 1 known-gap): `classes`, `namespaces`, `inheritance`, `structs_enums`, `free_functions` (clean); `templates` (known-gap). **Clean-set C++ = 1.00/1.00** on symbols, all-edges, call-edges; baseline + README regenerated (cpp row = 5 clean fixtures). Conformance tests 48; full suite **166 passed** in ~11s, still no GPU/model.

- **Finding 1 — harness defect (`normalize_fqn`), fixed — the C++ analog of the C# `/arity` collapse.** C++ FQNs use `::` as the *namespace* separator (`shop::Order`, `shop::Order::compute(int)`) and carry no path prefix; the normalizer's `::`-split (meant to strip a `<path>::` prefix) ate the namespace off *every* C++ symbol (`shop::Order` → `Order`), collapsing distinct symbols and masking recall — a meaningless 1.00, exactly the failure mode the C# fix guards against. Fixed to treat `::` as the path delimiter only when its left side looks like a filesystem path (separator or source extension); a bare-identifier left side is a namespace and is preserved. py/ts/C# scores unchanged (`--check-baseline` exit 0); pinned by `test_normalize_fqn_cpp_namespace_preserved`. **Found by probing the adapter before spawning the author** — a harness (`tools/`) fix is the orchestrator's serialized responsibility, not the author's.
- **Finding 2 — adapter bug (undocumented), encoded as `known_gap`.** A call at an **explicit-template-argument** site (`maxOf<int>(box.get(), 10)`) should emit an ordinary call edge to the (already-indexed) callee; the adapter drops it because tree-sitter-cpp wraps the call target in a `template_function` node that `_CALL_QUERY` doesn't match. The investigator confirmed this is **NOT** the docstring's "template instantiations are invisible" blind spot (that concerns instantiation-as-symbol-generation, `Box<int>` → no symbol) — the author's original citation was scope-creep — and found the bug is **broader** than first hypothesized: the *qualified* form (`std::make_shared<int>(...)`, via `qualified_identifier name: template_function`) is also missed, so the real fix needs **two** `_CALL_QUERY` alternatives. Recall-only, no FQN/identity corruption (strictly weaker than the C# file-scoped bug). Captured as the `templates` known-gap fixture with correct ground truth. **Adapter fix is out of scope here (Major/`src/` → its own ADR/issue); filed as issue #16 (ref updated from the scope-creep citation to the accurate undocumented-bug description).**
- **Still deferred:** §4/§5 remain blocked on the upstream `Edge.candidate`/`confidence` field (ADR-003/017 unbuilt). Recommended follow-up folded into the eventual C++ call-query fix PR: add a qualified explicit-template call (`std::make_shared<int>(...)`) to the `templates` fixture so the qualified branch of the fix is regression-covered. Files: `tools/conformance_eval.py`, `tests/test_conformance.py`, `tests/fixtures/conformance/cpp/*`, `benchmarks/conformance/baseline.json`, `docs/conformance-fixture-conventions.md`, README table.

**2026-07-08 — adapter extraction fixes closing the two conformance gaps (Major; branch `fix/adr-008-adapter-extraction-gaps`, stacked on the C++ branch).** Classification: **Major** per CONTRIBUTING §1 — touches `src/adapters/`. Governing ADR = this one (ADR-008): these are the measure→fix loop closing the two gaps its scorecard surfaced, same pattern as the 2026-07-07 Python fixes in PR #13. Both known-gap fixtures authored in the C#/C++ batches are the regression anchors and now score a clean 1.00; the harness unexpected-pass alert is what flags "gap closed → remove the marker." The only remaining known-gap is `interface_impl_gap` (the documented C# base-list limit). Full suite **166 passed**; `--check-baseline` exit 0; C#/C++ snapshot goldens unaffected (no drift). Baseline + README regenerated (csharp 6 clean, cpp 6 clean).

- **Fix — issue #14, C# file-scoped namespace (`src/adapters/csharp_adapter.py`).** `_walk` treated `file_scoped_namespace_declaration` as a container and recursed *its* children, but the tree-sitter-c# grammar makes the type declarations **siblings** of that node under `compilation_unit`, so the real types were walked with `ns=None` → unqualified FQNs (`Account` not `Ledger.Account`), zeroing out identity. Fixed by handling the file-scoped node in the child-iteration loop: passing it sets the namespace for every subsequent sibling (there is at most one, and it precedes all types). Block-form `namespace { … }` (its own `declaration_list`) and the separate `_USING_QUERY` import extraction are untouched. `filescoped_namespace` fixture 0.58/0.45 → **1.00/1.00**.
- **Fix — issue #16, C++ explicit-template-argument call sites (`src/adapters/cpp_adapter.py`).** `_CALL_QUERY` didn't match the `template_function` call-target node tree-sitter-cpp emits for `foo<T>(...)` / `std::make_shared<T>(...)`, silently dropping those call edges (and the matching `CALL` references, same query). Fixed by adding two alternatives — bare `(template_function name: (identifier) @name)` and qualified `(qualified_identifier name: (template_function name: (identifier) @name))` — capturing the callee as its bare final identifier (template args stripped), matching existing call-target notation. Docstring blind-spot list clarified (template-arg *call sites* are captured; only instantiation-as-symbol-generation stays invisible). `templates` fixture 1.00/0.75/0.50 → **1.00/1.00/1.00**. No over-match (C++ snapshot golden unchanged).

**2026-07-17 — §4/§5 implemented (Major; branch `feature/adr-008-graded-edge-confidence`).** Unblocked by this session's merges: `Edge.candidate` landed via ADR-017 P1 (#22) and ADR-023's three-state verdict via #23, which §4 reparameterizes from boolean to confidence floor. Touches `src/adapters/base.py` (`Edge.confidence` field), `src/db.py` (`confidence REAL` column in both `edges` CREATE sites + `_migrate_edge_confidence`; `EDGE_CONFIDENCE_FLOOR=0.5` + `effective_confidence()`; the recursive `_CALL_GRAPH_SQL` threads `confidence` per-hop and aggregates `MAX` alongside the existing `MIN(candidate)`; `CallGraphNode.confidence`), and `src/MCPServer.py` (`_caller_evidence` gates on `confidence < floor` — the single chokepoint all four verdict tools share). **The migration is behaviour-preserving:** with only ungraded edges, floor-gating gives the identical verified/candidate split as the old boolean (test-pinned). It diverges exactly when a producer grades a candidate edge at/above the floor — the point of §4 — which ADR-011 will exercise. Tests: `tests/test_edge_confidence.py` (7) — mapping, migration on an old edges table, CTE derivation, graded-candidate-passes-floor, MAX best-path, behaviour-preservation. Full suite **203 passed**, `src` flake8 clean, no GPU/model. Deviation: legacy `candidate=True` maps to `0.25` (a named sub-floor constant, not literally "0") so a name-match reads as low-but-nonzero confidence. **Follow-up for ADR-011:** it now has a real `Edge.confidence` field + a floor contract to emit graded values against.

**Phase 2 — deferred**
- [ ] B4 execution-verified ground truth.

**Notes:**
<!-- 2026-06-18: The moat bucket. Renumbered behind ADR-007 (the design doc tentatively called this "ADR-007"). Defaults: precision = correct÷emitted, recall = correct÷ground-truth, floor = 0.5, ground truth = hand-authored fixtures (Phase 1). Edge.confidence(float|None) EVOLVES the Edge.candidate:bool introduced by the ADR-003 §2.3 amendment (consumed by Tier-B ADR-017 §3); shared with ADR-011. candidate=True → below floor; verdict safe-direction rule lives in ADR-017 §7. Open: authoring ground-truth edges at scale; threshold tuning. -->
