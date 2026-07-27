# ADR-011: High-Precision Call Resolution — Hybrid Type Resolution That Earns the Precision ADR-008 Measures

**Status:** accepted (2026-07-17) — Stages 1–3 built and merged (PRs #27, #28, #29): C# then C++ receiver-type inference via `type_resolver.py` + `Edge.receiver_type` + a language-neutral typed regime in `call_resolver`, proven by the Stage-3 resolution-conformance harness (`tools/resolution_eval.py`), which measured the resolution rate rising **0.40 → 1.00 with precision held at 1.0**. **Still open (checkboxes below, not blockers):** Stage 2b member chains (`a.b().c()`), and Go/C passes when those adapters land. The ADR-012 contract obligation is **recorded, not blocking** — ADR-012 is unbuilt, and per CONTRIBUTING §4.1 an obligation to an unbuilt ADR does not hold a shipped ADR at `proposed`. *Status corrected 2026-07-27 — this clause alone was the reason it read `proposed`.*
**Date:** 2026-06-18
**Branch:** `feature/adr-011-high-precision-call-resolution`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-021 — the **baseline call-edge resolution layer this builds on.** ADR-021 resolves the *unambiguous* call (one provable in-repo target) and establishes the `resolved_target` write + `COALESCE(resolved_target, target)` traversal contract. This ADR adds the *hard, ambiguous* receiver-typed case (type inference) with graded `Edge.confidence` **on top of** that contract — it does not re-implement it.
- ADR-017 — needs the **Tier-A promotion path** (fitting-adapter tier); this ADR's hybrid resolution is the mechanism that promotes receiver-typed languages — **C++ and C# today** (existing adapters), **Go, C, and others as their adapters land** — toward Tier-A precision.
- ADR-008 — needs the shared **`Edge.confidence` field** (A3) to emit graded-confidence edges, and the **precision/recall harness** to measure that resolution rate rises *with precision held*. **Pairs with** ADR-008.
**Depended on by:**
- ADR-012 *(planned — docs/adr-backlog.md)* — Cross-Repository/Cross-Service graph consumes the **graded-confidence resolved edges** this ADR produces (shared `Edge.confidence`) as the in-repo precision foundation it extends across services.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-011 bucket + build kit) and design-doc
> A4. Citations `[n]` index [references-code-intelligence.md](../references-code-intelligence.md).

## Context

ADR-008 *measures* precision/recall. This ADR is the **mechanism that earns it** for the hardest case:
**call resolution in receiver-typed languages**, where `recv.Method()` (C#) or `obj->fn()` (C++) cannot be
resolved to a target without knowing the *type* of the receiver. The in-stack targets with adapters today are
**C++ and C#** — and C# is a textbook case: its adapter currently resolves extension methods to *candidates
only* ("no receiver-type inference"). **Go, C, and other receiver-typed languages are planned future
adapters** the same pass will extend to as they land. Today such calls are either dropped (recall loss) or
emitted as name-matched `candidate` edges (precision risk if trusted). The research is
blunt about the stakes: [2] Total Recall, [7] PyCG, and the cited **34%→76% type-inference finding** show
that adding even lightweight type resolution roughly doubles correct call-edge resolution — *if* it's done
without manufacturing wrong edges.

The non-negotiable constraint, inherited from the depth-over-breadth moat and the prefer-unknown
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
below as "Tier-A promotion by another name / a heavyweight verifier"). It targets
`src/adapters/cpp_adapter.py` and `src/adapters/csharp_adapter.py` today (and Go/C adapters as they land),
resolving:
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

### §4 — Measured by ADR-008, on C++/C# fixtures

Success is defined operationally: on C++ and C# feature-tagged fixtures (ADR-008 §2) — and Go/C fixtures as
those adapters land — the **call-edge resolution rate rises while precision is held**. The harness is the
referee; this ADR ships no separate accuracy claim.

### §5 — Limits & honest caveats (current state)

The resolver is deliberately lightweight, so its reach is bounded — and every limit resolves to `unknown`,
never a wrong edge:
- **In-repo only.** A receiver whose declaring type lives in an **external library or framework** (not in the
  indexed repo) cannot be resolved — it emits `unknown` / below-floor by design, rather than guessing.
- **C++ templates / metaprogramming** have a hard ceiling (an Open Question — how far to push them); deeply
  generic or macro-expanded call sites resolve to `unknown` rather than a fabricated target.
- **C# `dynamic` and reflection** are opaque to static resolution (the adapter already treats `dynamic` as
  invisible) → `unknown`.

These are *by-design unknowns*, consistent with prefer-unknown — the recall they cost is recoverable later
(e.g. via the heavier LSP path explicitly rejected here), but never at precision's expense.

## Consequences

**Better:**
- Receiver-typed languages (C++ and C# today; Go/C as their adapters land) gain resolved call edges where
  today there are only name-matches or gaps — the recall lift the 34%→76% finding predicts, *captured as
  graded edges* rather than fragile asserts.
- The correctness gate guarantees the lift can't come at precision's expense — the resolution rate goes up
  with precision held, which is the only credible version of "more edges."
- Graded confidence (shared with ADR-008) feeds downstream: ADR-012 consumes these edges; ADR-006 community
  detection can gate on their confidence.
- No new heavy dependency — uses tree-sitter, already present.

**Worse:**
- Per-language resolution passes are real, non-trivial work (effort H); C++ templates in particular have a
  hard ceiling (an Open Question — how far to push them).
- C++ and C# adapters exist but aren't Tier-A yet; this pass is part of their promotion. Go/C and other
  receiver-typed adapters are **planned but not yet built** — the pass is designed to extend to them as they
  land. Sequencing against ADR-017's promotion path needs care.
- Calibrating per-strategy confidence against data is ongoing tuning coupled to ADR-008's fixtures.

**Neutral:**
- Shares the `Edge.confidence` field with ADR-008 rather than inventing its own — one field, two consumers.
- Deliberately stops short of a full LSP/clangd integration; that heavier path is explicitly out of scope.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Resolve aggressively, accept some wrong edges | Direct violation of the prefer-unknown moat (ADR-008 §5); a wrong edge is worse than a missing one and would *lower* the precision ADR-008 reports. |
| Full on-demand LSP / clangd per language | "Tier-A promotion by another name" — duplicates the heavyweight per-language burden Tier B / this pass exist to avoid; huge runtime/dependency cost. |
| Keep emitting name-match `candidate` edges only | Caps resolution where the 34%→76% finding shows lightweight type inference roughly doubles it; leaves recall on the table. |
| One flat confidence for all resolutions | Throws away the signal that exact local-type resolution is more trustworthy than heuristic member-chain inference; graded per-strategy confidence is what makes the floor tunable. |
| Skip the correctness gate, measure precision after | Lets wrong edges into the graph first; the gate is cheaper and safer than retroactive cleanup, and protects every downstream consumer. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] **Stage 1** — New per-language type-resolution pass module (`src/type_resolver.py`), runs at parse time over the adapter's tree.
- [x] **Stage 1** — C# resolution pass: receiver-type inference for the **exact** strategies (parameter type, explicit local type, `var` + `new T()`, field type, `this` → enclosing type). Un-inferable receivers (chains, generics, external/predefined types) return `unknown`, never a guess. Wired into `csharp_adapter.py`.
- [x] **Stage 1** — Receiver-type channel: `Edge.receiver_type` field (base.py) + nullable `edges.receiver_type` column + additive migration `_migrate_edge_receiver_type` (db.py). Read exclusively by `call_resolver`.
- [x] **Stage 1** — `call_resolver.py` receiver-typed regime: restrict candidates to those whose **owning type's name** (via OWNS edges — language-neutral, no FQN string-parsing) matches the hint; a unique match resolves with graded `confidence` = `_TYPED_CONFIDENCE` (0.9, above the ADR-008 §5 floor, below the 1.0 unique-name grade). A hint matching 0 or ≥2 candidates stays **unresolved with no positional fallback** (§2, prefer-unknown).
- [x] **Stage 1** — Tests (`tests/test_receiver_type_resolution.py`, 16, CPU-only): every exact strategy, prefer-unknown paths, floor-clearing + graph traversal, and bare-path regression. Full suite 219 pass; `src` flake8 clean.
- [x] **Stage 2** — C++ resolution pass in `src/adapters/cpp_adapter.py`: receiver via `->` or `.`, exact strategies (pointer/reference/value parameter, local declaration incl. `T x = init`, field type, `this->`). Receiver reused across in-class and out-of-class (`Repo::Save() {…}`) definitions; out-of-class defs get `this` but not fields (class body lives elsewhere). Shares `type_resolver.py`, `Edge.receiver_type`, and the language-neutral `call_resolver` regime unchanged from Stage 1. **C++ overload sets** (known type, several signatures on it) land as a non-unique match → left unresolved, never one arbitrary overload (§5).
- [ ] **Stage 2b** — heuristic member-chain strategy (`a.b().c()`) with a lower graded `confidence` (§3); still `unknown` today.
- [ ] **Stage 2/3** — Go/C resolution passes when those adapters land (future; ADR-017 promotion path).
- [x] **Stage 3** — Proved the §4 lift on C#/C++ fixtures. The ADR-008 *extraction* scorecard scores raw parse output (bare targets) and never runs the resolver, so it could not measure resolution; Stage 3 adds the **resolution** sibling — `tools/resolution_eval.py` + `tests/fixtures/resolution/{csharp,cpp}/` + `tests/test_resolution_conformance.py`, gated in CI (`--check-baseline`). Each fixture is indexed through the **real** pipeline (`parse_file` → `db.upsert_file` → `resolve_call_edges`) twice on the identical parse — with the `receiver_type` hint (ADR-011) and with it stripped (ADR-021) — so the delta is attributable to receiver typing alone. **Measured (both languages): resolution rate 0.40 → 1.00, precision held at 1.00, zero wrong edges.** Baseline resolves only the unique names and leaves every shared `Save()` unresolved; the hint resolves all of them correctly; the prefer-unknown sites (external receiver type, chained receiver) stay `unknown` in both regimes. No `src/` behavior change — Stage 3 is measurement only.
- [ ] Resolve **Depended on by**: confirm the graded resolved-edge contract ADR-012 consumes, before `accepted`. (ADR-012 is still planned — docs/adr-backlog.md — so the mechanism and its measured lift are complete, but status stays `proposed` until that downstream contract is confirmed.)

**Stage-1 known limits (by-design, prefer-unknown):**
- `UNIQUE(source_fqn, target, kind)` (predates this ADR) collapses two calls to the *same method name* from one source into one edge — so a method calling `Save()` on two different receiver types keeps only one hint. A **recall** limit, not precision: the surviving edge still resolves correctly to its type.
- Only exact strategies ship; `var x = SomeMethod()` (return-type inference), `a.b().c()` chains, generics, and non-in-repo types resolve to `unknown`. These are the graded lower-confidence strategies Stage 2 adds.

**Notes:**
<!-- 2026-06-18: The mechanism behind the ADR-008 precision number. Pairs with ADR-008 (shares Edge.confidence A3). Defaults: correctness gate = emit `unknown`, never a wrong resolved target; graded confidence per resolution strategy (mirrors competitor's 6-strategy scoring). Done when call-edge resolution rises on C++/C# with precision held (Go/C as their adapters land). Open: how far to push C++ templates; C++/C# adapters not Tier-A yet, Go/C adapters not yet built. Effort H. -->
<!-- 2026-07-17: Stage 1 landed — C# exact receiver-type inference on the §4/§5 confidence rail (stacked on that branch until #26 merges). C++ (Stage 2) and the ADR-008 harness measurement (Stage 3) are follow-ups; status stays `proposed` until the measured lift lands. -->
<!-- 2026-07-17: Stage 2 landed — C++ exact receiver-type inference (branch feature/adr-011-stage2-cpp, off Stage 1). call_resolver + the receiver_type channel were unchanged (already language-neutral); Stage 2 is purely type_resolver + cpp_adapter wiring. Overload sets stay unresolved by design. Full suite 230 pass; src flake8 clean. Only Stage 2b (heuristic chains) and Stage 3 (harness measurement) remain before `accepted`. -->
<!-- 2026-07-17: Stage 3 landed — the §4 referee (branch feature/adr-011-stage3-resolution-conformance). Discovered the ADR-008 harness measures EXTRACTION (raw parse, bare targets, never runs the resolver), so it structurally could not see resolution; built the resolution sibling (tools/resolution_eval.py + tests/fixtures/resolution + test_resolution_conformance.py + benchmarks/resolution/baseline.json + a dedicated CI gate). Each fixture scored twice on the same parse (hint vs stripped) so the delta is receiver-typing alone. Measured C# and C++ both: rate 0.40 → 1.00, precision held 1.00, 0 wrong edges. No src/ change — measurement only. Full suite 238 pass; new files flake8 clean. Status stays `proposed`: only the ADR-012-contract obligation remains (ADR-012 still planned). -->
Grammar/harness note: the resolution harness reuses `conformance_eval.normalize_fqn` for path-prefix stripping and indexes via the production `db.upsert_file` with no chunks (no embedder, no GPU). Ground truth is authored from source semantics with `null` as a first-class expected value — a resolver that resolves a null-expected site has manufactured a wrong edge (§2).
Grammar notes (tree-sitter): C# `this` is a bare `"this"` keyword node, not `this_expression`; a C# qualified generic (`Ns.List<int>`) ends in a `generic_name` child, so the final-segment name must be a plain `identifier` or it stays unknown. C++ receivers are `field_expression` for both `->` and `.` (receiver = `argument:` field, method = `field:` field); `this` is likewise a bare `"this"` node; declaration types are the first type child, read only when a plain `type_identifier`/`qualified_identifier`.
