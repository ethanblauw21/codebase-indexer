# ADR-030: Summaries Get Their Own Index, Fused With the Code at Search Time

**Status:** accepted
**Date:** 2026-09-25
**Branch:** `feature/adr-030-summary-index` (cut from `feature/adr-027-summarizer-adaptive-batching` at aa3c367)
**Reviewer:** @edb
**Backlog:** [B-025](../backlog.md#b-025) — appended summaries make intent retrieval worse, and the same summaries help when kept apart.
**Depends on:**
- [ADR-027](./ADR-027-summarizer-adaptive-batching.md) — the summaries come from its batched pass 1. Its log holds all the measurements this ADR rests on.
- `chore/summaries-off-by-default` (02eb4bb, off `master`) — turns summarization off until this lands. This ADR turns it back on.

**Depended on by:** [ADR-028](./ADR-028-central-model-host.md) §5 (embed now, summarize later) gets much simpler: a late summary is one vector added to this index, with no re-embed of the code.

## Context

Since the summarizer was added, each chunk has been embedded as `code + "\n\n# Summary\n" + summary`, in `ingest_file` (`src/incremental_indexer.py`). Three measurements on 2026-09-24 and 25, all in ADR-027's log, show that this is the wrong place for the summary:

1. **Appending hurts the queries summaries exist for.** 55 new queries describe what a function's body does, with no identifiers. MRR@10 was 0.436 without summaries and 0.380 with them. On the original 83 queries, summaries made no difference (+0.006). The summary states a function's purpose, and that pulls the chunk's vector toward the purpose and away from the body.
2. **Most tier-2 and tier-3 summaries are never seen.** The embedder reads 512 tokens (`[embeddings].max_seq_length`, capped for memory; see `core.py`). For 77 percent of tier-2 and 68 percent of tier-3 chunks, the code alone fills that window, so an appended summary is cut off.
3. **The same summaries help when they have their own vectors.** Each summary was embedded alone, searched by cosine, mapped back to its chunk, and fused by RRF with the code-only ranking. That scored 0.613 against 0.457 on the original set and 0.606 against 0.450 on the intent set (`gpu-crash-repro/summary_store_eval.py`). Batched summaries did as well as batch-size-1 summaries.

Search already fuses the three tier indexes by RRF over FAISS ids (`HybridRetriever._semantic_search`). B-011 found that those three can never reinforce each other, because the tier is part of the id. A summary vector stored under its chunk's own id is the first list that can.

## Decision

### §1. Code vectors are code only

`ingest_file` embeds `chunk.text` and nothing else. The summary is still cached in `chunk_summaries` and still put on the DocumentStore entry, so tools can show it.

### §2. A fourth FAISS index, `summary.faiss`

- It sits beside the tier indexes in `.code-index/`. It is loaded and saved through the same `MultiIndexManager`, and it has the same dimension and the same flat inner-product type.
- Each summary is embedded alone as a document (no query instruction) and added under **its chunk's FAISS id**. A chunk without a summary simply has no entry.
- Stale removal needs no new code. `purge_stale_vectors` already calls `remove_ids` on every index in the dict, and a chunk's id is the same in both.
- Summaries are embedded in pass 2 with the code, per file and tier, so pass 2 does one more small embed per tier. Summaries are short, so the cost is expected to be small next to the code embed; see the gaps table.

### §3. Search fuses it with the finished ranking

`retrieve()` runs the whole pipeline as today (semantic search, graph expansion, final scoring) to a depth of 30. It then fuses that ranking by RRF with the top 30 of `summary.faiss` for the same query, in `_fuse_summaries`, and cuts the result to `top_n` distinct entries.
- A summary hit shares its chunk's id, so it lifts that chunk.
- A chunk found only through its summary joins the list with `source = "summary"`.
- `[retrieval].summary_weight` defaults to 0.5.
- `[retrieval].file_chunk_weight` (default 1.0) is how much a tier-2/3 chunk counts in the code ranking at this fusion, against 1.0 for tier 1. Its summary still counts in full.
- The fused list keeps one chunk per split parent, meaning the same file and the same scope once `_part_N` is stripped, across tiers. The best-scoring part stands for the others.
- With the index missing or empty, or the weight at 0, `retrieve()` makes exactly today's calls.

This replaced the first design, which added the summary list inside `_semantic_search`. That failed Verification 1; see the Notes.

### §4. Which tiers are summarized

`[summarization].tiers = [1, 2, 3]` by default, which keeps what is summarized today (settled 2026-09-25, @edb). Pass 1 and pass 2 both skip tiers not in the list. Tier-2 and tier-3 chunks are the long prompts, so they are most of the summarizer's GPU time, and their value has not been measured: every gold answer in both query sets is a single function. Verification 3 measures it, and the default can be narrowed if they do not pay for themselves.

### §5. Existing indexes

An index built before this ADR has summaries appended inside its code vectors and no `summary.faiss`. It keeps working, and it keeps the appended-summary behavior this ADR exists to remove. Moving to the new layout takes a full re-index. The summaries are cached by text, so a re-index costs an embed pass and not a summarizer pass. `index_meta` records `embed_layout = "code+summary-index"`, and the indexer prints one line recommending a full re-index when an existing index lacks it.

### §6. Summarization back on

`[summarization].enabled` goes back to `true` in this branch. `chore/summaries-off-by-default` turned it off because appending was worse than nothing. That reason is gone once this lands.

## Measured values this ADR still needs

| Value | Why | Source | Result |
|---|---|---|---|
| MRR@10 of a real build with this branch, both query sets | that the offline result holds through the real indexer and retriever | retrieval stages, variant `store` | **0.531 / 0.556** against 0.443 / 0.436 without summaries (see Notes) |
| Pass-2 time with summary embeds, this repository | the cost of §2 | stage 7 on this branch | _gap_ (today: 98 s; on click, 106 s against 77 s without summaries) |
| Tier-2/3 summaries: do they help find the right file | whether §4's default earns its GPU time | file-level query set, tiers [1] against [1, 2, 3] | _gap_ |
| `summary_weight` on a third query set | 0.5 won on both sets through the real build, but only narrowly on the original one | later | _gap_ |

## Consequences

**Better:**
- MRR@10 +0.120 on intent queries and +0.088 on the original set, measured through a real build with 95 percent intervals above zero. That is the largest retrieval gain this project has recorded.
- Code vectors stop being pulled toward a summary's purpose, so body-level queries recover.
- Tier-2 and tier-3 summaries become visible to search at all.
- A summary can arrive after its chunk is embedded, as one added vector. That is the hard part of ADR-028 §5 done.

**Worse:**
- A fourth index to keep consistent, and one more RRF list to reason about when ranking looks wrong.
- Every existing index needs a full re-index to benefit, and until then it quietly keeps the old behavior. The warning line is the only signal.
- Summarization goes back on by default, so a full index costs 16 minutes of GPU on this repository again, and much longer on CPU. The CPU case is already covered by the ADR-020 kill-switch and by `enabled = false`.
- One more setting (`summary_weight`), chosen on two query sets from the same three repos. p-queue got worse on the original set (0.550 to 0.494), and why is not known yet.

**Neutral:** the summaries themselves, the summarizer, batching and the cache are unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep appending, and also add the summary index | Appending is what hurt intent queries (0.436 to 0.380). Keeping it would carry that loss into the fused result. |
| Put the summary before the code instead of after | Fixes the 512-token cut for long chunks, but pulls every chunk even harder toward its purpose, which is the measured harm. |
| Raise `max_seq_length` so appended summaries fit | Tried at 4096 in ADR-009 with no eval change, costs attention memory as the square of the length, and does not address the pull toward purpose. |
| Summaries as BM25 text | The sparse arm is off by default (ADR-009 §P3), and the measured gain came from dense summary vectors. |
| Summary-only search | Scored 0.567 / 0.535 alone. Fused is better on both sets. |
| Tier 1 only | Cheaper, but gives up file-level summaries without testing them. Kept possible through `[summarization].tiers`. |

## Verification

1. A real build with this branch (the `store` variant) scores within noise of the offline result on both query sets, and above the `none` variant.
2. A changed file removes its old summary vectors: after a re-index of one edited file, `summary.faiss` has no ids that are missing from the `chunks` table. Covered by a unit test and by one real edit.
3. Tier-2/3 value: 10 to 15 queries whose answer is a file, graded by file, with summaries for tier 1 only against all tiers.
4. An index built before this ADR still loads and searches, and the indexer prints the re-index line once.
5. With `summary.faiss` absent, search results are identical to today's.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] B-025 in `docs/backlog.md`
- [x] §1 code-only embeds in `ingest_file`
- [x] §2 `summary.faiss`: build in pass 2, loaded and saved with the tiers, stale removal
- [x] §3 fusion, moved to the end of `retrieve()` (see Notes), `[retrieval].summary_weight`
- [x] §4 `[summarization].tiers`, honored in pass 1 and pass 2
- [x] §5 `index_meta.embed_layout` and the re-index line
- [x] §6 `[summarization].enabled = true` (already true on the ADR-027 base)
- [x] Config drift test for the new keys; `tests/test_summary_index.py` (10 tests). Suite: 341 passed.
- [x] Verification 1 (variant `store`)
- [ ] Verification 2 to 5
- [ ] Set status to `accepted` in the PR

**Notes:**

- 2026-09-25, **grill (@edb):** building now. Code vectors become code only, all three tiers stay summarized behind a knob with a file-level check to follow, and summary weight 1.0 behind a knob.
- 2026-09-25, **Verification 1, first design: failed.** Variant `store`, built from this branch with the summary cache seeded from the `batched` build, so no summarizer ran. The build itself was correct: 375 summary adds, and `summary.faiss` next to the tiers. Pass 2 on click took 106 s against 77 s without summary vectors.
  - Scores with the summary list inside `_semantic_search`: 0.429 on intent queries (none: 0.436) and 0.453 on the original set (none: 0.443). The offline gain did not appear.
  - Cause: inside the semantic step, the summary list was one of four, and `_rerank`'s category boost (+0.12, against RRF scores of about 0.02) outweighed it wherever it applied. Raising the weight to 3.0 made it worse (0.408).
  - `gpu-crash-repro/summary_fusion_diag.py` tried the fusion at the end of the pipeline on the same index instead: 0.564 / 0.550 at weight 0.5, and 0.531 / 0.548 at 1.0.
- 2026-09-25, **deviation from the grill:** the fusion moved to the end of `retrieve()`, and `summary_weight` changed from 1.0 to 0.5. @edb chose 1.0 on the offline numbers, where it won on intent queries. Through the real build, 0.5 won on both sets. This is recorded here for @edb to overrule.
- 2026-09-25, **Verification 1, final design: passed** (`retrieval/results_intent030.json`, `results_orig030.json`).
  - Intent: 0.556 against 0.436 without summaries. The mean gain is +0.120 (95 percent interval +0.044 to +0.195), with 28 queries up and 9 down. Against appended batch-size-1 summaries it is +0.175.
  - Original: 0.531 against 0.443, a gain of +0.088 (interval +0.015 to +0.160), with 34 up and 12 down.
  - By repo, zustand and click gained on both sets. p-queue on the original set lost, 0.550 to 0.494, which is not looked into yet.
  - The result is below the offline 0.61. The offline run merged split `_part_N` chunks by name before fusing, and a real build cannot do that without a schema change.
- 2026-09-25, **p-queue loss diagnosed, and the fused list made distinct.**
  - Cause: the split parts of `index.ts` (`Full File_part_N` at tiers 2 and 3, `PQueue_part_N`, `Global_part_N`) rank in both lists. Under RRF that beats a rank-1 hit found in only one list. The parts then filled the top 10 as near-duplicates: `pq-concurrency` returned 10 chunks covering 2 scopes, and lost its rank-1 answer.
  - Ruled out: the fusion depth of 30. Code-only at depth 30, cut to 10, scores exactly as the default path does.
  - Tried offline: fusing only tier-1 summaries fixed p-queue (0.557) but cost zustand 0.106 (original set 0.515 overall). So the tier-2/3 summaries carry zustand's gain.
  - Fix: `_fuse_summaries` keeps one chunk per split parent (§3). Result (`retrieval/results_orig030d.json`, `results_intent030d.json`): original 0.540 (+0.097, interval +0.027 to +0.166), intent 0.562 (+0.126, interval +0.054 to +0.200). p-queue original 0.494 to 0.524, against 0.550 without summaries.
  - What remains: the three lost queries now find their answer at ranks 4 to 8 instead of not at all. Whole-file and class-body parts still outrank the method, so the rest is a chunk-shape problem, not a fusion one.
- 2026-09-25, **the grader keys on the file too, and whole-file chunks count less in the fusion.**
  - Grader: `tools/real_repo_eval.py` now dedupes on (file, scope without `_part_N`). Keyed on the scope alone, every file's `Full File_part_N` graded as one entry. This moves the baselines: no summaries is now 0.436 original and 0.429 intent (was 0.443 and 0.436). Compare only numbers taken with the same grader.
  - Not done: putting the path into the stored scope. The FAISS id is `md5(tier::file::scope)` and the scope is in the embedded text, so that would re-key every vector and every summary and need a full rebuild of every index. Nothing but the grader lacked the path; each chunk carries `file`.
  - `[retrieval].file_chunk_weight` = 0.5 (§3). A replay over code-list weights 1, 0.5, 0.25 and 0, and summary-list weights 1 and 0.5, gave 0.535 / 0.555 at 1 and 1, and between 0.558 and 0.573 / 0.598 and 0.601 elsewhere. 0.5 was chosen because it keeps whole files present, not because it scored best.
  - Real build (`retrieval/results_orig030e.json`, `results_intent030e.json`): original 0.567 against 0.436 (+0.131, interval +0.062 to +0.198, 41 up, 9 down). Intent 0.601 against 0.429 (+0.172, interval +0.100 to +0.244, 32 up, 4 down). p-queue original 0.560 against 0.537, so no repo now loses.
  - **Caveat: every query in both sets has a symbol as its answer, so no query can show what demoting whole-file chunks costs.** The file-level query set (Verification 3) must check this knob before it is trusted.
  - Still below baseline: `pq-concurrency`, `pq2-enqueue` and `pq2-on-error`, now behind tier-1 `PQueue_part_N` class-body parts, which this knob does not touch. That is the class-skeleton question.
- 2026-09-25, **Verification 3 (file-level queries): `file_chunk_weight` back to 1.0, and all three tiers stay summarized.**
  - 15 queries whose answer is a whole file, 5 per repo, written from the source by one agent per repo, blind to the summaries (`gpu-crash-repro/file_fixtures/`). Graded on the file: "any" counts any chunk of the gold file, "whole" only a tier-2/3 chunk of it. Replayed from the `store` build alongside both symbol sets (`retrieval/results_file_level.json`).
  - File set, any / whole: no summaries 0.730 / 0.226. Code weight 1 with all tiers summarized 0.811 / 0.500. Code weight 0.5 0.717 / 0.150. Code weight 0 0.694 / 0.060. With tier-1 summaries only, 0.72 / below 0.10 at every code weight.
  - Symbol sets at code weight 1 / 0.5: original 0.535 / 0.567, intent 0.555 / 0.601.
  - So the 0.5 weight moved about as much score from file questions to symbol questions as it added, and put file questions below the no-summary baseline. Weight 1.0 with every tier summarized is the only setting here that beats no summaries on all three sets. The knob stays, defaulting to 1.0. Tier-2/3 summaries are what carry file-level questions (whole: 0.500 with them, below 0.10 without), so `[summarization].tiers` stays `[1, 2, 3]`.
  - n = 15 is small; one query moves the file mean by up to 0.067. Two queries sit near the file name ("Windows console" for `_winconsole.py`), and one gold file is a single function (`ssrSafe.ts`).
  - Takeaway for the chunk-shape work: a global weight trades one query type against the other. The p-queue symbol loss (0.511 against 0.537) is back at weight 1.0 and needs a structural fix: one outline chunk per file, not N slices.
- 2026-09-25, **Verification 4: re-measured after ADR-031 removed the ghost vectors. Most of ADR-030's measured gain was the ghosts.**
  - ADR-031 (one FAISS vector per chunk row) was merged into this branch, and `none031` and `store031` were built fresh from it (`gpu-crash-repro/telemetry/retrieval/results_orig031.json`, `results_intent031.json`, `results_file_level_031.json`). Every index now has exactly one vector per row and no duplicate ids; the old `store` had +1/+92/+36 on p-queue/zustand/click in tier 1 and in the summary index.
  - Mechanism, from the code: `_semantic_search` adds `1/(k+rank)` for every hit, so an id holding n ghost vectors collects up to n shares of RRF credit from one tier. zustand's redeclared test helpers (`useBoundStore` 15 and 21 times in two files) were such ids, and they outranked real answers whatever the query.
  - No summaries, ghosts removed: original **0.436 → 0.624** (+0.187, interval +0.119 to +0.256, 34 up, 2 down), intent **0.429 → 0.596** (+0.168, +0.079 to +0.257). zustand alone: 0.313 → 0.757 original, 0.247 → 0.696 intent.
  - The summary index against that baseline: original **0.624 → 0.635** (+0.012, interval −0.049 to +0.070), intent **0.596 → 0.646** (+0.050, −0.018 to +0.116). Neither interval clears zero. The "+0.10 to +0.17" of Verifications 1–3 was mostly the summary list out-voting ghost-inflated ids in the code list.
  - File-level set, any / whole: no summaries 0.767 / 0.264; store, code weight 1, all tiers 0.856 / **0.548**. Tier-2/3 summaries still roughly double whole-file retrieval; that result survives.
  - Replayed settings on `store031`: tier-1 summaries only gives original 0.674 / intent 0.693 but whole-file 0.112. Code weight 0.5 gives 0.671 / 0.705 but whole-file 0.171. The same trade as Verification 3: symbol queries gain what file queries lose.
  - p-queue still loses with summaries: original 0.539 → 0.516. B-026's target stands.
  - **What this means for the ADR:** the case for the summary index now rests on file-level retrieval (n = 15) and a small, not yet significant symbol-query gain, not on the +0.13 headline. Whether that justifies the summarizer's GPU cost per index is an open question for @edb before this merges. Every number in Verifications 1–3 carries the ghosts and should be read as superseded.
- 2026-09-25, **Verification 5: enlarged query sets on the clean indexes; the defaults hold.**
  - 65 new queries, written from source by one agent per repo, blind to summaries and results, every gold checked against the clean index's `symbols`/`files` tables (`gpu-crash-repro/intent_fixtures_v2/`, `file_fixtures_v2/`; merged sets in `*_fixtures_all/`). Sets are now original 83, intent 95, file 40. Every setting is a real `retrieve()` call, not a replay (`telemetry/retrieval/results_clean_sweep2.json`, paired bootstrap 95% intervals).
  - **Summary index (shipped settings) against no summaries:** original +0.012 (−0.049 to +0.070), intent **+0.052 (+0.009 to +0.095)**, file any **+0.076 (+0.001 to +0.151)**, file whole **+0.242 (+0.149 to +0.339, 27 up, 3 down)**. On clean indexes with more queries it earns its place: significant on intent and file questions, neutral on the original set.
  - **`summary_weight`:** 0.75, 1.0 and 1.5 against 0.5 are all inside noise on every set (1.5 leans +0.087 on file whole, −0.014 to −0.020 on symbols). Stays 0.5.
  - **`file_chunk_weight`:** 0.75 gains +0.028 original and +0.042 intent (both significant, none down) and loses **−0.276 on file whole** (34 down, none up). Stays 1.0. The trade is real in both directions, so it needs a structural answer (B-027) or query-type routing, not a global weight.
  - **Graph expansion (arm B against A):** small and positive; significant on intent (+0.015) and file (+0.022 any / +0.029 whole) without summaries. Stays on.
  - **BM25 convex fusion (arm D, `rank-bm25` installed):** large losses on symbol questions (−0.125 original, −0.284 intent without summaries), but **+0.296 on file whole** without summaries and +0.133 with them. Stays off; the file-level gain is a lead for B-027.
  - **A 4,096-token embedding window** (`*_s4096`, tier-2/3 slices seen whole instead of their first 512 tokens): no effect on any set, all within ±0.007 and file whole +0.001. The 512 cap is not what limits whole-file retrieval; the summaries are. `max_seq_length` stays 512.
- 2026-09-25, **Verification 6: alternative fusion rules. Summed RRF stays.**
  - Question from B-026's probes: `pq2-on-error` and `pq2-enqueue` are rank 1 in the code list but lose to chunks ranked moderately in both lists. Would a rule that protects a strong one-list hit do better overall? Replayed offline on `store031` from the exact lists `_fuse_summaries` receives (`gpu-crash-repro/fusion_rules.py`, `telemetry/retrieval/results_fusion_rules.json`). The replay of the shipped rule reproduces the real `retrieve()` numbers exactly.
  - **Max of the two lists** (no reward for both) and **putting each list's rank-1 first** fix both p-queue queries (rank 9 or 11 → 1) and lift the original set +0.025 to +0.031 (not significant). They cost intent −0.02 to −0.05 (max significant) and file-whole −0.14 to −0.23 (significant).
  - **max + λ·min** (λ 0.25, 0.5): no gain anywhere; file-whole −0.04 to −0.11 (significant).
  - **Sharper RRF k** (30, 20, 10): k = 20 gives original +0.010 (barely significant), intent +0.003, file-whole −0.020 (not significant). Too small to justify a change.
  - So the reward for appearing in both lists is what carries intent and file-level retrieval, and the two p-queue losses are its price. Recovering them needs a signal the lists do not have: `pq2-enqueue` is a one-word identifier query, which an exact symbol-name lookup would answer directly (B-016). Not a fusion change.
