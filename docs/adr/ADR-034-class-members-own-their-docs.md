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

Only **adjacent** members of one class body with the same name merge: TypeScript `get`/`set` pairs,
Python `@property` with `@x.setter`/`@x.deleter`, and `@overload` sets.
- **Order in the text.** The implementation comes first, then the others in source order. For an
  accessor pair that is the getter; for an overload set it is the undecorated definition. This keeps the
  implementation inside the embedder's 512-token window.
- **Lines.** The merged symbol runs from the first member's first line to the last member's last line.
- **Type row.** `symbol_types` keeps one row per FQN. The implementation's row wins: the getter's return
  type, or the overload implementation's signature.
- **Edges.** The calls of every merged member are unioned.
- **What stays unmerged.** Same-FQN symbols that are not adjacent, such as helpers redeclared in separate
  test callbacks (zustand has 92), are left alone. ADR-031 already keeps one vector for them, and B-027
  gives them distinct names.

### 4. Python skeletons stub decorated methods

`skeletonize` treats a `decorated_definition` that wraps a stub type as that stub type. The decorators
stay in the skeleton and the body becomes ` ...`.

### 5. Split skeleton parts carry the class header and a body line range

This step is the last arm, and it ships only if its measurement passes.
- **The header.** Each `_part_N` of an oversized class skeleton begins with the class declaration line,
  its generics and heritage, without decorators or doc.
- **The line range.** Each part records the body-only line range it covers in `start_line` and
  `end_line`, which split parts leave at 0 today. Stage 2 needs that range to pick the right part.

### Measurement: one arm per change

Every arm is built on the three eval repos (p-queue, zustand, click), on the GPU with summaries
regenerated, and logs its summary-cache hits and misses. Each one is compared with `store031`.

| Arm | Adds |
|---|---|
| 1 | doc-move (§1) |
| 2 | + `#` members and arrow fields (§2) |
| 3 | + merge (§3) and decorated stubs (§4) |
| 4 | + skeleton header and line range (§5) |

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
- [ ] §2 `#` members, call query and arrow fields: fixtures and tests
- [ ] Arm 2 built and measured
- [ ] §3 merge and §4 Python decorated stubs: fixtures and tests
- [ ] Arm 3 built and measured
- [ ] §5 skeleton header and body line range
- [ ] Arm 4 built and measured; the gate table filled in
- [ ] `CHUNKER_VERSION` → 3
- [ ] MCP Inspector run saved under the repo's test output
- [ ] Update B-026 in `docs/backlog.md` on `master`: Stage 1 promoted → ADR-034
- [ ] Resolve **Depended on by**: confirm the body-only line ranges and `class_context` for Stage 2

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
