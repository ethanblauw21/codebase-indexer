# ADR-011: High-Precision Call Resolution — Hybrid Type Resolution That Earns the Precision ADR-008 Measures

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-011-high-precision-call-resolution`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-004 — needs the **Tier-A promotion path** (§9); this ADR's hybrid resolution is the mechanism that promotes receiver-typed languages (Go/C/C++) toward Tier-A precision.
- ADR-008 — needs the shared **`Edge.confidence` field** (A3) to emit graded-confidence edges, and the **precision/recall harness** to measure that resolution rate rises *with precision held*. **Pairs with** ADR-008.
**Depended on by:**
- ADR-012 *(planned — docs/adr-backlog.md)* — Cross-Repository/Cross-Service graph consumes the **graded-confidence resolved edges** this ADR produces (shared `Edge.confidence`) as the in-repo precision foundation it extends across services.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-011 bucket + build kit) and design-doc
> A4. Citations `[n]` index [references-code-intelligence.md](../references-code-intelligence.md).

## Context

ADR-008 *measures* precision/recall. This ADR is the **mechanism that earns it** for the hardest case:
**call resolution in receiver-typed languages** (Go, C, C++), where `recv.method()` or `obj->fn()` cannot be
resolved to a target without knowing the *type* of the receiver. Today such calls are either dropped
(recall loss) or emitted as name-matched `candidate` edges (precision risk if trusted). The research is
blunt about the stakes: [2] Total Recall, [7] PyCG, and the cited **34%→76% type-inference finding** show
that adding even lightweight type resolution roughly doubles correct call-edge resolution — *if* it's done
without manufacturing wrong edges.

The non-negotiable constraint, inherited from the depth-over-breadth moat (ADR-004) and the prefer-unknown
policy (ADR-008 §5): **a resolution pass must emit `unknown` rather than a wrong resolved target.** A wrong
edge is worse than a missing one. So the mechanism is not "resolve aggressively"; it is "resolve when
provably correct, grade the confidence otherwise, and never assert a target you can't stand behind."

This is the resolver behind the precision number — Wave 1, paired with ADR-008.

## Decision

Add **LSP-style hybrid type-resolution passes** for receiver-typed languages, gated by a hard correctness
rule (emit `unknown`, never a wrong target) and emitting **graded-confidence edges** via the shared
`Edge.confidence` field. The lift is measured by ADR-008's harness: resolution rate must rise **with
precision held**.

### §1 — Hybrid type-resolution pass (A4)

A new **per-language type-resolution pass module**, run after the adapter's initial parse, that resolves
receiver types using the information tree-sitter already gives us plus local scope analysis — the
lightweight end of what an LSP does, without standing up a full language server (that path is rejected
below and in ADR-004 §7.1 as "Tier-A promotion by another name / a heavyweight verifier"). It targets the
`src/adapters/cpp_adapter.py` (and a Go adapter when added), resolving:
- local variable declared types → method/field targets,
- parameter types → calls on parameters,
- field/member access chains where the declaring type is in-repo.

"Hybrid" = combine syntactic evidence (declaration sites, type annotations the grammar exposes) with scoped
name resolution; it is not full semantic analysis and does not pretend to be.

### §2 — The correctness gate (emit `unknown`, never wrong)

Each resolution attempt yields one of:
- **Resolved** — a single in-repo target the pass can stand behind → high `confidence` (→ 1.0).
- **Graded** — partial evidence (e.g. a known type but an overload set, or a probable but unproven target)
  → a `confidence` in `(0, 1)` reflecting the strategy's reliability.
- **Unknown** — insufficient evidence → **no resolved target emitted** (the name-match `candidate` edge may
  remain, below the floor, but the pass never invents a resolved target).

This is the load-bearing rule: the pass is allowed to *fail to resolve*; it is never allowed to *resolve
wrongly*. That is what lets ADR-008 report a *rising* resolution rate without a *falling* precision.

### §3 — Graded confidence per strategy (shared with ADR-008 A3)

Each resolution strategy carries a **calibrated confidence**, written to the shared `Edge.confidence` field
(defined in ADR-008 §4). Mirroring the competitor's multi-strategy scoring (study), different strategies
(exact local-type resolution vs. heuristic member-chain inference) get different baseline confidences,
tuned against ADR-008's per-language precision/recall so the grades mean something measurable rather than
being arbitrary.

### §4 — Measured by ADR-008, on Go/C/C++ fixtures

Success is defined operationally: on Go/C/C++ feature-tagged fixtures (ADR-008 §2), the **call-edge
resolution rate rises while precision is held**. The harness is the referee; this ADR ships no separate
accuracy claim.

## Consequences

**Better:**
- Receiver-typed languages (Go/C/C++) gain resolved call edges where today there are only name-matches or
  gaps — the recall lift the 34%→76% finding predicts, *captured as graded edges* rather than fragile asserts.
- The correctness gate guarantees the lift can't come at precision's expense — the resolution rate goes up
  with precision held, which is the only credible version of "more edges."
- Graded confidence (shared with ADR-008) feeds downstream: ADR-012 consumes these edges; ADR-006 community
  detection can gate on their confidence.
- No new heavy dependency — uses tree-sitter, already present.

**Worse:**
- Per-language resolution passes are real, non-trivial work (effort H); C++ templates in particular have a
  hard ceiling (an Open Question — how far to push them).
- Go/C adapters aren't Tier-A yet, so this pass partly *precedes* their promotion; sequencing against
  ADR-004's promotion path needs care.
- Calibrating per-strategy confidence against data is ongoing tuning coupled to ADR-008's fixtures.

**Neutral:**
- Shares the `Edge.confidence` field with ADR-008 rather than inventing its own — one field, two consumers.
- Deliberately stops short of a full LSP/clangd integration; that heavier path is explicitly out of scope.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Resolve aggressively, accept some wrong edges | Direct violation of the prefer-unknown moat (ADR-008 §5); a wrong edge is worse than a missing one and would *lower* the precision ADR-008 reports. |
| Full on-demand LSP / clangd per language | "Tier-A promotion by another name" (ADR-004 §7.1) — duplicates the heavyweight per-language burden Tier B/this pass exist to avoid; huge runtime/dependency cost. |
| Keep emitting name-match `candidate` edges only | Caps resolution where the 34%→76% finding shows lightweight type inference roughly doubles it; leaves recall on the table. |
| One flat confidence for all resolutions | Throws away the signal that exact local-type resolution is more trustworthy than heuristic member-chain inference; graded per-strategy confidence is what makes the floor tunable. |
| Skip the correctness gate, measure precision after | Lets wrong edges into the graph first; the gate is cheaper and safer than retroactive cleanup, and protects every downstream consumer. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] New per-language type-resolution pass module (runs after adapter parse).
- [ ] C++ resolution pass in `src/adapters/cpp_adapter.py`: local/param/member type resolution; correctness gate emits `unknown` over wrong.
- [ ] Go adapter resolution pass (when the Go adapter lands via ADR-004).
- [ ] Emit graded `confidence` per strategy via the shared `Edge.confidence` (ADR-008 §4); calibrate against ADR-008 fixtures.
- [ ] Prove on Go/C/C++ fixtures: resolution rate up, precision held (ADR-008 harness).
- [ ] Resolve **Depended on by**: confirm the graded resolved-edge contract ADR-012 consumes, before `accepted`.

**Notes:**
<!-- 2026-06-18: The mechanism behind the ADR-008 precision number. Pairs with ADR-008 (shares Edge.confidence A3). Defaults: correctness gate = emit `unknown`, never a wrong resolved target; graded confidence per resolution strategy (mirrors competitor's 6-strategy scoring). Done when call-edge resolution rises on Go/C/C++ with precision held. Open: how far to push C++ templates; Go/C adapters aren't Tier-A yet. Effort H. -->
