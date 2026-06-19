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

1. **Semantic structural chunk as a NEW appended tier** *(Kit 9 decision, 2026-06-18)*. Materialize a
   class + its methods (a containment subtree) as a **new tier appended to `TIER_CONFIGS`** (e.g.
   `tier4_*`) — **not** a redefinition of the existing Tier-2 "component" boundary. `stable_id` is keyed on
   the tier-*name* string, so a new tier name cannot collide with existing IDs; and because `TIER_NUM` is
   `enumerate()`-derived (with the integer persisted in SQLite), the new tier must take the **next free
   integer** (append), leaving every existing tier integer and FAISS ID untouched — a purely additive,
   Mantra-4-safe change.
2. **Outline-as-tier-3.** Serialize the tree as the architectural summary instead
   of a coarse sliding window.
3. **Graph navigation.** Walk containment in the RTR pipeline for structural
   expansion.

A note on `stable_id` (`stable_id.py:40`, keyed on `tier::file_path::scope`): any **redefinition** of an
existing tier-2/tier-3 chunk *boundary* changes that chunk's `scope` and therefore its FAISS ID — an
index-invalidating change requiring a planned full reindex. The Kit 9 decision **avoids** this for the
whole-class chunk by *appending a new tier* rather than redefining Tier-2 (item 1 above), and the persisted
`parent_symbol_id` column is likewise additive — so the persistence work is **no longer index-invalidating**.
What remains deferred is therefore not a Mantra-4 risk but the **schema + incremental-sync cost**: we hold to
the promote-on-a-second-consumer discipline (same as Tier-B language promotion) rather than paying it
speculatively. Items 2–3 below, *if* pursued as boundary *redefinitions*, would reintroduce the
index-invalidation — so they too should prefer additive forms (a new tier / an additive serialization).

## Consequences

*To be assessed at promotion time.* Expected **better:** reusable structural asset; a semantically-whole
structural tier (appended, additive); navigability. Expected **worse:** new schema + incremental-sync burden.
*(Per the Kit 9 decision the structural chunk is an appended tier, so it is **not** index-invalidating; that
cost only returns if a future item redefines an existing tier boundary.)*

## Alternatives Considered

| Option | Why rejected (for now) |
|--------|------------------------|
| Persist the tree inside ADR-005 | Balloons the quality-loop ADR; its payoffs are independent of scoring; and the schema + incremental-sync cost (even though additive — see Kit 9) deserves its own ADR rather than riding the quality loop. |
| Never persist; always derive on the fly | Fine for coherence, but re-derived per consumer and unusable for tier redefinition / navigation that want a stable, queryable structure. |

## Implementation Log

> Not started. Promote when the first feature beyond ADR-005 coherence needs the tree.

- [ ] Trigger met: a second consumer (structural tier-2, outline tier-3, or RTR navigation) requires the persisted tree
- [ ] Design schema (`parent_symbol_id` vs closure table) + incremental sync
- [ ] Implement the whole-class chunk as an APPENDED tier (Kit 9 — additive, no reindex) + the additive `parent_symbol_id`; reserve a reindex migration only for any *future* item that redefines an existing tier boundary (items 2–3)

**Notes:**
<!-- 2026-06-18: Created as a deferred stub during the ADR-005 /grill-plan session. ADR-005 derives the containment parent on the fly; this ADR is its persistence graduation path. Kit 9 decision (2026-06-18): the whole-class structural chunk is a NEW APPENDED tier (NOT a Tier-2 redefinition) → additive, Mantra-4-safe, NOT index-invalidating. Remaining deferral reason = schema/sync cost; promote on a second consumer. -->
