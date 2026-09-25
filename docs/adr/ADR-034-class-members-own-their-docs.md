# ADR-034: Class Members Own Their Docs, and Every Member Gets a Chunk

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-034-class-member-chunks` (stacked on `feature/adr-033-chunker-version`, with
`feature/adr-030-summary-index` merged in for measurement)
**Reviewer:** @edb
**Backlog:** [B-026](../backlog.md#b-026) Stage 1 — class members lose their docs, private methods and
getters from the index
**Depends on:**
- ADR-031, because the merge policy here relies on its one-vector-per-row dedupe for the cases it leaves
  alone.
- ADR-033, whose `CHUNKER_VERSION` this ADR bumps to 3.
- ADR-030, for the `store031` baseline and the summary-fused arm that the gate is measured on.

**Depended on by:**
- B-026 Stage 2, which needs the skeleton parts' body-only line ranges and the unchanged `class_context`
  text.
- B-027, which gives test-block helpers distinct names.

## Context

B-026 records the diagnosis, the jury review (`CHUNK_SHAPE_PLAN_REVIEW.md`) and the prerequisites
measured on 2026-09-25. In short, a class's members are indexed poorly in four ways, each of which loses
data today.

1. **Member doc comments live only in the class skeleton.** A TypeScript `/** … */` is a sibling of its
   `method_definition` in the tree, so the method chunk does not contain it.
   - `PQueue`'s skeleton runs to 11,545 characters in 7 parts, and most of that is JSDoc.
   - A probe moved each member's JSDoc into that member's own chunk and re-embedded it. p-queue's
     original set rose from 0.555 to 0.735 without summaries, and from 0.554 to 0.696 with them.
     `pq-pause` went from rank 4 to 1 and `pq-sizeby` from 7 to 1.
2. **Private `#` members get no chunk and no call edges.**
   - `_TS_NAME_TYPES` (`ts_adapter.py:101`) lacks `private_property_identifier`, and `skeletonize`
     stubs the bodies, so the bodies of `PQueue`'s 21 `#` members are not indexed anywhere.
   - `_CALL_QUERY` (`ts_adapter.py:59`) matches only `property_identifier`, so a call written
     `this.#x()` produces no edge.
3. **Arrow-function class fields** (`private work = () => {…}`) are neither extracted nor stubbed.
4. **Contiguous symbols sharing an FQN overwrite each other.** This covers accessor pairs, Python
   `@property` with its setter, and `@overload` sets. `symbols.fqn` is UNIQUE (`db.py:135`), so only the
   last one's text survives, and ADR-031 drops the earlier chunk.

The probe for this ADR (2026-09-25, `probe_parse.py` in the session scratchpad) found one more defect:

5. **Python skeletons keep the bodies of decorated methods.** `skeletonize` is called with
   `{"function_definition"}`, but a decorated method's node in the class body is a
   `decorated_definition`. So every `@property`, `@staticmethod` and `@classmethod` body is copied
   whole into the class skeleton.

Two ideas were measured and rejected before this ADR:
- **Repeating the class declaration inside each member chunk** cost 0.10 against the doc-move alone,
  because every member then shares one long line.
- **Merging the getter and setter** changed no retrieval rank. It is kept here as hygiene: it removes a
  lost text, not a lost rank.

The losses that remain on p-queue have a different cause. `pq2-on-error` and `pq2-enqueue` lose to RRF
fusion (ADR-030 Verification 6), and `pq-concurrency` is a vocabulary gap. This ADR does not claim to
fix them.

## Decision

The change covers the TypeScript and Python adapters, `skeletonize` and `ast_chunker.py`.
- **C# and C++ are out of scope.** They have their own skeletonizers (`csharp_adapter.py:150` and
  `cpp_adapter.py:236`) and keep member docs inside the skeleton for now. That difference is deliberate,
  not a regression.
- **The chunker version is bumped to 3**, so existing indexes warn that they are stale (ADR-033).

### 1. Doc comments move to their members (TypeScript)

**The rule:** a leading doc comment moves out of the skeleton and into its member's text **only when
that member is emitted as a `Symbol` in the same parse.** Documented plain fields, index signatures,
abstract members and overload signatures keep their docs in the skeleton.

**"Leading":** a `/** … */` comment node that is the class body's previous named sibling of the member.
The member's decorators may sit between them, and at most one blank line may separate them. A `//`
comment and a `/* */` block that is not JSDoc do not move.

**Where the moved doc goes:**
- It is appended **after** the member's code in `Symbol.text`, so the chunk reads code first, then
  doc. Arm 1 measured both placements (see the log). With the doc first, a long doc ahead of a short
  body diluted the body's vector. `setPriority` went from 113 to 404 tokens, mostly prose and
  examples, and an intent query about its validation fell from rank 2 to 11. With the doc after the
  code, that loss is gone.
- Examples inside the doc stay. Stripping fenced and `@example` blocks made no measurable difference.
- The member's `start_line` is moved up to the doc's first line, so `path:start-end` covers what the
  chunk holds.
- `skeletonize` leaves the moved comment out of the skeleton, and the stub stays.

Python needs no doc-move, because a docstring is inside the body already.

### 2. Private `#` members and arrow fields are extracted (TypeScript)

- **`#` members.**
  - `private_property_identifier` is added to `_TS_NAME_TYPES`, so `#` methods and accessors are
    extracted.
  - The `#` stays in the FQN (`source/index.ts::PQueue.#tryToStartAnother`). A lookup by FQN must accept
    it, and the MCP Inspector run checks that.
  - `private_property_identifier` is added to `_CALL_QUERY`, so `this.#x()` produces a call edge and a
    reference. Without that, `find_dead_code` would report every `#` member as dead.
- **Arrow fields.**
  - A `public_field_definition` whose value is an `arrow_function` or `function_expression` is
    extracted as `kind="arrow_function"`, whatever its access modifier.
  - `skeletonize` stubs the body inside such a field.

### 3. Contiguous same-FQN siblings merge into one symbol

Only **consecutive** members of one class body with the same name merge: TypeScript `get`/`set` pairs
and Python `@property` with `@x.setter`/`@x.deleter`. Python also merges consecutive module-level
redefinitions, such as a class defined in both branches of `if t.TYPE_CHECKING:`. It never extracts
nested functions, so a top-level run there is always one name defined twice. TypeScript does not merge
top-level runs, because helpers redeclared in separate test callbacks are consecutive there.
- **Order in the text.** The member with the most code comes first, so the logic stays inside the
  embedder's 512-token window whether it lives in the getter or the setter. Arm 3 measured "getter
  first" (the first draft of this rule): it put p-queue's one-line `get concurrency()` ahead of the
  setter that drains the queue, and `pq-intent-07` fell from 11 to 17.
- **`@overload` stubs are dropped, not merged**, when an implementation of the same name exists. A stub
  is a signature without a body, and the implementation's own signature subsumes it. Merging them in
  measured as pure dilution: `click-intent-09`'s `Editor.edit` fell from rank 1 to 4. Without an
  implementation, as in a stub-only module, the stubs merge.
- **Decorators stay out of the symbol's text**, as before. The adapter reads them from the tree to rank
  a run. Putting them in the text was measured and rejected: long `@pytest.mark.parametrize` tables
  pushed 33 click tests past tier 1's 500 tokens into `_part_N`.
- **Lines.** The merged symbol runs from the first member's first line to the last member's last line.
- **Type row.** `symbol_types` keeps one row per FQN. For an accessor pair it is the getter's, which
  carries the return type.
- **Edges.** The calls of every merged member are kept, since edges are keyed by FQN.
- **What stays unmerged.** Same-FQN symbols that are not consecutive, such as the zustand helpers
  (92 of them), are left alone. ADR-031 already keeps one vector for them, and B-027 gives them
  distinct names.

### 4. Python skeletons stub decorated methods

`skeletonize` treats a `decorated_definition` that wraps a stub type as that stub type. The decorators
stay in the skeleton and the body becomes ` ...`.

### 5. Split skeleton parts record the source lines they cover

- **Line ranges.** Each `_part_N` of an oversized class skeleton records, in `start_line` and
  `end_line`, the source lines it covers. Split parts used to report 0/0. Stubbed bodies break the
  one-to-one mapping between skeleton and source lines, so `skeleton_with_lines` returns the source
  line of every skeleton line, and `fallback_token_chunker` maps each part's first and last lines
  through it. Stage 2 needs these ranges to pick the part that holds a given member.
- **No repeated header.** The first draft also repeated the class declaration at the top of parts 2
  to N. Arm 4 measured that and rejected it (see the log), as the earlier probe had done for member
  chunks: a line shared by many chunks makes them alike. The line ranges are metadata only, so chunk
  text is unchanged.

### Measurement: one arm per change

Every arm is built on the three eval repos (p-queue, zustand, click), on the GPU with summaries
regenerated, and logs its summary-cache hits and misses. Each one is compared with `store031`.

| Arm | Adds |
|---|---|
| 1 | doc-move (§1) |
| 2 | + `#` members and arrow fields (§2) |
| 3 | + merge (§3) and decorated stubs (§4) |
| 4 | + skeleton header and line range (§5); the header was rejected, the line range kept |

The kit's `clean_sweep2.py` and `pq_mechanism.py` are rerun on each arm.

### The gate

Every criterion has a threshold. The final arm must pass all of them.

| Criterion | Threshold |
|---|---|
| Named p-queue queries | Each ranks no worse than under `store031`, which covers the 23 queries with a rank today. `pq-concurrency` may stay not found. `pq2-on-error` and `pq2-enqueue` are exempt (@edb, 2026-09-25): ADR-030 Verification 6 attributes their loss to the fusion rule, not to chunk shape. |
| p-queue original set | MRR@10 ≥ 0.554 (`store031`) |
| Regressions per set | In each set, at most 2 queries lose 3 or more ranks against `store031` |
| Set baselines (`store031`, arm B, `arm_gate.py` at depth 50) | original 0.641, intent 0.671, file "any" 0.873, file "whole" 0.467. The means are reported with the paired CI95 but not gated on it. |
| Skeleton size | `PQueue`'s skeleton is at most 8,500 characters (probe: 7,996), and no class skeleton in the three repos grows, except by §5's header line per part |
| Token budget | A moved doc does not push a member that fit tier 1's 500 tokens into `_part_N`; each such member is listed with its token count |
| Chunk integrity | `index_status` reports vectors equal to chunk rows for every tier |
| Tests | ADR-008 fixtures and unit tests below pass, and the suite is otherwise unchanged |
| MCP Inspector | Tool listing passes `--strict`; a search returns a `#` FQN; a lookup by a `#` FQN works; a bad argument comes back `isError: true` |

**Fixtures and tests:**
- a documented plain field, whose doc stays in the skeleton;
- a documented method, whose doc moves;
- a decorated method with a doc;
- a `#` method with a caller, giving one symbol and one call edge;
- an arrow field, extracted and stubbed;
- a getter/setter pair, giving one chunk and one symbol row;
- Python `@property` with a setter;
- Python `@overload` with the implementation first;
- a Python decorated method, stubbed in the skeleton;
- scattered same-name helpers, not merged, with no duplicate ids.

## Consequences

**Better:**
- A member arrives with its documentation, so natural-language questions about a method can match it.
  That is the +0.14 measured on p-queue with summaries.
- Class skeletons shrink toward what they are for: signatures.
- `#` members and arrow fields become searchable and appear in the call graph, and `find_dead_code`
  stops being blind to them.
- An accessor pair or overload set is one chunk with its whole text, instead of the last declaration
  alone.

**Worse:**
- **Every TypeScript and Python index must be rebuilt**, summaries included, because nearly every class
  member's text changes. ADR-033's warning says so, but nothing rebuilds for the user.
- **Members' `start_line` moves up to their doc.** Anything that relied on it being the signature line
  changes. `path:start-end` in tool output gets longer, and that is the intent.
- **More symbols mean more chunks.** p-queue gains about 21 `#` members, so more summaries to generate
  and more candidates in each result list.
- **C# and C++ now behave differently from TypeScript and Python** until they get the same change.
- **The merge rule adds a new failure shape.** If two adjacent same-name members are unrelated, they are
  stitched together. The adjacency and same-class rules make that unlikely, but not impossible.

**Neutral:**
- `class_context` is unchanged, so B-026 Stage 2 can build on it.
- Scores on the `pq2-on-error`, `pq2-enqueue` and `pq-concurrency` queries are not expected to move.

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Repeat the class declaration in each member chunk | Measured: −0.10 against doc-move alone on p-queue original, with summaries |
| Strip JSDoc from the skeleton without moving it | Measured: +0.02, against +0.14 for moving it. The docs are content, not noise. |
| Merge every same-FQN symbol | Stitches unrelated test helpers together (zustand: 92). Adjacent siblings only. |
| Change the fusion rule for the one-list losses | ADR-030 Verification 6: every alternative costs intent or file "whole" significantly |
| Store a parent stable id instead of `class_context` text | Parts and tiers make an id ambiguous. Deferred to Stage 2, which keeps the parent as text. |
| Include C# and C++ now | Separate skeletonizers, no retrieval eval repos for them, and no measured need |

## Implementation Log

- [x] Merge `feature/adr-030-summary-index` in, for the measurement stack
- [x] §1 doc-move (TypeScript): adapter, `skeletonize`, tests (`tests/test_class_member_docs.py`)
- [x] Arm 1 built and measured against `store031` (see notes)
- [x] §2 `#` members, call query and arrow fields: tests (`tests/test_private_members.py`)
- [x] Arm 2 built and measured (see notes)
- [x] §3 merge and §4 Python decorated stubs: tests (`tests/test_member_merge.py`)
- [x] Arm 3 built and measured: four variants, 3d kept (see notes)
- [x] §5 line ranges kept; the header measured and dropped (`tests/test_skeleton_parts.py`)
- [x] Arm 4 built and measured; the gate table filled in (see notes)
- [x] `CHUNKER_VERSION` → 3 (16a5d02). It belonged in each chunk-changing commit; it landed after
  them, which only matters if the branch is split before merging
- [x] MCP Inspector run saved: `gpu-crash-repro/telemetry/inspector_034/` (see notes)
- [x] Update B-026 in `docs/backlog.md` on `master`: Stage 1 promoted → ADR-034
- [ ] Resolve **Depended on by**: Stage 2 gets per-part source line ranges (not "body-only": no header is
  added, so a part's range is simply what it holds) and an unchanged `class_context`

**Notes:**
<!-- 2026-09-25: branch cut from ADR-033 (0604392); probe found the Python decorated-body defect (§4). -->

**2026-09-25, arm 1 (§1 doc-move).**
- **Setup:** three eval repos rebuilt on the GPU with summaries. The summary cache was seeded, so only
  the changed chunks were re-summarized (p-queue: 21). Measured with `gpu-crash-repro/arm_gate.py`
  (depth 50, arm B). Results are in `telemetry/retrieval/results_gate_034*.json`.
- **Chunk sizes:**
  - `PQueue`'s skeleton went from 10,105 to 5,475 characters.
  - No class skeleton in the three repos grew.
  - The same 17 members exceed 500 tokens before and after, so no member was pushed into `_part_N`.
- **Doc placement, measured four ways:**

  | Variant | orig (83) | intent (95) | file any / whole (40) | p-queue orig |
  |---|---|---|---|---|
  | `store031` | 0.641 | 0.671 | 0.873 / 0.467 | 0.554 |
  | doc first | +0.050* | +0.018 | −0.013 / −0.017 | 0.729 |
  | doc first, examples stripped | +0.050* | +0.019 | −0.013 / −0.017 | 0.729 |
  | **doc after code** | **+0.051*** | **+0.025*** | **+0.004 / −0.010** | **0.730** |
  | doc after, examples stripped | +0.051* | +0.025* | +0.004 / −0.010 | 0.730 |

  \* = the paired 95% interval excludes zero.
- **Doc first fails the gate on intent.** 3 queries lose 3 or more ranks: `pq-intent-04` 2→11,
  `pq-intent-06` 2→5, `pq-intent-10` 9→14. Doc after code leaves only `pq-intent-06` (2→6), where the
  query names the "all-work-finished notification" and `onIdle`, which now carries its doc, fairly
  competes.
- **Named p-queue ranks, doc after code:**
  - Gains: `pq-pause` 4→1, `pq-saturated` 2→1, `pq-sizeby` 7→1, `pq-running-tasks` 13→3,
    `pq2-pending` 10→4, `pq2-on-pending-zero` 9→1 and `pq2-is-rate-limited` 4→1.
  - Worse: `pq2-on-error` 9→12. That is the fusion loss of Verification 6, and it fails the strict
    "no worse" gate criterion. The gate applies to the final arm, so it stays open.
- **Without summaries** (doc first, `none034a1` against `none031`): orig +0.058*, intent +0.028*,
  file unchanged.
- **Harness:** `arm_gate.py` retrieves at depth 50, so its `store031` means differ slightly from
  `clean_sweep2.py`'s depth-10 means (0.635 / 0.674 / 0.867 / 0.500). The gate table uses the
  harness's own numbers, and both arms are always compared in the same harness.

**2026-09-25, arm 2 (§2 `#` members and function-valued fields; `store034a2`).**
- **What changed:** p-queue gains 22 tier-1 chunks (133 → 155), all `#` members. zustand and click
  have none, so their chunks did not change.
- **Against `store031`:** orig +0.045\*, intent +0.024, file any +0.004, file whole −0.010.
  p-queue original is 0.709.
- **Against arm 1:**
  - Only one original query moved: `pq2-constructor` 1→2. The configuration question now puts
    `options.ts::Options_part_1` first, which is a defensible answer.
  - Intent moved on 4 queries, for a net −0.0005. `pq-intent-07` went 8→11. It asks whether raising
    concurrency launches waiting jobs, and the newly indexed `#processQueue`, which does exactly that,
    now ranks 6th.
  - File queries did not move.
- **Verdict:** arm 2 is retrieval-neutral within noise. Its value is completeness: 22 member bodies
  that were not indexed anywhere, and call edges that keep `#` members out of `find_dead_code`
  (`tests/test_private_members.py`).
- **Gate:** `pq2-constructor` 1→2 fails the strict "no worse" criterion. The gate is judged on the
  final arm.

**2026-09-25, arm 3 (§3 merge and §4 Python decorated stubs).** Four variants were built, each against
arm 2 unless noted.

| Variant | What differs | orig | intent | file whole | Verdict |
|---|---|---|---|---|---|
| 3 | getter first, overloads merged, decorators in the text | −0.004 | −0.005 | +0.018 | rejected: 33 click tests pushed into `_part_N`; intent gate FAIL (4 losers) |
| 3b | 3 with the most code first | same as 3, except `pq-intent-07` back to 11 | | | ordering kept |
| 3c | 3b with decorators out of the text | +0.000 | −0.007 | +0.004 | `click-intent-09` 1→4, caused by the merged overload stubs |
| **3d** | **3c with implemented overload stubs dropped** | **+0.000** | **+0.000** | **+0.004** | **kept** |

- **3d against `store031`:**
  - orig +0.045\* and intent +0.025 (its lower bound sits at −0.0000); file any +0.004 and file whole
    −0.006.
  - p-queue original is 0.709.
  - Intent has 2 queries losing 3 or more ranks: `pq-intent-06` (from arm 1) and `pq-intent-07` (from
    arm 2). That passes.
  - The only remaining gate failure is `pq2-constructor` 1→2, from arm 2.
- **Chunks:**
  - click's tier-1 chunks went from 1,298 to 1,286, because Python skeletons no longer carry decorated
    bodies. Click's class skeletons shrank by 14%, from 111,918 to 96,714 characters.
  - The two `if TYPE_CHECKING` class pairs merged into one symbol each.
  - No member was newly pushed past 500 tokens.
- **Summaries:** click was re-summarized for the first time in this ADR (368 changed chunks). p-queue
  and zustand did not change.

**2026-09-25, arm 4 (§5; `store034a4`, header and line ranges).**
- **Against arm 3d:** orig −0.006, file whole −0.021. `click-choice-type` fell 1→7 and
  `pq-intent-01` 3→14. Intent failed the gate with 3 losers.
- **Decision:** the repeated class header is rejected. The line ranges are kept: they change no chunk
  text. A parse of all three repos gives tier-1 text identical to `store034a3d` (1,611 of 1,611
  chunks), so retrieval is identical as well.
- **Line-map check:** every line of every class skeleton in the three repos (3,765 lines) maps to the
  source line holding that text.

**2026-09-25, final (`store034final`, built from 93bcbaa).** The gate, against `store031`:

| Criterion | Result | |
|---|---|---|
| Named p-queue queries | `pq2-constructor` 1→2 (arm 2: `options.ts::Options_part_1` now ranks first); every other ranked query equal or better | accepted by @edb, 2026-09-25 (see notes) |
| p-queue original set | 0.554 → 0.709 | pass |
| Regressions per set | orig 1 (`pq2-on-error`, exempt) · intent 2 · file 0 / 0 | pass |
| Set means (reported) | orig +0.045\* · intent +0.025 (lower bound −0.0000) · file any +0.004 · file whole −0.006 | reported |
| Skeleton size | `PQueue` 10,105 → 5,407 characters; no skeleton grew except by stub whitespace (+4 to +8 characters on three click classes); click skeletons −14% | pass |
| Token budget | no member newly past 500 tokens | pass |
| Chunk integrity | `index_status`: vectors == chunk rows in every tier; `chunker_version: 3 (== current)` | pass |
| Tests | 378 pass; ADR-008 conformance 1.000/1.000 for Python and TypeScript | pass |
| MCP Inspector | `tools/list --strict` exit 0; search returns `PQueue.#processQueue`, `#tryToStartAnother` and `#next`; `find_dead_code("#processQueue")` is REFERENCED through 3 resolved callers; `verify_candidate_edges` has no candidates; a missing argument returns `isError: true` (exit 5) for `semantic_code_search` and `find_dead_code` | pass |

- **How Inspector was run:** it drove the unmodified server through `run_server.py`, against a
  scratch copy of p-queue carrying the final index. The wrapper only loads the embedder in bf16 on
  the GPU, because `core.py` loads it in fp32 (6.2 GB), which overflows the 8 GB card and pages
  silently.
- **Found along the way:**
  - No tool declares `readOnlyHint`. CLAUDE.md's Inspector checklist asks for it. This is older than
    ADR-034 and is left for its own change.
- **Retrieval with no summaries (`none034a4`):** orig +0.047\*, intent +0.027, file whole −0.006\*.

**2026-09-25, `pq2-constructor` accepted (@edb).**
- Re-run on four indexes: the constructor and `options.ts::Options_part_1` were already ranks 1 and 2
  before this ADR.
- Arm 2's new `PQueue.#createIntervalTimeout` (rank 6) matches the query's "interval" and "timeout"
  words. Apart from it, the list is unchanged from arm 1, so the tie most likely flipped because it
  took a list position above the constructor. The per-list scores were not pulled.
- `Options` is a fair first answer to a question about configuration options, and the constructor
  stays second. Reverting would mean un-indexing the 22 `#` members. The gate now has no open items.

**2026-09-25, held-out check: two TypeScript repos the design was never tuned on (@edb).**
- **Why:** every TypeScript choice in this ADR was measured on p-queue alone.
- **Repos:**
  - `isaacs/node-lru-cache` v11.5.3 (7e71a1f): one 3,219-line class file with 144 JSDoc blocks and
    44 `#` members.
  - `taskforcesh/bullmq` v6.3.9 (10dc93c), `src/` only: 108 files; members are `private`/`protected`,
    not `#`.
- **Queries:** written from source by one agent per repo, blind to every result
  (`gpu-crash-repro/heldout_fixtures/`). 20 short and 20 intent questions per repo, plus 12
  file-level questions for bullmq. Every gold was checked against the built indexes.
- **Builds:**
  - `base` was built from 5c48bca (pre-ADR-034) and `final` from 4805ca1. Both use summaries in their
    own index.
  - `final` seeded its summaries from `base`, so only changed chunks were summarized again: 76 for
    lru-cache and 321 for bullmq.
- **Driver:** `gpu-crash-repro/heldout.py`, output in `telemetry/gate_heldout_034.txt`.
- **No retuning:** the rules were run exactly as committed.

| Set | lru-cache | bullmq | Both |
|---|---|---|---|
| short (20 + 20) | 0.409 → 0.796, +0.387\* | 0.793 → 0.742, −0.051 | +0.168\* |
| intent (20 + 20) | 0.237 → 0.670, +0.432\* | 0.656 → 0.664, +0.008 | +0.220\* |
| short, answer not `#`-only | +0.233\* (n 15) | −0.051 (n 20) | +0.071 (n 35) |
| intent, answer not `#`-only | +0.086 (n 7) | +0.008 (n 20) | +0.028 (n 27) |
| file any / whole (12) | | +0.042 / +0.035 | |
| queries losing ≥3 ranks | 0 | 0 | **0 in every set: gate passes** |

- **lru-cache:**
  - Most of the gain is coverage. 18 answers are `#`-only (`#evict`, `#backgroundFetch` …), and
    they went from unrankable to 0.850 (short) and 0.619 (intent).
  - The doc-move alone, on answers the base could rank, is still +0.233\* on the short set.
- **bullmq is neutral.** Of its 40 symbol queries, 10 moved; the only rank changes were 1↔2 and
  unranked↔ranked.
  - The biggest change: `Worker.concurrency`, a merged accessor pair, went from not found to 2.
  - Four 1→2 flips come from the doc-move working on both sides. The competitor that overtook the
    gold also received its doc: `FlowProducer.toFlowError` (+215 characters), `waitForEvent`,
    `whenCurrentJobsFinished` and `removeOrphanedJobsBatch`. The same effect wins `bmq-intent-15`
    3→1 and `bmq-intent-05` 2→1.
  - One flip (`bmq-sym-03`) involves no changed chunk on either side; it is a fusion shift.
  - Like `pq2-constructor`, these are close ties settled by which neighbour's doc matches better, not
    a loss of the answer.
- **Reading:**
  - The p-queue result generalizes to another `#`-heavy class file, strongly.
  - On a large `private`-keyword codebase, the doc-move is about neutral for symbol questions and
    slightly positive for file questions.
  - No design choice failed on unseen code.
