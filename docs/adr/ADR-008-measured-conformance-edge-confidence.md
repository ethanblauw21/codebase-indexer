# ADR-008: Measured Conformance & Edge Confidence — Turning the Accuracy Claim into a Reported Number

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-008-measured-conformance-edge-confidence`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **harness pattern** (fixture → run → metric → committed baseline) so the extraction precision/recall arm is a sibling of the retrieval arm, not a parallel invention.
- ADR-003 — introduces the **`Edge.candidate: bool` field** (§2.3, for C++ overload sets); this ADR **evolves** that boolean into a graded `confidence: float`.
- ADR-017 — needs the **language-tier model** (Tier-A fitting adapter vs Tier-B generic fallback) so precision/recall can be reported per tier; ADR-017 §3 is the `candidate` field's second consumer (Tier-B edges).
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
- [ ] Evolve `Edge.candidate: bool` (from the ADR-003 amendment / ADR-017) into `Edge.confidence: float | None` on `src/adapters/base.py`; backfill `candidate=True → below floor`, `False → ≥ floor`; thread through `src/db.py` edge write/read (additive).
- [ ] Verdict tools in `src/MCPServer.py` gate on the confidence floor (default 0.5), reusing the ADR-004 §7 safe-direction rule.
- [ ] Feature-tagged micro-benchmark fixtures in `tests/fixtures/` declaring expected symbols + edges.
- [ ] Extend `tests/test_adapter_snapshots.py` to compute per-language / per-tier precision/recall against those fixtures (ADR-007 harness pattern).
- [ ] Auto-generate the per-language precision/recall table into `README.md`.
- [ ] Show the precision/recall-vs-floor curve so the prefer-unknown threshold is a measured dial.
- [ ] Resolve **Depended on by** obligations: confirm the `Edge.confidence` contract + verdict-floor semantics for ADR-011/012, the conformance machinery for ADR-013, and the floor for ADR-006, before `accepted`.

**Phase 2 — deferred**
- [ ] B4 execution-verified ground truth.

**Notes:**
<!-- 2026-06-18: The moat bucket. Renumbered behind ADR-007 (the design doc tentatively called this "ADR-007"). Defaults: precision = correct÷emitted, recall = correct÷ground-truth, floor = 0.5, ground truth = hand-authored fixtures (Phase 1). Edge.confidence(float|None) EVOLVES the Edge.candidate:bool introduced by the ADR-003 §2.3 amendment (consumed by Tier-B ADR-017 §3); shared with ADR-011. candidate=True → below floor; verdict safe-direction rule lives in ADR-017 §7. Open: authoring ground-truth edges at scale; threshold tuning. -->
