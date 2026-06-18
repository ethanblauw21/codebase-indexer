# ADR-007: Evaluation & Benchmark Harness — A Retrieval Scorecard the Rest of the Roadmap Can Stand On

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-007-evaluation-benchmark-harness`
**Reviewer:** @ethanblauw21
**Depends on:** none — this is Wave 0, the foundation. It only extends the existing `tools/eval_retrieval.py` and reads the index we already build.
**Depended on by:**
- ADR-008 *(planned — docs/adr-backlog.md)* — Measured Conformance reuses this ADR's **harness pattern** (fixture → run → metric → committed baseline) for its precision/recall *extraction* arm. It needs the harness to be the established shape so the extraction arm is a sibling, not a parallel invention.
- ADR-009 *(planned)* — Retrieval Modernization needs the **committed Wave-0 baseline numbers** and a **fast CI subset** so every component swap (embedder, fusion, reranker) can be validated as a measurable lift rather than a claim.
- ADR-014 *(planned)* — Adaptive Ranking needs a **held-out evaluation split** to prove tuned weights beat static fusion without overfitting.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-007 bucket + build kit). This ADR
> assembles that research; numbers/paths are from the 2026-06-18 audit and must be re-verified at
> implementation time. Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

The project sells **provable** code structure (the depth-over-breadth thesis, ADR-004). But "provable"
currently means a passing conformance suite — a binary, per-adapter gate — not a *number we can put on a
README*. The accuracy moat (ADR-008) and the modernization work (ADR-009) are both literally unprovable
without a scorecard: there is no way to say "this embedder swap helped retrieval by X" or "our extraction
precision is Y" because we have no standing benchmark and no committed baseline.

What exists today is `tools/eval_retrieval.py`: a small, hand-curated set of ~10 queries with expected
results, run ad hoc. It is enough to catch a gross regression and nothing more. It is single-repo,
single-language-mix, and its grading is whatever the author eyeballed. The competitor analysis
([study-codebase-memory-mcp.md](../study-codebase-memory-mcp.md) §9.5) flagged exactly these validity
holes in *their* evaluation — blind grading absent, single repo, no per-language breakdown — and we would
inherit every one of them if we extended the ad-hoc harness without fixing them.

The field has a standard for this: **CoIR** ([36]), a code information-retrieval benchmark with established
qrels (relevance judgments) across code-retrieval and text↔code tasks. Adopting CoIR gives us automated,
reproducible grading against a published gold standard instead of author judgment — and a number other
people already understand.

This ADR is the **shared substrate** the backlog calls Wave 0: "nothing else is provable without it."

## Decision

Adopt **CoIR** as the standard retrieval benchmark and grow `tools/eval_retrieval.py` from an ad-hoc smoke
check into a **standing harness** that reports retrieval quality *and* operational cost, baselines the
current stack, and commits those numbers to the repo. This ADR owns the **retrieval** arm only; the
**extraction** (symbol/edge precision/recall) arm is ADR-008's, built to the same pattern.

### §1 — Metrics

Report, per run:
- **Retrieval quality:** MRR@10, NDCG@10, Recall@{1,5,10} — the standard ranked-retrieval set, graded
  automatically against CoIR qrels.
- **Operational cost:** tokens consumed, tool-calls issued, and wall-clock latency per query. These are
  first-class, not afterthoughts — the project's whole pitch is token-efficient retrieval for agents
  (A7 perf targets, design-doc), so a quality win that doubles tokens is not a win.

A run emits one machine-readable record (JSON) and one human-readable table.

### §2 — CoIR subset selection

CoIR is broad; we index a focused 5-language stack. Select the CoIR subtasks that are **representative of
our corpus**: code-retrieval and text↔code tasks whose languages overlap ours (Python, TS/JS, C#, C++).
Record the chosen subset explicitly in config so the benchmark is reproducible and the selection is
auditable rather than implicit.

Two open modeling questions are acknowledged, not hidden (see Open Questions): *which* CoIR subtasks are
truly representative, and how to **project our 3-tier index onto CoIR's flat corpus** (CoIR assumes a flat
document set; our index is tiered skeleton/chunk/symbol).

### §3 — Two run profiles: full vs. CI subset

- **Full benchmark:** the complete selected CoIR subset, run on demand, produces the numbers we publish.
- **Fast CI subset:** a small, fixed sample runnable in CI on every change to the retrieval path, so a
  regression is caught at the PR, not at the next manual full run. The CI subset is a *regression tripwire*,
  not a publishable measurement.

### §4 — Grading is automated, blind by construction

Grading is **automated against CoIR qrels** — no human in the loop, which structurally eliminates the
blind-grading validity hole the competitor analysis identified. There is no author judgment to bias because
there is no author judgment at all. Per-language breakdowns are reported (not a single blended number),
fixing the second validity hole (single aggregate hides per-language weakness).

### §5 — Where it lives

- `tools/eval_retrieval.py` — extended beyond the fixed query set into the CoIR-driven harness; keeps the
  legacy hand-curated queries as an additional smoke layer.
- `benchmarks/` — new directory: cached/pinned CoIR subset references, committed baseline result records,
  and the published table.
- `indexer.toml` — new `[eval]` block: chosen CoIR subtasks, metric list, CI-subset size, baseline path.

### §6 — The committed baseline is the deliverable

The point of Wave 0 is a **checked-in baseline for the current stack** — the line every later wave must
beat. Wave 0 is not done when the harness runs; it is done when the current stack's numbers are committed
to `benchmarks/` so ADR-009's lift is measured against a fixed, version-controlled reference.

## Consequences

**Better:**
- Every later quality claim (ADR-008 extraction precision, ADR-009 retrieval lift, ADR-014 tuned ranking)
  becomes a *measured delta against a committed baseline* instead of an assertion.
- Adopting CoIR means our numbers are comparable to published work, not a private metric only we trust.
- Automated qrel grading removes human grading bias by construction — the exact validity hole we criticized
  in the competitor is structurally absent here.
- Token/tool-call/latency reporting keeps the efficiency pitch honest: regressions in cost are as visible
  as regressions in quality.

**Worse:**
- New dependency surface: `coir-eval` (or pulling CoIR via HF `datasets`) plus the cached corpus, which has
  storage cost.
- Projecting a tiered index onto CoIR's flat corpus is a genuine modeling problem; a poor projection would
  understate our true quality. Mitigated by recording the projection method as an auditable config choice.
- A standing benchmark is maintenance: CoIR versions move, and the baseline must be re-cut deliberately when
  the stack legitimately changes (a baseline you silently overwrite is no baseline).

**Neutral:**
- Read-only over the index — the harness never mutates the index or schema, so it carries no migration risk.
- This is the retrieval arm only; the extraction arm (ADR-008) is a deliberately separate scorecard sharing
  the same pattern, so the two can evolve independently.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep extending the ad-hoc 10-query set | Inherits every validity hole we criticized: author grading, single repo, no per-language breakdown, no standard others recognize. A bigger hand-curated set is still hand-curated. |
| Build a bespoke in-house benchmark instead of CoIR | Reinvents qrels we'd have to author and defend; loses comparability to published work; recreates the grading-bias problem CoIR's published qrels solve. |
| Human-graded relevance | Reintroduces the exact blind-grading validity hole flagged in the competitor analysis; not reproducible; doesn't scale to CI. |
| One blended quality number | Hides per-language weakness — the thing the depth-over-breadth thesis most needs to see. Per-language breakdown is required. |
| Defer the harness until after a modernization win | Backwards: without the baseline the "win" is unmeasurable. The backlog is explicit that this is Wave 0, gated before everything. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Add `coir-eval` (or HF `datasets` pull) dependency; cache the selected CoIR subset under `benchmarks/`.
- [ ] Choose + record the representative CoIR subtask set in `indexer.toml` `[eval]`; document the rationale.
- [ ] Decide and document the tier→flat-corpus projection (the load-bearing modeling choice).
- [ ] Extend `tools/eval_retrieval.py`: CoIR runner, MRR@10 / NDCG@10 / Recall@{1,5,10}, plus tokens / tool-calls / latency capture.
- [ ] Define the fast CI subset; wire it as a regression tripwire on retrieval-path changes.
- [ ] Cut and **commit the Wave-0 baseline** for the current stack to `benchmarks/` (the actual deliverable).
- [ ] Resolve every downstream obligation in **Depended on by** (ADR-008 harness pattern, ADR-009 baseline + CI subset, ADR-014 held-out split) before setting status to `accepted`.

**Notes:**
<!-- 2026-06-18: Wave 0. Retrieval arm only; extraction precision/recall is ADR-008's sibling scorecard. Default metrics MRR@10 + NDCG@10 + Recall@{1,5,10} + tokens/tool-calls/latency; CoIR subset matched to our 5 languages; grading automated vs qrels (no human). Open: representative subtask set; tier→flat projection. -->
