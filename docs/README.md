# Docs map — codebase-indexer

Start here.

| Doc | What it's for |
|---|---|
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Contribution standards — change classification, branches, the ADR lifecycle, PRs. Read before starting work. |
| [`../src/CLAUDE.md`](../src/CLAUDE.md) | Architecture, the three-tier index, the RTR pipeline, the MCP tool surface. Read before touching code. |
| [`../README.md`](../README.md) | What the tool is and what it does. |

## Working lists

Three jobs, no overlap. Keeping them separate is deliberate — the ADR set carried all three at once
until 2026-07-27, and drifted in every direction: 19 of 25 ADRs read `proposed`, of which 6 were
actually built and 9 had never been started.

| Where | Holds | Never holds |
|---|---|---|
| [`backlog.md`](./backlog.md) | **wants** — problems and requests, with a source and a date | how you'll solve them, or ADR build state |
| [`roadmap.md`](./roadmap.md) | **order** — sequencing and dependency edges | wants, or build state |
| [`adr/`](./adr/) | **decisions** — one committed solution per file, plus its Implementation Log | wants you haven't committed to |
| [`adr-backlog.md`](./adr-backlog.md) | **history** — the 2026-06-18 research planning index, frozen | anything new |

The one-line test: **a backlog item asserts a problem, an ADR asserts a solution you are building.**
An item can sit unresolved forever without lying; an unbuilt ADR is a lie the moment it lands on
`master`.

**The convention: create the ADR in the first commit of the branch that builds it.** `proposed` is a
branch-only status; reaching `master` means `accepted`. A decision you think about and abandon dies
with its branch. See
[`CONTRIBUTING.md` §4.1](../CONTRIBUTING.md#41-architecture-decision-records-adrs).

## ADRs

One decision per file in [`adr/`](./adr/), from [`ADR-000-template.md`](./adr/ADR-000-template.md).
Numbers are sequential and **never reused**.

> **This index carries no statuses.** Each ADR's header is the truth for its own state; an index that
> repeats it drifts — which is exactly what happened to `adr-backlog.md`. Navigation only.

**Governance + foundations:** [001](./adr/ADR-001-engineering-governance.md) governance ·
[002](./adr/ADR-002-pre-expansion-hardening.md) pre-expansion hardening ·
[004](./adr/ADR-004-ci-observability-and-workflow-tooling.md) CI observability + commit traceability

**Extraction + the graph:** [003](./adr/ADR-003-adapter-architecture-and-language-expansion.md)
adapter architecture · [006](./adr/ADR-006-graph-analytics-and-community-detection.md) graph
analytics + communities · [021](./adr/ADR-021-baseline-call-edge-resolution.md) baseline call-edge
resolution · [011](./adr/ADR-011-high-precision-call-resolution.md) high-precision call resolution ·
[017](./adr/ADR-017-tiered-language-support.md) tiered language support *(unbuilt)*

**Measurement — the accuracy moat:** [007](./adr/ADR-007-evaluation-benchmark-harness.md) CoIR
benchmark harness · [008](./adr/ADR-008-measured-conformance-edge-confidence.md) measured conformance
+ edge confidence · [019](./adr/ADR-019-real-repo-retrieval-eval.md) real-repo retrieval eval

**Retrieval:** [009](./adr/ADR-009-retrieval-stack-modernization.md) retrieval stack modernization ·
[023](./adr/ADR-023-unify-mcp-tools-on-rtr-pipeline.md) unify MCP tools on RTR ·
[022](./adr/ADR-022-graph-neighbor-retrieval-scoring.md) graph-neighbor scoring *(deferred)* ·
[018](./adr/ADR-018-syntactic-clone-matching.md) syntactic clone matching *(unbuilt)*

**Index lifecycle:** [025](./adr/ADR-025-index-freshness-metadata.md) freshness metadata ·
[005](./adr/ADR-005-chunk-versioning-self-healing.md) chunk versioning *(unbuilt)* ·
[010](./adr/ADR-010-content-addressed-drift-detection.md) drift detection *(unbuilt)* ·
[016](./adr/ADR-016-persisted-symbol-tree.md) persisted symbol tree *(deferred stub)*

**Runtime:** [024](./adr/ADR-024-gpu-auto-device-selection.md) GPU auto-detection ·
[020](./adr/ADR-020-unified-device-resolution.md) unified device resolution

**Reach + research *(all unbuilt)*:**
[012](./adr/ADR-012-cross-repository-cross-service-graph.md) cross-repo graph ·
[013](./adr/ADR-013-domain-specific-industrial-adapters.md) industrial/DSL adapters ·
[014](./adr/ADR-014-usage-driven-adaptive-ranking.md) adaptive ranking ·
[015](./adr/ADR-015-local-graph-retrieval-explorer.md) explorer UI

## Contracts

- [`index-schema-contract.md`](./index-schema-contract.md) — the on-disk index schema.
- [`conformance-fixture-conventions.md`](./conformance-fixture-conventions.md) — how conformance
  fixtures are authored, and the integrity rule that a fixture is never edited to match the parser.

## Research (2026-06-18 — history, not spec)

The competitor study and the positioning work it produced. Where these disagree with the code, the
code won.

- [`study-codebase-memory-mcp.md`](./study-codebase-memory-mcp.md) — analysis of *Codebase-Memory:
  Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP* (Vogel et al.,
  arXiv:2603.27277), the competitor named in ADR-004.
- [`prior-art-depth-over-breadth.md`](./prior-art-depth-over-breadth.md) — the positioning thesis:
  fewer languages, provable accuracy. A language count is a *recall* claim; precision is the moat.
- [`references-code-intelligence.md`](./references-code-intelligence.md) — master bibliography, §A–§J.
- [`modernization-stack-review.md`](./modernization-stack-review.md) — per-pillar SOTA review; the
  source of ADR-009.
- [`merkle-tree-drift-handling.md`](./merkle-tree-drift-handling.md) — the design behind ADR-010.
- [`design-research-informed-improvements.md`](./design-research-informed-improvements.md) — Bucket A
  (engine) / Bucket B (accuracy moat) roadmap that became ADRs 007–015.
- [`suggestions-future-directions.md`](./suggestions-future-directions.md) — five candidate
  directions (S1–S5).
- [`agent-prompt-code-intelligence-architect.md`](./agent-prompt-code-intelligence-architect.md) —
  the architect-agent brief.
