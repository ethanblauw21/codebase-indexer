# ADR-019: Real-Repo Retrieval Eval — System-Accuracy Scorecard for the Full Production Pipeline

**Status:** accepted (2026-07-02) — built + merged (PR #12). Both ADR-009 decisions settled → both flags stay off. **Reranker (C−B):** public n=148 passed clauses 1 & 2, but the private contamination-free slice (clause 3, 2026-07-07) **FAILED** — pooled CI includes 0 and clean TypeScript regresses, so private *disagrees* with public → default off (§5). The public win was partly a contamination artifact (zustand TS outlier); clean Python lift is real. **Convex fusion (D−B):** FAILS the §5 bar (negative in all 5 langs). The B−A graph gate is **formally dropped** per §8 below (the graph layer is recorded as rerank-only, not a first-pass retrieval signal); making it retrieval-competitive is deferred to ADR-022.
**Date:** 2026-06-22
**Branch:** `feature/adr-019-real-repo-retrieval-eval`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — reuses the **harness pattern** (fixture → run → metric → committed baseline) and the concrete **metric/CI/paired-lift helpers** in `tools/coir_eval.py` (`score_query`, `_ci95`, the paired-lift + `append_baseline` logic). This eval is the **§9 complement** ADR-007 promised: it covers the C#/C++ and structural-graph gaps CoIR structurally cannot reach.
- ADR-009 — reuses the **`reranker_enabled` flag + `load_reranker()` scorer** (§P4) *and* the **`fusion_mode` + `convex_fuse` path** (§P3), both now merged to `master`. The reranker A/B *is* `HybridRetriever(reranker_enabled=False/True)`; the fusion A/B *is* `fusion_mode="rrf"/"convex"`. This ADR is the instrument that decides whether ADR-009 flips **either** `[reranker].enabled` **or** `[retrieval].fusion_mode` — the two decisions CoIR could not settle.
- ADR-003 — needs the **language adapters** (C#/C++ extraction) so the indexed corpus carries real symbols + a real call graph for those languages.
- ADR-008 — *concept only.* ADR-008 nominally "owns" the feature-tagged fixture + precision/recall machinery, but it is **not built**. Rather than serialize behind it, **this ADR defines the shared retrieval-fixture harness itself** (query → gold-FQN, feature-tagged, query-class). ADR-008's *extraction* arm can later reuse the same fixture-loading + CI scaffolding. The two remain **separate scorecards** (retrieval here, extraction there) by design — same split ADR-007/008 already established.

**Depended on by:**
- ADR-009 *(§P4 reranker + §P3 fusion)* — **both** "enable only when it beats the baseline" gates are **operationalized here** (§5 rules; arms C−B and D−B). ADR-009 cannot move either pillar past "off by default, no quality claim" until this eval produces a verdict. **Resolve before `accepted`:** confirm the decision rules + the public/private split satisfy ADR-009's gates, and add the reciprocal link back into ADR-009's "Depended on by" note (now on `master`).
- ADR-014 *(usage-driven adaptive ranking)* — will need a **held-out query set** to learn over without overfitting the eval; this ADR's query corpus is the natural source. **Resolve before `accepted`:** confirm a train/held-out partition exists or is reserved.

## Context

ADR-007 built the CoIR retrieval scorecard and was deliberately honest about its two structural limits (§9):

1. **Language gap** — CoIR's code-search tasks cover Python/JS/Go/Java/Ruby/PHP, **not C# or C++**, two of our five target languages. The CoIR number says *nothing* about C#/C++ retrieval.
2. **Layer gap** — CoIR's corpus is flat (no call graph), so it can only measure the **semantic** layer (embedder ± reranker). It **cannot exercise the Retrieve→Traverse→Rerank** structural expansion that is the engine's whole differentiator.

Both gaps were explicitly deferred to a "planned internal-repo eval … reusing ADR-008's fixture machinery." This ADR is that eval — with one deliberate change of identity: **the corpus is pinned public GitHub repositories, not internal code.** The project's moat (ADR-008 Context) is *reporting a number competitors cannot* — and a number is only credible if its corpus is **inspectable and reproducible**. A score on a secret corpus is the very thing the thesis rejects. Public, SHA-pinned repos make the scorecard auditable; anyone can clone the same code and re-grade.

That choice carries a real, named cost: **training-data contamination.** The embedder (`jina-embeddings-v2-base-code`) and the reranker (`Qwen/Qwen3-Reranker-0.6B`) were trained on public GitHub. Popular repos may be *in* the models' training data, which can inflate absolute scores and — more dangerously for our purpose — **compress the reranker's measured lift**: if the embedder already ranks memorized gold at #1, there is no headroom for the reranker to demonstrate value, biasing the off-vs-on decision toward "don't enable." This eval's headline job is exactly those two decisions (does ADR-009 flip `[reranker].enabled` or `[retrieval].fusion_mode`?), so contamination is not a footnote. We address it directly (§6) by pairing the public scorecard with a **small private, contamination-free slice** used only to confirm the enable verdicts.

The pipeline under test is the real one — `src/hybrid_retriever.py` — driven end to end against an index built by the production indexer (`src/incremental_indexer.py`), not a re-implementation. `tools/eval_retrieval.py` already proves the shape of an ablation (tier-1-only vs three-tier RRF over 10 queries); this generalizes it to a committed, multi-language, multi-arm scorecard with confidence intervals.

## Decision

Build a **real-repo retrieval eval** (`tools/real_repo_eval.py`) that indexes pinned public repositories with the production indexer, runs the real `HybridRetriever` against hand-authored `query → gold` fixtures, and reports a per-language / per-arm / per-query-class scorecard with 95% CIs — mirroring ADR-007's metrics for comparability. A **private slice** (identical harness, never committed) confirms the reranker decision on contamination-free code.

### §1 — Corpus: pinned public repositories (committed eval)

A curated set of **public GitHub repos covering all five target languages** (Python, TypeScript, JavaScript, C#, C++), pinned by commit SHA in a manifest. Selection criteria, in priority order:
- **Permissive license** (MIT/Apache/BSD) — so the corpus may be redistributed/cited.
- **Real call graphs** — genuine cross-symbol/cross-module structure, so the Traverse step has something to traverse (rules out single-file utilities).
- **Moderate, bounded size** — large enough to be non-trivial, small enough to index in minutes and to author trustworthy gold against.
- **Contamination-aware** — prefer *moderately-known, not ultra-famous* projects and, where possible, recent commit SHAs, to reduce (never eliminate) training-set overlap.

**Selection process — proposed, then vetoed.** Candidates are proposed per language (starter set below); the reviewer swaps in any repo they know better before SHAs are pinned, because gold authoring is far cheaper and more trustworthy on code the author has actually read. Starter candidates (permissive license, moderate size, real cross-symbol structure, deliberately *not* ultra-famous to reduce contamination — **pending veto; SHAs pinned at implementation**):

| Language | Candidate repo | License | Why |
|---|---|---|---|
| Python | `pallets/click` | BSD-3 | Real cross-module command/decorator call structure; mid-size |
| TypeScript | `pmndrs/zustand` | MIT | Genuine cross-file calls (not just type declarations); compact |
| JavaScript | `sindresorhus/p-queue` | MIT | Small but real call graph (queue/worker orchestration) |
| C# | `serilog/serilog` | Apache-2.0 | Mid-size, real interface/call structure; not Newtonsoft-famous |
| C++ | `gabime/spdlog` | MIT | Real cross-file call graph (sinks/loggers), not a header-only single file |

These balance two competing pulls — *rich enough call graph* (rules out single-file utils and pure type/header libs) against *low enough fame* (rules out the most-memorized repos). Final picks + SHAs are settled at implementation with the reviewer's vetoes applied.

Data handling mirrors ADR-007 §6: **commit the queries + gold + the pinning manifest; git-ignore the cloned source and built index** (large, regenerable). A `tools/real_repo_prepare.py` clones each repo at its pinned SHA and runs the production indexer into a throwaway `.code-index`.

### §2 — Ground truth: hand-authored, feature-tagged, query-classed

Each fixture is a `query → gold` record, hand-authored and tagged:
- **gold** = one or more **fully-qualified symbol identifiers** (FQN-level, stable across reindex via `stable_id`). Grading checks whether the retriever's returned top-K chunks include a gold symbol — robust to chunk-boundary/FAISS-id churn.
- **feature tag** — the language feature or retrieval pattern exercised (e.g. `cpp/virtual-dispatch`, `cs/async-await`, `py/decorator`), so a regression points at a capability (the ADR-008 feature-tag idea, defined here for retrieval).
- **query class** — one of:
  - `semantic` — NL→code; gold is findable by meaning alone.
  - `graph-only` — gold is reachable **only via call-graph expansion**, *not* by semantic similarity (e.g. "what call sites break if I change the signature of `Foo::bar`?" → gold = its callers, which need not resemble the query). These exist specifically to prove the Traverse step earns its place (§3).

The set is deliberately **small and defensible** over large and noisy: authoring trustworthy gold on unfamiliar code (especially the "this is the *only* right answer" claim for `graph-only`) is the real cost, so we bound it to repos we actually read.

### §3 — Scoring the graph + fusion layers: ablation arms, graph-only queries

The eval runs **four ablation arms** on the same query set, by toggling stages of the real pipeline. Three paired lifts fall out — one per stalled decision (graph value, reranker enable, sparse-fusion enable):

| Arm | Pipeline (`fusion` / graph / rerank) | Equivalent production config |
|-----|----------|------------------------------|
| **A — semantic** | rrf / — / — (multi-tier FAISS RRF only) | — |
| **B — semantic+graph** | rrf / graph / — | today's default (`fusion_mode = "rrf"`, `[reranker].enabled = false`) |
| **C — +rerank** | rrf / graph / rerank | `[reranker].enabled = true` |
| **D — +sparse fusion** | convex / graph / — | `fusion_mode = "convex"` |

- **Graph lift = B − A** (paired) — what the Traverse step adds, the thing CoIR cannot measure. The `graph-only` query class is where B should dominate A decisively; if it doesn't, the graph step is not earning its latency.
- **Reranker lift = C − B** (paired) — the reranker off-vs-on comparison; drives the §5 reranker rule.
- **Sparse-fusion lift = D − B** (paired) — the convex-vs-rrf comparison at the current default (graph on, rerank off); drives the §5 fusion rule. This is the **literal-identifier re-test** CoIR under-powered: ADR-009 §P3 rejected convex on CoIR's NL→code queries, but here the `semantic` fixture set deliberately includes exact-identifier / rare-token queries where BM25 is supposed to win. Isolating D−B (rather than stacking convex under the reranker) keeps the two enable decisions independent.

*(A combined arm E = convex + graph + rerank is deferred — the two enable gates are decided on the isolated paired lifts above; E only matters once both independently pass.)*

### §4 — Metrics & scorecard

Reuse ADR-007's metric set verbatim for cross-scorecard comparability: **MRR@10, NDCG@10, Recall@{1,5,10}, Success@{1,5,10}, MAP**, plus token-economy and latency where meaningful. To avoid a second implementation, **extract the shared helpers** (`score_query`, `_ci95`, paired-lift, `append_baseline`) from `tools/coir_eval.py` into a small shared module both tools import — the same single-implementation discipline applied to `src/reranker.py` in ADR-009. Every reported number carries a **95% CI** (sampled where the query set is subsampled), and every lift is **paired** (B−A, C−B, D−B) so sampling noise cancels. Results are broken down **per language, per arm, and per query class**, appended to a committed `benchmarks/real_repo_baseline.jsonl` (deduped, git-SHA-stamped).

### §5 — The enable decision rules (operationalizes ADR-009 §P4 reranker + §P3 fusion)

Two ADR-009 config flags flip **only when their paired lift clears the same three-clause bar** — the measured gate ADR-009 promised, with contamination firewalled.

**Rule (applied independently to each decision):** flip the flag **only if all hold** —
1. **Mean lift > 0** on **both MRR@10 and NDCG@10**, with the **95% CI excluding zero** (a real, not-noise improvement).
2. **No target-language regression** — no language shows a negative mean lift on MRR@10.
3. **Confirmed on the private slice (§6)** — the contamination-free eval agrees with the public verdict.

| Decision (ADR-009) | Flag | Paired lift |
|---|---|---|
| Enable the reranker (§P4) | `[reranker].enabled` → `true` | arm **C − B** |
| Enable convex fusion (§P3) | `[retrieval].fusion_mode` → `"convex"` | arm **D − B** |

If the public eval says "enable" but the private slice does not, **default to off** (conservative) and investigate the discrepancy. The two decisions are independent — one may pass while the other fails. Note the fusion gate is not a second bite at the same apple: the CoIR full sweep already rejected convex under this rule on NL→code queries (ADR-009 §P3 log); a pass *here* would reflect the literal-identifier query mix CoIR structurally lacked, not a re-run of the same test.

### §6 — The private slice (contamination-free decision check)

A **small private eval** — same harness, same fixture format, same arms — over a **freshly-written, clean-room repo authored to post-date the models' training cutoff** (contamination-free *by construction*, rather than hoping an internal repo escaped the crawl). Kept **entirely out of the repo** (git-ignored, results reported as numbers only). It need not span all five languages — a compact codebase in ~2 languages with genuine cross-symbol call edges is enough to exercise arms B/C/D. Its sole role is §5 clause 3: confirm the reranker **and** fusion verdicts on clean code, so a contamination-compressed public lift cannot, by itself, wrongly veto a change that genuinely helps real users. It is **not** part of the publishable scorecard.

**Authoring caveat (flag, don't skip).** A hand-written repo must have *real* structure — non-trivial call chains, some rare identifiers, realistic naming — or it becomes the "synthetic fixtures" alternative we rejected. Budget it as a genuine (if small) codebase, not a toy.

### §7 — CI tripwire

A tiny subset (one small repo, a handful of queries, arm B only) wired as a **regression tripwire** on retrieval-path changes — the analogue of ADR-007's `ci_subtasks`/`ci_limit_queries`. Fast, not publishable; it fails the build if MRR@10 drops below a committed floor, so a refactor that silently breaks retrieval is caught.

*Implemented (2026-07-02):* `tools/real_repo_tripwire.py` grades **p-queue** (the smallest pinned repo, 8 fixtures) on **arm B** against a committed **MRR@10 floor of 0.45** (measured baseline 0.5875; the floor sits ~one query's worth of MRR below it, so deterministic-embedding noise / a model-version bump never trips it but a real collapse does). It self-prepares (clone at the pinned SHA + production index, incl. ADR-021 call resolution) so the git-ignored corpus need not be committed, then exits non-zero below the floor and lists the missing queries. A dedicated `retrieval-tripwire` job in `.github/workflows/ci.yml` runs it on every PR with the ~300 MB embedder HuggingFace-cached. Re-baseline the floor only on an intentional change (`--floor` overrides for a one-off).

### §8 — Coverage & limits (honest current state)

Per Mantra 2, the number must never be oversold:
- **Contamination caveat (now).** Public-repo absolute scores and the reranker lift may be affected by training-set overlap; the scorecard header states this, and the **private slice (§6) is the contamination-free control**. Label: *"real-repo retrieval, {languages}"* — never "true production accuracy."
- **Coverage is bounded by fixture authorship, not engine capability.** A language/feature with no fixture is simply absent from the table — "measured on the fixtures we wrote," not "the engine's true precision on language X." *(Planned: grow the feature-tagged set as adapters mature — shared remediation with ADR-008 §7.)*
- **Gold is hand-authored and small by design** — a curated probe of what we claim to handle, not an exhaustive corpus.
- **The graph layer is rerank-only for retrieval — B−A gate dropped (2026-07-02).** Measured honestly: the Traverse step adds **≈ 0** to first-pass retrieval under the shipped RRF default (pooled B−A mrr@10 ≈ 0 across all five languages; max +0.075 on click with CI including zero). This is structural, not a fixture-authoring failure — resolved structural neighbours are pool-truncated and, even when kept, score below the semantic top-10 under RRF (ADR-022 §Context). So the graph does **not** currently improve what a user sees at K=10; its retrieval value is **latent**, materialising only when a reranker rescores candidates and can pull a structural neighbour up. Accordingly this ADR **drops the B−A enable gate** and records the graph as a **rerank-time signal, not a first-pass retrieval signal**. This is *not* a claim that the call graph is valueless — it remains the substrate for ADR-006 analytics (community/centrality/coupling), blast-radius, and callers/callees navigation, all of which consume the same resolved edges independently of retrieval ranking. What is inert is specifically *graph-as-first-pass-retrieval-ranker under RRF*. **Decision (current stage):** keep the Traverse step in place (B−A ≈ 0 means no quality harm; it preserves the graph→reranker candidate path) rather than tune it (premature — its payoff is gated on a reranker that isn't yet justified) or retire it from retrieval (would foreclose the reranker synergy and cost re-integration later). Revisit via ADR-022 **only if** a better-powered reranker rerun pushes C−B over the §5 bar. Caveat for future-us: if retrieval-latency profiling ever flags the expansion step as a real cost, that flips the keep-vs-retire call toward retiring it from the hot path (the call graph itself stays, for the non-retrieval consumers above).

## Consequences

**Better:**
- Closes both ADR-007 §9 gaps: **C#/C++ retrieval** and the **structural-graph layer** finally get a number.
- Produces the **inspectable, reproducible accuracy artifact** the depth-over-breadth moat needs — a score on a corpus anyone can clone and re-grade.
- Gives **both** stalled ADR-009 decisions — the §P4 reranker enable **and** the §P3 convex-fusion enable — a **rigorous, contamination-firewalled gate** on real code, instead of CoIR's wrong-instrument neutral/negative datapoints. One eval, two verdicts.
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

- [x] Extract shared metric/CI/paired-lift/append helpers from `tools/coir_eval.py` into a shared module (`tools/eval_common.py`); refactor `coir_eval.py` to import them (no behavior change).
- [x] Propose the pinned-repo manifest (`benchmarks/real_repo/repos.toml`: name, URL, SHA, language, license) from the §1 starter candidates; apply reviewer vetoes; pin SHAs.
- [x] `tools/real_repo_prepare.py`: clone each repo at its SHA + build the index via the production indexer into a git-ignored `.code-index`. (Also `--symbols` dump for gold authoring; runs ADR-021 call resolution at finalization.)
- [x] Define the fixture format (`query`, `gold` FQNs, `feature` tag, `class`); author the committed **semantic** set across all **five** languages (42 queries; incl. exact-identifier/rare-token queries for the fusion arm). **`graph-only` class PAUSED** — see the pivot note below.
- [x] `tools/real_repo_eval.py`: drive the real `HybridRetriever` for arms **A/B/C/D** (graph/fusion/reranker toggles); grade returned chunks against gold FQNs (normalized suffix match — scope format is language-dependent); emit per-language / per-arm / per-class metrics + CIs + paired lifts to `benchmarks/real_repo_baseline.jsonl`.
- [x] Implement the §5 rules as explicit printed verdicts (PASS/FAIL per clause) for reranker (C−B) and fusion (D−B). **Public verdicts DONE (2026-07-01): both FAIL → both flags stay off** (reranker +0.078 CI incl 0 + js regression; convex −0.096 negative in all 5 langs). Recorded in ADR-009 §P3/§P4. **UPDATE (2026-07-06):** the reranker C−B was re-run at **n=148** and now **PASSES clauses 1 & 2** on the public eval (mrr@10 +0.1405, CI excludes 0; no per-language regression) — see the power-rerun note below. Clause 3 (private slice) still pending, so the flag stays off; the flip from n=42 is entirely a statistical-power effect. Convex D−B was not rerun and stays FAIL.
- [x] Stand up the §6 private slice: author a **freshly-written clean-room repo** (post-cutoff, ~2 languages, real call edges) + git-ignored fixtures; document how to run it locally. **DONE (2026-07-07) → clause 3 FAIL.** Authored `benchmarks/real_repo/private/quanta` (Python) + `relay` (TypeScript), 25+19 git-ignored fixtures, indexed; ran arms B/C (`--no-write`, numbers-only, ~30 min CPU). `verdict()` output: **`[reranker].enabled` → FAIL (§6 private)** — pooled C−B **mrr@10 +0.0920 ±0.1070 (CI incl 0), NDCG@10 +0.0760 ±0.0815 (CI incl 0), no-regression ✗ typescript → private DISAGREES with the public verdict.** Split: clean Python C−B **+0.1867** (excludes 0, real lift) vs clean TS **−0.0325** (negative). Per §5 (all three clauses; public-enable/private-disagree → **default off**), `[reranker].enabled` **stays `false` — decision now settled, not pending.** The public pooled pass leaned on the contamination-exposed zustand TS outlier (+0.378); on clean TS the lift is negative → the §6 contamination control did its job, catching a memorization artifact the public eval would have waved through to enable. Recorded in ADR-009 §P4 (clause-3 note). *(Convex fusion D−B not part of this run — stays rejected.)*
- [x] Wire the §7 CI tripwire (tiny subset, arm B, committed MRR@10 floor). **DONE (2026-07-02):** `tools/real_repo_tripwire.py` (p-queue, arm B, floor 0.45 vs measured 0.5875 baseline; self-prepares clone+index) + a `retrieval-tripwire` job in `.github/workflows/ci.yml` (HF-model-cached). Exits non-zero on collapse below the floor.
- [ ] Add the §8 contamination + coverage caveats to the scorecard header and `README` table label.
- [x] `.gitignore` the cloned corpora + `.code-index` + private slice; commit only fixtures + manifest + baseline.
- [ ] Resolve **Depended on by**: confirm the §5 rules satisfy ADR-009 §P4 **and** §P3 gates and add the **reciprocal link into ADR-009 on `master`**; reserve a held-out partition for ADR-014 — before `accepted`.

**Notes:**
<!-- 2026-06-22: Created from /grill-plan. Key decisions captured in the grill: corpus = pinned PUBLIC GitHub repos (transparency/reproducibility = the moat) + a private contamination-free SLICE for the reranker decision; ground truth = hand-authored, feature-tagged, query-classed, harness defined HERE (not blocked on ADR-008); graph layer scored via ablation arms (A semantic / B +graph / C +rerank) PLUS a dedicated graph-only query class; reranker enable rule = paired lift C−B with 95% CI excluding zero on MRR@10 AND NDCG@10, no per-language regression, confirmed on the private slice. Renamed from "internal-repo eval" (ADR-007 §9 / ADR-008 wording) because the committed corpus is public, not internal. -->
<!-- 2026-07-01: Second /grill-plan pass — sharpened the design into an implementation-ready plan now that ADR-009 (P1/P3/P4) + ADR-007 are merged to master. Four grilled decisions: (1) build the FULL five-language scorecard up front (no phasing); (2) ADD a convex-fusion arm D so this one eval settles BOTH stalled ADR-009 decisions — reranker (C−B) AND sparse fusion (D−B, the literal-identifier re-test CoIR under-powered per the §P3 rejection); (3) private slice = a FRESHLY-WRITTEN clean-room repo (contamination-free by construction, post-cutoff), ~2 languages, not existing internal code; (4) corpus = I propose per-language candidates (starter table in §1), reviewer vetoes before SHA pinning. §5 generalized to two flag-flip rules under one three-clause bar; impl log + cross-refs updated; reciprocal link back into ADR-009 (now on master) is an explicit checklist item. -->
<!-- 2026-07-01 (build + GRAPH-LAYER PIVOT): Building the arms exposed that the graph Traverse step was a retrieval NO-OP, via a four-cause cascade: (1) CALLS edges stored unresolved bare names → fixed by NEW ADR-021 (baseline call-edge resolution + CTE COALESCE), merged; (2) the C# adapter emitted ZERO call edges (tree-sitter field `member:` should be `name:`) → fixed (PR #11), serilog 0→1230 call edges; (3) `_MAX_POOL_SIZE=35 < _SEMANTIC_K=50` truncates structural nodes before return; (4) structural nodes get hop-decayed RRF scores BELOW the semantic top-10, so they never surface at K=10 under RRF — the graph only helps once a reranker rescores them. Consequence: **`graph-only` fixtures cannot register a B−A lift under the shipped RRF default no matter how well authored** (proven: p-queue B−A = +0.003 ±0.007, gold stays >rank-10 even with the pool cap lifted). Reviewer decision: PIVOT — deliver the reranker (C−B) + sparse (D−B) verdicts ADR-009 actually needs using the SEMANTIC fixtures; PAUSE graph-only authoring; causes #3/#4 become follow-up ADRs (a `graph_only_scout.py` that ranks resolved call pairs by ascending embedding similarity is kept for that future work). B−A is still reported, honestly, as ≈0-under-RRF. Also added a language-robust grader (scope format differs by adapter: JS `file::Sym` vs C# `Ns.Class.M/arity` vs C++ signatures, plus `_part_N` splits). -->
<!-- 2026-07-06 (POWER RERUN, n=148): re-ran the reranker arm (B,C) on an expanded, dip-weighted fixture set (41→148: p-queue 24, click 32, zustand 27, serilog 30, spdlog 35) to tighten the C−B CI that was underpowered at n=42 (and marginal at an interim n=82). Pooled paired lift on the real HybridRetriever: **mrr@10 +0.1405 ±0.0641 (CI [+0.076, +0.205] — EXCLUDES 0); ndcg@10 +0.1174 ±0.0520 (CI [+0.065, +0.169] — EXCLUDES 0)** → **CLAUSE 1 now PASSES on both metrics.** Per-language mrr@10 all positive (js/p-queue +0.032, py/click +0.033, cpp/spdlog +0.132, c#/serilog +0.138, ts/zustand +0.378) → **CLAUSE 2 PASSES.** The two dips that failed the interim n=82 check (js −0.014, cpp −0.019) flipped clearly positive once the set was large + balanced — confirming they were small-n noise, not real regressions. So the public eval now clears **both** clauses that blocked at n=42/82. **HONEST CAVEATS:** (1) **CLAUSE 3 (private §6 slice) is STILL PENDING** and §5 requires all three — so **`[reranker].enabled` STAYS `false`** until the private slice confirms; this result makes clause 3 the *live* gate, no longer moot. (2) **The pooled CI was RECONSTRUCTED — now CONFIRMED authoritative (RESOLVED 2026-07-06).** The n=148 run was originally split (4 repos 2026-07-05 + spdlog 2026-07-06, after a mid-run PC crash lost the in-flight spdlog pass), and the baseline persists per-repo *aggregates*, not per-query diffs — so each repo's SD was inverted from its stored `ci95` (`SD = ci95·√n/1.96`, exact under `eval_common.ci95`'s normal approx) and the groups combined. To remove any doubt, the clean single-process full-run was then executed on a **spot NVIDIA T4 in GCP** (all 148 queries through arm C on-GPU, one process; see `cloud/`). `verdict()`'s own printed output — **mrr@10 +0.1405 ±0.0641, ndcg@10 +0.1174 ±0.0520, both CI>0 → PASS (public)** — is *identical to the reconstruction to 4 dp*, so the pooled-variance math is confirmed exact rather than merely "exact modulo rounding." Result in `benchmarks/real_repo/real_repo_authoritative.jsonl`. **One residual provenance gap:** the authoritative file's `git_sha` reads `unknown` (the cloud bundle is not a git checkout, so it can't self-stamp its commit) — the numbers are sound, only the in-file commit stamp is absent. (3) **zustand (+0.378) is an outlier** inflating the pooled magnitude, and 6 of its 10 new fixtures target public types/interfaces (its function surface was near-exhausted) — the conclusion survives without it (other four all positive) but the pooled *size* leans on it. (4) **js/py lifts are small and individually noisy** (per-repo CIs still include 0); the pooled significance carries them. Results in `benchmarks/real_repo/real_repo_power_rerun.jsonl` (separate from the canonical n=42 baseline, which is untouched). Also added flushed per-query progress logging (`_progress` + `EVAL_PROGRESS_LOG`) to `real_repo_eval.py` so the long arm-C pass is auditable live (a `q N/35 + rolling ETA` line per query) instead of a buffered black box — the crash recovery relied on it. -->
<!-- 2026-07-07 (CLAUSE 3 — PRIVATE §6 SLICE, FAIL → reranker decision SETTLED off): stood up the contamination-free control the n=148 rerun promoted to the sole remaining gate. Clean-room repos authored to post-date the models' training cutoff (contamination-free BY CONSTRUCTION): `benchmarks/real_repo/private/quanta` (Python — a small spreadsheet engine: lexer/parser/evaluator/graph) + `relay` (TypeScript — a scheduler: heap/backoff/tokenBucket/policy), with genuine cross-symbol call edges. 25 (py) + 19 (ts) git-ignored fixtures (`fixtures/cleanroom-{py,ts}.jsonl`); indexed with the production indexer; ran arms B,C via `real_repo_eval.py --repos cleanroom-py,cleanroom-ts --arms B,C --no-write` (numbers-only; the script auto-detects an all-private run and prints "this IS clause 3"). Run was CPU (~30 min; the reranker arm is ~64–90 s/query). NOTE: a first attempt hibernated overnight mid-run (progress timer ran to 4000 s/q); killed + restarted clean. **`verdict()` output — `[reranker].enabled` → FAIL (§6 private):** pooled C−B **mrr@10 +0.0920 ±0.1070 (CI incl 0 ✗), ndcg@10 +0.0760 ±0.0815 (CI incl 0 ✗), no-regression ✗ typescript → private slice DISAGREES with the public verdict.** Per-language: **cleanroom-py C−B +0.1867 ±0.1335 (arm B 0.773 → C 0.960; CI excludes 0 — a real Python lift) vs cleanroom-ts −0.0325 ±0.1622 (arm B 0.705 → C 0.673; negative).** Per §5 (all three clauses; public-enable / private-disagree → default off + investigate), `[reranker].enabled` **stays `false`, now by settled verdict** (was "pending clause 3"). INTERPRETATION: the public n=148 pooled PASS leaned on the zustand +0.378 TS outlier whose fixtures target public types/interfaces (maximally contamination-exposed); on CLEAN TypeScript the reranker lift is negative — so the §Context contamination worry materialized, INVERTED: contamination *inflated* the public TS lift rather than compressing it. The §6 control did exactly its job — caught a memorization artifact the public eval alone would have waved through to enable. Results NOT committed (numbers-only): `private/`, `fixtures/cleanroom-*`, and the result/log files are all git-ignored; raw scorecard at `benchmarks/real_repo/private/private_slice_result.txt` (local only). FOLLOW-UPS (logged, non-blocking — flag is off regardless): (a) grow the private slice (n=44 thin, split 25/19) to de-noise the TS negative; (b) per-language reranking (on for Python, off for TS) = a new ADR, since `[reranker].enabled` is a single global flag today. Recorded in ADR-009 §P4 (clause-3 note) + status/impl-log. -->
<!-- 2026-07-01 (RESULTS): full A/B/C/D over 42 queries × 5 repos, committed to benchmarks/real_repo/real_repo_baseline.jsonl. Pooled paired lifts (mrr@10): graph B−A ≈ 0 everywhere (max +0.075 click, CI incl 0 — inert under RRF, ADR-022); reranker C−B +0.078 ±0.120 (CI incl 0; positive on 4/5 langs — TS +0.264, C# +0.182 — but js/p-queue −0.129) → FAIL the enable bar, `[reranker].enabled` stays false; sparse D−B −0.096 ±0.108 (negative all 5 langs, incl exact-identifier queries) → FAIL, `[retrieval].fusion_mode` stays rrf. Both ADR-009 flags stay OFF; verdicts recorded in ADR-009 §P3/§P4 + reciprocal link. p-queue reranker regression diagnosed: the reranker demoted 2 queries the embedder already had at rank 1 (`add` 1.0→0.33, `concurrency` 1.0→0.0 — short accessor chunks) while helping 2 others — variance on already-solved queries, not a systematic failure; supports the "underpowered at n=42, grow the query set" reading. Eval reranker-load cached (was reloaded once per repo). Remaining: §7 CI tripwire; §6 private slice (moot unless a rerun flips a flag to enable); promote ADR-007/009 status. -->

