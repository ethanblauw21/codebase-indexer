# ADR-026: Configuration Authority — `indexer.toml` Is the Source of Truth for Operator Knobs

**Status:** proposed
**Date:** 2026-07-27
**Branch:** `feature/adr-026-configuration-authority`
**Reviewer:** @edb
**Backlog:** [B-001](../backlog.md#b-001) (scan gate) + [B-002](../backlog.md#b-002) (summarizer) — combined here because they are one question asked twice.
**Depends on:** none. `src/config.py` (`load_indexer_config()`) and the cached-read pattern in `core.py` (`_emb_cfg()`, ADR-009 §P1) already exist; this ADR extends both rather than inventing anything.
**Depended on by:** none yet.

## Context

Three knobs that an operator must be able to change are **module constants that no configuration can
reach**, while their neighbours are all config-driven. Each has produced a real defect.

**1. The scan gate ignores the wrong things.** `IGNORE_DIRS`
(`src/incremental_indexer.py:94-99`) was seeded from a JavaScript project. It excludes
`node_modules`, `.next`, `dist` and `build` thoroughly, and the Python ecosystem not at all —
`venv`, `.venv`, `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache` and `.tox` are all
absent. It also still carries `"indexer"`, `"public"` and `"mocks"`, which are meaningless here and
actively wrong elsewhere.

This is not hypothetical. A CPU reindex of *this repository* on 2026-07-27 immediately began
embedding `benchmarks/real_repo/corpus/click/…` — the cloned eval corpora. **503 of the 601 indexable
files in this tree live under `benchmarks/`**, so an unmodified run produces an index that is ~84 %
third-party corpus. Any user with an in-tree virtualenv gets `site-packages` chunked, embedded and
graphed as if it were their own code, at unbounded cost.

**2. `[summarization].enabled` does nothing.** The real gate is `ENABLE_SUMMARIZATION`, a module
constant at `src/incremental_indexer.py:92`. Setting `enabled = false` in `indexer.toml` has no
effect; the only way to turn summarization off is to edit source. On CPU that is the difference
between an index that completes and one that does not.

**3. The summarizer model id is unreachable.** Both `ChunkSummarizer` (`:168-170`) and
`IsolatedChunkSummarizer` (`:292-294`) default `model_id` to `"Qwen/Qwen2.5-Coder-1.5B-Instruct"`,
and `incremental_indexer.py:614` constructs `IsolatedChunkSummarizer()` with **no arguments** — so
the `[summarization].model_id` in `indexer.toml` is never read by anything.

**The shared root cause, and why one ADR.** In every case a documented, operator-facing knob exists in
`indexer.toml` and the code reads a constant instead. Configuration that silently does nothing is
worse than no configuration: it invites a change, accepts it, and ignores it. Answering
"constant or config?" separately for the scan gate and the summarizer is how a third variant appears
later. It is one contract, decided once.

`src/config.py`'s own docstring is also stale — it says the embedder "still hardcodes" its model id,
which ADR-009 §P1 fixed.

## Decision

**`indexer.toml` is authoritative for every operator-facing knob. Module constants remain, but only
as the default that applies when config is absent or silent.** No knob documented in `indexer.toml`
may be unreadable by the code.

### 1. A `[scan]` block owns the scan gate

```toml
[scan]
# Extend the built-in defaults (recommended). Names match at any depth.
extra_ignore_dirs = ["fixtures-vendored"]
# Extend the root-only exclusions.
extra_ignore_root_dirs = ["benchmarks", "gpu-crash-repro", "graphify-out"]
# Replace the defaults outright. Omit unless you mean it.
# ignore_dirs = [...]
# ignore_root_dirs = [...]
```

**`extra_*` extends; the bare key replaces.** Extension is the common case and the safe one — a user
adding one directory should not silently inherit an empty default set and start indexing
`node_modules`. Replacement stays available for someone who genuinely wants full control.

Read through a cached `_scan_cfg()` accessor mirroring `core.py`'s `_emb_cfg()`, so both consumers —
`scan_disk()` and the MCP watchdog filter at `MCPServer.py:1894` — resolve the same values from the
one existing import site.

### 2. Virtualenvs are detected by `pyvenv.cfg`, not by name

A directory is a virtualenv **iff it contains `pyvenv.cfg`** — that is what the tooling itself
writes, so it is exact. Name matching is wrong in both directions: it skips legitimate source
(`src/env/` is ordinary) and misses real venvs named `.venv-3.12` or `venv-linux`.

`site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.eggs` and
`htmlcov` are added to the name-based defaults as well — they are unambiguous and cost nothing.
`venv` / `.venv` are **not** added by name; `pyvenv.cfg` covers them precisely.

### 3. The stale JavaScript entries are removed

`"indexer"`, `"public"` and `"mocks"` come out of the defaults. `"indexer"` would skip a directory
named after this project; `"public"` is a real source directory in many web projects, so today's list
silently under-indexes them.

### 4. `[summarization]` is actually read

`enabled` and `model_id` are read from config and threaded to **both** summarizer classes.
`ENABLE_SUMMARIZATION` becomes the default behind `enabled`, not the gate.

### 5. Defaults live in one place per knob

Each knob has exactly one default, defined next to its accessor, documented in `indexer.toml`, and
reachable by config. A default that appears in two places is a defect.

## Consequences

**Better.** The scan gate becomes correct for Python repos and adjustable for anyone else's layout.
`[summarization].enabled = false` becomes the supported way to make CPU indexing viable, which
matters directly now that the local GPU is unavailable. Config stops lying.

**Free migration — no reindex required.** `scan_disk()` returns the disk view, and
`DiffResult.deleted` is "present in SQLite, absent from disk" (`:184-188`). Files that become ignored
therefore fall into `deleted` and are purged on the next ordinary incremental run. **An existing
bloated index cleans itself up.** This is worth stating because the obvious assumption — that
changing the scan set invalidates the index — is false here.

**Worse — a real behaviour change, and it needs a release note.** Dropping `"public"` and `"mocks"`
means repos containing those directories will index *more* than before, in some cases much more. That
is the correct behaviour and it is still a surprise. Removing `"indexer"` is a strict improvement.

**Neutral.** `pyvenv.cfg` detection costs one `os.path.isfile` per candidate directory during the
walk — immaterial against hashing every file.

**A gate this important is currently untested.** No test in `tests/` references `scan_disk`,
`IGNORE_DIRS` or `IGNORE_ROOT_DIRS`. That absence is how a JS-ecosystem list survived on a Python
tool for this long, so tests are part of this ADR, not a follow-up.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Just add the missing names to the constants (~10 lines) | Fixes today's symptom and leaves the cause. The next operator-facing knob becomes the fourth unreachable constant, and `[summarization]` stays a lie. |
| Config replaces defaults entirely (no `extra_*`) | A user adding one exclusion silently loses `node_modules`. The failure is invisible and expensive. |
| Name-matching for virtualenvs | Wrong in both directions: skips real source called `env/`, misses venvs with non-standard names. `pyvenv.cfg` is exact and no harder. |
| Read `.gitignore` instead of maintaining a list | Attractive, and wrong: plenty of indexable source is git-ignored (generated clients, vendored code), and plenty of git-tracked content should not be embedded. Different questions. Revisit only with evidence. |
| Separate ADRs for scan and summarizer | They are one question — "constant or config?" — and answering it twice is how a third variant appears. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `[scan]` block in `indexer.toml`, documented, with `extra_*` and replace semantics
- [ ] `_scan_cfg()` cached accessor in `src/incremental_indexer.py`, mirroring `core.py::_emb_cfg()`
- [ ] `scan_disk()` reads resolved values; `MCPServer.py:1894` watchdog filter resolves identically
- [ ] `pyvenv.cfg` detection in the `os.walk` prune step
- [ ] Add `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.eggs`, `htmlcov` to defaults
- [ ] Remove `"indexer"`, `"public"`, `"mocks"` from defaults
- [ ] `[summarization].enabled` gates indexing; `ENABLE_SUMMARIZATION` demoted to the default
- [ ] `[summarization].model_id` threaded to **both** `ChunkSummarizer` and `IsolatedChunkSummarizer` — verify both load paths, per the ADR-020 split-brain precedent
- [ ] Fix the stale `src/config.py` docstring (the embedder does read config, since ADR-009 §P1)
- [ ] **Tests (the bulk of the work — none exist today):** venv detected by `pyvenv.cfg` and skipped; a directory named `env/` containing source is *not* skipped; `extra_ignore_dirs` extends rather than replaces; bare `ignore_dirs` replaces; root-only exclusions do not match at depth; a previously-indexed file that becomes ignored appears in `DiffResult.deleted`
- [ ] Verify against this repo: a full scan yields ~98 files (`src` + `tools` + `tests`), not 601
- [ ] Release note for the `"public"` / `"mocks"` behaviour change
- [ ] Resolve the downstream obligations listed in **Depended on by** (none) while the context is fresh

**Notes:**
<!-- 2026-07-27: Created on its branch per CONTRIBUTING §4.1. Combines backlog B-001 + B-002 at @edb's direction. The exclusion set and the summarization-off path were both proven out first in a throwaway run wrapper (scratchpad/run_cpu_index.py) during the 2026-07-27 CPU reindex, which is what surfaced the benchmarks/ corpus problem — the wrapper is where the numbers in Context come from. -->
