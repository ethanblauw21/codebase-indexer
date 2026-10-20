# ADR-055: One Result per Text, and Eval Rows That Name Their Stack

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-055-dedupe-results`
**Reviewer:** @edb
**Backlog:** [B-010](../backlog.md#b-010) (read-time dedup only), [B-009](../backlog.md#b-009), [B-005](../backlog.md#b-005)
**Depends on:** none
**Depended on by:** none yet. [B-011](../backlog.md#b-011) (reinforcement) and B-010's ingestion-time gate build on the dedup key chosen here.

## Context

**B-010: the same text comes back twice.**
- When a file is smaller than the tier-2 window, its tier-2 and tier-3 windows are both the whole
  file. Both are embedded and stored under different FAISS ids (the tier name is part of
  `stable_id`), so both can be retrieved.
- On 2026-07-27, about a fifth of the returned slots were such a twin: `src/stable_id.py`'s 3,794
  characters at ranks 2 and 3 of a top-5.
- With summaries on (ADR-030), `_fuse_summaries` already keeps one chunk per `(file, scope without
  _part_N)`, which catches the twins whose scope labels match. Without summaries, and in
  `_rerank`'s cut, nothing collapses them.

**B-009: an eval row can't say what produced it.**
- `benchmarks/real_repo/*.jsonl` rows carry `git_sha` but no model ids, and `git_sha` is
  `"unknown"` on the authoritative n=148 cloud run.
- So every reranker number was found, two hours of archaeology later, to predate the
  `bge-code-v1` swap. `CONTRIBUTING.md` §4.2 now asks for the stack by convention; this makes the
  harness write it.

**B-005:** two research-doc pointers still describe a closed question as open.

## Decision

### 1. Collapse identical text in one file, before the cut (B-010)

`hybrid_retriever._dedupe_same_text()` runs over the ranked list before every top-k cut: the three
exits of `_rerank` and the final cut in `_fuse_summaries`.
- **Key:** `(file, text)`. The dict hashes the text, so this is a content hash with no collision
  risk; the strings are already in memory. Byte-identical only, no whitespace normalization.
- **Survivor:** the hit from the most specific tier (tier 1, then 2, then 3). It takes the best
  score and the best position in its group, so collapsing never demotes the text.
- **Refill:** the cut comes after, so the freed slot goes to the next distinct candidate. A caller
  asking for 5 gets 5 different texts when the pool has them.
- **Order:** every other chunk keeps its relative order.
- **Empty text is never a match.** A chunk with no text says nothing about its content (the
  existing tests build chunks with `text=""`), so it is kept as it is.

### 2. Identical text in different files is kept, both copies (B-010)

The backlog wants cross-file copies caught too. **This branch does not collapse them.**
- `find_similar_code` exists to find copies. Collapsing the second file would delete the answer.
- `detect_pattern_violations`, `trace_data_flow`, `find_dead_code` and `find_test_coverage`
  report per file. A copy-pasted violating file would vanish from the report.
- An "also in: …" note on the survivor would keep the path in the retriever, but nothing would
  show it: every tool renders `RetrievedChunk`s itself, in `MCPServer.py`, which this branch does
  not edit. The path would be dropped in practice.

So the dedup key includes the file. Cross-file collapse is an open question for @edb (below).

### 3. Every eval row names its stack (B-009)

`tools/eval_common.append_baseline` stamps each row with `embedder_model_id`, `embedder_dim`,
`reranker_model_id`, `reranker_enabled` and `summarizer_model_id` (`stack_provenance()`).
- The values come from `load_indexer_config()`, the same call (from the same working directory)
  that `core.py` and `HybridRetriever` make during the run. A caller may pass `config=` instead.
- A missing key falls back to the code default the run itself would use (`core._DEFAULT_*`,
  `hybrid_retriever._DEFAULT_RERANKER_*`, `config.DEFAULT_SUMMARIZER_MODEL_ID`), not to a third
  copy of the defaults.
- **A field the record already has wins.** The reranker is switched per arm, not by config: the
  real-repo arm C turns it on whatever `[reranker].enabled` says. So the two harnesses set
  `reranker_enabled` themselves (one line each, see the log), and the config value is only the
  fallback.
- Existing `benchmarks/` files are not rewritten: an old row cannot be stamped honestly after the
  fact. New rows carry the fields from the next run on.

### 4. Two stale pointers (B-005)

- `study-codebase-memory-mcp.md`: the ⚠️ fact-check flag becomes a note that it is discharged.
- `adr-backlog.md`: the amendment is against ADR-017, the tier model's home, and the "150+" fix is
  marked done. ADR-017 already says 66, and ADR-008 states the correction.

## Consequences

**Better:**
- A search returns as many distinct texts as it was asked for, and the 4,000-token packing budget
  of `semantic_code_search` stops paying for the same bytes twice. Existing indexes benefit with no
  rebuild.
- A gold chunk's rank can only stay or rise with the dedup on the summary-free path: everything
  removed is a copy of something ranked above it.
- A baseline row states its own stack, so a stale number is visible in the row.

**Worse:**
- Vendored or copy-pasted files still take one slot per copy in `semantic_code_search`.
- A dedup pass over the pool on every call: a dict over the pool, at most `max(35, top_n)` chunks
  (`top_n` is 80 for `trace_data_flow`). Negligible next to an embedding.
- With summaries on, removing a twin from the code ranking moves every chunk below it up a rank, so
  their code-side RRF share grows a little against summary-only chunks. The fused order can change
  in ways other than "one fewer duplicate". Not measured: there is no GPU or model here.

**Neutral (the side effects checked):**
- **`iterative_retriever.py`.** Each round gets `top_n` distinct texts. Twins had equal scores, so
  they could make the 5th-best-to-top ratio look like a plateau and stop the loop early;
  confidence is now a little lower when a round had twins, which can mean an extra round. Its own
  final dedup is by `faiss_id` and is unchanged. *Pre-existing, not fixed here:* its
  `explored_fqns` keys on `scope` alone, and tier-2/3 scopes like `Full File_part_1` aren't
  file-qualified, so one file's whole-file chunk excludes every other file's from later rounds.
- **Graph expansion** runs on the semantic hits before the dedup, so its seeds and budget are
  unchanged. Seeds are tier-1 FQNs, and same-file tier-1 twins are practically nonexistent.
- **`tier_projection`** is a CoIR harness setting (`tools/coir_eval.py`). CoIR runs its own FAISS
  over the CoIR corpus and never calls `HybridRetriever`, so it is untouched. The real-repo harness
  grades `retrieve()`'s output, and per the rank argument above its MRR can only stay or rise on
  the summary-free arms.
- **`core.pack_context_safely`** has no caller in `src/` or `tools/` today. If one appears, it
  receives distinct texts. `MCPServer._pack_results` (the live packer) gets the same benefit.
- The index stays homogeneous: every file still has its tier-3 chunk. That only changes with the
  ingestion-time gate, which is out of scope.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Collapse identical text across files, keep an "also in: …" list on the survivor | Breaks `find_similar_code` and the per-file scan tools, and no tool would render the list without an `MCPServer.py` change. Open question below. |
| Key on `text` alone, drop cross-file copies silently | Same breakage, with the path gone too. |
| Keep the best-scoring twin instead of the most specific tier | Twins have the same embedding, so their scores differ only by tier rank noise. The tier-2 chunk is the narrower claim about what the text is. |
| Dedupe inside `_semantic_search` | Before expansion and the category boost: the boost and the reranker could reorder afterwards, and structural hits would be missed. The cut is the place. |
| Dedupe in `MCPServer._search` | Only MCP callers would get it; the eval harness and the iterative retriever would not. The prompt and the backlog both put it in the retriever. |
| Hash with MD5, like `chunk_summaries` | A dict on the text is the same operation without a hash collision to reason about. The MD5 precedent matters if the key is ever persisted, which this one is not. |
| Ingestion-time gate (skip a tier that equals the lower one) | Out of scope: it doesn't fix existing indexes and it makes them heterogeneous. Waits on a GPU eval with B-011. |
| B-009: stamp `reranker_enabled` from config only | Wrong for arm C, which turns the reranker on by constructor argument. A row would claim the reranker was off on the run that measured it. |
| B-009: rewrite the existing `benchmarks/` rows | The stack can't be recovered honestly for those runs; that is the whole problem. |

## Open questions for @edb

1. **Cross-file copies in `semantic_code_search`.** Should the read-only search tool collapse
   byte-identical text across files, and show "also in: a.py, b.py" on the survivor? It would need a
   `retrieve(..., collapse_copies=True)` opt-in, an `also_in` field on `RetrievedChunk`, and a line
   in `_pack_results`. It must stay off for `find_similar_code` and the per-file scan tools.
2. **`iterative_retriever`'s scope-only `explored_fqns` key** (noted above). Small fix, separate
   item?
3. **B-009: should the row also carry `summarization_enabled`** and the fusion knobs
   (`fusion_mode`, `summary_weight`, `file_chunk_weight`)? They change results as much as a model
   id does. Not added, because the backlog names the four model fields.

## Implementation Log

- [x] ADR (this file), first commit on the branch
- [x] B-010: `_dedupe_same_text` before the three cuts in `_rerank` and the final cut in
  `_fuse_summaries`. In `_fuse_summaries` the fused list is now built in full (≤ ~60 chunks)
  before the cut, so the dedup runs ahead of the existing ADR-030 part filter.
- [x] B-010 tests, `tests/test_dedupe_results.py` (9):
  - a tier-2/tier-3 twin pair becomes one tier-2 hit at the pair's best score, and the slot refills;
  - the tier-2 hit wins when it ranks first too; a tier-1 chunk equal to its whole file stands for all three;
  - identical text in two files keeps both copies;
  - distinct texts keep their order, and a collapse leaves every other hit in order;
  - empty text never matches;
  - the reranked path collapses too;
  - a twin that arrives only through its summary, under another scope label, doesn't take a second slot.
- [x] B-009: `stack_provenance()` and the `append_baseline` stamp; tests in `tests/test_eval_common.py`
  (3): a temp `indexer.toml` with non-default ids reaches the row through the working directory; a
  field the record sets wins; a missing key reports the code default.
- [x] B-009, outside this ADR's file list (smallest change, both files are owned by no session):
  - `tools/real_repo_eval.py`: per-arm rows record `ARMS[arm]["reranker_enabled"]`; lift rows
    record it when both arms agree and `None` when they differ (the reranker lift);
  - `tools/coir_eval.py`: rows record `embedder_model_id` and `reranker_model_id` from the
    harness's own `load_config()` (it loads its models from `_ROOT/indexer.toml` with its own
    fallbacks, not through `core.py`), and `reranker_enabled = reranker is not None`.
- [x] `CONTRIBUTING.md` §4.2: one line saying the harness stamps each row.
- [x] B-005: the two doc pointers, in their own commit. `adr-backlog.md`'s lines had moved to 151 and
  178 since the backlog item was written.
- [x] Gates, on CPU, Python 3.12.3, no model and no network to huggingface.co (`HF_HUB_OFFLINE=1`):
  - flake8 on `src/`: clean;
  - `pytest --co`: 716 collected (704 on `871b55c`), no errors;
  - `pytest`: 666 passed, 2 skipped, 48 failed. `871b55c` gives 654 passed, 2 skipped and the
    **same 48 failures**, all from the HuggingFace tokenizer the chunker needs, which isn't
    cached here (31 direct `OSError`/`LocalEntryNotFoundError`, the rest are files that then
    failed to index);
  - `conformance_eval.py --check-baseline`, `resolution_eval.py --check-baseline`,
    `l5x_fixture_teeth.py --check`: all exit 0.
- [x] MCP Inspector: not run. No tool, signature or docstring changed; the dedup is inside
  `retrieve()`.
- [ ] **Not measured:** the duplicate rate before and after on a real index. The tier-2/3 windows
  are cut by the embedder's tokenizer, which isn't available here, so not even a model-free count
  of twin files was possible. The 2026-07-27 figure (about a fifth of the slots; `BAAI/bge-code-v1`,
  dim 1536, reranker off, `fusion_mode = "rrf"`, 98-file index of this repo) is the only number.
  Worth re-running those four queries on the next GPU session.

**Notes:**
- 2026-10-20: chose `(file, text)` over `text` as the key once it was clear `find_similar_code`
  and `detect_pattern_violations` read `retrieve()`'s output directly. See Decision §2.
- 2026-10-20: `pip install torch --index-url https://download.pytorch.org/whl/cpu` was blocked
  here; `pip install -e ".[dev]"` pulled torch from PyPI instead.
