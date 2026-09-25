# ADR-030: Summaries Get Their Own Index, Fused With the Code at Search Time

**Status:** proposed
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

### §3. Search fuses it as one more RRF list

`_semantic_search` searches `summary.faiss` with the same query vector and adds `summary_weight / (k + rank)` to each id's fused score, next to the three tier lists. `[retrieval].summary_weight` defaults to 1.0 (settled 2026-09-25, @edb: 1.0 measured better on intent queries, 0.606 against 0.582 at 0.5). With the index missing or empty, search behaves exactly as it does today.

### §4. Which tiers are summarized

`[summarization].tiers = [1, 2, 3]` by default, which keeps what is summarized today (settled 2026-09-25, @edb). Pass 1 and pass 2 both skip tiers not in the list. Tier-2 and tier-3 chunks are the long prompts, so they are most of the summarizer's GPU time, and their value has not been measured: every gold answer in both query sets is a single function. Verification 3 measures it, and the default can be narrowed if they do not pay for themselves.

### §5. Existing indexes

An index built before this ADR has summaries appended inside its code vectors and no `summary.faiss`. It keeps working, and it keeps the appended-summary behavior this ADR exists to remove. Moving to the new layout takes a full re-index. The summaries are cached by text, so a re-index costs an embed pass and not a summarizer pass. `index_meta` records `embed_layout = "code+summary-index"`, and the indexer prints one line recommending a full re-index when an existing index lacks it.

### §6. Summarization back on

`[summarization].enabled` goes back to `true` in this branch. `chore/summaries-off-by-default` turned it off because appending was worse than nothing. That reason is gone once this lands.

## Measured values this ADR still needs

| Value | Why | Source | Result |
|---|---|---|---|
| MRR@10 of a real build with this branch, both query sets | that the offline result holds through the real indexer and retriever | retrieval stages, variant `store` | _gap_ (offline: 0.613 / 0.606) |
| Pass-2 time with summary embeds, this repository | the cost of §2 | stage 7 on this branch | _gap_ (today: 98 s) |
| Tier-2/3 summaries: do they help find the right file | whether §4's default earns its GPU time | file-level query set, tiers [1] against [1, 2, 3] | _gap_ |
| `summary_weight` on a third query set | 1.0 against 0.5 was split between the two sets | later | _gap_ |

## Consequences

**Better:**
- The measured +0.15 MRR on both query sets, the largest retrieval gain this project has recorded.
- Code vectors stop being pulled toward a summary's purpose, so body-level queries recover.
- Tier-2 and tier-3 summaries become visible to search at all.
- A summary can arrive after its chunk is embedded, as one added vector. That is the hard part of ADR-028 §5 done.

**Worse:**
- A fourth index to keep consistent, and one more RRF list to reason about when ranking looks wrong.
- Every existing index needs a full re-index to benefit, and until then it quietly keeps the old behavior. The warning line is the only signal.
- Summarization goes back on by default, so a full index costs 16 minutes of GPU on this repository again, and much longer on CPU. The CPU case is already covered by the ADR-020 kill-switch and by `enabled = false`.
- One weight measured on two query sets that disagreed about it.

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

- [ ] B-025 in `docs/backlog.md`
- [ ] §1 code-only embeds in `ingest_file`
- [ ] §2 `summary.faiss`: build in pass 2, loaded and saved with the tiers, stale removal
- [ ] §3 fusion in `_semantic_search`, `[retrieval].summary_weight`
- [ ] §4 `[summarization].tiers`, honored in pass 1 and pass 2
- [ ] §5 `index_meta.embed_layout` and the re-index line
- [ ] §6 `[summarization].enabled = true`
- [ ] Config drift test for the new keys; unit tests
- [ ] Verification 1 (retrieval stages, variant `store`)
- [ ] Verification 2 to 5
- [ ] Set status to `accepted` in the PR

**Notes:**

- 2026-09-25, **grill (@edb):** building now. Code vectors become code only, all three tiers stay summarized behind a knob with a file-level check to follow, and summary weight 1.0 behind a knob.
