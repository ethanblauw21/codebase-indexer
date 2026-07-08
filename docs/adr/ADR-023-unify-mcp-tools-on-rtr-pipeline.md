# ADR-023: Unify the MCP Tools on the RTR Pipeline — One Retrieval Surface, Edge-Aware Verdicts

**Status:** proposed
**Date:** 2026-07-08
**Branch:** `feature/adr-023-unify-mcp-retrieval`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-017 — the `Edge.candidate` field (§3, landed in the ADR-017 P1 data-model slice) and the
  **three-state verdict + safe-direction rule (§7)** this ADR implements in the verdict tools.
  Without the field the verdict tools have no candidate/resolved signal to gate on.
- ADR-021 — the resolved `CALLS` edges + `COALESCE(resolved_target, target)` traversal. The
  back-ported structural signal is only meaningful because neighbours now resolve to real FQNs.
- ADR-007/ADR-009 — the RTR pipeline (`HybridRetriever`) and its measured Wave-0 default
  (RRF top-10, reranker off) are the surface every tool is routed onto; this ADR must not change
  that default behaviour, only extend its reach.

**Depended on by:**
- ADR-008 — its §4 verdict floor-gating **reparameterizes the boolean three-state rule this ADR
  installs** (`instead of the boolean`, ADR-008 §4). ADR-008 must land *after* this ADR. Recorded
  reciprocally in ADR-008.
- ADR-022 — the graph-scoring/reserved-slot work tunes `_expand_structurally_budgeted`, the single
  expansion path this ADR makes every tool share; doing the unification first means ADR-022 tunes
  one surface, not eight.

## Context

The engine's headline is the **Retrieve → Traverse → Rerank** pipeline (`src/hybrid_retriever.py`):
semantic FAISS retrieval, one-hop call-graph expansion with import-corroboration labels, and an
optional reranker. ADR-021 made the Traverse step produce real structural neighbours.

But only **one** MCP tool actually uses it. `investigate_architecture` calls
`HybridRetriever.retrieve()`; the other eight run their **own raw multi-tier FAISS search** and
never touch the graph:

| Tool | Today | Class |
|---|---|---|
| `semantic_code_search` | `t1/t2/t3.search(10)` + ad-hoc merge | Search |
| `find_similar_code` | `t1.search(15)` + heuristics | Search |
| `analyze_blast_radius` | `t1/t2.search(30)` + regex import checks | **Verdict** |
| `detect_pattern_violations` | `t1/t2.search(60/30)` + regex | **Verdict** |
| `find_dead_code` | `t1/t2.search(30/30)` + regex import checks | **Verdict** |
| `trace_data_flow` | `t1/t2.search(80/40)` + regex db-write detection | Tracing |
| `find_test_coverage` | `t1/t2/t3.search(20/20/10)` | Discovery |
| `find_unabstracted_collection_reads` | `t1.search(40)` | Discovery |

Two costs follow. **(1) The resolved call graph + import-corroboration (ADR-021) reach exactly one
tool.** Every other tool is blind to the structural signal the project advertises — a blast-radius
that ignores the call graph is the sharpest example. **(2) The verdict tools are graph-blind by
construction**, so ADR-017 §7's candidate-edge safe-direction rule has nowhere to live: you cannot
"block the *dead* verdict on any candidate reference" in a tool that never reads references. The
`Edge.candidate` field shipped in ADR-017 P1 has, today, **no consumer**.

## Decision

Route every retrieval-backed tool through a **single shared retrieval surface** built on
`HybridRetriever`, and make the three verdict tools **edge-aware** so ADR-017 §7's three-state rule
becomes real. Four coupled changes.

### §1 — One shared retrieval entry point

Introduce a module-level singleton accessor (generalizing the existing `_get_hybrid_retriever()` in
`MCPServer.py`) and a thin `search(query, ...)` helper returning `list[RetrievedChunk]`. Every
Search/Discovery/Tracing tool replaces its `t1/t2/t3.search(...)` block with a call to it. The tools
keep their **own output formatting and post-filters** (test-file detection, layer classification,
db-write regex) — those are tool-specific presentation, not retrieval, and stay. Only the *candidate
generation* is unified. The default behaviour is unchanged: RRF top-10, reranker off (ADR-007).

### §2 — Structural signal reaches every tool, for free

Because the shared surface runs the full RTR pipeline, every tool now receives structural neighbours
carrying `corroborated` (import-backed) labels and resolved-edge provenance. Tools that want a wider
candidate set pass a larger `k`; the pipeline's `_MAX_POOL_SIZE`/budget still bound cost. No tool
re-implements graph expansion.

### §3 — Edge-aware three-state verdicts (ADR-017 §7)

The three verdict tools gain a direct edge-graph read (`db.get_callers`/`get_callees`, which now
carry `CallGraphNode.candidate` from ADR-017 P1) and implement the **three response states** with the
**safe-direction rule** (ADR-017 §7), keyed on the **boolean** `candidate` today (ADR-008 §4 later
swaps the key to a confidence floor):

- **VERIFIED** — evidence is all non-candidate (or the single-candidate = resolved nuance).
- **ADVISORY** — a verified core plus a labelled candidate set (returned as a bounded checklist).
- **INSUFFICIENT** — genuinely no evidence.

Per-tool dangerous direction (ADR-017 §7):
- `find_dead_code` — **any** candidate reference *blocks* the "dead" verdict ("not provably dead —
  N unverified references"). Deletion is the one verdict whose wrong answer destroys data, so
  candidate evidence never clears it.
- `analyze_blast_radius` — candidate neighbours *expand* the radius in a separate **unverified**
  bucket, never silently merged into the verified count.
- `detect_pattern_violations` — candidate evidence *softens* an accusation to "possible violation —
  review", never a hard finding.

### §4 — `verify_candidate_edges` (ADR-017 §7.1)

A new snippet-returning tool: given a symbol (or an ADVISORY checklist), it returns each candidate
edge's `(caller_fqn, file, line)` **plus the code snippet** from the `DocumentStore`, with **zero
resolution logic** — the host agent is the verifier (§7.1). Advisory is enough for estimation; this
is the opt-in second pass required before irreversible action (the `find_dead_code` safety loop). The
existing reranker may pre-sort the checklist by plausibility when enabled.

## Consequences

**Better:**
- The resolved call graph + corroboration (ADR-021) reach **all** tools; blast-radius, dead-code and
  pattern-violation stop being graph-blind. The `Edge.candidate` field gains its first consumer.
- One retrieval path to tune, measure (ADR-019), and later graph-score (ADR-022) — not eight.
- Verdict tools become honest: three states with a safe direction, plus a cheap
  pre-filter → agent-verify loop that keeps `find_dead_code` safe in polyglot repos.

**Worse:**
- Behavioural change: tools that were pure top-k FAISS now include structural neighbours and the RRF
  fusion. Regression tests must pin the new expected output; a few tuned `k`s and regex post-filters
  need re-checking against the pipeline's output shape.
- The verdict tools grow from a binary to three states plus a new tool surface — more branches, more
  tests. (ADR-017 §7 anticipated this.)
- Touches the hot MCP path across many tools in one PR; mitigated by unifying Search/Discovery/Tracing
  first (mechanical) and the three verdict tools second (semantic), each with tests.

**Neutral:**
- The RTR default (RRF top-10, reranker off) is unchanged — this widens reach, not ranking policy.
- `map_module_communities` (pure graph analytics) and `reindex` (maintenance) are out of scope.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Leave the tools on raw FAISS; add graph only to verdict tools | Keeps six tools blind to the advertised structural signal and duplicates retrieval logic eight ways; the `Edge.candidate` field stays consumer-less. |
| One mega-tool that replaces the eight | Destroys the intent-specific docstrings that make the tools AI-legible (CLAUDE.md: docstrings describe *when* to call); the value is a shared *surface*, not a shared *tool*. |
| Put the three-state verdict logic in `HybridRetriever` | Verdicts are tool-intent-specific (dead-code blocks, blast-radius expands, violations soften); the retriever should stay a neutral candidate generator. Verdict logic lives in the tools. |
| Defer verdicts to ADR-008 (float floor) | ADR-008 §4 *reparameterizes* an existing boolean rule; the boolean rule has to exist first (this ADR). Building float-gating with no boolean rule and no consumer is dormant plumbing. |

## Testing Additions

| Area | Type | Notes |
|------|------|-------|
| Shared `search()` surface | Unit | Returns RTR `RetrievedChunk`s; default RRF top-10 unchanged vs a pinned fixture index |
| Each unified Search/Discovery/Tracing tool | Regression | Output pinned against the RTR pipeline; structural neighbours + corroboration present |
| Three-state verdicts | Unit | VERIFIED / ADVISORY / INSUFFICIENT per tool; single-candidate → VERIFIED |
| Safe-direction rule | Unit | dead-code candidate ref blocks "dead"; blast-radius candidate → unverified bucket; violation softens |
| `verify_candidate_edges` | Unit | Returns `(caller_fqn, file, line)` + snippet from `DocumentStore`; zero resolution logic |
| MCP server smoke | Integration | Server starts; every unified tool runs without error on the fixture index |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 1 — Shared surface + Search/Discovery/Tracing tools — DONE 2026-07-08**
- [x] Shared `_search()` helper + retriever singleton in `MCPServer.py`; added a `top_n` param to
      `HybridRetriever.retrieve()` (widens the semantic pool + structural cap so wide-k scan tools
      get real results, not padding; default `top_n=10` calls are byte-identical).
- [x] Routed `semantic_code_search`, `find_similar_code`, `find_test_coverage`,
      `find_unabstracted_collection_reads`, `trace_data_flow` through it; kept every tool-specific
      post-filter and full-file scan (producer detection, test-file naming, canonical-abstraction
      checks) — only *candidate generation* is unified.
- **Verification (no fixture-index test harness exists yet, so verified against the live `.code-index`
  + full unit suite):** captured each tool's output pre-refactor as a golden baseline, reconverted,
  re-ran. Server imports clean; no tool crashes. `semantic_code_search` + `find_test_coverage`
  byte-identical (RTR default = old RRF top-of-list; graph neighbours rank below the token-cap fill /
  the no-test branch short-circuits). `find_similar_code` (1741→1925), `trace_data_flow` (3771→3276),
  `find_unabstracted` (1055→1033) shifted to the RTR pool as intended — real callers now surface.
  Full unit suite 171 passed. **Follow-up:** a durable fixture-index regression harness (ADR-023
  testing table) is still owed; today's golden-diff was a one-shot local check, not a committed test.

**Phase 2 — Edge-aware three-state verdicts (ADR-017 §7)**
- [ ] `analyze_blast_radius`, `find_dead_code`, `detect_pattern_violations`: consume
      `get_callers`/`get_callees` candidate flags; implement VERIFIED/ADVISORY/INSUFFICIENT +
      safe-direction rule
- [ ] `verify_candidate_edges` snippet tool over `DocumentStore` (§7.1); optional reranker pre-sort
- [ ] Update ADR-017 §7 impl-log (verdict machinery lands here); resolve ADR-008 reciprocal note

**Notes:**
<!-- 2026-07-08: Split from the "unify MCP tools on hybrid_retriever" roadmap item (the biggest
     internal-quality refactor). Carries ADR-017 §7's verdict machinery, which was deferred out of the
     ADR-017 P1 data-model slice precisely because the verdict tools must be edge-aware — which is this
     refactor. Ordered before ADR-008 (§4 reparameterizes this boolean rule to a float floor) and
     before ADR-022 (tunes the single shared expansion path). -->
