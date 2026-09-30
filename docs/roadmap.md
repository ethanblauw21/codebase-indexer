# Roadmap — codebase-indexer

**What this is:** sequencing and dependency order. *What* to build lives in
[`backlog.md`](./backlog.md); *how and why* lives in [`adr/`](./adr/); *what actually happened* lives
in each ADR's Implementation Log.

This file holds none of those. If you find a build log or a fix list here, it has drifted — move it
back to the ADR that owns it. That drift is what this file was split out of: for the project's first months the ADR set
*was* the backlog, and [`adr-backlog.md`](./adr-backlog.md) carried intake, sequencing and per-ADR
build kits in one document. It is now frozen; this file and `backlog.md` replace it.

---

## Milestones (direction, for reference; added 2026-09-25)

The three directions the project is heading in. They aren't scheduled; each points at what exists
today and what would carry it further.

1. **Language expansion stays gated by conformance.**
   - A new language ships only once it scores against hand-authored extraction fixtures in
     `tests/fixtures/conformance/` (ADR-008).
   - Python, TypeScript, C# and C++ are covered today (plus Rockwell L5X, ADR-013), and the four together cover a large share of
     professional codebases.
   - Depth before breadth: a language whose symbols and edges can't be shown correct doesn't get
     indexed.
2. **Test suites as first-class symbols** ([B-027](./backlog.md#b-027)).
   - `describe`, `it` and `test` blocks become symbols in their own right.
   - Today most test files have no named symbols at tier 1 and fall back to token slices
     (`Global_part_N`). Test code is where plain AST parsers struggle most, so this is where the
     indexer can do best.
   - It also gives redeclared test helpers distinct names (B-026).
3. **Graph-augmented retrieval.** Keep growing what the call-edge graph can answer.
   - The graph tools today are `trace_data_flow`, `analyze_blast_radius`, `find_dead_code` and
     `verify_candidate_edges`.
   - Search already expands its candidates along call edges. That measured small but positive:
     +0.015 on intent questions, +0.022 / +0.029 on file questions (ADR-030 Verification 5).
   - Vector search joined with explicit call-graph traversal is what makes the MCP tools act like an
     IDE assistant rather than a text search.

---

## Where the work is

This section doesn't track status; dated snapshots here went stale within weeks (#61). For what's
open, read the index table at the top of [`backlog.md`](./backlog.md), and GitHub issues for defects
found in review. For what's in flight, read the open PRs. For the state at the end of the last
session, read `HANDOFF.md` in the repository root.

What does hold:

- **The indexer is in daily use** on real projects since 2026-09-28 (InventoryApp-V2 and GanttWebApp,
  both in git mode, ADR-042). Problems found in use go to the backlog first.
- **The local GPU works again** (repaired; 6 h of staged load on 2026-09-24 with no errors). Local
  builds and measurements run on it. `CODE_INDEXER_DEVICE=cpu` (ADR-020) remains the whole-stack
  switch for CPU-only machines.
- **First-run experience is the priority** for sharing the repository: [B-008](./backlog.md#b-008)
  (Windows `cp1252`), the README (#60) and packaging.

### The reranker thread is closed (2026-07-27)

`[reranker].enabled` stays `false` **permanently as a default**, and no further reranker measurement
is planned. The decisive finding: every reranker number was measured on the *previous* embedder, and
`bge-code-v1` absorbed the gain — on Python and C#, dense-only now scores above the old embedder with
reranking attached, at 0.3–2 s/query instead of ~90 s on CPU. Backlog **B-003 and B-004 are dropped**;
the reasoning is recorded there and in ADR-009 §P4 / ADR-019 §6. The feature survives as a documented
opt-in; the research thread does not.

---

## Dependency edges that still bind

```
ADR-008 ──► ADR-013     conformance machinery for DSL adapters
ADR-011 ──► ADR-012     the graded resolved-edge contract
ADR-005 ──► ADR-010     the recheck/self-healing loop + the XXH3 hash standardization
ADR-006 ──► ADR-010     vital set auto-derived from centrality
ADR-006 ──► ADR-015     communities for the explorer's map view
ADR-009 ──► ADR-014     fusion must be parameterized before weights can be learned over it
ADR-007 ──► ADR-014     the held-out split the anti-overfit gate needs
ADR-005 ◄─► ADR-017     mutual — the Tier-B→Tier-A promotion path
ADR-005 ──► ADR-016     trigger-gated; promote only on a second consumer
```

Every edge above has an **unbuilt ADR on at least one end**, which is why none of them are currently
scheduling anything.

**Discharged** — both ends built, no further obligation: `ADR-007 → ADR-008`, `ADR-007 → ADR-009`,
`ADR-003 → ADR-019`, `ADR-009 → ADR-019`, `ADR-024 → ADR-020`, `ADR-021 → ADR-023`.

`ADR-022 → ADR-019` is **moot while 022 is deferred**: ADR-019 §8 formally dropped the B−A graph gate
and recorded the graph layer as rerank-only, so 022 no longer owes 019 a verdict.

---

## Closed waves

The wave plan from `adr-backlog.md` (2026-06-18) is complete through Wave 1 and abandoned after it —
recorded here so the shape of the finished work stays legible. Per-item detail is in each ADR.

| Wave | Plan | Outcome |
|---|---|---|
| **0 · Foundation** | ADR-007 | **built** — CoIR harness + committed Wave-0 baseline. Its C#/C++ and structural-graph gap became ADR-019. |
| **1 · ROI + moat** | ADR-009, ADR-008, ADR-011 | **built, and the gates answered "no" twice.** P1 embedder swap → bge-code-v1 promoted to default. P3 convex fusion **rejected** (negative in all 5 languages). P4 reranker **settled off** (public passed, private clause-3 failed). ADR-008's conformance scorecard drove two Python adapter fixes; ADR-011 lifted resolution 0.40 → 1.00 with precision held. |
| **2 · Robustness** | ADR-010 drift detection | **not started.** |
| **3 · Reach / research / UX** | ADR-013, 012, 014, 015 | ADR-013 **built** (the L5X adapter, #32, 2026-09-26); the rest **not started.** |

The two "no" verdicts in Wave 1 are the wave's most valuable output — the harness was built precisely
so a component could be rejected on a number, and it rejected two. Work that arrived after the
grouping and shipped anyway: 019, 020, 021, 023, 024, 025.

---

## Deliberate gates — not forgotten, waiting on a trigger

These are open by decision, not neglect. **Each is an open checkbox in its own ADR's Implementation
Log, which remains the truth for it** — this table is a summary and owns nothing.

| Gate | ADR | Waiting on |
|---|---|---|
| End-to-end reindex confirming freshness timestamps | 025 | the GPU, or a CPU-forced box |
| segmem `codemap` connector re-verified against a rebuilt `graph.db` (fails *silently*) | 025 | the reindex above |
| P2 contextual chunk augmentation — implemented on a branch, flag off | 009 | a validation run (GPU) |
| Precision/recall-vs-floor curve — the prefer-unknown dial | 008 | unblocked and CPU-cheap; today a step function until 011 emits graded values |
| B4 execution-verified ground truth | 008 | Phase 2, unscheduled |
| Stage 2b — heuristic member chains (`a.b().c()`), still `unknown` | 011 | a decision to accept lower-confidence edges |
| Go/C resolution passes | 011 | those adapters existing (ADR-017 promotion path) |
| Leiden backend behind import-availability | 006 | optional; Louvain is the shipped default |
| Incremental community recompute (A2) | 006 | caching of `map_module_communities` results |
| §8 contamination + coverage caveats into the scorecard header and README | 019 | nobody — doc-only, unclaimed |

---

## The legacy unbuilt set

**ADR-005 · 010 · 012 · 014 · 015 · 016 · 018 · 022**, plus **ADR-017** (whose Phase-1
`Edge.candidate` slice shipped while the tier model did not).

These are plans, not commitments — the pile that motivated splitting this document set. They stay on
`master` because each carries research a backlog paragraph would destroy, and because other ADRs'
`Depends on` fields cite their numbers. They are **sequenced by trigger, not by wave** — do not
schedule them:

| ADR | Promote when |
|---|---|
| **010** drift detection | incremental reindex cost or human↔AI drift actually bites |
| **012** cross-repo | a second repo must be queryable in one index |
| **014** adaptive ranking | research appetite; needs 009's fusion parameterized first |
| **015** explorer UI | the graph output needs to be human-legible to someone other than an agent |
| **016** persisted symbol tree | a **second consumer** needs the tree (explicitly trigger-gated in the ADR) |
| **018** clone matching | `find_similar_code` precision becomes a complaint |
| **005** chunk versioning | adapter churn makes stale chunks a real problem |
| **017** tier model | a Tier-B language is genuinely wanted |
| **022** graph-in-retrieval | only if a better-powered reranker rerun makes graph scoring worth tuning |

**This set is closed and will not grow.** New wants go to [`backlog.md`](./backlog.md); anything here
that gets built follows the ordinary branch-born ADR rule from wherever its work starts.
