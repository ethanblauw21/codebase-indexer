# ADR-005: Chunk Provenance Versioning + Self-Healing Quality Loop

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-005-chunk-versioning-self-healing`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — the `LanguageAdapter` Protocol (extended here with a `version` field) and the `stable_id` invariant that provenance versioning must not violate (§2).
- ADR-017 — the tier model whose Tier-B/C output the scorer flags and whose Tier-B→Tier-A promotion this ADR triggers (§6). *(Was mislabeled "ADR-004"; ADR-004 is CI observability, the tier model is ADR-017.)*
**Depended on by:**
- ADR-017 — its tier model consumes this ADR's promotion demand signal and `recheck` migration trigger for Tier-B→Tier-A (mutual; see ADR-017 §9 and this ADR's §6).
- ADR-010 *(planned — docs/adr-backlog.md)* — content-addressed drift detection extends this ADR's `recheck`/self-healing loop and **consumes** the XXH3 change-detection standardization this ADR owns (the 2026-06-18 amendment below); ADR-010's Merkle leaves use the same hash.
- ADR-016 *(deferred stub — docs/adr/ADR-016)* — persists, as a first-class asset, the symbol containment tree this ADR derives on the fly for coherence scoring (§3). Trigger-gated; no obligation on ADR-005 beyond keeping the on-the-fly derivation as the documented graduation source.

## Context

The sibling Rust filesystem indexer (`Rust Indexer/File System`) implements a
"self-healing" pass (its `step-9` design, `src/scorer.rs`): every chunk is
stamped with a version-tagged chunker method (`"xlsx/v1"`, `"markdown/v1"`); a
separate scoring pass flags low-quality chunks via structural heuristics plus
embedding **coherence** (cosine of a chunk vs the centroid of its parent-tier
vectors); and a `recheck` subcommand detects when the file-type→chunker mapping
version has drifted and reindexes only the affected files. Chunks that are
flagged but whose method has *not* changed are surfaced for manual review.

This code-intelligence project has none of that. A chunk records no provenance:
the `chunks` table is `(id, file_id, scope, tier, start_line, end_line, text,
tags)` (`db.py:118`) with no indication of which adapter — or which *version* of
that adapter — produced it. When an adapter improves (a better skeletonizer, a
fixed FQN convention), there is no way to know which already-indexed chunks are
stale, short of a full reindex. And there is no quality signal at all: a
truncated or incoherent chunk is indistinguishable from a clean one.

This matters most in the context of ADR-004. Tier-B (generic `tags.scm`) and
Tier-C (text fallback) chunks are exactly the ones most likely to be low-quality
— and we need a *measured* signal of which Tier-B language most deserves
promotion to a hand-written Tier-A adapter, rather than guessing. The Rust
indexer's self-healing loop is that signal, and porting it is the natural
companion to tiered language support.

The hard constraint: `stable_id()` keys FAISS vector IDs on
`"{tier}::{file_path}::{scope}"` (`stable_id.py:40`), and a change to that
formula orphans every existing index (guarded by `tests/test_stable_id.py`).
Provenance versioning must therefore be **pure SQLite metadata that never enters
the ID** — exactly as the Rust indexer stores `chunker_method` as a column, not
as part of any key.

## Decision

Port the self-healing loop, adapted to this project's adapter architecture and
three-tier index. Four parts: (1) stamp every chunk with its producing
adapter+version; (2) a scorer pass with code-appropriate structural heuristics
plus tier coherence; (3) flag storage; (4) fold `score` and version-drift
`recheck` into the existing indexer/`reindex` flow.

### §1 — Chunk provenance: adapter version stamping (two-model)

Every chunk is stamped with its producing `method()` string in a new
`chunks.chunker_method` column. The *current* method for a file is
`get_adapter(ext).method()`; the adapter REGISTRY (`adapters/__init__.py`) is the
source of truth (this project's equivalent of the Rust indexer's
`chunker_map.toml` — no separate editable TOML). `method()` is added to the
`LanguageAdapter` Protocol (`adapters/base.py`).

How `method()` is derived **differs by tier**, because what drives "output for
unchanged source would change" differs:

```python
class LanguageAdapter(Protocol):
    language_id: str
    extensions:  frozenset[str]
    def method(self) -> str: ...      # NEW — tier-specific derivation below
```

**Tier A — human-bumped + snapshot-guarded.** Hand-written logic has no clean
"behavior hash," so each fitting adapter carries a `version: str` bumped manually
on any output-changing change. `method() -> f"{language_id}/{version}"`, e.g.
`"python/v3"`. The bump is the deliberate drift trigger, guarded by the existing
ADR-003 golden snapshots (a snapshot diff without a version bump fails CI — §Phase 4).

**Tier B — auto-derived, no human bump.** A single shared `GenericTreeSitterAdapter`
serves every Tier-B language, and Tier-B output depends not on adapter code but on
the **grammar version** and the **vendored `tags.scm`** (either can change a
language's output with zero adapter-code change). A single hand-bumped string
cannot express per-language drift on shared code, and there are no goldens to guard
it. So the method string is a **deterministic hash of its real inputs**:

```python
def method(self) -> str:                       # generic adapter, per language
    return f"generic-{self.language_id}/{GENERIC_VERSION}" \
           f"@{self.grammar_version}+{sha(self.tags_scm)[:4]}"
    # e.g. "generic-go/v1@tsgo-0.21.0+a3f9"
```

`grammar_version` and the `tags.scm` source are already pinned/recorded in the
ADR-004 §5 registration row, so this needs no new data. Any change to the grammar
pin or the `tags.scm` content **automatically** changes the method string →
`recheck` detects drift → reindex. **The "someone forgot to bump" failure mode
does not exist for Tier B** — nothing is hand-bumped. It is per-language despite
the shared adapter, because the grammar and `tags.scm` are per-language.

**Tier C** — `"fallback/v1"` (the text chunker; human-bumped like Tier A).

**Floating experimental lane (ADR-004 §6)** uses the same auto-derivation as
Tier B; since the lane already stamps the grammar version used at index time, that
version folds straight into the hash, so experimental languages drift-detect
identically — no special case.

### §2 — Stable-ID invariant (the load-bearing constraint)

`chunker_method` is metadata only. It is **never** an argument to `stable_id()`.
A version bump does not change any FAISS ID; it changes only the SQLite column
that `recheck` reads to decide what to reindex. `tests/test_stable_id.py` stays
green untouched. This is the single most important rule in this ADR — it is what
makes versioning a cheap additive change rather than an index-invalidating one.

### §3 — Scorer pass

A separate pass, never inline with indexing (matching the Rust design). Two
scores per chunk.

**Structural score** — the Rust heuristics, re-weighted for *code* rather than
prose. The Rust checks assume natural-language ("ends with `.`/`?`/`!`"), which
is wrong for code; the code-appropriate checks are:

| Check | What it catches | Weight |
|-------|-----------------|--------|
| Token count in healthy range | < 15 tokens, or ≥ 99% of the tier budget (truncation pressure) | 0.25 |
| Brace/bracket balance `()[]{}` | Chunk split mid-statement (angle brackets excluded — generics/comparisons) | 0.25 |
| Not a mid-symbol split | `scope` does **not** end in `_part_N/M` from `fallback_token_chunker` (`ast_chunker.py:258`) | 0.20 |
| Rich-header present | AST chunks carry the `File:/Entity:/Lines:/Code:` header (`_symbol_rich_text`); its absence signals a raw fallback chunk | 0.15 |
| Repetition ratio | unique/total tokens ≥ 0.3 (catches degenerate/boilerplate blobs) | 0.15 |

**Scope: tier-1 chunks only.** These checks assume a chunk is a *complete
syntactic unit*, which holds only for tier-1 (surgical) AST symbol chunks.
Tier-2/tier-3 are sliding windows **designed to fill their token budget** and cut
at token boundaries (and tier-2 may be **skeletonized** — bodies stripped), so
they are legitimately near-budget, brace-unbalanced, and lack the
`_symbol_rich_text` header; applying these checks to them would false-flag the
majority *by construction*. Tier-2/3 are therefore **exempt** from structural
scoring — their truncation is already surfaced at pack time by
`pack_context_safely`. This matches coherence, which is already tier-1-only. If a
tier-2/3 signal is ever wanted, apply only the tier-agnostic checks (repetition
ratio + whitespace density).

Flag threshold `structural_score < 0.5`. By construction, clean tier-1 AST chunks
score ~1.0; truncated, headerless, or `_part_N` fallback chunks score low — so
**flags concentrate on Tier-C and weak Tier-B output**, which is the promotion
signal (§6).

**Coherence score** — for a tier-1 (surgical) symbol chunk, return the cosine of
the chunk's embedding vs the embedding of its **structural containment parent**.
Two design points were settled under `/grill-plan` and supersede the naive port:

*Vectors come from re-embedding, not FAISS reconstruction.* `reconstruct(stable_id)`
is **not feasible** on this index: the base is `IndexIDMap(IndexFlatIP)`
(`core.py:96-97`) — plain `IndexIDMap`, not `IndexIDMap2`, so it has no reverse
id→vector map — and `_maybe_upgrade_to_ivfpq()` swaps the base to IVFPQ at ≥256
vectors, where reconstruction is **lossy** (PQ-decoded) and would require the
`make_direct_map()` call deliberately removed to fix a prior removal bug. A
quality metric whose accuracy silently degrades with corpus size is disqualifying.
Instead, coherence **re-embeds the authoritative `chunks.text`** (the exact
rich-text the indexer embedded via `_symbol_rich_text`) for the symbol and its
parent. This is exact at any corpus size, coupled to no index type, and needs no
`reconstruct`. Cost is an embedder pass over scored chunks — acceptable because
scoring is an offline batch pass that already loads the embedder, and it is
incremental (unscored chunks only).

*The parent is the structural container, not a sliding window.* Tiers 2/3 are
sliding windows (`stable_id.py:23-27`), so "which window is the parent" is
ambiguous and, if taken as the whole-file mean, produces false flags (a fine
utility function in a thematically-mixed file scores low merely for being
different from the file average). Instead, the parent is the symbol's **enclosing
container**, derived per-file at score time from existing data — `Symbol.class_context`
(`adapters/base.py:35`) and `OWNS` edges plus line ranges. This is a per-file
*symbol containment tree* **derived on the fly here**; persisting it as a
first-class structure is reserved for a future ADR (see Future Work):
- `parent(method)` = its enclosing class's tier-1 chunk → "does this method belong
  to its class," a real misfiling/copy-paste defect signal.
- `parent(top-level symbol)` = the file's tier-3 (architectural) chunk — the file
  node is the containment root when there is no enclosing symbol.

Returns `None` — no flag — for the file-root tier-3 chunk itself, chunks with no
resolvable parent, or when no embedder is loaded (structural-only mode). Flag
threshold `coherence_score < 0.6` — calibrated against the existing index at
implementation time, not ported blind from the Rust document indexer (see Future
Work).

> This is the *only* legitimate use of a centroid in the code project: a chunk
> *health* metric over the structural containment hierarchy — never a router that
> picks an adapter (see ADR-004 Alternatives).

### §4 — Flag storage

Additive columns on `chunks`, applied at startup via `ALTER TABLE ... ADD COLUMN`
guarded by a `PRAGMA table_info` check (the established migration pattern —
`_migrate_*` in `db.py`):

```sql
ALTER TABLE chunks ADD COLUMN chunker_method   TEXT;     -- "python/v3", "generic-go/v1", "fallback/v1"
ALTER TABLE chunks ADD COLUMN structural_score REAL;     -- NULL = unscored
ALTER TABLE chunks ADD COLUMN coherence_score  REAL;     -- NULL = unscored or no parent tier
ALTER TABLE chunks ADD COLUMN is_flagged        INTEGER NOT NULL DEFAULT 0;
```

`chunker_method` is written in the `upsert_file` chunk loop (`db.py:566`); the
INSERT column list and the per-chunk tuple each gain one field. Existing rows
migrate with `chunker_method = NULL` and are treated as "unknown method" by
`recheck` (eligible for rescoring, not for forced reindex until next touched).

New `db.py` helpers, mirroring the Rust API:
`set_chunk_scores(chunk_id, structural, coherence, is_flagged)`,
`get_unscored_chunks()`, `get_chunks_for_rescoring()`,
`get_flagged_summary() -> [(language, method, flagged, total)]`.

### §5 — `score` and `recheck`, folded into existing flows

The Rust indexer exposes these as `clap` subcommands; this project already has
entry points (`code-indexer`, `code-indexer-serve`) and a `reindex` MCP tool, so:

- **`score`** — a post-index pass (and a `code-indexer score [--rescore]` entry
  point). Scores chunks where `structural_score IS NULL` (or all, with
  `--rescore`). Prints `Scored N chunks — F flagged (S structural, C coherence,
  B both)`.
- **`recheck`** — version-drift detection, folded into `reindex`. For each
  `(file, stored_chunker_method)`, compare `stored` to the file's current
  `get_adapter(ext).method()`. The mechanism is precise, not hand-waved:

  *Method drift does not change file content, so MD5 is unchanged and `compute_diff`
  never marks the file `modified` (`incremental_indexer.py:498-512`) — it would be
  silently skipped.* `recheck`'s **only** new job is to **inject method-drifted
  files into `stale_paths` as synthetic "modified" entries** despite an unchanged
  content hash. They then flow through the **existing** purge-then-reindex path
  (`get_stale_ids` → `purge_stale_vectors` → reindex, `incremental_indexer.py:205-272`).

  This is **already scope-change-safe**, which matters because promotion B→A changes
  the FQN scheme (Tier-B `path::name` → Tier-A namespace-qualified), hence the
  `scope`, hence the `stable_id`. `get_stale_ids` reads the **old stored chunk rows
  by file** (`db.get_chunk_metadata_for_files`) and removes *every* chunk the DB
  currently holds for that file — whatever their old scopes — before the new chunks
  are added. So the old Tier-B vectors are purged and cannot orphan; no new purge
  code is needed. (There is no "stable IDs unchanged unless a scope changes" upsert
  shortcut — that framing was wrong; the full per-file purge-by-stored-ID is what
  makes scope changes safe.)

  After reindex the file is rescored. Files that are *flagged but un-drifted* are
  reported as "manual review needed" — the conformance-author queue, not an
  auto-reindex.

  *Optional lightness (content edits only):* within a routine content reindex, a
  `stable_id` set-diff — remove `old_ids − new_ids`, add `new_ids − old_ids`, skip
  the intersection — avoids re-embedding unchanged chunks on small edits to large
  files. Near a no-op for promotions (almost every scope changes), so promotions
  just take the full per-file reindex.
- **Surfacing** — `get_flagged_summary()` is exposed through a lightweight MCP
  read tool (or appended to `reindex` output) so an agent/user sees flagged
  counts per language and method, the analog of the Rust TUI `Flagged` screen.

Thresholds start as module constants (`STRUCTURAL_THRESHOLD = 0.5`,
`COHERENCE_THRESHOLD = 0.6`), matching the Rust defaults; making them
configurable via `indexer.toml` is a follow-up.

### §6 — The promotion-demand signal (why this pairs with ADR-004)

Because structural/coherence flags concentrate on Tier-C and weak Tier-B chunks,
`get_flagged_summary()` grouped by language *is* the prioritized promotion
backlog: *"`go` (generic-go/v1) — 142 flagged / 380 chunks"* means Go has earned
a hand-written Tier-A adapter next. When that adapter ships and bumps the method
string (`generic-go/v1` → `go/v1`), `recheck` detects the drift and auto-reindexes
every Go file onto the fitting chunker. ADR-004 defines the tiers; this ADR
measures which tier-B language to graduate and performs the migration.

## Consequences

**Better:**
- Adapter improvements become *targeted*: bump `version`, `recheck` reindexes
  only affected files instead of a full rebuild.
- A real, measured quality signal over the index — truncation/incoherence is
  detectable, not invisible.
- Supplies ADR-004's missing promotion backlog: which language to make "fitting"
  next is data, not a guess.
- Provenance makes the index auditable: every chunk says what produced it.

**Worse:**
- Four new columns and a scoring pass to maintain; coherence scoring re-embeds
  `chunks.text` and needs an embedder (skipped cleanly in structural-only mode).
  The re-embed pass is exact but costs an embedder run over scored chunks.
- Adapter authors must remember to bump `version` on output-changing changes; a
  forgotten bump means stale chunks linger until the file is next touched. (CI
  check: snapshot-diff in an adapter PR without a version bump fails.)

**Neutral:**
- `chunker_method` is metadata only; the FAISS ID formula and
  `tests/test_stable_id.py` are untouched by design.
- Scoring is a separate pass; indexing latency is unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Put adapter version into `stable_id()` | Orphans every FAISS ID on each bump; defeats incremental indexing; breaks `test_stable_id.py`. Version must be metadata. |
| Editable `chunker_map.toml` (Rust's exact mechanism) | Redundant here — the adapter REGISTRY already maps extension → adapter, and the adapter owns its version. A parallel TOML would be a second source of truth to drift. |
| Score inline during indexing | Couples slow coherence math to the hot indexing path; the Rust design deliberately separates them. Kept separate. |
| LLM-based chunk quality scoring | Cost, latency, nondeterminism. Structural heuristics + embedding coherence need no LLM (matching Rust). |
| Prose sentence-completeness heuristic as-is | Wrong for code; replaced with brace-balance, mid-symbol-split, and rich-header checks. |
| Coherence via FAISS `reconstruct(stable_id)` | Infeasible: base is `IndexIDMap` (not `IDMap2`) so no reverse map; IVFPQ upgrade makes reconstruction lossy and needs the removed `make_direct_map()`. A metric whose accuracy degrades with corpus size is disqualifying. Re-embed `chunks.text` instead — exact, index-type-agnostic. |
| Coherence parent = sliding-window / whole-file mean | Tiers 2/3 are sliding windows; "which window" is ambiguous and the file mean false-flags legitimately-distinct symbols. Parent = structural container (enclosing class; file-root for top-level). |
| Persist a first-class symbol-tree now | Its payoff (structural tier-2, outline-as-tier-3, navigation) is independent of the quality loop and would balloon this ADR. Derive the containment parent on the fly here; persist in a future ADR (Future Work). |
| Adopt `IndexIDMap2` to enable reconstruct | Doesn't help — IVFPQ tiers stay lossy regardless of the map. IDMap2 is a separate, optional improvement for exact id→vector lookup, not a dependency of this ADR. |
| Single human-bumped `version` for all tiers | Works for Tier A; breaks for Tier B — the generic adapter is shared (can't express per-language drift), Tier-B output depends on grammar/`tags.scm` not adapter code (a code guard never fires), and Tier B has no goldens to diff. Two-model split instead: Tier-A human-bumped + snapshot guard; Tier-B method auto-derived from grammar+`tags.scm` hash. |

## Testing Additions

| Area | Type | Notes |
|------|------|-------|
| `chunks` schema migration | Unit | Four `ADD COLUMN`s idempotent via `PRAGMA table_info`; existing rows migrate with `chunker_method=NULL` |
| `chunker_method` write | Unit | `upsert_file` stamps each chunk with `adapter.method()`; round-trips |
| Structural scorer | Unit | Clean AST chunk ~1.0; truncated/`_part_N`/headerless chunk < 0.5; port the Rust scorer unit cases, re-weighted |
| Structural scope | Unit | Scorer runs on **tier-1 only**; tier-2/3 (near-budget, brace-unbalanced, skeletonized) are not scored and never flagged |
| Coherence scorer | Unit | Re-embed `chunks.text`; cosine vs structural-containment parent (enclosing class; file tier-3 for top-level); `None` for file-root / no-parent / no-embedder; reuse Rust cosine cases |
| Containment-parent derivation | Unit | `parent(method)`=enclosing class chunk via `class_context`/OWNS; `parent(top-level)`=file tier-3 chunk; handles missing class_context |
| Coherence exactness vs corpus size | Unit | Re-embedded vector reproduces indexed vector; score identical whether tier is FlatIP or IVFPQ (no reconstruct path) |
| `stable_id` invariant | Unit — merge blocker | Versioning touches no ID; `test_stable_id.py` golden fixtures unchanged |
| `recheck` drift | Integration | Bump an adapter `version` → only that language's files reindex; un-drifted flagged files reported as manual-review, not reindexed |
| Synthetic-stale injection | Unit | Method drift with **unchanged content hash** still enters `stale_paths`; a file with neither content nor method drift is not reindexed |
| Promotion orphan-prevention | Integration | B→A promotion (FQN scheme change) leaves **zero** old Tier-B vectors/rows after reindex; search returns no pre-promotion ghost chunks |
| Flagged summary | Unit | `get_flagged_summary()` groups by language/method with correct flagged/total counts |
| Structural-only mode | Unit | Scorer runs structural-only and flags correctly when no embedder is loaded |

## Future Work

> Decisions deliberately deferred out of this ADR to keep the quality loop focused.
> Each is a candidate for its own ADR when a second consumer justifies the cost.

- **ADR-016 (planned) — Persisted file symbol containment tree.** This ADR derives
  the containment parent on the fly for coherence scoring. Persisting it as a
  first-class structure (`parent_symbol_id` or a closure table, materialized at
  index time) unlocks three things independent of the quality loop: (1) **structural
  tier-2** — define the "component" tier as a class + its methods instead of a blind
  1500-token sliding window; (2) **outline-as-tier-3** — serialize the tree as the
  architectural summary; (3) **graph navigation** — walk containment in the RTR
  pipeline. Trigger to promote: the first feature beyond coherence that needs the
  tree. Same promotion logic as Tier-B languages — build the derived version now,
  graduate to a persisted asset when a second consumer appears.
- **Threshold configurability.** Start `STRUCTURAL_THRESHOLD`/`COHERENCE_THRESHOLD`
  as calibrated constants; move them to `indexer.toml` once the calibrated values
  are stable.
- **`IndexIDMap2` / drop IVFPQ.** Independent of this ADR: adopting `IndexIDMap2`
  enables exact id→vector lookup on flat tiers (useful for `find_similar_code` /
  dedup), and dropping the premature IVFPQ upgrade (the corpus is small enough for
  exact `IndexFlatIP`) would remove the lossy-reconstruct class entirely. Neither is
  needed here because coherence re-embeds, but both are noted as "improve as we go."
- **Amend (from research) — standardize the change-detection hash on XXH3.** The
  current code splits hashes: `incremental_indexer.scan_disk` uses **MD5**
  (`md5_file`, line ~160) while `db.hash_content` uses **SHA-256** (line ~419). This
  ADR's `recheck` rides the same `compute_diff` path, so when implementing it,
  standardize both onto **XXH3** (~30 GB/s, collision resistance is not a security
  requirement here — same choice the competitor and the merkle-drift research make).
  This is the natural companion to **ADR-010** (content-addressed drift), which is
  the *content-drift* axis to this ADR's *method-drift* axis — the two compose and
  share the hash. (Backlog: amend-ADR-005.)
- **Embedder-swap interaction with ADR-009.** Coherence **re-embeds `chunks.text`
  with the current embedder**, so it is model-agnostic by design — an argument *for*
  re-embedding over persisted vectors. When ADR-009 swaps the embedder
  (jina-v2 → jina-code-1.5b/Qwen3), that is already a one-time full reindex (vectors
  change; `stable_id` is model-independent and untouched), and coherence scores must
  be **recomputed in the same pass** (`score --rescore`). Note in the ADR-009 cutover
  checklist so coherence isn't left comparing vectors across two embedder generations.

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 1 — Provenance**
- [ ] Add `version` + `method()` to `LanguageAdapter` Protocol; set on all five Tier-A adapters (start `vN` at current behavior)
- [ ] `chunks.chunker_method` column + migration; write in `upsert_file` chunk loop

**Phase 2 — Scorer**
- [ ] `src/scorer.py`: code-adapted `structural_score`; `coherence_score` by **re-embedding `chunks.text`** (no FAISS reconstruct) vs the **structural-containment parent** (enclosing class chunk; file tier-3 for top-level), derived per-file from `class_context`/OWNS
- [ ] `structural_score`/`coherence_score`/`is_flagged` columns + migration; `set_chunk_scores`, `get_unscored_chunks`, `get_chunks_for_rescoring`
- [ ] Calibrate `STRUCTURAL_THRESHOLD`/`COHERENCE_THRESHOLD` against the existing index (flag-rate sanity check) before locking the constants — do not port Rust defaults blind
- [ ] `code-indexer score [--rescore]` entry point + post-index hook

**Phase 3 — Recheck + surfacing**
- [ ] Fold version-drift `recheck` into `reindex`: inject method-drifted files into `stale_paths` as synthetic "modified" (content hash unchanged) so they ride the existing `get_stale_ids → purge_stale_vectors → reindex` path; flagged-but-un-drifted → manual-review report
- [ ] Verify promotion orphan-prevention: assert no old-scope chunks survive a B→A reindex (the purge is by stored old IDs, not recomputed scopes)
- [ ] Emit informative `recheck` report (files reindexed, `method-from → method-to`, chunks purged/added, manual-review list)
- [ ] `get_flagged_summary()` + MCP read tool (or `reindex` output) for per-language flagged counts

**Phase 4 — Guardrails**
- [ ] Tier-A: CI fails when an adapter PR shows a golden-snapshot diff without a `version` bump (human-bump guard)
- [ ] Tier-B: `method()` auto-derived from `generic_version` + `grammar_version` + `sha(tags.scm)`; CI asserts a grammar-pin or `tags.scm` change produces a changed method string (no human bump, no goldens required)
- [ ] Floating experimental lane reuses the same auto-derivation (grammar version already stamped per ADR-004 §6)
- [ ] Confirm ADR-004 promotion path: `generic-go/v1@…` → `go/v1` triggers `recheck` reindex of Go files

**Notes:**
<!-- 2026-06-18: Ported from Rust Indexer step-9 self-healing (src/scorer.rs). Centroid here is a chunk-health metric over the structural containment hierarchy, NOT an adapter router — that distinction is the whole reason this is safe in the code project. Stable-ID invariant (§2) is non-negotiable. -->
<!-- 2026-06-18 (grill): Coherence-via-FAISS-reconstruct REJECTED — index is IndexIDMap(IndexFlatIP), no reverse map; IVFPQ upgrade makes reconstruct lossy + needs the removed make_direct_map(). Coherence now re-embeds chunks.text (exact, index-type-agnostic). Parent = structural container (enclosing class / file-root), not sliding window. Persisted symbol tree deferred to ADR-006. Thresholds to be calibrated, not ported blind. OPEN grill points remaining: version-bump CI discipline for Tier-B/generic adapters; recheck reindex churn + stale-chunk purge when promotion changes scope (FQN scheme change orphans old chunks). -->

---

### AMENDMENT: 2026-06-18 — Standardize change-detection hashing on XXH3

**Context.** Change detection currently uses **two different hashes**: `incremental_indexer.md5_file` (MD5
over file bytes → `files.content_hash`) and `db.hash_content` (SHA-256). The split is historical, not
principled — neither is a security hash, both only answer "did this content change?" Two algorithms for one
job is needless cost and a latent inconsistency.

**Decision.** Standardize **all change-detection hashing on XXH3** (`xxhash`) — a fast non-cryptographic hash
purpose-built for change detection — replacing both `md5_file` and `hash_content`. This is the
hashing-standardization amendment that **ADR-010 consumes** (its Merkle leaves are XXH3, ADR-010 §7): ADR-005
**owns** the migration, ADR-010 builds on it (Rule A — a hashing change inside *this* ADR's change-detection
boundary is an amendment here, not a fold-in there).

**Mantra-4 carve-out (binding).** "Change-detection hashing" is exactly two call sites: `md5_file`
(`src/incremental_indexer.py`) and `hash_content` (`src/db.py`). It does **NOT** include:
- **`src/stable_id.py`'s MD5** — the FAISS **ID formula** (`md5("tier::file::scope")[:15]`), not
  change-detection; altering it orphans every index (the §2 `stable_id` invariant, Mantra 4). **Off-limits.**
- the **`chunk_summaries` MD5 cache key** — swapping it silently invalidates the LLM-summary cache; a
  separate, deliberate one-time-invalidation decision, not part of this sweep.

(Cryptographic hashing, if ever needed for integrity rather than change-detection, stays a separate concern.)

**Consequences.** *Better:* one fast hash replaces the MD5/SHA-256 split — faster disk scanning, no dual-hash
inconsistency, a single source ADR-010's Merkle layer aligns to. *Worse:* a new `xxhash` dependency, and a
one-time rehash of `files.content_hash` on the first run after the swap — note this forces re-evaluation but
**not a reindex**: `stable_id` is untouched, so no vectors are orphaned. *Neutral:* by the carve-out, stable
IDs and the summary cache are deliberately excluded.
