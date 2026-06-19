# ADR-012: Cross-Repository & Cross-Service Graph — Provable Edges Beyond One Repo

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-012-cross-repository-cross-service-graph`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-008 — needs the **`Edge.confidence` field** to mark cross-service edges that are only heuristically inferred vs. contract-verified.
- ADR-011 — needs the **graded-confidence resolved-edge contract** as the in-repo precision foundation; cross-repo identity builds on resolved in-repo identity.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-012 bucket + build kit), suggestions S3,
> design-doc A5, study (competitor HTTP_CALLS). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md). **Caution:** [21] LogicLens is
> close prior art — differentiate on provability.

## Context

The graph stops at the repository boundary. Real systems span repos and services: a frontend calls a
backend over HTTP, services talk over async queues, a shared library is consumed by many repos. The
competitor emits `HTTP_CALLS` edges (study), and [21] LogicLens does cross-service analysis — both are
**close prior art**. Our differentiation is the same one as everywhere else: **provability.** A cross-service
edge should be `candidate` (heuristic) *unless* a parsed contract — OpenAPI, protobuf, a manifest —
*verifies* it, in which case it carries provenance and high confidence.

Two scopes are in play:
- **S3 — multi-repo linked index:** one index spanning several repositories, with cross-repo symbol
  identity so a call into a shared library resolves across the boundary.
- **A5 — cross-service edges:** HTTP/async edges between services, resolved via verifiable contracts where
  they exist, marked `candidate` when only heuristic (e.g. a string URL matched to a route by convention).

There is **no project/repo node in the schema today** — `src/db.py`'s tables are
files / symbols / chunks / edges / symbol_references / symbol_types — so the multi-repo schema is
**introduced from scratch** by this ADR. (The only adjacent existing machinery is project-*descriptor*
ingest — `.csproj` / `.sln` / `compile_commands.json` parsed for edges in `incremental_indexer.py` — which
is build-file edge extraction, **not** a graph node to build on.) This is Wave 3 reach work; it depends on
the edge-confidence machinery (008/011) being in place so cross-service edges can carry honest provenance
instead of being asserted.

## Decision

Extend the index to **span multiple repositories** and emit **cross-service edges whose confidence reflects
whether a machine-checkable contract verifies them.** Differentiate from LogicLens on *provability*: a
verified cross-service edge cites the contract it came from; an unverified one is plainly `candidate`.

### §1 — Multi-repo linked index (S3)

**Introduce** a multi-project/repo schema in `src/db.py` — a net-new project/repo node plus a `project_id`
on the relevant rows (**no such node exists today**) — so several repos index into one linked graph.
Cross-repo **symbol identity** is the hard part (an Open Question): a symbol's
in-repo `stable_id` is file-scoped, so resolving "repo A calls a function defined in shared library repo B"
needs a cross-repo identity scheme layered on ADR-011's in-repo resolution. `src/import_resolver.py` and
`src/MCPServer.py` (multi-repo selection) are touched.

### §2 — Contract-verified cross-service edges (A5)

A new **cross-service extractor** parses machine-checkable contracts and emits edges accordingly:
- **OpenAPI** (`prance` / `openapi-spec-validator`) — a client call to a path is verified against the spec's
  declared routes.
- **protobuf** (`protobuf`) — gRPC service/method definitions verify RPC edges.
- **manifests** — declared service dependencies.

**Confidence rule (the differentiator):** a cross-service edge is `candidate` (low confidence) **unless** a
parsed contract verifies it, in which case it is high-confidence **and carries provenance** (which contract,
which declaration). A bare string-URL-to-route guess stays `candidate`. This is exactly the prefer-unknown
moat applied across the network boundary — and the thing that distinguishes us from LogicLens's heuristic
cross-service edges.

**Mantra 1 (local-first).** Every cross-service edge is derived by parsing **static contract files committed
in the repos** (OpenAPI / proto / manifests) — never by tracing live traffic or calling a running service.
That offline, static-evidence basis is both the privacy guarantee *and* the precise differentiator from
runtime-tracing tools like LogicLens.

### §3 — Provenance on every cross-service edge

Every cross-service edge records *how* it was derived (contract-verified + source, or heuristic). This is
the auditability claim: a consumer can see whether "service A calls service B" is proven by a spec or merely
guessed from a URL string. Reuses the `Edge.confidence` field (ADR-008) plus a **net-new, additive
edge-provenance attribute** — the current `Edge` is `source_fqn/target/kind/resolved_target` (+ `confidence`
from ADR-008), and provenance is one more additive field, so existing edges need no migration.

## Consequences

**Better:**
- The graph models real multi-service systems, not just single repos — a substantial reach increase.
- Cross-service edges carry honest confidence + provenance, so "A calls B" is qualified by whether a
  contract proves it — the provability differentiation from LogicLens and the competitor's `HTTP_CALLS`.
- Reuses the 008/011 confidence machinery rather than a parallel system. (The project/repo schema itself is
  net-new — there is no existing `Project` node to build on.)

**Worse:**
- Cross-repo symbol identity is a genuinely hard, unsolved-here problem (Open Question); a weak scheme would
  produce wrong cross-repo edges — the exact failure the moat forbids.
- New dependencies (`prance` / `openapi-spec-validator`, `protobuf`) and a new extractor subsystem.
- LogicLens is close prior art; differentiation must be *demonstrated* (provability), not just asserted, or
  the work looks derivative.
- Heuristic cross-service edges (no contract) are inherently noisy — kept safe only because they're
  `candidate` and firewalled from verdicts by the ADR-008 §5 prefer-unknown floor.

**Neutral:**
- Single-repo behavior is unchanged when only one repo is indexed and no contracts are present.
- Effort H; sits in Wave 3, gated behind the edge-confidence foundation.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Emit cross-service edges heuristically like the competitor's `HTTP_CALLS` / LogicLens | Manufactures unverified cross-service edges with no provability distinction — abandons the moat and looks derivative of close prior art. |
| Cross-service edges always high-confidence | A string-URL match is not proof; confidence must reflect whether a contract verifies the edge. |
| Reuse in-repo `stable_id` directly across repos | File-scoped IDs collide/ambiguate across repos; cross-repo identity needs its own scheme on top of ADR-011 resolution. |
| Skip contract parsing, infer everything | Loses the one thing that differentiates us from LogicLens; contract verification *is* the value. |
| Defer until a real multi-repo user exists | Reasonable to sequence in Wave 3, but the schema seam (`Project` nodes, confidence field) is laid by 008/011 so the design is recorded now. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Introduce the multi-project/repo schema in `src/db.py` (net-new project/repo node + `project_id` — no existing Project node); cross-repo symbol identity scheme.
- [ ] Multi-repo selection in `src/MCPServer.py`; `src/import_resolver.py` cross-repo path.
- [ ] Cross-service extractor: OpenAPI (`prance`/`openapi-spec-validator`), protobuf, manifests.
- [ ] Confidence rule: contract-verified ⇒ high confidence + provenance; heuristic ⇒ `candidate` (ADR-008 `Edge.confidence`).
- [ ] Edge-provenance note on every cross-service edge.
- [ ] Demonstrate two linked repos queryable + cross-service edges carrying provenance/confidence.

**Notes:**
<!-- 2026-06-18: Wave 3 reach. Default: cross-service edges are `candidate` unless a parsed contract (OpenAPI/proto/manifest) verifies them. Differentiate from LogicLens [21] and the competitor's HTTP_CALLS on provability. Open: cross-repo symbol identity; differentiation from LogicLens. NOTE: project/repo schema is NET-NEW — no Project node exists in db.py today; the earlier "already exist" claim was phantom. Cross-service edges from STATIC contract parsing (offline, Mantra 1). Effort H. -->
