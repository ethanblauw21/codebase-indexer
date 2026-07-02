# ADR-022: Graph-Neighbor Retrieval Scoring & Pool Budgeting — Let the Traverse Step Reach the Top-K

**Status:** proposed — **deferred** (2026-07-02). No longer blocks ADR-019 (now `accepted` with the graph recorded as rerank-only, B−A gate dropped). Pick up only if a better-powered reranker rerun makes graph-in-retrieval worth tuning.
**Date:** 2026-07-01
**Branch:** `feature/adr-022-graph-neighbor-scoring`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-021 — the resolved CALLS edges + `COALESCE(resolved_target, target)` traversal. Without those the graph produces no neighbours at all; this ADR is only meaningful once neighbours exist.
- ADR-019 — the real-repo eval is the **measurement instrument**. Any change here is judged by whether it moves the arm **B−A** (graph) lift, per language, without regressing arm B.

**Depended on by:**
- ADR-019 — its `graph-only` query class and the **B−A graph lift are un-measurable until this lands** (structural neighbours can't reach the graded top-10 under the current scoring). ~~**Resolve before ADR-019 `accepted`:** either this ADR makes B−A measurable, or ADR-019 formally records the graph layer as rerank-only and drops the B−A gate.~~ **Resolved (2026-07-02):** ADR-019 took the second branch — it is `accepted` with the graph recorded as **rerank-only** and the B−A gate **dropped** (ADR-019 §8). This ADR therefore no longer blocks anything; it stays **proposed and deferred**, to be picked up *only if* a better-powered reranker rerun (ADR-009 §P4) pushes C−B over the enable bar and makes graph-in-retrieval worth tuning.

## Context

ADR-021 made the call graph *traversable* and ADR-019's arms were built to measure what the Traverse step adds (paired lift **B−A**). Measuring it surfaced two reasons the structural neighbours never reach the returned top-10 — so the graph is, today, **inert for retrieval** even though the edges are correct:

1. **Pool truncation (bug).** `hybrid_retriever._expand_structurally_budgeted` seeds the pool with up to `_SEMANTIC_K = 50` semantic hits (inserted first), appends structural neighbours, then returns `list(pool.values())[:_MAX_POOL_SIZE]` with `_MAX_POOL_SIZE = 35`. Since `35 < 50`, the slice keeps the first 35 (all semantic) and **discards every structural neighbour** whenever semantic retrieval fills ≥35 slots — which is the normal case.

2. **Uncompetitive scores (design).** Even with the cap lifted, a structural neighbour enters with a **hop-decayed RRF score** (`parent_score × 0.7`), which sits *below* every semantic hit's RRF score. Under the shipped RRF-only ranking (arm B), the top-10 is taken by score, so the neighbour — though now in the pool — never appears at K=10. Empirically on p-queue: the correct structural gold stays below rank 10 even with the pool cap removed, and **B−A = +0.003 ±0.007** (indistinguishable from zero).

The consequence is sharp: **`graph-only` fixtures cannot register a lift under RRF@10 no matter how well authored.** The graph's value only materializes when a reranker rescores candidates by query-relevance (arm C) and can pull a structural neighbour up. That entangles "graph value" with "reranker value" and leaves the advertised structural differentiator unmeasured and, by default, unused.

## Decision

*(Proposed — to be grilled before implementation.)* Make structural neighbours **reach the top-K on their own merits**, then re-measure with ADR-019. Two coupled changes plus a measurement gate:

### §1 — Pool budgeting: never truncate structural before semantic
Return the pool so structural neighbours are not silently dropped — reserve room for them (`semantic[:_MAX_POOL_SIZE − n_structural] + structural`) or, better, cap by **score after all candidates are scored** rather than by insertion order. Ensure `_MAX_POOL_SIZE ≥ _SEMANTIC_K` isn't required by construction. This is the unambiguous bug fix; it is necessary but **not sufficient** (§2).

### §2 — Competitive structural scoring
Give a structural neighbour a score that lets a *genuinely relevant* one surface at K=10 without flooding the results with weak graph noise. Candidate mechanisms (to grill):
- a **structural relevance signal** — re-score the neighbour against the query (cheap cross-encoder-free similarity), not just hop-decay from its parent;
- an **interleave / reserved-slot** policy — guarantee the top-N structural neighbours a look-in at the final ranking;
- a **corroboration-weighted bonus** — boost import-corroborated neighbours (already labelled) over uncorroborated ones.
The gate is precision: a structural boost must not push unrelated callers/callees into the top-10 (that would *lower* arm B on semantic fixtures).

### §3 — Measure, don't assert
Every change is validated through ADR-019's arms: **B−A must rise on the `graph-only` class with no regression on the `semantic` class**, per language. If no scoring change makes B−A positive without hurting B, the honest conclusion is that **the graph layer is a rerank-time signal, not a first-pass retrieval signal** — record that in ADR-019 §8 and stop gating on B−A.

## Consequences

**Better:** the structural differentiator can finally contribute to first-pass retrieval (or be honestly retired as rerank-only); ADR-019's B−A gate becomes meaningful; the pool-truncation bug is fixed regardless of the scoring outcome.
**Worse:** structural scoring is a real tuning problem with a precision downside (weak graph hits crowding the top-10); needs the ADR-019 harness as a guardrail; touches the hot retrieval path.
**Neutral:** independent of the reranker/sparse decisions (ADR-009) — those ride on arm B and are unaffected by whether the graph surfaces at K=10.

## Alternatives Considered

| Option | Why rejected (for now) |
|--------|-------------|
| Just raise `_MAX_POOL_SIZE` above `_SEMANTIC_K` | Fixes §1 truncation but not §2 — structural nodes still rank below the semantic top-10 under RRF, so B−A stays ~0. Necessary, not sufficient. |
| Accept graph as rerank-only; delete the B−A arm | Possibly the right end state (§3), but premature before trying to make structural scoring competitive; would abandon the differentiator without evidence it can't work. |
| Grade the graph at a deeper K (e.g. @35) | Measures "is the neighbour anywhere in the pool" not "does the user see it"; inflates a number that doesn't reflect shipped top-10 behaviour. |
| Drop the graph layer entirely | Throws away a real capability (resolved call edges) that ADR-006 analytics and blast-radius already use; the question is retrieval surfacing, not extraction. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Fix §1 pool budgeting so structural neighbours survive the cap; unit-test that a resolved neighbour is present in the returned pool.
- [ ] Prototype §2 structural scoring option(s); guard precision on the `semantic` class.
- [ ] Re-run ADR-019 arms A/B on the `graph-only` fixtures (un-pause them); report B−A per language.
- [ ] §3 verdict: either B−A becomes a real positive lift, or record "graph = rerank-only" in ADR-019 §8 and drop the B−A gate.
- [ ] Resolve **Depended on by**: report the outcome into ADR-019 before its `accepted`.

**Notes:**
<!-- 2026-07-01: Split out of ADR-019's build. The graph Traverse step is resolved (ADR-021) + emits edges for all 5 langs (incl. the C# call-query fix), but structural neighbours (a) get truncated by _MAX_POOL_SIZE<_SEMANTIC_K and (b) score below the semantic top-10 under RRF, so B−A ≈ 0 by construction. tools/graph_only_scout.py (ranks resolved call pairs by ascending embedding similarity) already exists to author the graph-only fixtures this ADR will validate against. -->
