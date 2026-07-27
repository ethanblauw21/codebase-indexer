# ADR-026 — Jury Review

**Subject:** `docs/adr/ADR-026-configuration-authority.md` (status `proposed`, branch
`feature/adr-026-configuration-authority`)
**Date:** 2026-07-27
**Panel:** Architect (Opus) · Maverick (Sonnet) · Grounder (Sonnet) · Critic (Sonnet) ·
Synthesis Guard (Opus)

> **Advisory.** This document records five independent adversarial reviews. **The ADR itself
> is unchanged** — nothing here has been applied. It exists so the decisions below are made
> deliberately rather than discovered during implementation.

---

## Executive synthesis

Five reviewers, run independently. They converged on five things.

### 1. The ADR violates its own core rule, in the very file it governs

Its governing sentence is *"no knob documented in `indexer.toml` may be unreadable by the
code."* But `indexer.toml` **already ships an `[ignore]` block** — `dirs`, `root_dirs`,
`extensions` — that nothing in `src/` reads. ADR-026 proposes a *new* `[scan]` block and
never disposes of `[ignore]`. Adopted as written, the shipped config file would carry two
spellings of one knob: one honored, one dead. That is the ADR's own defect, relocated from
code constants to config keys.

Flagged HIGH by the Architect and the Critic independently. Neither the Decision section
nor any of the 13 Implementation Log boxes mentions `[ignore]`.

### 2. `pyvenv.cfg` detection cannot be honored by the second consumer — the exact split-brain the ADR cites

All four pre-Guard reviewers landed on this, from four different angles. The scan gate has
**two** consumers:

- `scan_disk()` walks the tree, sees directory entries, and can stat.
- `MCPServer._is_relevant()` (`:1893`) receives a bare path string on every filesystem
  event and never touches the filesystem.

"A directory is a virtualenv iff it contains `pyvenv.cfg`" is a *content* check. The
watchdog structurally cannot perform it without stat-ing every ancestor per event. So a
custom-named venv would be correctly excluded from the index and simultaneously keep
triggering reindexes — divergence between the two consumers.

The ADR cites the ADR-020 split-brain precedent as the thing to avoid, and then reproduces
it. Its claim that `_scan_cfg()` makes "both consumers resolve the same values" is true and
insufficient: it synchronizes config *values*, not detection *capability*.

### 3. The pattern the ADR holds up as its model has already drifted

Rule 5 says "a default that appears in two places is a defect." The cited model,
`core.py::_emb_cfg()`, defaults to `jinaai/jina-embeddings-v2-base-code`/768 while the
shipped `indexer.toml` says `BAAI/bge-code-v1`/1536. `src/CLAUDE.md` repeats the stale id.
The pattern has drifted in three places, undetected.

The Architect called the fix higher-leverage than any other line item: **a test that loads
the shipped `indexer.toml`, asserts every documented key is reachable through an accessor,
and asserts code defaults equal shipped values.** It should fail today, on `core.py`,
before it is fixed. Without it, "one default per knob" is an aspiration with no enforcement.

### 4. The extend-vs-replace API is under-specified, and may not need to exist

`extra_ignore_dirs` + a bare `ignore_dirs` that replaces = four keys for one concept, with
no stated precedence when both are set and no defined meaning for `extra_ignore_dirs = []`.
The Critic's sharpest point: **the bare key's name is itself the footgun the `extra_` prefix
exists to prevent.** Nothing about `ignore_dirs` reads as "this replaces the defaults," so a
user who sets it expecting an addition — the natural reading — silently loses `node_modules`.
The design defends one key and leaves the trap live on its sibling.

The Maverick proposed dissolving the problem: a `.indexerignore` file in gitignore *syntax*.
The ADR rejected gitignore *semantics* (reading the user's real `.gitignore`) and never
considered borrowing the *format*. Append a line to add, delete a line to remove — no
extend/replace dichotomy to design or test.

The Guard then crash-tested that alternative and found it covers only half the gate: it is a
deny-list, so the allow-list (`INDEXABLE_EXTS`, and the stale 5-vs-11 extensions trap) still
needs a home; and pre-populated defaults only work inside *this* clone — a third party
indexing their own repo has no `.indexerignore`, so `node_modules` must stay a code default
and the extend-vs-replace question returns. One real advantage survives: pathspec matching is
**path-string-only**, so unlike `pyvenv.cfg` it gives the watchdog genuine parity.

### 5. The scan gate is destructive-adjacent, and it changes behavior on `git pull` alone

The Grounder confirmed the "free migration" claim end-to-end and it is exactly as advertised
— which is the problem. One *ordinary* incremental run purges ~84% of an existing index with
no prompt, and the flat FAISS indexes physically compact, so it is irreversible in place.

Worse, the Guard noticed the default-set edits fire on **update alone, in both directions**:
adding `site-packages`/`pyvenv.cfg` mass-*deletes*, while removing `"public"`/`"mocks"`
mass-*adds* a whole tree to a CPU-only embedding run — the same unattended-runaway shape as
the incident that motivated the ADR, inflicted on someone who only pulled.

Proportionate guard for a single-developer local tool, per the Guard — **not** a subsystem:
if `len(diff.deleted)` exceeds `max(50, 20%)` of the indexed file count, print the top-level
directories responsible and require confirmation. ~20 lines.

---

## Per-persona findings

### The Architect — Structural Lead

- **[HIGH]** Creates a second config block for a concern that already has one, and never
  disposes of the first. Nothing in the Implementation Log deletes or migrates `[ignore]`.
- **[HIGH]** `MCPServer.py:1894` is a **function-local** import re-executed per event (the
  review brief said module-level; the Architect corrected it and the correction was verified).
  The risk isn't a stale frozenset — it's that introducing `_scan_cfg()` while leaving the raw
  constants importable means the watchdog imports the *unconfigured* names forever. Fix:
  export one resolved policy, make both call sites use it, and privatize the bare constants so
  the wrong import can't compile.
- **[HIGH]** `pyvenv.cfg` changes the ignore *concept* from a set to a predicate, and the
  watchdog cannot evaluate a predicate.
- **[MED]** `[ignore].extensions` (5) vs `INDEXABLE_EXTS` (11) is a loaded gun. Wire it
  naively and C#/C++ Tier-A support (ADR-003/ADR-017) dies silently on every repo carrying
  the shipped toml.
- **[MED]** Rule 5 has no enforcement, and the cited model already violates it. Add the drift
  test.
- **[MED]** "Free migration" is a claim about a code path this ADR does not touch, at a scale
  it has never seen.
- **[LOW]** Items 2 and 3 are opportunistic riders carrying all the regression risk and none
  of the necessity. Tests land *first* — you are modifying provably untested code.
- **[LOW]** The two halves have opposite risk profiles; keep them as separate commits.

**Constraints map:** two consumers that must never disagree, one walk-based and one
path-only · long-running MCP server, so a cached accessor needs a restart and a test-visible
reset · `indexer.toml` is a *published* interface cloned by third parties · zero existing
test coverage on the riskiest edits · verification is manual, so make the 601→98 check a test
· `proposed` is branch-only, so there is no land-and-amend escape hatch.

**Verdict:** *The diagnosis is right and the ~10-line alternative genuinely does not fix the
cause — but as written this ADR ships a second config block on top of the dead one it was
meant to kill.*

### The Maverick — Pushback Developer

- **[HIGH]** The `pyvenv.cfg` rule cannot be implemented consistently by both consumers.
- **[HIGH]** The motivating incident is a one-line fix: add `"benchmarks"` to
  `IGNORE_ROOT_DIRS`. Everything else is feature work bundled onto the incident's urgency.
  Rejected alternative (a) is dismissed rhetorically, not argued.
- **[HIGH]** `.indexerignore` in gitignore syntax dissolves the extend/replace problem. Cuts
  roughly half the implementation log.
- **[MED]** "Tests are the bulk of the work" is not credible. Realistic estimate **2–3×**
  what "mostly tests" implies — a 2–3 session piece of work, not one sitting. The 601→98
  verification alone is a debugging session, not a checkbox.
- **[MED]** Copying a pattern that has already silently drifted, without adding drift
  detection, relocates the defect and hides it better.
- **[MED]** "Neutral cost" is asserted, not measured — on the same machine that just had a
  scan-performance incident.
- **[LOW]** Zero validation alongside 4–6 new TOML keys. `ignore_dirs = "foo"` won't error;
  it will silently iterate characters.

**Verdict:** *Kill the `[scan]` design in favor of `.indexerignore`, fix the actual incident
with the one-line addition now, and file the rest as honestly-scoped follow-ups.*

### The Grounder — Source Verification

- **[CONTRADICTED]** `pyvenv.cfg` has a real false-negative surface the ADR does not name.
  **conda does not write `pyvenv.cfg`** (own metadata, not PEP 405), so a conda env falls
  through to name matching — which the ADR deliberately does not cover for `venv`/`.venv`.
  **Pre-2020 `virtualenv` (<20.0), including pipenv shelling out to it** (`pypa/pipenv#3303`),
  wrote a modified `site.py` and no `pyvenv.cfg`. Modern `virtualenv` 20.x+ and `uv venv` **do**
  write it. Two cases the ADR worried about are non-issues: `--without-pip` still writes the
  file, and detection is **relocation-safe** (the file travels with the directory; a stale
  `home` key doesn't remove it). Sources: PEP 405, `docs.python.org/3/library/venv.html`,
  `pypa/pipenv#3303`, `pypa/virtualenv#719`, uv's `uv-virtualenv` crate.
- **[UNCERTAIN]** Test count is **244** collected, not the brief's ~238.
- **[CONFIRMED]** Nothing in `src/` reads `[ignore]`; zero test hits for `scan_disk`,
  `IGNORE_DIRS`, `IGNORE_ROOT_DIRS`. The gate is genuinely untested.
- **[CONFIRMED]** `[ignore].extensions` is 5 entries vs `INDEXABLE_EXTS` 11.
- **[CONFIRMED]** **"Free migration, no reindex" holds end-to-end.** `run_incremental_index()`
  (`:653-661`) → `purge_stale_vectors()` → `db.delete_file()` (`db.py:809-835`), cascading via
  `ON DELETE CASCADE` to `symbols`, `chunks`, `symbol_locations`, `symbol_references` (FKs at
  `db.py:136,154-155,169,212`; `PRAGMA foreign_keys = ON` at `:98`); `edges` and `symbol_types`
  deleted explicitly by FQN. Every tier is `faiss.IndexIDMap(faiss.IndexFlatIP(dim))`
  (`core.py:139-166`) — `remove_ids` is supported and **physically compacts** survivors rather
  than leaving tombstones; `save_all()` at `:745` rewrites each index, so the `.faiss` files
  genuinely shrink.
- **[CONFIRMED]** The `os.path.isfile` cost claim is right, **including on Windows** — one
  `GetFileAttributesW` per directory vs a full-content MD5 read per file; Windows' higher stat
  latency applies symmetrically to `md5_file`'s opens. Caveat the ADR should state: the check
  must run on *every* directory, since any directory could be a venv root.

**Verdict:** *Ground truth is solid and the free-migration cascade genuinely works. The one
real gap is the detector's false-negative surface — conda and pre-2020 virtualenv.*

### The Critic — Semantic Inspector

- **[HIGH]** "Both consumers resolve the same values" is false for the flagship feature. The
  ADR conflates "same config" with "same behavior."
- **[HIGH]** The ADR violates its own governing rule in the artifact it ships into —
  `[ignore].extensions` has no disposition, and `[ignore]` is neither renamed nor removed.
- **[HIGH]** Extend-vs-replace has no precedence rule and no defined empty case. And the bare
  key's *name* is the trap: nothing about `ignore_dirs` signals "replaces."
- **[HIGH]** The pattern held up as the model has already drifted in production, undetected,
  with no test or lint proposed to prevent the same for `[scan]`.
- **[MED]** "Silent" is overloaded — "config absent or silent" (a presence state) vs "silently
  inherit an empty default set" (an implicit-behavior state). An ADR whose thesis is precision
  about default resolution should not overload its own key term.
- **[MED]** "Authoritative" and "operator-facing knob" are never defined, and the scope test is
  applied inconsistently.
- **[LOW]** No escape hatch for `pyvenv.cfg` — no toggle, and no stated interaction for a repo
  that intentionally versions a directory containing a stray `pyvenv.cfg` (a fixture, or a
  broken-venv regression test).

**Verdict:** *The scan-gate half is not implementable as written.*

### The Synthesis Guard — Crash Test

- **[HIGH]** Default-set edits mutate every existing index on `git pull` alone, **in both
  directions**: venv/`site-packages` additions mass-delete, `public`/`mocks` removal mass-adds
  a tree to a CPU-only embedding run. Ship them as their own revertible commit; announce
  `public`/`mocks` as opt-out, not a silent flip.
- **[HIGH]** **Root-anchored config silently no-ops when launched from a subdirectory — and
  this repo already does that.** `MCPServer.py:1948` sets `repo_path = os.getcwd()`;
  `config.py` walks *up* from cwd; `.gitignore` documents in its own comment that "the MCP
  server writes one under `src/` when launched from there." Launched from `src/`, config is
  found at the real root while `repo_root` is `src/`, so `extra_ignore_root_dirs =
  ["benchmarks"]` no-ops. Anchor the policy to the directory *containing* `indexer.toml` and
  refuse or warn when that isn't the scan root.
- **[HIGH]** Silent mass deletion needs one guard, not a subsystem: threshold at
  `max(50, 20%)` of indexed files, print the responsible top-level directories, require
  confirmation; the watchdog path logs and skips. ~20 lines. No backup system, no undo log.
- **[HIGH]** `.indexerignore` covers only half the gate — it is a deny-list, so the allow-list
  and the 5-vs-11 extensions trap survive untouched; and pre-populated defaults only work
  inside this clone, so `node_modules` must stay a code default and the extend/replace question
  returns.
- **[HIGH]** "Gitignore syntax" as ~20 lines of glob matching **is not gitignore syntax**, and
  the mismatch fails silently — users write `!keep/`, `/anchored`, `**/x`, `dir/` and get a
  quiet subset with no error. Take the `pathspec` dependency (verified absent from
  `requirements.txt` and `pyproject.toml`) or don't promise git semantics. Credit where due:
  pathspec matching is path-string-only, so it gives the watchdog genuine parity.
- **[MED]** If both `.indexerignore` and `[scan]` can exist, a silent union is the worst
  outcome. Recommended: `.indexerignore` present ⇒ sole authority, with a startup warning that
  `[scan]` is being ignored.
- **[MED]** `find_config_path` walks up with **no repo boundary**. Today a stray ancestor
  `indexer.toml` only misconfigures reranker knobs; under ADR-026 one sitting in
  `C:\Users\edb\Documents\` would decide what gets *deleted* from every index beneath it. Stop
  at a `.git` boundary.
- **[MED]** The cached accessor fails invisibly: a user edits config, saves, sees no change,
  concludes it's broken. Log the resolved policy once at startup (source path, effective sets,
  file count) and expose a cache reset the tests actually use.
- **[MED]** Prune-order and Windows mechanics: the check must be folded into the **same**
  in-place `dirs[:] = [...]` at `:156` — a second `dirs = [...]` rebinding compiles and
  silently does nothing. Name comparison is case-sensitive today, so `Public`/`Node_Modules`
  slip through on a case-insensitive filesystem. And `os.walk` never tests the top directory,
  so a repo that *is* a venv is never pruned.
- **[MED]** Put the shared policy in a leaf module. `config.py` imports only `os` and
  `tomllib`, so it (or a new leaf) is cycle-free; a helper in `incremental_indexer` imported by
  `config` deadlocks at import and would surface only under the server's function-local import.
- **[LOW]** **The summarizer half is verified inert at current values and should ship first
  and separately.** `indexer.toml:105,113` are byte-identical to both class defaults, so item 4
  is a no-op today with none of the scan gate's risk. One caveat: it makes the git-tracked
  shipped toml the only way to turn summarization off, so "disable for a fast run" now dirties
  the working tree.
- **[LOW]** Two feared edge cases are non-issues: `Path(".indexerignore").suffix == ""` and
  `.toml ∉ _ALL_SCAN_EXTS`, so neither config file can index itself; and `os.walk` defaults to
  `followlinks=False`, so symlink loops can't occur. Windows directory junctions are the one
  case not to reason about — add a fixture.

---

## Go / No-Go gate

From the Synthesis Guard. Each condition is checkable.

- [ ] The Decision section names **exactly one** ignore mechanism (`[scan]` **or**
      `.indexerignore`, not both), and `[ignore]` is deleted or migrated **in the same diff**.
      No merge while the shipped toml has two spellings of one knob.
- [ ] `[ignore].extensions` has an explicit disposition: deleted, or promoted to a live key
      seeded with all 11 of `INDEXABLE_EXTS`. Silence is not a disposition.
- [ ] A single resolved export (`scan_policy()` / `is_indexable(rel_path)`) exists, **both**
      `scan_disk()` and `MCPServer._is_relevant` call it, and the bare constants are renamed
      with a leading underscore so the old import **raises** rather than silently diverging.
- [ ] The ADR states in one sentence what the watchdog does about `pyvenv.cfg` — stats
      ancestors, consumes a venv-root cache, or the rule is scan-only and documented as a known
      asymmetry — and the conda / pre-2020-virtualenv false negatives are named in Consequences.
- [ ] The extend-vs-replace precedence table exists and covers all four cells, including both
      keys set and `extra_* = []`; or the design is replaced and `.indexerignore`'s precedence
      over the toml is stated.
- [ ] Scan config resolves relative to the directory **containing** `indexer.toml`, with a
      mismatch refused or warned — and a test that launches the scan from `src/` and asserts
      `benchmarks/` is still excluded.
- [ ] A deletion guard exists: one threshold check blocking a bulk purge without confirmation,
      plus a test that a diff deleting >20% of the index does not silently purge.
- [ ] Suite green at **≥244** collected, plus: a test asserting a full scan of this repo yields
      ~98 not 601 files; and a **drift test** that loads the shipped `indexer.toml`, asserts
      every documented key is reachable through an accessor, and asserts code defaults equal
      shipped values — **this test must fail today on `core.py`'s jina/768 default** before it
      is fixed.
- [ ] Name matching is casefolded on both sides, and the `pyvenv.cfg` prune is a single
      in-place `dirs[:] =` expression, with a test using a mixed-case directory name and a
      custom-named venv.
- [ ] The commit series is **at least three revertible steps**: (1) tests + summarizer
      threading, (2) config plumbing with no default changes, (3) default-set edits + deletion
      guard. Step 3 reverts without losing 1 or 2.
- [ ] Release note covers **both directions** of the pull-only behavior change — what starts
      being indexed (`public`, `mocks`) and what gets deleted (venv/`site-packages` content) —
      and tells third-party users where *their* `indexer.toml` goes, since the shipped one only
      governs this repo.
- [ ] Final read-back at merge: the ADR's Decision text matches the diff line-for-line. Under
      branch-only `proposed` there is no second chance to correct it.

**Guard verdict:** *GO WITH CONDITIONS — the diagnosis is right and the ~10-line alternative
genuinely does not fix the cause, but the scan gate is destructive-adjacent, currently
untested, and changes behavior on `git pull` alone.*

---

## Recommended revisions

Synthesized across the panel, in the order they should be resolved.

**A. Decide the ignore mechanism, once.** Three live options:

| Option | Cost | What it fixes | What it leaves |
|---|---|---|---|
| `[scan]` block as written | as scoped | operator control | two dead blocks; extend/replace ambiguity; watchdog asymmetry |
| Fold the new keys into the **existing `[ignore]` block** | lowest | the duplicate-block defect, immediately | extend/replace still needs a precedence table |
| `.indexerignore` + `pathspec` | one dependency | extend/replace *and* watchdog parity | needs an allow-list home; defaults still live in code |

The panel did not converge here — the Maverick pushed `.indexerignore`, the Guard found it
covers only half the gate. **Reusing `[ignore]` is the cheapest way to satisfy the Guard's
first gate condition** and does not preclude `.indexerignore` later.

**B. Export one policy, delete the raw constants.** The single highest-value structural change
in the review, and the one the ADR most clearly intended but did not specify. Both consumers
call it; the old names are privatized so a stale import raises.

**C. Add the drift test.** It enforces Rule 5 instead of asserting it, and it should fail today
on `core.py`. Highest leverage per line in the whole ADR.

**D. Add the deletion guard.** ~20 lines. One threshold, one confirmation, one log line.

**E. Anchor config to its own directory.** Fixes a latent bug that exists *today* and that
ADR-026 would upgrade from "wrong reranker settings" to "wrong things deleted."

**F. Split the commits.** Summarizer threading first (verified inert, zero risk). Config
plumbing with no default changes second. Default-set edits plus the guard third, revertible
alone.

**G. Name the false negatives.** conda and pre-2020 `virtualenv` in Consequences. And drop the
two non-issues the ADR implicitly worried about — `--without-pip` and relocation are both fine.

**H. Re-cost honestly.** 2–3 working sessions, not one. The 601→98 verification is a debugging
session; make it a test.

**I. Tighten the prose.** Define "operator-facing knob," stop overloading "silent," and give
`[ignore].extensions` a disposition.

### Two corrections to facts stated in the ADR or its brief

1. `MCPServer.py:1894` is a **function-local** import, not module-level (Architect; verified
   against the file).
2. The suite collects **244** tests, not ~238 (Grounder).
