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

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Implement the L5X stub (`src/adapters/l5x_adapter.py`, currently `NotImplementedError`) into real extraction (routines, tags, function blocks, call/use edges); add IEC 61131-3 Structured Text parsing.
- [ ] Curated feature-tagged conformance fixtures in `tests/fixtures/`; measure via ADR-008 precision/recall.
- [ ] Register adapters in `src/adapters/__init__.py`; `lxml` for XML DSLs, tree-sitter where a grammar exists.
- [ ] Update the README tier+capability table per passing adapter.
- [ ] Subsequent DSL targets (HDL, mapping/config) by the same recipe, gated on grammar/format availability.

**Notes:**
<!-- 2026-06-18: Wave 3, best near-term differentiation. Default first target = IMPLEMENT the L5X stub (currently NotImplementedError, deferred ADR-003 §D4) + IEC 61131-3 ST parsing (no parser today). Reuses ADR-003 registry + ADR-017 tiers + ADR-008 conformance. Done when a new DSL adapter passes its conformance suite + tier table updated. Open: grammar availability per DSL. Effort M per DSL. -->
