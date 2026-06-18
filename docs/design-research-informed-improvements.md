# Design Proposal: Research-Informed Improvements

> **Status:** proposed design synthesis (may spawn ADR-007+). Not a decision record itself — it collects
> concrete, sourced improvement opportunities and maps each to a place in this codebase.
> **Date:** 2026-06-18
> **Inputs:** [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md) (competitor paper),
> [references-code-intelligence.md](./references-code-intelligence.md) (accuracy literature),
> [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md) (positioning thesis).
> **Relates to:** ADR-004 (tiers), ADR-005 (versioning/self-healing), ADR-006 (graph analytics).

## How to read this

Each item: **what the source shows → where it lands in this project → why → effort/priority → which ADR it
extends.** Two buckets:
- **Bucket A — Engine improvements** borrowed from the competitor paper (ref [1]). General quality/perf wins.
- **Bucket B — Accuracy-proof improvements** from the call-graph literature ([2]–[10]). These are the ones that
  *operationalize the depth-over-breadth thesis* — they turn "we claim accuracy" into "here is the measured,
  reproducible accuracy." **Bucket B is the strategic priority**; it builds the moat the positioning depends on.

> Path/symbol references mirror the ADRs (`src/db.py`, `src/graph_analytics.py`, `adapters/`, `ast_chunker.py`,
> `stable_id.py`, planned `src/scorer.py`). Verify exact locations against the current tree before implementing.

---

## Bucket A — Engine improvements (from the Codebase-Memory paper [1])

### A1. Louvain refinement step — split low-density communities · **Low effort · High value**
- **Source:** [1] §3.7 — after local-moving, communities with **<1% internal density are split by ejecting
  weakly-connected members**, converging in 3–5 iterations.
- **Where:** `src/graph_analytics.py` (ADR-006 engine), after the Louvain pass, before reporting.
- **Why:** cheap post-processing that improves cluster quality and makes god-object/split suggestions cleaner;
  ADR-006 does not currently specify a refinement phase.
- **ADR:** extends ADR-006 §1. Add to its Implementation Log Phase 1.

### A2. Incremental community recompute — only affected partitions · **Med effort · Med value**
- **Source:** [1] §3.6 — on a file change, recompute **only affected** Louvain assignments, not the whole graph.
- **Where:** ADR-006 analysis layer + the incremental path that ADR-005 `recheck` already touches.
- **Why:** keeps `map_module_communities` cheap on large repos if results are ever cached. ADR-006 is currently
  recompute-on-demand; this matters only once caching is added — so **defer until caching exists**.
- **ADR:** ADR-006 (note as a scaling follow-up).

### A3. Confidence scores on candidate edges (not just a boolean) · **Med effort · High value**
- **Source:** [1] §3.4 — 6-strategy cascade with **per-strategy confidence (0.30–0.95)**.
- **Where:** `adapters/base.py` `Edge` dataclass — augment ADR-004's `candidate: bool` with an optional
  `confidence: float | None`. Tier-A resolved edges → high/None; Tier-B `tags.scm` edges → a low fixed score;
  hard-resolved edges (A4) → graded.
- **Why:** a boolean throws away signal the literature says is useful for ranking and for precision reporting
  (Bucket B). It also makes "prefer unknown over wrong" *tunable* (gate at a confidence threshold) rather than
  all-or-nothing.
- **ADR:** extends ADR-004 §3 (candidate-edge contract) — likely its own ADR if the schema/verdict-tool surface
  grows. **Coordinate with B2** (confidence is what you report precision over).

### A4. LSP-style hybrid type resolution for hard languages · **High effort · High value**
- **Source:** [1] §3.4 "LSP-Style Hybrid Type Resolution" for Go/C/C++ (receiver types, pointer indirection,
  templates). Corroborated by the literature: a no-runtime type-inference pass lifted call-edge resolution
  **34% → 76% with a correctness gate that prefers *unknown* over a wrong edge** (search finding; aligns with
  PyCG [7] and the LLM type-analysis study [9]).
- **Where:** new resolution passes layered on the Tier-A adapters for languages where receiver typing dominates
  (when C++ / Go reach Tier A per ADR-004 §7). Must obey the **correctness gate**: emit `unknown`, never a
  wrong resolved target.
- **Why:** this is the *mechanism* that justifies the depth-over-breadth claim — it's how a "fitting" adapter
  earns its higher precision. Directly grows the moat.
- **ADR:** new ADR (depends on ADR-004 Tier-A promotion path). **Highest-leverage engine item.**

### A5. Cross-service HTTP/async edges · **High effort · Low/Deferred value**
- **Source:** [1] §3.2 — `HTTP_CALLS`/`ASYNC_CALLS` via framework route matching across services.
- **Where:** adapter framework-tag layer (`analyze_tags`).
- **Why:** powerful for microservice graphs but speculative for current corpora; high cost, low present demand.
- **ADR:** **defer** — note only.

### A6. Benchmark methodology for our own eval harness · **Med effort · High value**
- **Source:** [1] §4.1 — 12 question categories × repos, PASS/PARTIAL/FAIL grading, measuring **tokens +
  tool-calls + latency**, not just quality.
- **Where:** a new `benchmarks/` harness (does not exist yet).
- **Why:** lets us publish the **graph-vs-RTR-vs-hybrid** three-way the paper never ran, and back the
  "hybrid is our baseline" claim (study §9.4) with numbers. **But fix the paper's validity holes**: blind/
  independent grading, multiple repos per language. Overlaps heavily with Bucket B — build them together.
- **ADR:** new ADR (eval harness). **Pairs with B1.**

### A7. Performance targets to track · **No effort · Reference only**
- **Source:** [1] §4.3 — ~1K tokens/query, <1 ms structural query, 0.3 ms depth-5 BFS, 6 s / 50K-node index,
  1.2 s incremental.
- **Where:** record as the regression/aspiration targets in the A6 harness.

---

## Bucket B — Accuracy-proof improvements (the strategic moat)

> These convert the thesis in [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md) from a stance
> into a measured, reproducible deliverable. **This is where the differentiation is built.**

### B1. Conformance suite reports precision/recall, not just pass/fail · **Med effort · Critical**
- **Source:** Total Recall [2], Judge/CATS [3], Deblometer [6] — accuracy is *measured* as precision & recall
  against curated ground truth, not asserted as a snapshot match.
- **Where:** extend the Tier-A conformance harness (ADR-003/004). Today it asserts byte-identical golden
  snapshots (binary pass/fail). Add: from the same fixtures, compute **edge precision** (emitted edges that are
  correct) and **edge recall** (correct edges that were emitted) per language, and emit a report.
- **Why:** this is *the* "prove it" deliverable. A golden snapshot proves *stability*; precision/recall proves
  *correctness* — which is the claim the positioning rests on. Publishing per-language precision is the rebuttal
  to an unverified language count.
- **ADR:** extends ADR-004 §5 (conformance) — likely **its own ADR (ADR-007: Measured Conformance)**. Depends
  on B-ground-truth (B3). **Top priority in Bucket B.**

### B2. Publish a per-language / per-tier accuracy table · **Low effort (once B1 exists) · Critical**
- **Source:** the unsoundness evals [4][5] (soundness is a spectrum) + the thesis.
- **Where:** README, next to ADR-004's tier table; generated from B1's output and ADR-005's
  `get_flagged_summary()`.
- **Why:** "60 searchable, 6 conformance-guaranteed at P=0.9x recall=0.9x — and we tell you which you queried"
  is a sharper, defensible market position than any raw count. Consumes A3's confidence scores.
- **ADR:** ADR-004 (table) + ADR-005 (flagged summary feed).

### B3. Curated feature-exercising micro-benchmarks per language · **Med effort · High value**
- **Source:** Judge/CATS [3], Deblometer [6] — small programs each crafted to exercise *one* language feature
  (dynamic dispatch, generics, decorators, macros, …) with known-expected calls as ground truth.
- **Where:** formalize/extend the existing Tier-A fixtures into a **feature-tagged** micro-benchmark set; each
  fixture declares the exact expected symbols/edges. This is the ground truth B1 measures against.
- **Why:** turns "we have fixtures" into "we have a feature-coverage matrix per language" — and the *gaps* in
  that matrix become an honest, public statement of each language's known limits (the macro-C problem, owned).
- **ADR:** ADR-007 (with B1).

### B4. Execution-verified ground truth (dynamic call graphs) · **High effort · High value (later)**
- **Source:** Total Recall [2] (dynamic call graphs as baseline), TraceEval [10] (ground truth by execution).
- **Where:** an optional `benchmarks/` mode that runs fixture programs, captures the actual runtime call graph,
  and diffs static extraction against it — the gold standard for recall measurement.
- **Why:** the strongest possible "prove it." Hand-authored ground truth (B3) can itself be wrong; execution
  can't. Heavy (needs runnable fixtures + tracing per language), so **stage after B1–B3** prove their value on
  curated truth first.
- **ADR:** new ADR, later. Pairs with the A6 harness.

### B5. Make "prefer unknown over wrong" a measured, tunable policy · **Low/Med effort · High value**
- **Source:** Rice [14]; WALA "precise but incomplete" [8]; the 34%→76% correctness-gate finding.
- **Where:** the verdict tools (`analyze_blast_radius`, `find_dead_code`) already gate on non-candidate edges
  (ADR-004 §3). Add a **configurable confidence threshold** (using A3's scores) and *report the precision/recall
  trade-off at that threshold* (from B1). Document it as a deliberate, measured policy knob.
- **Why:** elevates the principle from an implementation detail to a stated, evidenced design choice with numbers
  behind it — exactly how the literature frames the soundness/precision trade-off.
- **ADR:** extends ADR-004 §3; consumes A3 + B1.

---

## Suggested sequencing

1. **A1** (Louvain refinement) — trivial, immediate ADR-006 win; do now.
2. **B1 + B3 → ADR-007 (Measured Conformance)** — the strategic core: ground-truth micro-benchmarks + precision/
   recall reporting. **Start here for the moat.**
3. **A3** (edge confidence scores) — small schema change that B2/B5 depend on.
4. **B2 + B5** — publish the accuracy table; make the precision policy explicit. Cheap once B1/A3 land.
5. **A6** (benchmark harness) — enables the graph-vs-RTR-vs-hybrid story; build alongside B-work.
6. **A4** (LSP-style hybrid resolution) — high-leverage engine work; the mechanism that *earns* the precision B1
   measures. Schedule after the measurement layer exists, so its gains are quantified.
7. **A2, A5, B4** — deferred / scaling / later-stage.

## What *not* to take from the competitor
- No flat language-count race (the whole point — see [prior-art-depth-over-breadth.md](./prior-art-depth-over-breadth.md)).
- No fuzzy low-confidence edges silently feeding verdicts or community detection (ADR-006 stays EXTRACTED-only;
  if A3 lands, gate communities on a confidence floor).
- No first-author/self-graded accuracy claims — B1's reproducible precision/recall is the replacement.
