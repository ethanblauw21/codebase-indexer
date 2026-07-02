# ADR-021: Baseline Call-Edge Resolution — Make the Traverse Step Actually Traverse

**Status:** accepted (2026-07-02) — resolver + COALESCE CTE implemented, tested, and merged; call edges now resolve for all five languages. (Note: making the *retrieval* Traverse step surface these neighbours at top-K is a separate, still-open problem — ADR-022.)
**Date:** 2026-07-01
**Branch:** `feature/adr-021-baseline-call-resolution`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — the language adapters emit the raw `CALLS` edges (`source_fqn` → **bare
  callee name**) and the `symbols` table (fqn ↔ name) that this pass resolves against.
  This ADR adds no new extraction; it resolves what the adapters already produce.

**Depended on by:**
- ADR-011 *(high-precision call resolution)* — this is the **baseline layer beneath
  ADR-011**. ADR-021 resolves the *unambiguous* call (one provable in-repo target);
  ADR-011's receiver-type inference resolves the *hard, ambiguous* receiver-typed case
  (`recv.Method()` / `obj->fn()`) with graded `Edge.confidence`. **Resolve before
  ADR-011 `accepted`:** confirm ADR-011 layers on top of (does not re-implement) the
  `resolved_target` write + `COALESCE` traversal established here, and add the reciprocal
  "depends on ADR-021" note into ADR-011.
- ADR-019 *(real-repo retrieval eval)* — arm **B (semantic+graph)** and the whole
  `graph-only` query class are **un-gradeable until this lands**: with unresolved CALLS
  edges the B−A graph lift is provably ~0 (measured: +0.003 ±0.007 on p-queue). **Resolve
  before ADR-019 `accepted`:** re-run arm A vs B post-ADR-021 and confirm a measurable,
  non-zero graph lift before authoring `graph-only` gold.

## Context

The engine's headline differentiator is the **Retrieve → Traverse → Rerank** pipeline
(`src/hybrid_retriever.py`): after semantic retrieval, it expands the call graph one hop
to pull in structurally-related code that pure embedding search misses. ADR-019's arm
A/B ablation was built to *measure* that Traverse step — and immediately exposed that it
contributes **zero usable nodes**. The graph layer is a retrieval no-op.

Two independent root causes, both confirmed by direct probe on a freshly-indexed repo
(`p-queue`, `PriorityQueue.enqueue → lowerBound`):

1. **Resolution is never written.** The adapters emit `CALLS` edges whose `target` is the
   **bare callee name** (`lowerBound`, `enqueue`). `src/incremental_indexer.py` resolves
   `resolved_target` **only for `IMPORTS` edges** (line ~372); every `CALLS` edge keeps
   `resolved_target = NULL`. So even a call with exactly one possible in-repo target — a
   free function, a uniquely-named method — is left unresolved.
2. **Resolution is never consumed.** The traversal CTE (`db._CALL_GRAPH_SQL`) walks
   `e.target` (the bare name). Its final `JOIN symbols s ON s.fqn = cg.fqn` therefore joins
   a bare name against fully-qualified `symbols.fqn` and always misses → the node comes
   back with `file_path = NULL` → `HybridRetriever._expand_structurally_budgeted` skips it
   (`node.file_path is None: continue`). Even if cause #1 were fixed, the CTE would ignore
   the resolved target.

The result: `get_call_graph("…::lowerBound")` returns only bare-name callee nodes
(`push`, `splice`, `at`, `lowerBound`) — all `file_path=None`, all discarded — and the
`called_by` (reverse) direction never fires at all, because no edge's `target` equals a
real FQN. Arm B collapses to arm A.

This is squarely a **prefer-unknown** problem (the moat ADR-008/ADR-011 codify): the fix
must make the *provable* edges real **without inventing wrong ones**. Note this is the
*easy, language-general* half of call resolution; the *hard* receiver-typed half
(ADR-011) needs type inference and is explicitly out of scope here.

## Decision

Two small, coupled changes — one write-side, one read-side — plus an idempotency rule.

### §1 — Resolution pass (`src/call_resolver.py`): resolve only the provable call

A finalization pass, run at the end of `run_incremental` once the full `symbols` table
exists, sets `resolved_target` on `CALLS` edges whose target resolves to **exactly one**
in-repo symbol. For each `CALLS` edge `(source_fqn, bare_name)`, candidates =
`symbols WHERE name = bare_name`, then, in priority order:

1. **Unique repo-wide** — exactly one candidate → resolve to its `fqn`.
2. **Same-file** — >1 candidate but exactly one in `source_fqn`'s file → resolve to it.
3. **Import-scoped** — >1 candidate but exactly one in a file the source file `IMPORTS`
   (via `IMPORTS.resolved_target`) → resolve to it.
4. **Ambiguous / none** — anything else (0 candidates = external/library call; ≥2 after
   the above filters = genuine name collision) → **leave `resolved_target = NULL`.**

The pass never writes a target it cannot prove unique — that is the load-bearing rule
(inherited from ADR-011 §2 / ADR-008 §5: emit `unknown`, never a wrong edge). It adds no
confidence grading (that is ADR-011's graded-`Edge.confidence` layer); every edge it
resolves is, by construction, high-confidence.

### §2 — Traversal consumption: `COALESCE(resolved_target, target)`

`db._CALL_GRAPH_SQL` is changed to traverse on `COALESCE(e.resolved_target, e.target)` in
both recursive branches (forward *calls* and reverse *called_by*). Resolved edges now walk
to a real FQN whose `symbols` join yields a `file_path`, so the retriever includes the
neighbour; unresolved edges still degrade to the bare name → `file_path=NULL` → skipped,
exactly as today. `get_graph_edges` (ADR-006 analytics) already prefers `resolved_target`
(db.py ~line 842), so community detection benefits from the same resolved edges for free.

### §3 — Idempotency & incrementality

The pass recomputes resolution for **all** `CALLS` edges each run (bounded: one indexed
scan over `edges` × a `name → fqn` map built once from `symbols`). This keeps it correct
under incremental updates: if a second symbol named `foo` is later added, `foo`'s calls
fall from "unique repo-wide" to ambiguous and are **demoted back to `NULL`** — a stale
resolution can never outlive the uniqueness that justified it. No partial/dirty state.

### §4 — Scope & limits (honest current state)

- **No receiver-type inference.** `recv.Method()` where several classes define `Method`
  stays unresolved here — that is ADR-011's job. ADR-021 deliberately resolves only what
  a name + import scope proves.
- **External calls stay unknown.** Calls into libraries/builtins (`push`, `resolve`,
  `Symbol`) have no in-repo symbol → `NULL`, by design (not a miss to fix).
- **Recall is bounded by provable uniqueness**, not engine capability — the ambiguous
  tail is recoverable later via ADR-011, never at precision's expense.

## Consequences

**Better:**
- The Traverse step produces real structural neighbours for the first time — arm B can
  now beat arm A, and ADR-019's `graph-only` query class becomes authorable/gradeable.
- Language-general: fixes Python/TS/JS/C#/C++ at once (it resolves by name+scope, not by
  language-specific typing), so the graph layer works on every Tier-A language.
- ADR-006 community detection and any `get_graph_edges` consumer get denser, correct
  intra-repo edges with no extra work (already `COALESCE`-aware).
- Precision-preserving by construction: only unique-target edges are asserted.

**Worse:**
- A full re-index (or at least a re-run of the finalization pass) is needed to populate
  `resolved_target` on existing indexes — the eval corpora must be rebuilt once.
- Recomputing all CALLS edges each run is O(edges); negligible at current repo sizes but
  a (documented) cost to revisit if very large repos are indexed.
- The same-file / import-scoped tie-breaks are heuristics; they are conservative (a tie
  they can't break stays `NULL`), so the failure mode is lost recall, never a wrong edge.

**Neutral:**
- Adds no new dependency and no new schema (the `resolved_target` column already exists).
- Leaves `Edge.confidence` (ADR-008/011) unbuilt; ADR-021's edges are implicitly
  high-confidence and can be back-annotated when that field lands.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Resolve inside the recursive CTE by name-JOIN at query time | Pushes ambiguity + scope logic into a recursive SQL CTE (hard to express, impossible to unit-test cleanly) and re-resolves on every query instead of once per index. |
| Resolve by bare name against `symbols.name` with no uniqueness gate | Manufactures wrong edges on every name collision (`add`, `size`, `remove`) — a direct violation of the prefer-unknown moat; a wrong edge is worse than a missing one. |
| Just fix the CTE (`COALESCE`) without writing `resolved_target` | No-op: `resolved_target` is NULL for all CALLS edges, so `COALESCE` falls through to the bare name and nothing changes. Both halves are required. |
| Fold this into ADR-011 | ADR-011 is tightly scoped to receiver-typed inference + graded confidence + the (unbuilt) ADR-008 harness. The unambiguous, language-general baseline is smaller and shouldn't block on that framing — it is the layer ADR-011 builds on. |
| Wait for ADR-016 persisted symbol tree | The `symbols` table already carries fqn↔name; resolution needs nothing ADR-016 adds. No reason to serialize behind it. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] `src/call_resolver.py`: `resolve_call_edges(db)` — unique / same-file / import-scoped
      resolution with the correctness gate; returns counts (resolved / ambiguous / external).
- [x] Change `db._CALL_GRAPH_SQL` to `COALESCE(resolved_target, target)` in both recursive
      branches; keep the `file_path=NULL` skip for still-unresolved edges.
- [x] Wire the pass into `run_incremental` finalization (after ingest, before save); print
      a one-line resolution summary. Invalidate the graph cache.
- [x] Tests (`tests/test_call_resolver.py`, no embedder): unique resolves; collision stays
      NULL; same-file + import-scoped tie-breaks; external (0-candidate) stays NULL; demotion
      when a name becomes ambiguous; `get_call_graph` returns resolved nodes *with* file_path.
- [x] Validate on p-queue (real index): 18 resolved / 0 ambiguous / 60 external;
      `get_call_graph` now returns real structural neighbours with file_path. Full ADR-019
      arm A-vs-B B−A remeasurement is deferred to ADR-019 (its Depended-on-by obligation).
- [x] Added reciprocal "Depends on ADR-021" note into ADR-011 on this branch.
- [ ] Resolve **Depended on by**: add reciprocal link into ADR-019 (graph-arm unblock) when
      that branch resumes; confirm ADR-011 layers on top before its `accepted`.

**Notes:**
<!-- 2026-07-01 (validation): resolve_call_edges on the real p-queue index → 18 resolved,
     0 ambiguous, 60 external. get_call_graph("…::lowerBound") now returns a called_by edge
     to PriorityQueue.enqueue WITH file_path (was empty before); enqueue surfaces callers
     PQueue.add + setPriority and callee lowerBound. External builtins (push/splice/at) stay
     file_path=None as designed. Full suite: 92 passed. -->
<!-- 2026-07-01: Discovered while wiring ADR-019's arm A/B ablation — the Traverse step was a
     retrieval no-op (CALLS edges unresolved + CTE ignores resolved_target). Confirmed by
     probe (all expanded nodes file_path=None) AND empirically (B−A = +0.003 ±0.007 on
     p-queue, CI includes 0). Reviewer chose a new ADR under ADR-011 (not an expansion of it):
     ADR-021 = precision-first, language-general, unambiguous-only resolution + CTE
     consumption; ADR-011 = the hard receiver-typed inference layered on top. -->
