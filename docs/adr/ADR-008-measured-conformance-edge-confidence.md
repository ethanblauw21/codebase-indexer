# ADR-008: Measured Conformance & Edge Confidence — Turning the Accuracy Claim into a Reported Number

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-008-measured-conformance-edge-confidence`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **harness pattern** (fixture → run → metric → committed baseline) so the extraction precision/recall arm is a sibling of the retrieval arm, not a parallel invention.
- ADR-004 — needs the **tier model** and the **`candidate`-edge mechanism** (ADR-004 §3); this ADR evolves the `candidate` boolean into a graded `confidence`.
**Depended on by:**
- ADR-011 *(planned — docs/adr-backlog.md)* — High-Precision Call Resolution emits **graded-confidence edges using the `Edge.confidence` field defined here** (shared field, A3), and its correctness is *measured by this ADR's* precision/recall harness. **Pairs with** this ADR.
- ADR-012 *(planned)* — Cross-Repository/Cross-Service Graph marks cross-service edges with the **`Edge.confidence`** introduced here.
- ADR-013 *(planned)* — DSL/industrial adapters reuse this ADR's **per-feature conformance machinery** (feature-tagged fixtures + precision/recall) as their acceptance suite.
- ADR-006 *(graph-analytics)* — its community detection will, once edges carry confidence, **gate on a confidence floor** (ADR-006 §Context, A3 coupling); until then ADR-006 is EXTRACTED-only.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-008 bucket + build kit) and
> [prior-art-depth-over-breadth.md](../prior-art-depth-over-breadth.md) (whole doc). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

The depth-over-breadth thesis (ADR-004) is the project's moat: support fewer languages, but *prove* the
structure. Today "prove" stops at a passing conformance suite — a binary, per-adapter gate. We can say "the
C++ adapter passes its fixtures," but we cannot say "our call-edge extraction is 0.92 precision / 0.78
recall on Python." A competitor can *claim* 66 languages (the corrected figure — see amend-ADR-004; their
own README's "150+" is unsubstantiated); we want to **report a number they cannot**, because reporting it
requires the conformance machinery they don't have.

The research (prior-art-depth-over-breadth, whole doc; [2] Total Recall, [3] Judge/CATS, [6] Deblometer)
converges on the same point: in code intelligence, a *wrong* edge is worse than a *missing* one, and the
only credible accuracy story is measured precision/recall against ground truth — paired with a policy of
**preferring `unknown` over a confident-but-wrong answer**. Two things are missing to make that real:

1. **A measurement harness for extraction** — precision/recall over edges and symbols, per language and per
   tier, not a pass/fail snapshot. This is the *extraction* sibling of ADR-007's *retrieval* harness.
2. **Graded edge confidence.** Edges today are binary: `candidate: bool` (ADR-004 §3). That is enough to
   firewall unverified edges from verdicts, but too coarse to *tune* a "prefer-unknown" policy or to let
   ADR-011's resolver express "0.9 sure" vs "0.4 sure." A3 (design-doc) evolves the boolean into
   `confidence: float | None`.

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

### §4 — Graded edge confidence (A3)

Evolve `Edge.candidate: bool` into `Edge.confidence: float | None` on `src/adapters/base.py`:
- `None` → not applicable / not scored (preserves existing untouched Tier-A semantics where appropriate).
- `1.0` → fully resolved/verified (the old `candidate=False`).
- `(0, 1)` → graded; a Tier-B name-match or an ADR-011 partial resolution lands here.
- The legacy `candidate=True` maps to "below the floor" — semantics preserved, expressiveness gained.

`src/db.py` threads `confidence` through the edge write/read path (additive column, defaulting to preserve
current behavior). `src/MCPServer.py` verdict tools **gate on a confidence floor** instead of on the
boolean: this is the same safe-direction rule ADR-004 §7 established, now parameterized by a threshold.

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
- `Edge.candidate: bool` → `Edge.confidence: float | None` is a schema/field migration touching `base.py`,
  `db.py`, and every verdict tool; a backfill maps existing `candidate` values onto the floor.
- Threshold tuning is now a live knob — power, but also a parameter that must be documented and defended so
  it isn't quietly changed to flatter the numbers.

**Neutral:**
- The retrieval arm (ADR-007) and this extraction arm stay separate scorecards by design.
- Phase-2 execution-verified ground truth is deferred behind an explicit seam, not abandoned.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep pass/fail conformance | Yields a binary "passes," never a precision/recall number; can't be put on a README as a comparative claim. |
| Keep `candidate: bool` | Too coarse to tune a prefer-unknown policy or to carry ADR-011's graded resolution; a boolean can't express "0.9 vs 0.4 sure." |
| Confidence-scored *verdicts* surfaced to users (e.g. "radius 4.2 @ 0.7") | Rejected already in ADR-004 — less actionable than "3 verified + 5 to review." Confidence lives on the *edge* and drives gating; the user-facing verdict stays VERIFIED/ADVISORY/INSUFFICIENT. |
| One blended accuracy number | Hides per-language/per-tier variance — the exact thing the thesis needs visible. |
| Hand-maintained README accuracy table | Rots immediately and becomes another unverifiable claim; the table must be auto-generated from the harness. |
| Execution-verified ground truth now | Heavier (instrumentation/trace harness); correctly deferred to Phase 2 so Phase 1 ships. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 1 — measured conformance + graded confidence**
- [ ] `Edge.confidence: float | None` on `src/adapters/base.py`; map legacy `candidate` → floor; thread through `src/db.py` edge write/read (additive, default-preserving).
- [ ] Verdict tools in `src/MCPServer.py` gate on the confidence floor (default 0.5), reusing the ADR-004 §7 safe-direction rule.
- [ ] Feature-tagged micro-benchmark fixtures in `tests/fixtures/` declaring expected symbols + edges.
- [ ] Extend `tests/test_adapter_snapshots.py` to compute per-language / per-tier precision/recall against those fixtures (ADR-007 harness pattern).
- [ ] Auto-generate the per-language precision/recall table into `README.md`.
- [ ] Show the precision/recall-vs-floor curve so the prefer-unknown threshold is a measured dial.
- [ ] Resolve **Depended on by** obligations: confirm the `Edge.confidence` contract + verdict-floor semantics for ADR-011/012, the conformance machinery for ADR-013, and the floor for ADR-006, before `accepted`.

**Phase 2 — deferred**
- [ ] B4 execution-verified ground truth.

**Notes:**
<!-- 2026-06-18: The moat bucket. Renumbered behind ADR-007 (the design doc tentatively called this "ADR-007"). Defaults: precision = correct÷emitted, recall = correct÷ground-truth, floor = 0.5, ground truth = hand-authored fixtures (Phase 1). Edge.candidate(bool) → Edge.confidence(float|None) is the A3 evolution shared with ADR-011. Open: authoring ground-truth edges at scale; threshold tuning. -->
