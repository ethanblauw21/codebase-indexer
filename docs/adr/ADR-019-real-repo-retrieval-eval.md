# ADR-019: Real-Repo Retrieval Eval — System-Accuracy Scorecard for the Full Production Pipeline

**Status:** proposed
**Date:** 2026-06-22
**Branch:** `feature/adr-019-real-repo-retrieval-eval`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — reuses the **harness pattern** (fixture → run → metric → committed baseline) and the concrete **metric/CI/paired-lift helpers** in `tools/coir_eval.py` (`score_query`, `_ci95`, the paired-lift + `append_baseline` logic). This eval is the **§9 complement** ADR-007 promised: it covers the C#/C++ and structural-graph gaps CoIR structurally cannot reach.
- ADR-009 — reuses the **`reranker_enabled` flag + `load_reranker()` scorer** introduced in §P4. The reranker off-vs-on A/B *is* `HybridRetriever(reranker_enabled=False)` vs `(reranker_enabled=True)`. This ADR is the instrument that decides whether ADR-009 flips `[reranker].enabled` to `true`.
- ADR-003 — needs the **language adapters** (C#/C++ extraction) so the indexed corpus carries real symbols + a real call graph for those languages.
- ADR-008 — *concept only.* ADR-008 nominally "owns" the feature-tagged fixture + precision/recall machinery, but it is **not built**. Rather than serialize behind it, **this ADR defines the shared retrieval-fixture harness itself** (query → gold-FQN, feature-tagged, query-class). ADR-008's *extraction* arm can later reuse the same fixture-loading + CI scaffolding. The two remain **separate scorecards** (retrieval here, extraction there) by design — same split ADR-007/008 already established.

**Depended on by:**
- ADR-009 *(§P4)* — its "enable the reranker only when it beats the baseline" gate is **operationalized here** (§5 decision rule). ADR-009 cannot move P4 past "off by default, no quality claim" until this eval produces a verdict. **Resolve before `accepted`:** confirm the decision rule + the public/private split satisfy ADR-009's gate.
- ADR-014 *(usage-driven adaptive ranking)* — will need a **held-out query set** to learn over without overfitting the eval; this ADR's query corpus is the natural source. **Resolve before `accepted`:** confirm a train/held-out partition exists or is reserved.

## Context

ADR-007 built the CoIR retrieval scorecard and was deliberately honest about its two structural limits (§9):

1. **Language gap** — CoIR's code-search tasks cover Python/JS/Go/Java/Ruby/PHP, **not C# or C++**, two of our five target languages. The CoIR number says *nothing* about C#/C++ retrieval.
2. **Layer gap** — CoIR's corpus is flat (no call graph), so it can only measure the **semantic** layer (embedder ± reranker). It **cannot exercise the Retrieve→Traverse→Rerank** structural expansion that is the engine's whole differentiator.

Both gaps were explicitly deferred to a "planned internal-repo eval … reusing ADR-008's fixture machinery." This ADR is that eval — with one deliberate change of identity: **the corpus is pinned public GitHub repositories, not internal code.** The project's moat (ADR-008 Context) is *reporting a number competitors cannot* — and a number is only credible if its corpus is **inspectable and reproducible**. A score on a secret corpus is the very thing the thesis rejects. Public, SHA-pinned repos make the scorecard auditable; anyone can clone the same code and re-grade.

That choice carries a real, named cost: **training-data contamination.** The embedder (`jina-embeddings-v2-base-code`) and the reranker (`Qwen/Qwen3-Reranker-0.6B`) were trained on public GitHub. Popular repos may be *in* the models' training data, which can inflate absolute scores and — more dangerously for our purpose — **compress the reranker's measured lift**: if the embedder already ranks memorized gold at #1, there is no headroom for the reranker to demonstrate value, biasing the off-vs-on decision toward "don't enable." This eval's headline job is exactly that decision (does ADR-009 flip `[reranker].enabled`?), so contamination is not a footnote. We address it directly (§6) by pairing the public scorecard with a **small private, contamination-free slice** used only to confirm the reranker verdict.

The pipeline under test is the real one — `src/hybrid_retriever.py` — driven end to end against an index built by the production indexer (`src/incremental_indexer.py`), not a re-implementation. `tools/eval_retrieval.py` already proves the shape of an ablation (tier-1-only vs three-tier RRF over 10 queries); this generalizes it to a committed, multi-language, multi-arm scorecard with confidence intervals.

## Decision

Build a **real-repo retrieval eval** (`tools/real_repo_eval.py`) that indexes pinned public repositories with the production indexer, runs the real `HybridRetriever` against hand-authored `query → gold` fixtures, and reports a per-language / per-arm / per-query-class scorecard with 95% CIs — mirroring ADR-007's metrics for comparability. A **private slice** (identical harness, never committed) confirms the reranker decision on contamination-free code.

### §1 — Corpus: pinned public repositories (committed eval)

A curated set of **public GitHub repos covering all five target languages** (Python, TypeScript, JavaScript, C#, C++), pinned by commit SHA in a manifest. Selection criteria, in priority order:
- **Permissive license** (MIT/Apache/BSD) — so the corpus may be redistributed/cited.
- **Real call graphs** — genuine cross-symbol/cross-module structure, so the Traverse step has something to traverse (rules out single-file utilities).
- **Moderate, bounded size** — large enough to be non-trivial, small enough to index in minutes and to author trustworthy gold against.
- **Contamination-aware** — prefer *moderately-known, not ultra-famous* projects and, where possible, recent commit SHAs, to reduce (never eliminate) training-set overlap.

Data handling mirrors ADR-007 §6: **commit the queries + gold + the pinning manifest; git-ignore the cloned source and built index** (large, regenerable). A `tools/real_repo_prepare.py` clones each repo at its pinned SHA and runs the production indexer into a throwaway `.code-index`.

### §2 — Ground truth: hand-authored, feature-tagged, query-classed

Each fixture is a `query → gold` record, hand-authored and tagged:
- **gold** = one or more **fully-qualified symbol identifiers** (FQN-level, stable across reindex via `stable_id`). Grading checks whether the retriever's returned top-K chunks include a gold symbol — robust to chunk-boundary/FAISS-id churn.
- **feature tag** — the language feature or retrieval pattern exercised (e.g. `cpp/virtual-dispatch`, `cs/async-await`, `py/decorator`), so a regression points at a capability (the ADR-008 feature-tag idea, defined here for retrieval).
- **query class** — one of:
  - `semantic` — NL→code; gold is findable by meaning alone.
  - `graph-only` — gold is reachable **only via call-graph expansion**, *not* by semantic similarity (e.g. "what call sites break if I change the signature of `Foo::bar`?" → gold = its callers, which need not resemble the query). These exist specifically to prove the Traverse step earns its place (§3).

The set is deliberately **small and defensible** over large and noisy: authoring trustworthy gold on unfamiliar code (especially the "this is the *only* right answer" claim for `graph-only`) is the real cost, so we bound it to repos we actually read.

### §3 — Scoring the structural-graph layer: ablation arms + graph-only queries

The eval runs three **ablation arms** on the same query set, by toggling stages of the real pipeline:

| Arm | Pipeline | Equivalent production config |
|-----|----------|------------------------------|
| **A — semantic** | multi-tier FAISS RRF only (no traverse, no rerank) | — |
| **B — semantic+graph** | RRF + one-hop call-graph expansion | `[reranker].enabled = false` (today's default) |
| **C — full** | RRF + graph + rerank | `[reranker].enabled = true` |

- **Graph lift = B − A** (paired) — quantifies what the Traverse step adds, the thing CoIR cannot measure. The `graph-only` query class is where B should dominate A decisively; if it doesn't, the graph step is not earning its latency.
- **Reranker lift = C − B** (paired) — the off-vs-on comparison that drives the §5 decision.

### §4 — Metrics & scorecard

Reuse ADR-007's metric set verbatim for cross-scorecard comparability: **MRR@10, NDCG@10, Recall@{1,5,10}, Success@{1,5,10}, MAP**, plus token-economy and latency where meaningful. To avoid a second implementation, **extract the shared helpers** (`score_query`, `_ci95`, paired-lift, `append_baseline`) from `tools/coir_eval.py` into a small shared module both tools import — the same single-implementation discipline applied to `src/reranker.py` in ADR-009. Every reported number carries a **95% CI** (sampled where the query set is subsampled), and every lift is **paired** (B−A, C−B) so sampling noise cancels. Results are broken down **per language, per arm, and per query class**, appended to a committed `benchmarks/real_repo_baseline.jsonl` (deduped, git-SHA-stamped).

### §5 — The reranker decision rule (operationalizes ADR-009 §P4)

`[reranker].enabled` flips to `true` **only if all hold**, measured as the paired reranker lift (arm C − arm B):
1. **Mean lift > 0** on **both MRR@10 and NDCG@10**, with the **95% CI excluding zero** (a real, not-noise improvement).
2. **No target-language regression** — no language shows a negative mean lift on MRR@10.
3. **Confirmed on the private slice (§6)** — the contamination-free eval agrees with the public verdict.

If the public eval says "enable" but the private slice does not, **default to off** (conservative) and investigate the discrepancy. This is the measured gate ADR-009 §P4 promised, with the contamination risk explicitly firewalled.

### §6 — The private slice (contamination-free decision check)

A **small private eval** — same harness, same fixture format, same arms — authored over code **known not to be in the models' training data** (e.g. internal/unpublished repos), kept **entirely out of the repo** (git-ignored, results reported as numbers only). Its sole role is §5 clause 3: confirm the reranker verdict on clean code, so a contamination-compressed public lift cannot, by itself, wrongly veto a reranker that genuinely helps real users. It is **not** part of the publishable scorecard.

### §7 — CI tripwire

A tiny subset (one small repo, a handful of queries, arm B only) wired as a **regression tripwire** on retrieval-path changes — the analogue of ADR-007's `ci_subtasks`/`ci_limit_queries`. Fast, not publishable; it fails the build if MRR@10 drops below a committed floor, so a refactor that silently breaks retrieval is caught.

### §8 — Coverage & limits (honest current state)

Per Mantra 2, the number must never be oversold:
- **Contamination caveat (now).** Public-repo absolute scores and the reranker lift may be affected by training-set overlap; the scorecard header states this, and the **private slice (§6) is the contamination-free control**. Label: *"real-repo retrieval, {languages}"* — never "true production accuracy."
- **Coverage is bounded by fixture authorship, not engine capability.** A language/feature with no fixture is simply absent from the table — "measured on the fixtures we wrote," not "the engine's true precision on language X." *(Planned: grow the feature-tagged set as adapters mature — shared remediation with ADR-008 §7.)*
- **Gold is hand-authored and small by design** — a curated probe of what we claim to handle, not an exhaustive corpus.

## Consequences

**Better:**
- Closes both ADR-007 §9 gaps: **C#/C++ retrieval** and the **structural-graph layer** finally get a number.
- Produces the **inspectable, reproducible accuracy artifact** the depth-over-breadth moat needs — a score on a corpus anyone can clone and re-grade.
- Gives ADR-009 §P4 a **rigorous, contamination-firewalled gate** for the reranker decision instead of CoIR's wrong-instrument neutral/negative datapoints.
- Tests the **real `HybridRetriever`** end to end (not a re-implementation), so the number reflects what ships.
- Reuses ADR-007 metrics + ADR-009's `reranker_enabled` flag — small net-new surface; the A/B is a constructor argument.

**Worse:**
- **Hand-authoring trustworthy gold on unfamiliar public code is real labor** (the dominant cost), especially defensible `graph-only` gold. Bounded by keeping the set small + on repos we read, but not eliminated.
- **Two corpora to maintain** (public + private slice) — the private slice roughly doubles authoring on the decision arm.
- **Contamination cannot be fully removed** from public repos — mitigated and disclosed, not solved.
- Per-run **clone + full index** of each repo adds wall-clock vs CoIR's cached embeddings (bounded by repo-size selection + a resumable index).

**Neutral:**
- Retrieval (this ADR) and extraction (ADR-008) stay separate scorecards by design.
- The eval defines the shared retrieval-fixture harness ADR-008 can later reuse — a small ownership shift from ADR-008, recorded in its Depends-on note.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Internal/private repos only (the original "internal-repo eval") | Most representative + contamination-free, but **cannot be committed or published** — fails the moat's "inspectable number" requirement. Retained in reduced form as the §6 private slice. |
| Synthetic fixture repos only | Contamination-free and fully controlled, but **synthetic** — weakest signal on real retrieval difficulty; doesn't prove anything about real-world C#/C++ code. |
| Public repos only, no private slice | Simpler, but the **reranker decision rides on a contamination-biased lift** with no clean control. The §6 slice is cheap insurance on the one decision this eval exists to make. |
| Reuse CoIR for C#/C++ + graph | Impossible — CoIR has no C#/C++ tasks and a flat (graph-less) corpus; this is the exact §9 gap. |
| Block on ADR-008 building the fixture harness first | Serializes this behind an unbuilt ADR; the fixture machinery is small enough to define here and share. |
| Auto-mine gold from the call graph | Grades the retriever against the same graph it uses — circular; rejected in favor of hand-authored gold. |
| Any-positive-mean-lift reranker rule | Too weak — vulnerable to noise and per-language regressions; §5 requires CI-excludes-zero + no regression + private confirmation. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Extract shared metric/CI/paired-lift/append helpers from `tools/coir_eval.py` into a shared module; refactor `coir_eval.py` to import them (no behavior change).
- [ ] Author the pinned-repo manifest (`[real_repo_eval]` in `indexer.toml` or `benchmarks/real_repo/repos.toml`): name, URL, SHA, language, license — per §1 selection criteria.
- [ ] `tools/real_repo_prepare.py`: clone each repo at its SHA + build the index via the production indexer into a git-ignored `.code-index`.
- [ ] Define the fixture format (`query`, `gold` FQNs, `feature` tag, `class` ∈ {semantic, graph-only}); author the initial committed set across all five languages.
- [ ] `tools/real_repo_eval.py`: drive the real `HybridRetriever` for arms A/B/C; grade returned chunks against gold FQNs; emit per-language / per-arm / per-class metrics + CIs + paired lifts to `benchmarks/real_repo_baseline.jsonl`.
- [ ] Implement §5 decision rule as an explicit, printed verdict (PASS/FAIL per clause), run on both public eval and private slice.
- [ ] Stand up the §6 private slice (git-ignored fixtures + repos) and document how to run it locally.
- [ ] Wire the §7 CI tripwire (tiny subset, committed MRR@10 floor).
- [ ] Add the §8 contamination + coverage caveats to the scorecard header and `README` table label.
- [ ] `.gitignore` the cloned corpora + `.code-index` + private slice; commit only fixtures + manifest + baseline.
- [ ] Resolve **Depended on by**: confirm the §5 rule satisfies ADR-009 §P4's gate; reserve a held-out partition for ADR-014, before `accepted`.

**Notes:**
<!-- 2026-06-22: Created from /grill-plan. Key decisions captured in the grill: corpus = pinned PUBLIC GitHub repos (transparency/reproducibility = the moat) + a private contamination-free SLICE for the reranker decision; ground truth = hand-authored, feature-tagged, query-classed, harness defined HERE (not blocked on ADR-008); graph layer scored via ablation arms (A semantic / B +graph / C +rerank) PLUS a dedicated graph-only query class; reranker enable rule = paired lift C−B with 95% CI excluding zero on MRR@10 AND NDCG@10, no per-language regression, confirmed on the private slice. Renamed from "internal-repo eval" (ADR-007 §9 / ADR-008 wording) because the committed corpus is public, not internal. -->
