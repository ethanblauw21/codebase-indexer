# ADR-007: Evaluation & Benchmark Harness — A Retrieval Scorecard the Rest of the Roadmap Can Stand On

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-007-evaluation-benchmark-harness`
**Reviewer:** @ethanblauw21
**Depends on:** none — this is Wave 0, the foundation. It extends the existing `tools/eval_retrieval.py`; per §7 it indexes CoIR's *own* corpus and reads nothing from the repo `.code-index`.
**Depended on by:**
- ADR-008 *(planned — docs/adr-backlog.md)* — Measured Conformance reuses this ADR's **harness pattern** (fixture → run → metric → committed baseline) for its precision/recall *extraction* arm. It needs the harness to be the established shape so the extraction arm is a sibling, not a parallel invention.
- ADR-009 *(planned)* — Retrieval Modernization needs the **committed Wave-0 baseline numbers** and a **fast CI subset** so every component swap (embedder, fusion, reranker) can be validated as a measurable lift rather than a claim.
- ADR-014 *(planned)* — Adaptive Ranking needs a **held-out evaluation split** to prove tuned weights beat static fusion without overfitting.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-007 bucket + build kit). This ADR
> assembles that research; numbers/paths are from the 2026-06-18 audit and must be re-verified at
> implementation time. Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

The project sells **provable** code structure (the depth-over-breadth thesis). But "provable"
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

Report, per run, across three groups. Quality says *whether* we found the right code; token economy and
operational cost say *what it cost to find it* — and for an agent-facing engine the cost half is not a
footnote, it is half the product.

**Group 1 — Retrieval quality** (graded automatically against CoIR qrels):
- **MRR@10** — rank of the first relevant hit.
- **NDCG@10** — graded-relevance ranking quality.
- **Recall@{1,5,10}** — coverage at depth.
- **Success@{1,5,10}** (a.k.a. Hit@k) — binary "was any relevant doc in the top-k"; cheap, intuitive,
  and the headline number a reader scans first.
- **MAP** (mean average precision) — averages precision across *all* relevant docs per query, the honest
  metric when a query has several (codefeedback-mt and the CodeSearchNet tasks carry multi-judgment qrels;
  MRR alone hides whether we found the 2nd and 3rd correct docs).

**Group 2 — Token economy** (the billing-efficiency moat; decomposed so a regression is attributable):
- **Query/input tokens** — cost of issuing the query.
- **Returned-context tokens** — tokens in the top-k payload an agent would actually pack into context.
  This is the billing-relevant number and the one the competitor pads.
- **Token efficiency** = returned-context tokens ÷ relevant docs retrieved — directly measures padding
  waste. A run that retrieves the same answers in fewer context tokens wins even at equal quality.
- **Budget adherence** — fraction of queries whose returned context fits under a configured token budget,
  plus the truncation rate. Mantra 3 (protect local 8B models from VRAM panics) is unprovable without it.
- **Corpus-embedding tokens** — the one-time index-build token cost, reported **separately** (amortized,
  not per-query) so build cost is never confused with query cost.

**Group 3 — Operational cost:**
- **Tool-calls issued** per query. *(Degenerate on single-shot CoIR retrieval — effectively 1. This metric
  earns its keep in the agentic/iterative eval, where one query can trigger several tool round-trips; it is
  captured here for schema parity with that future eval, not as a live signal.)*
- **Wall-clock latency** per query, reported as **mean, p50, and p95** — tail latency, not just the
  average, is what local interactive UX actually feels (Mantra 3).

A quality win that doubles returned-context tokens or p95 latency is not a win; the scorecard is designed
so those regressions are as visible as a quality regression. A run emits one machine-readable record (JSON)
and one human-readable table.

### §2 — CoIR subset selection

CoIR is broad; we index a focused 5-language stack (Python, TS/JS, C#, C++). But CoIR's **code-retrieval**
and **text↔code** tasks cover **Python, JavaScript, Go, Java, Ruby, PHP** — so the overlap we can actually
grade is **Python and JavaScript**; **C# and C++ have no CoIR coverage at all** (this gap is recorded plainly
in §9). Select the representative tasks and record the chosen subset explicitly in `indexer.toml [eval]` so
the benchmark is reproducible and the selection is auditable rather than implicit.

The **Wave-0 core set** (primary languages): `cosqa`, `stackoverflow-qa`, `codefeedback-mt`,
`CodeSearchNet-python`, `CodeSearchNet-javascript`. The remaining CodeSearchNet languages
(`go`/`java`/`ruby`/`php`) are an **extensible add-on batch** appended later under the incremental-baseline
mechanics of §6 — broad, reviewer-recognizable coverage without blocking Wave 0 on a multi-day run.

The once-open "how do we project a tiered index onto CoIR's flat corpus" question is **resolved in §7** — it
was a category error; we index CoIR's own corpus atomically rather than projecting our index onto it.

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

### §6 — The committed baseline is the deliverable (and it is incremental)

The point of Wave 0 is a **checked-in baseline for the current stack** — the line every later wave must
beat. Wave 0 is not done when the harness runs; it is done when the current stack's numbers are committed
to `benchmarks/` so ADR-009's lift is measured against a fixed, version-controlled reference.

The baseline is **append-structured — one JSON record per (subtask × config)** — so it can be cut in batches
over time without re-running finished work (the corpus embedding is the expensive step; on CPU the core set
is ~one overnight run, so batchability is not optional):
- A **`--subtasks` override** runs only the named tasks; records are **deduped on append** (re-running a
  task replaces its record, never duplicates).
- Every record stamps the **git SHA of the stack** that produced it. Batches are comparable *only* if the
  retrieval stack is unchanged between them — the SHA is the audit trail that proves it. Re-cut deliberately;
  a baseline you silently overwrite is no baseline.
- Practical consequence: the §2 primary-language core lands first; the broader CodeSearchNet languages append
  later and merge into the same baseline file under the same frozen stack.

### §7 — Resolved (2026-06-18): what gets indexed — the CoIR protocol

> Resolves the §2 "tier→flat projection" open question, which was a category error in the original framing.

CoIR is a *retrieval* benchmark: each subtask ships its own **corpus** (atomic code/answer documents with
ids), **queries**, and **qrels** (query→doc relevance). The benchmark measures a retrieval **stack** by
embedding *CoIR's own corpus* with that stack's embedder, building an index over it, retrieving top-k per
query, and grading the returned doc-ids against the qrels. **It does not touch the repo's `.code-index`** —
our repo index holds *our* code, whose ids are absent from CoIR's qrels, so grading our index against CoIR
would score ~0 and measure nothing. (This is the bug in the first harness draft, now corrected.)

Our three tiers are a **chunking strategy for whole repo files**; CoIR corpus documents are *already*
chunk-sized atoms. Therefore:

- **Wave-0 baseline = atomic-doc indexing (the default).** Each CoIR corpus doc → one embedding (our stack
  embedder + the stack's normalization / `max_seq_length` policy) → one FAISS index per subtask. Query →
  embed → search → doc-ids → grade. Projection is the identity (`"atomic"`). This is a clean, publishable
  **embedder/retrieval** baseline, directly comparable to published CoIR numbers, and is exactly the
  fixed reference ADR-009 needs.
- **Chunk-tier-pipeline measurement is a deliberately deferred, labeled path** — *not* the Wave-0 baseline.
  Measuring our full chunk→tier pipeline on CoIR means chunking each corpus doc into tiers, indexing all
  chunks with a chunk→doc backpointer, and projecting chunk-hits back to a single doc-id (`top_tier_per_file`
  = the doc of the best-ranked chunk). The `_project_*` strategies are retained for this path; they operate
  over *CoIR-corpus* hits, never the repo index. This path is future work, run and reported separately so it
  cannot be confused with the embedder baseline.

`[eval].tier_projection` therefore defaults to **`"atomic"`** for the Wave-0 baseline; the tier-projection
strategies remain documented options for the deferred chunked path.

### §8 — Pipeline configurations baselined

The shipped retrieval stack has optional stages, so the baseline records the **as-shipped configurations**,
not a single number:
- **dense** — the embedder alone (atomic top-k over the CoIR corpus). The core embedder/retrieval number.
- **dense + reranker** — the same retrieval followed by the cross-encoder rerank that ships by default,
  capturing the reranker's contribution. ADR-009 (Pillar 4) will swap that reranker, so this "before" must
  be on record.

Two honest caveats: (1) the production **3-tier RRF fusion** has nothing to fuse on CoIR's single flat
corpus, so it collapses to plain dense top-k here — fusion is exercised on our own repos, not CoIR (§9).
(2) The reranker pass is **priced separately** — it scales with query-count × rerank-depth, not corpus size,
so it can be run on the core set only rather than across every appended language batch.

### §9 — Coverage & limits (current state, with remediation under consideration)

CoIR is a real, publishable number for one specific layer; stating its boundaries *in this ADR* is part of
"correctness over breadth" (Mantra 2) — the number must never be oversold as system accuracy. These are
**honest current-state limits, not accepted permanent gaps**: each has a planned remediation under
consideration (none built yet — scoped here so the roadmap owns them rather than letting them hide).

- **Language gap (current state).** CoIR's code-search tasks cover Python, JS, Go, Java, Ruby, PHP — **not
  C# or C++**, two of our five target languages. As it stands today the CoIR scorecard says *nothing* about
  C#/C++ retrieval.
  *Planned (under consideration):* a separate **internal-repo eval** — curated queries with known answers
  over our own indexed C#/C++ code — reusing ADR-008's fixture machinery.
- **Layer gap (current state).** CoIR's corpus is flat — no call graph — so today this scorecard measures
  only the **semantic retrieval layer** (embedder ± reranker) and cannot exercise the
  Retrieve→**Traverse**→Rerank structural-graph expansion central to the engine.
  *Planned (under consideration):* the same internal-repo eval, which carries our extracted call graph and
  can therefore score the structural step CoIR cannot.
- **Labelling rule (now).** Until those land, published numbers are labelled "CoIR semantic-retrieval,
  {languages}" — never "system accuracy." The internal-repo eval is the intended closer for both gaps and is
  tracked toward ADR-008.

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
- New dependency surface: `datasets` (HF) plus the cached CoIR corpora, which carry storage cost (the large
  CodeSearchNet corpora are git-ignored and regenerated on demand, not committed).
- **Coverage gaps by construction (§9):** C#/C++ and the structural-graph layer are unmeasurable on CoIR, so
  the scorecard is a partial picture today. Mitigated near-term by honest labelling; the planned internal-repo
  eval (under consideration, §9) is the intended closer.
- **Compute cost:** cutting the baseline embeds whole corpora with our model — on CPU the core set is roughly
  an overnight run. Mitigated by the incremental, append-structured baseline (§6).
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

- [x] Add `coir-eval` (or HF `datasets` pull) dependency; cache the selected CoIR subset under `benchmarks/`.
  **Done (2026-06-19):** no new dependency — the CoIR-Retrieval datasets were already in the local HF cache,
  so "fetch" became a local extraction. `tools/coir_prepare.py` materializes `queries.jsonl` (test split),
  `qrels.tsv` (test split, score>0), and any missing `corpus.jsonl` into `benchmarks/coir/<task>/` offline
  (`HF_HUB_OFFLINE=1`). Large CSN corpora are git-ignored and regenerated on demand (§5/Worse).
- [x] Choose + record the representative CoIR subtask set in `indexer.toml` `[eval]`; document the rationale.
  **Done (2026-06-19):** `[eval]` block added — `subtasks` (5 core), `tier_projection="atomic"`,
  `configs`, `budget_tokens`, `ci_subtasks`/`ci_limit_queries`, `baseline_path`.
- [x] Decide and document the tier→flat-corpus projection (the load-bearing modeling choice). **Resolved §7
  (2026-06-18):** index CoIR's own corpus with our embedder; Wave-0 baseline uses atomic-doc indexing
  (`tier_projection = "atomic"`); chunk-tier projection is a deferred, separately-reported path.
- [x] CoIR runner (indexes CoIR's own corpus, §7), MRR@10 / NDCG@10 / Recall@{1,5,10}, plus tokens /
  tool-calls / latency capture. **Done (2026-06-19) — DEVIATION from the §5 plan:** built as a *new*
  `tools/coir_eval.py` rather than by extending `tools/eval_retrieval.py`. The legacy 10-query repo-eval
  is a fundamentally different shape (curated queries vs. the live `.code-index`), and folding the CoIR
  protocol into it would have entangled the two; cleaner as siblings. `eval_retrieval.py` is left intact
  as the smoke layer §5 intended. ADR §5 text still says "extended" — treat that as superseded.
- [x] Extend the metric set per the revised §1: **Success@{1,5,10}**, **MAP**, decomposed **token economy**
  (query / returned-context / corpus-embedding tokens, token-efficiency, budget-adherence + truncation rate),
  and **p50/p95** latency. **Done (2026-06-19):** full §1 metric set emitted per record; corpus-embedding
  tokens reported separately (amortized). Embedded once, with the complete metric set.
- [x] Implement the incremental-baseline mechanics (§6): `--subtasks` override, dedupe-on-append, and a
  git-SHA stamp on every record. **Done (2026-06-19):** `--subtasks`/`--config`/`--limit-queries` CLI;
  dedupe on `(subtask, config)`; every record stamped with the stack git SHA. Embedding is sharded and
  resumable (checkpoint every 20K docs) so a reboot mid-run does not lose work.
- [~] Run + record both pipeline configs (§8): **dense**, and **dense + reranker**. **dense: DONE
  (2026-06-19)** — all 5 core subtasks cut to `benchmarks/baseline.jsonl` under git `4950d3f`. Headline
  dense numbers: CSN-python MRR@10 0.937, stackoverflow-qa 0.874, CSN-javascript 0.733, cosqa 0.451,
  codefeedback-mt 0.407. **dense+reranker: investigated, then DEFERRED to ADR-009 (2026-06-21)** — the
  reranker (Qwen3-Reranker-0.6B) showed neutral-to-negative lift on CoIR after a real bug fix; running the
  full ~22–44 h baseline for a non-positive "before" was judged not worth the compute now. Full findings in
  the dated note below; the dense+reranker "before" is cheap to cut later under a frozen SHA when ADR-009
  picks a reranker deliberately.
- [x] Record the §9 coverage limits in the published table header (label = "CoIR semantic-retrieval,
  {languages}"). **Done (2026-06-19):** the scorecard footer prints the §9 label and the C#/C++ +
  structural-graph caveat. The **internal-repo eval** complement remains open (tracked toward ADR-008).
- [~] Define the fast CI subset; wire it as a regression tripwire. **Defined (2026-06-19):** `ci_subtasks`
  (`cosqa`) + `ci_limit_queries` (50) in `[eval]`, and `--limit-queries` supports it. **Not yet wired into
  CI** as an automated tripwire.
- [x] Cut and **commit the Wave-0 baseline** for the current stack to `benchmarks/` (the actual deliverable).
  **Done (2026-06-21):** `benchmarks/baseline.jsonl` holds the 5 **dense** core records (SHA `4950d3f`).
  The Wave-0 deliverable is explicitly the **dense** baseline; dense+reranker is deferred (above), so the
  baseline is "complete" for the dense config the §8 minimum requires. Diagnostic sampled reranker rows used
  during the investigation were removed from the committed file.
- [ ] Resolve every downstream obligation in **Depended on by** (ADR-008 harness pattern, ADR-009 baseline + CI subset, ADR-014 held-out split) before setting status to `accepted`.

**Notes:**
<!-- 2026-06-18: Wave 0. Retrieval arm only; extraction precision/recall is ADR-008's sibling scorecard. Default metrics MRR@10 + NDCG@10 + Recall@{1,5,10} + tokens/tool-calls/latency; CoIR subset matched to our 5 languages; grading automated vs qrels (no human). Open: representative subtask set. RESOLVED (§7): index CoIR's OWN corpus with our embedder (not the repo .code-index); Wave-0 = atomic-doc indexing, tier projection deferred. -->

- **2026-06-19 — reranker model corrected; Qwen3-Reranker wired in.** The configured reranker
  `jinaai/jina-reranker-v2-base-code` (§8 / `indexer.toml`) **does not exist** — a fabricated id (false
  analogy from the embedder's `-code` suffix); HF returns 401 for it. Both Jina v2/v3 rerankers are
  CC-BY-NC (non-commercial) + the v2 multilingual one is gated, so they were rejected for a commercial
  product. Adopted **`Qwen/Qwen3-Reranker-0.6B`** (Apache-2.0, ungated, code-strong, ~0.6B → CPU-feasible).
  It is a causal-LM yes/no scorer, not a sentence-transformers CrossEncoder, so the harness got a
  `Qwen3Reranker` wrapper + a `load_reranker()` factory (branches by model id; CrossEncoder remains the
  fallback path). Also surfaced: the **production** retriever (`src/hybrid_retriever.py`) hardcodes the same
  fabricated id and ignores `[reranker].model_id`, so its reranker has been silently RRF-only — fixing that
  (Qwen3 in production via the same scorer + actually reading config) is **ADR-009** scope, tracked there.
- **2026-06-19 — harness built and dense Wave-0 baseline cut.** Two new tools: `tools/coir_prepare.py`
  (offline data materialization from the HF cache) and `tools/coir_eval.py` (the standing harness). **Key
  deviation:** the harness is a *new* file, not an extension of `tools/eval_retrieval.py` (see the runner
  log item) — the §5 "extend" wording is superseded; the legacy script stays as the smoke layer. The run was
  CPU-only (14 threads), embedder `jinaai/jina-embeddings-v2-base-code` (dim 768, `max_seq_length` 512,
  normalized), FAISS `IndexFlatIP` per subtask. Embedding is sharded/resumable (20K-doc checkpoints), which
  survived a mid-run lock/sign-in. Full core set ran in roughly one morning (~3h wall): largest corpus
  CSN-python = 280,310 docs. Launchers `run_wave0.bat` / `run_wave0.sh` were added because PowerShell is
  Cylance-blocked on this host. **Still open before `accepted`:** dense+reranker rows (§8), git-committing
  the baseline, wiring the CI tripwire, and the §9 internal-repo eval (C#/C++ + structural graph).
- **2026-06-21 — dense+reranker investigated on Qwen3-Reranker-0.6B; neutral/negative on CoIR; DEFERRED.**
  Sized the reranker honestly before committing a multi-day run. Findings, in order:
  - **Throughput:** Qwen3-Reranker-0.6B (causal-LM yes/no scorer) on CPU runs ~**0.6 s/pair (~64 s/query at
    rerank_depth=100)**. Reranking all queries × depth across the core set ≈ **~12 days**; hence sampling.
  - **Harness hardening for feasibility + honesty:** added **seeded random query sampling**
    (`[eval].rerank_sample_queries`, default 500; `sample_seed`), **95% CIs** on every metric, **paired
    dense-vs-reranked lift** (cancels sampling noise), and per-query **progress logging**. Dense baseline is
    unaffected (always full query set). At 500/subtask, depth 100 ≈ ~21 h (vs ~12 days).
  - **Real bug found & fixed:** candidate ordering used `np.argsort(scores)[::-1]`, which **reverses tied
    score groups**. CoIR corpora contain **duplicate documents**; the reranker scores identical text
    identically, so the reversal sent the dense-#1 gold to the *bottom* of its tie group. Fixed with a
    **stable descending sort** (`argsort(-scores, kind="stable")`) that breaks ties toward dense order.
    (Two earlier hypotheses — a 401 on the model id, and an attention-mask/padding bug — were wrong: the id
    was fabricated, see prior note, and `tok.pad` already supplied the mask, so that "fix" was a no-op. The
    diagnostic that found the real cause printed the duplicate texts + identical scores directly.)
  - **Effect of the fix:** cosqa paired lift MRR@10 went **−0.26 → −0.08** (clearly-negative → neutral, CI
    spans zero). Correct/expected for cosqa (duplicate-heavy, single-label).
  - **But still negative elsewhere:** codefeedback-mt lift **−0.32 ± 0.17** *after* the fix — a different
    failure mode. **Hypothesis (unconfirmed):** codefeedback-mt has long multi-turn queries; the reranker
    truncates the *concatenated* query+doc to 512 (`longest_first`) and starves on the query tail, whereas
    dense embeds query/doc separately (each gets its own 512). Not chased further.
  - **Decision (2026-06-21):** with neutral-to-negative lift and **no positive datapoint**, running the full
    ~22–44 h dense+reranker baseline (incl. offloading to a spare laptop) was judged not worth the compute.
    **Dense is the committed Wave-0 deliverable; the reranker is deferred to ADR-009**, which scopes reranker
    selection deliberately. The harness reranker path (Qwen3 scorer, sampling, CIs, paired lift, stable sort)
    is built and validated, so ADR-009 can cut the "before" cheaply under a frozen SHA. New `[eval]` knobs:
    `rerank_depth`, `rerank_sample_queries`, `sample_seed`. Reranker launchers
    (`run_reranker_smoke`, `run_reranker_calibrate`, `run_wave0_reranker`) are retained for that future cut.

