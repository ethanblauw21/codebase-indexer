# Study: Chunk Shape and Embedder, Measured (2026-09-25)

> **Question.** With ADR-030 (separate summary index), ADR-031 (one vector per chunk row), ADR-034
> (class members own their docs) and ADR-035 (bf16 embedder) merged, is the shipped chunk shape and
> embedder the right one? The shipped shape has three tiers: tier 1 is AST symbol chunks, tier 2 is
> 1,500-token slices of the whole file, and tier 3 is 4,000-token slices, all summarized. The
> embedder is `BAAI/bge-code-v1` in bf16 with a code-search query instruction.
>
> **Answer.** Yes, on this corpus. Nineteen arms each change one thing: the embedder, the query
> instruction, the whole-file tiers or tier 1. Only one arm has a significant gain anywhere: fp32,
> on held-out intent (+0.033), and it loses as much on dev symbol questions. Most arms lose on at
> least one set with a significant loss. B-027's
> leading idea, an outline chunk per file, was built and measured. It loses whole-file retrieval
> badly (−0.24 MRR@10) and gains nothing elsewhere.
>
> **Status:** advisory. It changes no code. It feeds [B-027](backlog.md#b-027) and closes ADR-035's
> fp32 question.

---

## 1. Setup

**Stack (provenance, per CONTRIBUTING §4.2).**

| | |
|---|---|
| Code | `master` at `0f39e44`, in a detached worktree. All changes below are monkeypatches in the harness; `src/` is untouched. |
| Embedder | `BAAI/bge-code-v1`, bf16, `max_seq_length` 512, dimension 1536, and core's query instruction ("Given a code search query, retrieve relevant code that answers it") |
| Summarizer | `Qwen/Qwen2.5-Coder-1.5B-Instruct`, fp16, as shipped (ADR-030 layout: a separate summary index fused with the code index) |
| GPU | RTX PRO 1000 Blackwell Laptop, 8 GB, driver 582.70 |
| Libraries | torch 2.9.0+cu129, transformers 4.53.3, sentence-transformers 5.5.0, Python 3.14 |

**Corpus.**
- *Dev:* p-queue, zustand and click, pinned in `benchmarks/real_repo/repos.toml`.
- *Held-out:* lru-cache (isaacs/node-lru-cache v11.5.3, `7e71a1f`) and bullmq (taskforcesh/bullmq
  v6.3.9, `10dc93c`, `src/` only). Neither was used to tune anything.
- All five are JavaScript/TypeScript or Python; see §5.

**Query sets.** The sets are committed in `benchmarks/shape_study/fixtures/`.

| Set | What a query is | Dev n | Held-out n |
|---|---|---|---|
| `orig` / `sym` | the committed real-repo symbol questions (held-out: new ones in the same style) | 83 | 40 |
| `intent` | "what does X do" in plain language, with no identifiers | 95 | 40 |
| `file` | a question whose answer is a whole file | 40 | 12 (bullmq only) |

**Grading** (as ADR-030's `arm_gate`).
- The metric is MRR@10 over distinct (file, scope) results.
- The file set is graded two ways:
  - **any:** any chunk of the gold file counts.
  - **whole:** only a tier-2 or tier-3 chunk of the gold file counts. This is the grading B-027 is
    about.
- Deltas are paired against `bge.base`.
- `*` means the paired-bootstrap CI95 excludes zero (5,000 resamples, seed 0).
- "+g/−l" counts the queries whose gold rank moved by 3 or more (depth 50).

**Controls.**
- Every arm embeds the same chunk texts: token counts always use bge-code-v1's tokenizer.
- Every summary comes from one pool keyed by chunk-text hash, so an arm only summarizes texts
  that no earlier arm produced. Summaries are therefore identical across arms wherever the texts
  are identical, and generation randomness can't separate two arms.
- Each arm is built from nothing into its own directory.
- **Every index was checked:** in each tier, FAISS vectors equal chunk rows, and every arm with the
  shipped shape has the same summary-vector count as `bge.base`.
  - The check was added after the first fp32 build of click was killed mid-run (it was paging).
  - Its database rows survived without their vectors, and the rebuild skipped those files as
    unchanged. So 8 of 15 click file questions found nothing, which looked like fp32 losing
    −0.21\*.
  - Click was rebuilt from nothing, and the fp32 numbers below come from that rebuild.

## 2. Embedders

All run in bf16, except `bge32`, with each model's own recommended query and document prefixes.
Chunk texts are identical across arms.

| Arm | Model | Dev orig | Dev intent | Dev file any | Dev file whole | HO sym | HO intent | HO file any | HO file whole |
|---|---|---|---|---|---|---|---|---|---|
| **bge.base** | BAAI/bge-code-v1 (1.5B) | **0.694** | **0.696** | **0.877** | **0.454** | **0.769** | **0.667** | **0.861** | **0.440** |
| qwen3.base | Qwen/Qwen3-Embedding-0.6B | −0.008 | **−0.112\*** | −0.076 | **−0.088\*** | +0.022 | −0.064 | **−0.272\*** | −0.152 |
| jina15.base | jinaai/jina-code-embeddings-1.5b | +0.010 | **−0.197\*** | −0.058 | −0.022 | −0.023 | −0.136 | **−0.299\*** | **−0.090\*** |
| coderank.base | nomic-ai/CodeRankEmbed (137M) | +0.020 | **−0.303\*** | −0.050 | +0.010 | **−0.070\*** | **−0.222\*** | −0.111 | −0.076 |
| c2llm.base | codefuse-ai/C2LLM-0.5B | −0.016 | **−0.181\*** | **−0.241\*** | **−0.120\*** | −0.054 | **−0.139\*** | **−0.243\*** | **−0.135\*** |
| bge32.base | bge-code-v1 in **fp32** | **−0.030\*** | −0.021 | −0.025 | −0.021 | +0.016 | **+0.033\*** | −0.042 | −0.039 |

- **fp32 is not better than bf16.** Its two significant deltas point opposite ways (§6).
- **Every other model loses intent questions,** by 0.11 to 0.30 on dev. Symbol questions, which
  name an identifier, are within noise for every model on dev.
- **The difference is in plain-language queries.** That is where an agent's search differs from
  grep, so it is the set that should decide.
- `jina-code-embeddings-1.5b` is also CC-BY-NC-4.0, which would have ruled it out for a
  distributable default.
- **C2LLM** needed workarounds to load on Windows: its modeling file imports DeepSpeed, which does not
  install on Windows, and peft. The harness stubs `deepspeed.utils.zero_to_fp32`, which is used for
  training checkpoints only. It loads and runs; it loses intent and file questions on dev and held-out alike.

## 3. The query instruction

bge-code-v1 is instruction-tuned: core sends `<instruct>{instruction}\n<query>{query}`, and
documents get no prefix. These arms change only the query side, so they reuse the `bge.base` index.

| Arm | Instruction | Dev orig | Dev intent | Dev file whole | HO intent |
|---|---|---|---|---|---|
| **bge.base** | Given a code search query, retrieve relevant code that answers it | 0.694 | 0.696 | 0.454 | 0.667 |
| bgeq0 | none (bare query) | −0.043 | **−0.258\*** | +0.083 | **−0.257\*** |
| bgeqa | Given a question about a code repository, retrieve the code that answers it | −0.002 | −0.010 | +0.016 | +0.014 |
| bgeqb | Given a description of what code does, retrieve the code that does it | **−0.032\*** | **−0.031\*** | +0.006 | −0.001 |
| bgeqc | Given a code search query, retrieve relevant code or files that answer it | −0.016 | +0.000 | **−0.009\*** | −0.006 |

- **The instruction is worth a quarter of intent MRR** (−0.26 without it, on dev and held-out
  alike). A caller that bypasses `core.embed` and sends a bare query loses that.
- **Rewording it does not help.** No wording gains significantly, and "a description of what code
  does" loses on both symbol and intent questions. The shipped wording stays.

## 4. Chunk shape

Tier 1 is the shipped AST chunking in every arm of §4.1. Only the whole-file tiers change.

### 4.1 Whole-file tiers (B-027)

| Arm | Tier 2 | Tier 3 | Dev orig | Dev intent | Dev file any | Dev file whole | HO file whole |
|---|---|---|---|---|---|---|---|
| **bge.base** | 1,500-token slices | 4,000-token slices | 0.694 | 0.696 | 0.877 | 0.454 | 0.440 |
| outline | one outline per file\*\* | none | −0.006 | +0.011 | **−0.089\*** | **−0.238\*** | **−0.169\*** |
| outlined | outline + first doc sentence per symbol | none | −0.005 | +0.006 | **−0.091\*** | **−0.239\*** | **−0.144\*** |
| outline3 | outline | 4,000-token slices | −0.012 | +0.007 | −0.019 | −0.047 | +0.010 |
| slice512 | 512-token slices (64 overlap) | none | −0.005 | **−0.055\*** | +0.037 | +0.031 | −0.049 |
| both512 | outline | 512-token slices | −0.023 | **−0.040\*** | +0.029 | +0.063 | −0.004 |
| t2only | 1,500-token slices | none | **−0.018\*** | **−0.016\*** | +0.015 | **−0.065\*** | **−0.088\*** |
| t3only | none | 4,000-token slices | −0.010 | −0.004 | −0.039 | **−0.115\*** | **−0.088\*** |

\*\* The outline is the file's leading comment, its imports and one line per symbol (kind, name,
signature). It is paged at 480 tokens so the embedder reads all of it. A file with no symbols gets
512-token slices.

**What this says.**

1. **An outline cannot replace the slices.** The outline arms lose a quarter of whole-file MRR on
   dev and a sixth on held-out.
   - A likely reason, not tested separately: an outline has no code bodies, so its summary can
     only restate names.
   - Adding each symbol's doc sentence (`outlined`) does not recover it.
   - With the slices kept beside it (`outline3`), the outline adds nothing measurable.
   - B-027's first idea is measured and rejected.
2. **Both slice tiers carry weight.** Dropping tier 3 (`t2only`) or tier 2 (`t3only`) each costs
   whole-file MRR significantly, on dev and on held-out. Dropping tier 3 also costs symbol and
   intent questions a little.
   - So tier 3 is not redundant as a whole. This does not test B-010's ingestion gating, which
     skips tier 3 only where it is byte-identical to tier 2 (files smaller than the tier-2 window).
   - A likely reading: the two tiers summarize the file at two grains, and the summaries are what
     carry file-level retrieval (ADR-030 Verification 5: whole 0.259 → 0.500 with them).
3. **Slices the embedder can read whole (512 tokens) cost intent questions.** The gain in file
   questions is not significant and does not hold on held-out.
   - The mechanism is ADR-030's: more whole-file chunks means more of them in each fused list,
     crowding out the symbol chunks that answer intent questions.
   - It is consistent with B-027's measured note that a 4,096-token window changed whole-file MRR
     by +0.001. The window was never the limit.
4. **Cost.** Slice and outline arms that need new summaries are the expensive ones. `slice512`
   summarized 1,291 new chunks across the five repos, and click alone took 426 s against 112 s for
   base.

### 4.2 Tier 1

| Arm | Change | Dev orig | Dev intent | Dev file any | Dev file whole | HO sym | HO intent |
|---|---|---|---|---|---|---|---|
| **bge.base** | AST chunks capped at 500 tokens (overlap 50), with File, Entity, Tags and Lines header lines | 0.694 | 0.696 | 0.877 | 0.454 | 0.769 | 0.667 |
| t1lean | no Tags or Lines lines | −0.005 | −0.018 | +0.015 | −0.018 | −0.029 | −0.047 |
| t1s300 | cap 300 tokens (overlap 50) | +0.001 | −0.045 | +0.006 | +0.040 | +0.015 | −0.001 |
| t1s1000 | cap 1,000 tokens (overlap 100) | **−0.036\*** | −0.019 | +0.004 | −0.006 | −0.019 | −0.026 |

- **The header lines stay.** Removing them is slightly worse on seven of eight measures, though no
  loss is significant.
- **500 tokens is the right cap.**
  - At 1,000, more symbols fit whole, but the embedder still reads only the first 512 tokens.
    Symbol questions lose significantly (−0.036\*).
  - At 300, more symbols are split into parts. Intent questions trend down (−0.045, CI
    [−0.103, +0.012]); held-out intent is flat (−0.001).
  - Neither moves anything significantly upward. Whole-file on dev rises +0.040 at 300 without
    reaching significance, and it does not hold on held-out.
- **Cost:** 300 tokens adds 15% more tier-1 chunks (3,086 → 3,555 over five repos; click 1,286 → 1,455, bullmq 1,262 → 1,463). It
  needed 1,089 new summaries across the five repos.

## 5. Limits

- **Five repositories, two languages** (TypeScript/JavaScript and Python). C# and C++ are Tier-A
  languages whose chunking is the same code path, but their retrieval was not measured here.
- **The held-out file set is small:** 12 questions from one repo. Its CIs are wide, and a
  held-out file result is a direction, not a verdict.
- **Summaries are fixed per chunk text.** An arm that changes a chunk's text gets a newly
  generated summary for it, and one generation is one sample. Summary variance between runs of
  the same text is not measured.
- **bf16 everywhere except `bge32`.** Its question is whether bf16 costs retrieval quality. See §2.
- **Test-file handling** (B-027's second idea, `describe`/`it` blocks as symbols) was not built.
  It needs a parser change and an FQN decision first, and it is the part of B-027 this study
  leaves open.
- **BM25 file-level fusion** (B-027's measured lead) is a retrieval change, not a chunk shape, and
  it is out of scope here.

## 6. Recommendations

1. **Keep `BAAI/bge-code-v1`** as the embedder, with the shipped query instruction.
2. **Keep the shipped shape:** AST tier 1 at 500 tokens with its headers, plus tier 2 and tier 3
   slices, all summarized.
3. **B-027:** drop the outline-replaces-slices idea (measured, §4.1) and the "512-token-blind" framing
   (measured twice now). What remains is test-file blocks and file-level BM25.
4. **B-010's ingestion gating must stay narrow:** skip tier 3 only where its text is identical to
   tier 2. Dropping tier 3 more widely costs whole-file retrieval (`t2only`, §4.1).
5. **ADR-035: bf16 stays, and its open question is closed.** fp32 is within ±0.05 of bf16 on
   every measure. Its two significant deltas point opposite ways: dev symbol −0.030\*, held-out
   intent +0.033\*. Neither precision is better. fp32 also needs 6.2 GB and embed batches of 4 to
   avoid paging on the 8 GB card, and it built about 3 times slower (bullmq 416 s against 137 s).

## 7. Reproducing

Everything is in `benchmarks/shape_study/`:
- `fixtures/`: the query sets (dev intent and file sets, and the three held-out sets; the dev symbol
  set is `benchmarks/real_repo/fixtures/`).
- `results/<arm>.json`: every query's gold rank for every arm, which is enough to recompute any
  number here or compare a future arm without rebuilding this one.
- `table.txt`: the full comparison, with CIs and gained/lost counts.
- `harness/`: `shape_study.py` and `study_queue.sh`, **as a record**. They ran from a local,
  untracked GPU kit and import its `arm_gate.py` and `file_level_eval_031.py`, so they do not run
  from this directory as they stand. Porting them into `tools/` is the step before anyone reruns
  an arm.

An arm is run as `shape_study.py build <arm>`, then `eval <arm>`, from a `master` checkout, and
compared with `table bge.base <arm> …`.
