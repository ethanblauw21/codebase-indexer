# ADR-016: Persisted File Symbol Containment Tree

**Status:** proposed (deferred — placeholder pending a second consumer)
**Date:** 2026-06-18
**Branch:** `feature/adr-016-persisted-symbol-tree` *(not yet started)*
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-005 — derives the symbol containment parent on the fly for coherence scoring (§3); this ADR persists that derived structure as a first-class asset.
**Depended on by:**
- *(none yet)* — promote when a second consumer (structural tier-2, outline-as-tier-3, or RTR graph navigation) needs the persisted tree.

> **Numbering note (2026-06-18):** originally drafted as ADR-006 during the ADR-005
> grill, then renumbered to ADR-016 to resolve a collision — ADR-006 is *Graph
> Analytics & Community Detection*. The backlog reserves 007–015, so this took the
> next free slot.

> **Deferred stub.** This ADR reserves the decision; it is not yet designed. It
> exists so the seam is recorded and the on-the-fly containment logic in ADR-005
> has a documented graduation path. Full design (schema, sync, tier redefinition)
> is out of scope until a second consumer justifies the schema cost — same
> promotion discipline used for Tier-B languages in ADR-004.

## Context

ADR-005 §3 needs a symbol's **structural containment parent** (enclosing class;
file-root for top-level symbols) to compute coherence scores. It derives that
parent **on the fly** per file at score time from existing data —
`Symbol.class_context` (`adapters/base.py:35`), `OWNS` edges, and line ranges —
deliberately *not* persisting a tree, to keep the quality loop focused.

That derived-on-the-fly view is a *materialized-view-in-waiting*. The same
containment hierarchy, if persisted as a first-class structure, would serve
consumers well beyond coherence scoring. Persisting it is a schema change with an
incremental-sync obligation, so it should not ride inside the quality-loop ADR.

## Decision

*Deferred.* When promoted, this ADR will specify a persisted per-file symbol
containment tree (likely a `parent_symbol_id` column on `symbols`, or a closure
table), materialized at index time and kept consistent by the incremental
indexer. The likely payoffs to design against:

1. **Structural tier-2.** Redefine the "component" tier as a class + its methods
   (a containment subtree) instead of a blind ~1500-token sliding window —
   semantically meaningful component chunks.
2. **Outline-as-tier-3.** Serialize the tree as the architectural summary instead
   of a coarse sliding window.
3. **Graph navigation.** Walk containment in the RTR pipeline for structural
   expansion.

Any redefinition of tier-2/tier-3 chunk *boundaries* interacts with `stable_id`
(`stable_id.py:40`, keyed on `tier::file_path::scope`): changing what a tier-2
chunk *is* changes its `scope` and therefore its FAISS ID, which is an
index-invalidating change requiring a planned full reindex. This is the principal
reason the decision is deferred rather than bundled — it is not the cheap additive
change ADR-005 is.

## Consequences

*To be assessed at promotion time.* Expected **better:** reusable structural
asset; meaningful tier-2/3; navigability. Expected **worse:** new schema + sync
burden; tier-boundary changes are index-invalidating (full reindex).

## Alternatives Considered

| Option | Why rejected (for now) |
|--------|------------------------|
| Persist the tree inside ADR-005 | Balloons the quality-loop ADR; its payoffs are independent of scoring; tier-boundary changes are index-invalidating and need their own planned migration. |
| Never persist; always derive on the fly | Fine for coherence, but re-derived per consumer and unusable for tier redefinition / navigation that want a stable, queryable structure. |

## Implementation Log

> Not started. Promote when the first feature beyond ADR-005 coherence needs the tree.

- [ ] Trigger met: a second consumer (structural tier-2, outline tier-3, or RTR navigation) requires the persisted tree
- [ ] Design schema (`parent_symbol_id` vs closure table) + incremental sync
- [ ] Assess `stable_id` impact of any tier-2/3 boundary redefinition; plan the reindex migration

**Notes:**
<!-- 2026-06-18: Created as a deferred stub during the ADR-005 /grill-plan session. ADR-005 derives the containment parent on the fly; this ADR is its persistence graduation path. Do not start until a second consumer justifies the schema cost. -->
