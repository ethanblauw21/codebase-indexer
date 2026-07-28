# ADR-026: Configuration Authority — `indexer.toml` Is the Source of Truth for Operator Knobs

**Status:** proposed
**Date:** 2026-07-27
**Branch:** `feature/adr-026-configuration-authority`
**Reviewer:** @edb
**Backlog:** [B-001](../backlog.md#b-001) (scan gate) + [B-002](../backlog.md#b-002) (summarizer) — combined here because they are one question asked twice.
**Depends on:** none. `src/config.py` (`load_indexer_config()`) and the cached-read pattern in `core.py` (`_emb_cfg()`, ADR-009 §P1) already exist; this ADR extends both rather than inventing anything.
**Depended on by:** none yet.
**Reviewed:** five-persona jury review, 2026-07-27 — see `ADR-026_REVIEW.md`. This document is the post-review revision; §Revision history records what changed and why.

## Definitions

Used precisely throughout. The first draft of this ADR used two of them loosely, which is
how it shipped a contradiction.

- **Operator-facing knob** — a value a user may need to change to index *their* repo
  correctly, without editing Python. Scan exclusions and summarization qualify. Internal
  thresholds, tier token budgets and the stable-id formula do not.
- **Authoritative** — for a given knob, exactly one resolution path exists, and every
  consumer reads it through that path. Not "config wins over code"; "there is one answer".
- **Inert** — documented in `indexer.toml` but read by nothing. The defect this ADR exists
  to remove.
- **Default** — the value used when the config file is absent, or present and does not
  mention the key. Defaults live in code, in exactly one place per knob.

## Context

Knobs an operator must be able to change are **module constants that no configuration can
reach**, while their neighbours are all config-driven. Each has produced a real defect.

**1. The scan gate ignores the wrong things.** `IGNORE_DIRS`
(`src/incremental_indexer.py:94-99`) was seeded from a JavaScript project. It excludes
`node_modules`, `.next`, `dist` and `build` thoroughly, and the Python ecosystem not at all
— `venv`, `.venv`, `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache` and `.tox`
are all absent. It also still carries `"indexer"`, `"public"` and `"mocks"`, which are
meaningless here and actively wrong elsewhere.

This is not hypothetical. A CPU reindex of *this repository* on 2026-07-27 immediately began
embedding `benchmarks/real_repo/corpus/click/…` — the cloned eval corpora. **503 of the 601
indexable files in this tree live under `benchmarks/`**, so an unmodified run produces an
index that is ~84 % third-party corpus. Any user with an in-tree virtualenv gets
`site-packages` chunked, embedded and graphed as if it were their own code, at unbounded
cost.

**2. `[summarization].enabled` is inert.** The real gate is `ENABLE_SUMMARIZATION`, a module
constant at `src/incremental_indexer.py:92`. Setting `enabled = false` in `indexer.toml` has
no effect; the only way to turn summarization off is to edit source. On CPU that is the
difference between an index that completes and one that does not.

**3. The summarizer model id is inert.** Both `ChunkSummarizer` (`:168-170`) and
`IsolatedChunkSummarizer` (`:292-294`) default `model_id` to
`"Qwen/Qwen2.5-Coder-1.5B-Instruct"`, and `incremental_indexer.py:614` constructs
`IsolatedChunkSummarizer()` with **no arguments** — so `[summarization].model_id` is never
read by anything.

**4. An `[ignore]` block already exists, and it is inert.** The shipped `indexer.toml`
carries `[ignore].dirs`, `[ignore].root_dirs` and `[ignore].extensions`. Nothing in `src/`
reads any of them. `dirs` and `root_dirs` duplicate the constants exactly; `extensions`
lists **5** extensions where `INDEXABLE_EXTS` has **11** — the C#/C++ entries added for
ADR-003 / ADR-017 Tier-A support are missing, so the shipped config is not merely dead but
*wrong*, and would silently kill C#/C++ indexing the day someone wired it naively.

*(The first draft of this ADR missed this block entirely and proposed a new `[scan]` block
beside it. That would have left the shipped config file with two spellings of one knob, one
honored and one inert — the exact defect this ADR exists to remove. Reusing `[ignore]` is
the correction.)*

**The shared root cause, and why one ADR.** In every case a documented, operator-facing knob
exists in `indexer.toml` and the code reads a constant instead. Configuration that does
nothing is worse than no configuration: it invites a change, accepts it, and ignores it.
Answering "constant or config?" separately for the scan gate and the summarizer is how a
third variant appears later. It is one contract, decided once.

`src/config.py`'s own docstring is also stale — it says the embedder "still hardcodes" its
model id, which ADR-009 §P1 fixed.

## Decision

**`indexer.toml` is authoritative for every operator-facing knob. Module constants remain,
but only as the default that applies when config is absent or does not mention the key. No
knob documented in `indexer.toml` may be inert.**

### 1. The existing `[ignore]` block owns the scan gate — no new block

```toml
[ignore]
# Extend the built-in defaults (recommended). Names match at any depth, case-insensitively.
extra_dirs = ["fixtures-vendored"]
# Extend the root-only exclusions.
extra_root_dirs = ["benchmarks", "gpu-crash-repro", "graphify-out"]
# Extend the indexable extension set.
extra_extensions = [".rs"]

# Replace a default set outright. Omit unless you mean it.
# dirs = [...]
# root_dirs = [...]
# extensions = [...]
```

`extra_*` extends; the bare key replaces. Extension is the common case and the safe one — a
user adding one directory should not inherit an empty default set and start indexing
`node_modules`.

**Precedence table. All four cells are defined, and one of them is an error:**

| bare key | `extra_*` key | Result |
|---|---|---|
| absent | absent | defaults |
| absent | present | defaults ∪ extra |
| present | absent | the bare key's value, defaults discarded |
| present | present | **`ValueError` at load, naming both keys** |

An explicitly empty list is honored as written and is **not** the same as an absent key:
`extra_dirs = []` is a no-op, and `dirs = []` means "index everything, exclude nothing" —
legal, destructive, and exactly what someone asking for it asked for. `.git` and the index
directory itself are excluded unconditionally regardless of config; they are not knobs.

Setting both keys is an error rather than a silent precedence because there is no reading of
"extend *and* replace" that a user could have meant, and guessing is how the original defect
was born.

Types are validated at load: each key must be a list of strings. `dirs = "foo"` raises
rather than silently iterating characters.

**`extensions` gets a real disposition.** The shipped 5-entry list is corrected to all 11 of
`INDEXABLE_EXTS` in the same commit that makes it live. A key that is documented, wrong, and
about to become load-bearing is the most dangerous of the three states.

### 2. Exactly one export, and the raw constants are privatized

The gate has **two** consumers that must never disagree:

- `scan_disk()` — walks the tree, sees directory entries.
- `MCPServer._is_relevant()` (`:1893`) — receives a bare path string on every filesystem
  event, and never touches the filesystem. Its docstring already claims to "mirror" the
  scan logic; the mirroring is by hand.

A shared *config accessor* is not sufficient — it synchronizes values, not behaviour. So:

- A leaf module (`src/scan_policy.py`, importing only `os`, `tomllib` and `config`) exports
  `scan_policy()` and `is_indexable(rel_path: str) -> bool`.
- Both consumers call it. `MCPServer._is_relevant` becomes a call, not a re-implementation.
- `IGNORE_DIRS` / `IGNORE_ROOT_DIRS` / `INDEXABLE_EXTS` are renamed with a leading
  underscore. **The old import must raise, not silently resolve to an unconfigured value.**

Leaf placement is not cosmetic: a policy helper living in `incremental_indexer` and imported
by `config` deadlocks at import, and would surface only through the server's function-local
import path.

The resolved policy is cached like `core.py::_emb_cfg()`, with two additions that pattern
lacks: a `reset()` used by tests, and **one log line at startup** naming the config file
path, the effective sets, and the resulting file count. A cached config that requires a
restart is acceptable for this tool; one that fails invisibly is not.

### 3. Virtualenvs are excluded by name — `pyvenv.cfg` detection is rejected

`venv`, `.venv`, `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache`,
`.ruff_cache`, `.tox`, `.eggs` and `htmlcov` are added to the name-based defaults. Bare
`env` is **not** added — `src/env/` is ordinary source in too many projects.

*The first draft chose content-based detection — "a directory is a virtualenv iff it
contains `pyvenv.cfg`" — on the grounds that it is exact. It is not, and it costs more than
it buys:*

- **It cannot be evaluated by the second consumer.** `_is_relevant` has a path string and no
  filesystem access. A content check would be honored by `scan_disk()` and ignored by the
  watchdog — the ADR-020 split-brain, reintroduced by the very ADR that cites it.
- **It is not exact anyway.** `conda` does not write `pyvenv.cfg` (its own metadata, not
  PEP 405), and `virtualenv` < 20.0 — including anything pipenv shelled out to
  (`pypa/pipenv#3303`) — wrote a modified `site.py` instead. Modern `virtualenv` 20.x+ and
  `uv venv` do write it.

Name matching is inexact in the other direction, and that is the trade accepted here: a venv
named `.venv-3.12` needs one line of `extra_dirs`. That is a config edit, which is precisely
what this ADR makes possible. Both consumers behave identically, which is worth more than
exactness on a narrow slice.

Name comparison is **case-folded on both sides**. It is case-sensitive today, so `Public`
and `Node_Modules` slip through on Windows and macOS.

### 4. The stale JavaScript entries are removed

`"indexer"`, `"public"` and `"mocks"` come out of the defaults. `"indexer"` would skip a
directory named after this project; `"public"` is a real source directory in many web
projects, so today's list silently under-indexes them.

### 5. A bulk deletion is confirmed, not performed silently

`DiffResult.deleted` drives an irreversible purge (see Consequences). When a single run's
`len(diff.deleted)` exceeds `max(50, 20 % of indexed files)`, the run prints the top-level
directories responsible and requires `--prune` or an interactive `y/N`. The watchdog path
logs and skips the bulk purge rather than prompting a process nobody is watching.

One threshold, one prompt, one log line. No backup subsystem and no undo log — this is a
local tool whose index is rebuildable by definition.

### 6. Scan config is anchored to its own directory

`load_indexer_config()` walks *up* from cwd. `MCPServer.py:1948` sets
`repo_path = os.getcwd()`, and this repo's own `.gitignore` notes the server is sometimes
launched from `src/`. Launched that way, config resolves at the real root while the scan
root is `src/`, so `extra_root_dirs = ["benchmarks"]` silently resolves against the wrong
tree and does nothing.

Therefore: **the scan root is the directory containing `indexer.toml`**, and a mismatch
between that directory and the requested scan root is refused with an explicit message. The
upward walk also stops at a `.git` boundary — today a stray ancestor `indexer.toml` only
misconfigures reranker knobs; under this ADR it would decide what gets *deleted* from every
index beneath it.

### 7. `[summarization]` is actually read

`enabled` and `model_id` are read from config and threaded to **both** summarizer classes.
`ENABLE_SUMMARIZATION` becomes the default behind `enabled`, not the gate.

### 8. Defaults live in one place per knob, and a test enforces it

Each knob has exactly one default, defined next to its accessor, documented in
`indexer.toml`, and reachable by config.

A principle is not a mechanism. So: **a drift test loads the shipped `indexer.toml`, asserts
every documented key is reachable through some accessor, and asserts each code default
equals the shipped value.** It must **fail on first write** — `core.py:29` defaults to
`jinaai/jina-embeddings-v2-base-code`/768 while the shipped config says `BAAI/bge-code-v1`/1536,
and `src/CLAUDE.md` repeats the stale id. That drift is live today, in the file this ADR
originally held up as its model, and nothing detected it.

## Consequences

**Better.** The scan gate becomes correct for Python repos and adjustable for anyone else's
layout. `[summarization].enabled = false` becomes the supported way to make CPU indexing
viable, which matters directly now that the local GPU is unavailable. Config stops lying,
and the drift test keeps it honest.

**Migration is free, and that is exactly why it needs a guard.** `scan_disk()` returns the
disk view, and `DiffResult.deleted` is "present in SQLite, absent from disk" (`:184-188`).
Files that become ignored fall into `deleted` and are purged on the next ordinary
incremental run: `run_incremental_index()` (`:653-661`) → `purge_stale_vectors()` →
`db.delete_file()` (`db.py:809-835`), cascading via `ON DELETE CASCADE` to `symbols`,
`chunks`, `symbol_locations` and `symbol_references`, with `edges` and `symbol_types`
deleted explicitly by FQN. Every tier is `faiss.IndexIDMap(faiss.IndexFlatIP(dim))`
(`core.py:139-166`), so `remove_ids` **physically compacts** the survivors rather than
leaving tombstones, and `save_all()` (`:745`) rewrites each file.

So an existing bloated index cleans itself up with no reindex — and one unattended run can
irreversibly drop ~84 % of an index. Both halves of that sentence are consequences of the
same verified mechanism. Hence §5.

**A behaviour change that fires on `git pull` alone, in both directions.** A user who only
updates gets: venv and cache directories mass-*deleted* from their index, and `public/` and
`mocks/` trees mass-*added* to a CPU embedding run. The second is the same unattended-runaway
shape as the incident that motivated this ADR. The release note must cover both directions,
and the default-set edits ship as their own revertible commit.

**Cost.** One case-folded name comparison per directory in the walk — strictly cheaper than
the rejected `pyvenv.cfg` stat, and immaterial against reading every file for MD5.

**Turning summarization off now dirties the working tree.** `indexer.toml` is git-tracked, so
the author's routine "disable for a fast local run" shows up in `git status`. Accepted: the
knob is aimed at third parties, whose `indexer.toml` is their own file. If it becomes
annoying, a CLI flag is a separate, small change.

**A gate this important is currently untested.** No test in `tests/` references `scan_disk`,
`IGNORE_DIRS` or `IGNORE_ROOT_DIRS`. That absence is how a JS-ecosystem list survived on a
Python tool for this long, so tests are part of this ADR, not a follow-up.

**Known limits, stated rather than discovered later.** `os.walk` never tests the top
directory, so a repo that *is* a virtualenv is not pruned. Conda environments and
pre-2020 `virtualenv` output are not excluded by default and need one `extra_dirs` line.
Windows directory junctions are untested (symlinks are safe — `os.walk` defaults to
`followlinks=False`).

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Just add the missing names to the constants (~10 lines) | Fixes today's symptom and leaves the cause. It would have fixed the `benchmarks/` incident just as durably — but `[summarization]` stays inert, the `[ignore]` block stays inert, and the next operator-facing knob becomes the fourth. |
| A new `[scan]` block (the first draft of this ADR) | `indexer.toml` already has `[ignore]`. Two blocks for one concept, one live and one dead, is this ADR's own defect relocated from code to config. |
| `pyvenv.cfg` content detection | Cannot be evaluated by the path-only watchdog consumer, so it reintroduces the ADR-020 split-brain. And it is not exact anyway — misses conda and pre-2020 `virtualenv`. See §3. |
| Config replaces defaults entirely (no `extra_*`) | A user adding one exclusion silently loses `node_modules`. The failure is invisible and expensive. |
| Bare key + `extra_*` with a silent precedence when both are set | There is no reading of "extend *and* replace" the user could have meant. Guessing is how the original defect was born. It raises. |
| `.indexerignore` in gitignore *syntax* | Genuinely attractive — it dissolves the extend/replace question and, being path-string-only, would give the watchdog real parity. Deferred, not dismissed: it is a deny-list, so the allow-list (`extensions`) still needs a home and the duplicate-block defect survives; pre-populated defaults only work inside *this* clone, so `node_modules` stays a code default and extend-vs-replace returns; and "gitignore syntax" implemented as hand-rolled globbing silently mis-handles `!negation`, `/anchored` and `**/`, which means taking a `pathspec` dependency this repo does not have. Revisit as its own ADR if `[ignore]` proves insufficient. |
| Read the user's `.gitignore` | Attractive, and wrong: plenty of indexable source is git-ignored (generated clients, vendored code), and plenty of git-tracked content should not be embedded. Different questions. |
| Separate ADRs for scan and summarizer | They are one question — "constant or config?" — and answering it twice is how a third variant appears. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions
> made in the moment.

**Estimate: 2–3 working sessions.** The first draft said "tests are the bulk of the work",
which undersold it: this is a config schema, a new leaf module, two rewired consumers, a
deletion guard, an anchoring fix, and a live end-to-end verification run.

**Commit 1 — tests and the inert half (no behaviour change)** ✅ done 2026-07-28
- [x] Baseline tests for `scan_disk()` as it exists today, before touching it — `tests/test_scan_disk.py`, 13 cases. Four pin behaviour commit 3 will *invert* (venv indexed, `__pycache__` indexed, `public`/`mocks` skipped, case-sensitive matching), each marked `# ADR-026 commit 3 will invert this` so the flip shows up in a diff instead of happening silently.
- [x] Drift test — `tests/test_config_drift.py`. **Verified it fails on the old defaults before fixing them:** reverting `core.py` to jina/768 produced 2 failures (`test_code_defaults_equal_shipped_values`, `test_embedder_dimension_default_matches_the_model`), then 0 after. The gate condition asked for exactly this evidence.
- [x] Fix `core.py`'s stale embedder defaults (`bge-code-v1` / 1536) and the `src/CLAUDE.md` references
- [x] `[summarization].enabled` gates indexing
- [x] `[summarization].model_id` threaded to **both** summarizer classes
- [x] Fix the stale `src/config.py` docstring
- [x] Suite green: **265 passed** (244 before, +21 new)

**Deviations from the Decision text, decided during commit 1:**

1. **`ENABLE_SUMMARIZATION` was removed, not "demoted to the default".** §7 said the
   constant becomes the default behind `enabled`. That cannot work: the default must sit
   beside its accessor (§8), the accessor has to live in a leaf module so
   `incremental_indexer` can ask "is summarization on?" without importing `summarizer`
   (and therefore torch), and `config.py` importing `incremental_indexer` for the constant
   would be the import cycle §2 warns about. So `DEFAULT_SUMMARIZATION_ENABLED` lives in
   `config.py` and the module constant is gone. **This breaks anything that monkey-patched
   `ii.ENABLE_SUMMARIZATION`** — including `scratchpad/run_cpu_index.py`, which no longer
   needs to: `[summarization].enabled = false` now works.
2. **Two more inert keys found:** `[indexer].repo_root` and `[indexer].index_dir` are
   documented and read by nothing (`REPO_PATH = os.getcwd()`, `INDEX_DIR = ".code-index"`).
   That makes five inert keys, not three. They are recorded in the drift test's
   `KNOWN_INERT` map with reasons; `repo_root` is what §6's anchoring work will resolve.
3. **`query_instruct` was added to the wired set.** Its default was `""` (the jina
   behaviour) while the shipped config carries the bge instruction — the same drift as
   `model_id`, one line below it, and it would have survived the fix otherwise.
4. **`tools/coir_eval.py` has a third defaults table** (`load_config`, `:81-104`) still
   naming `jinaai/jina-embeddings-v2-base-code` and `jinaai/jina-reranker-v2-base-code` —
   the latter being the model id `indexer.toml` records as *non-existent*. Out of scope
   for commit 1 (it is `tools/`, not `src/`) and **not** yet covered by the drift test.
   Flagged here rather than fixed silently.
5. **A test assumption of mine was wrong, in a way worth recording.** The case-sensitivity
   test first used `Node_Modules/`; on Windows the fixture's existing `node_modules/`
   absorbed it, so the file landed in the correctly-ignored path and the test passed for
   the wrong reason. A case-variant only slips the gate when the canonically-cased
   directory does *not* already exist. The test now uses `Dist/` and says why.

**Commit 2 — plumbing (no default changes)** ✅ done 2026-07-28
- [x] `src/scan_policy.py` leaf module: `scan_policy()` + `is_indexable(rel_path)`, cached per root, with `reset()` for tests
- [x] `[ignore]` block reads `dirs` / `root_dirs` / `extensions` and `extra_*` variants; precedence table enforced; both-keys-set raises `ValueError` naming both; list-of-strings type validation
- [x] The shipped `[ignore]` block given a disposition — **removed rather than corrected**, see deviation 1
- [x] `scan_disk()` and `MCPServer._is_relevant` both call the shared export; `_is_relevant` stops re-implementing the logic
- [x] `IGNORE_DIRS` / `IGNORE_ROOT_DIRS` / `INDEXABLE_EXTS` **deleted** from `incremental_indexer` so the old import raises (deviation 4)
- [x] Scan root anchored to the directory containing `indexer.toml`; mismatch refused; upward walk stops at a `.git` boundary
- [x] Startup log line: config path, effective sets, resulting file count
- [x] Tests: `tests/test_scan_policy.py` (24 cases) + 3 new in `tests/test_scan_disk.py` + 1 in `tests/test_config_drift.py`
- [x] Suite green: **290 passed** (265 before, +25 new)

**Verified on this repo, before any default changed:** the scan drops from **613 files
to 102** — `src` 35, `tests` 54, `tools` 13, and nothing else. `benchmarks/` (510 files
of cloned CoIR corpus) and `gpu-crash-repro/` are gone. That is the whole of the
motivating incident fixed by *configuration*, with the default exclusion set still
byte-identical to the one that shipped on 2026-07-27 — which is the point of splitting
the commits.

**Deviations from the Decision text, decided during commit 2:**

1. **The shipped `[ignore].dirs` / `.root_dirs` / `.extensions` were removed, not
   corrected.** §1 said to fix `extensions` from 5 entries to all 11 "in the same commit
   that makes it live". Making it live is what exposed the deeper problem: a **bare key
   replaces the defaults**, so any values kept in the shipped file freeze this repo's gate
   at the day they were written. A future Tier-A language added to
   `DEFAULT_INDEXABLE_EXTS` would be silently ignored *in this repository* — the same
   class of defect as the 5-vs-11 rot, reintroduced one layer up. The shipped file now
   sets only `extra_root_dirs` (this repo's own generated trees) and documents the
   defaults in a comment marked do-not-paste. A wrong list is now impossible rather than
   merely corrected.
2. **A third export, `is_scannable()`, was necessary — and it fixed a live bug.**
   `scan_disk` admits `.csproj`/`.sln`/`compile_commands.json` as edges-only descriptors;
   `_is_relevant` tested `INDEXABLE_EXTS` alone. So **editing a `.csproj` never triggered
   a reindex even though the indexer reads it** — precisely the drift §2 predicts from a
   hand-mirrored rule. `is_indexable()` (chunked + embedded) and `is_scannable()` (that,
   plus descriptors) are now separate predicates and the watchdog uses the latter.
   `tests/test_scan_disk.py::test_the_two_consumers_agree_on_every_scanned_path` asserts
   the two agree in both directions over a real tree, rather than trusting either.
3. **§6's mismatch is refused, which supersedes a checklist line.** The commit-2 checklist
   asked that "a scan launched from `src/` still excludes `benchmarks/`". Under §6 the
   scan root *is* the config's directory, so that scan is refused with an explicit message
   instead — you cannot resolve root-only exclusions against a subtree you did not write
   them for. The test asserts the refusal.
4. **The constants were deleted, not underscore-renamed.** A leading-underscore alias is
   still a second definition of the knob, and `scan_policy` is the first. Both spellings
   raise `AttributeError`/`ImportError` now; `tests/test_cs_cpp_indexing.py` was repointed
   at `scan_policy.DEFAULT_INDEXABLE_EXTS`.
5. **`.code-index` is a literal in `ALWAYS_IGNORED_DIRS`, not a read of
   `[indexer].index_dir`.** That key is still inert (`INDEX_DIR` is a module constant), and
   reading it *only* for exclusion would leave one knob with two resolution paths — the
   ADR-020 split-brain, in miniature. It stays in `KNOWN_INERT` with that reason.
6. **Extensions must carry a leading dot, validated at load.** Not in the Decision text.
   `extra_extensions = ["rs"]` parses fine, is type-correct, and matches zero files; the
   ADR's own §1 example (`[".rs"]`) is the only thing that says otherwise. It raises.
7. **The drift test grew a `WIRED_NO_DEFAULT` category.** `extra_*` keys are wired but have
   no single code default to compare against — the shipped value is this repo's layout,
   not something another repo should inherit. Left uncategorized they would have read as
   unaccounted-for. A companion test asserts every key parked there is genuinely reachable,
   so the category cannot become a place to hide dead knobs. `KNOWN_INERT` ratchets 5 → 2.

**Commit 3 — default changes and the guard (revertible without losing 1 or 2)**
- [ ] Add `venv`, `.venv`, `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.eggs`, `htmlcov` to defaults; bare `env` deliberately excluded
- [ ] Remove `"indexer"`, `"public"`, `"mocks"` from defaults
- [ ] Case-folded name matching on both sides; single in-place `dirs[:] = [...]` prune expression at `:156` — a second `dirs = [...]` rebinding compiles and silently does nothing
- [ ] Deletion guard: `len(diff.deleted) > max(50, 20 %)` requires `--prune` or `y/N`; watchdog path logs and skips
- [ ] Tests: a mixed-case directory name is excluded; a custom-named venv is *not* excluded by default but is via `extra_dirs`; a diff deleting >20 % does not silently purge; a previously-indexed file that becomes ignored appears in `DiffResult.deleted`
- [ ] Verify against this repo as a test, not a manual check: a full scan yields 98 files (`src` 34 + `tests` 51 + `tools` 13), not 601
- [ ] Release note covering **both** directions of the pull-only change, plus where a third-party user's own `indexer.toml` goes

**At merge**
- [ ] Suite green at ≥244 collected
- [ ] Read-back: the Decision text matches the diff line-for-line. Under branch-only `proposed` there is no second chance to correct it.
- [ ] Resolve the downstream obligations listed in **Depended on by** (none) while the context is fresh

## Revision history

**2026-07-27 — post-jury-review revision.** A five-persona adversarial review
(`ADR-026_REVIEW.md`) returned GO WITH CONDITIONS. Changes made in response:

1. **`[scan]` → the existing `[ignore]` block.** The first draft proposed a new block beside
   an inert one it had not noticed. Flagged HIGH independently by two reviewers as the ADR
   violating its own governing rule.
2. **`pyvenv.cfg` detection dropped** for name matching. It could not be honored by the
   path-only watchdog consumer — the ADR-020 split-brain the draft cited as the thing to
   avoid — and verification showed it misses conda and pre-2020 `virtualenv` anyway.
3. **Single `scan_policy()` / `is_indexable()` export added**, with the raw constants
   privatized so a stale import raises. The draft's shared *accessor* synchronized config
   values but not behaviour; `MCPServer._is_relevant` re-implements the logic by hand.
4. **Precedence table added** — the draft specified neither the both-keys-set case nor the
   empty-list case, and the bare key's name reads as "extend" to anyone who has not read
   this document.
5. **`[ignore].extensions` given a disposition** (5 stale entries vs 11 real ones); silence
   would have left a trap that kills C#/C++ Tier-A support.
6. **Deletion guard added** (§5) — the "free migration" claim was verified correct
   end-to-end, which means one ordinary run can irreversibly purge ~84 % of an index.
7. **Config anchoring added** (§6) — a latent bug today that this ADR would have upgraded
   from "wrong reranker settings" to "wrong things deleted".
8. **Drift test added** (§8) — it fails today on `core.py`, proving Rule 5 was an assertion
   with no mechanism.
9. **Case-folding, single-prune-expression, and known limits** recorded rather than left to
   be discovered.
10. **Re-cost** from "mostly tests" to 2–3 sessions, and the work split into three
    revertible commits with opposite risk profiles separated.

**Notes:**
<!-- 2026-07-27: Created on its branch per CONTRIBUTING §4.1. Combines backlog B-001 + B-002 at @edb's direction. The exclusion set and the summarization-off path were both proven out first in a throwaway run wrapper (scratchpad/run_cpu_index.py) during the 2026-07-27 CPU reindex, which is what surfaced the benchmarks/ corpus problem — the wrapper is where the numbers in Context come from. That wrapper's patched scan resolves to exactly 98 files, which is the number the verification test asserts. -->
