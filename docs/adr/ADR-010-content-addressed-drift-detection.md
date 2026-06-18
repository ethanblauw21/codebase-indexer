# ADR-010: Content-Addressed Drift Detection & Incremental Reindexing

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-010-content-addressed-drift-detection`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-005 — **extends** its `recheck` / self-healing loop and its provenance-versioning model; this ADR is the content-hash change-detection layer ADR-005 anticipated. Also standardizes ADR-005's MD5/SHA-256 split onto one hash (XXH3) — the amend-ADR-005 item, folded in here.
- ADR-006 (graph-analytics) — needs the **centrality / god-object scoring** to auto-derive the "vital" file set for the 3-tier check. Falls back to a config glob when ADR-006 results are absent.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-010 bucket + build kit) and
> [merkle-tree-drift-handling.md](../merkle-tree-drift-handling.md) (all sections). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md).

## Context

Incremental indexing today re-derives "what changed" by scanning the disk and diffing against stored
records. It works, but it has three weaknesses the drift research
([merkle-tree-drift-handling.md](../merkle-tree-drift-handling.md)) calls out:

1. **No cheap whole-subtree skip.** Without a hierarchical content hash, deciding a large unchanged subtree
   is unchanged still costs per-file work.
2. **Git operations cause re-hash storms.** A branch checkout rewrites mtimes across thousands of files;
   absent a content-addressed reconciliation, that looks like "everything changed."
3. **No alarm for out-of-band edits (human↔AI drift).** If a file is edited outside the indexer's
   knowledge, nothing *notices the index and disk have silently diverged* — only the next full scan would,
   eventually.

The research prescribes a **content-addressed state structure** (Merkle tree; Prolly tree as a future
option — no mature Python lib, merkle doc §7) sitting *in front of* the existing diff pipeline: a tree of
content hashes whose root hash is a single fingerprint of the whole corpus. Equal subtree hash ⇒ skip;
changed root hash with no indexer-initiated write ⇒ **drift alarm**. Git-SHA reconciliation (§6.8) defuses
the checkout storm by recognizing that a checkout's "new" content is content the index already knows.

This is a robustness layer, not new retrieval capability — Wave 2 in the backlog.

## Decision

Add a **Merkle-tree content-addressed state layer** in front of the existing incremental diff, with a
mandatory `(mtime,size)` fast-gate, git-SHA reconciliation, a 3-tier freshness check whose "vital" set is
auto-derived from ADR-006 centrality, and a **root-hash drift alarm** for out-of-band edits. Standardize all
change-detection hashing onto **XXH3**.

### §1 — Merkle state structure

Build a Merkle tree over the indexed tree: **XXH3 leaf hashes** per file, internal nodes hashing their
children, up to a single **root hash**. Persisted as `.code-index/merkle.json` (or a `dir_hashes` table in
`src/db.py` — decided at implementation, see Open Questions). Equal subtree hash ⇒ the whole subtree is
skipped without descending. Prolly trees are recorded as a **future option** (better incremental rebalancing)
but default to Merkle because no mature Python Prolly library exists (merkle doc §7).

### §2 — Mandatory `(mtime,size)` fast-gate

Before hashing anything, a **mandatory** `(mtime, size)` check short-circuits unchanged files — hashing is
only spent on candidates that *might* have changed. This keeps the common "nothing changed" run cheap; the
Merkle hash is the *authority*, the stat-gate is the *accelerator*.

### §3 — Git-SHA reconciliation (the checkout-storm fix)

When `.git` is present, reconcile against git's object SHAs (§6.8): a checkout that rewrites mtimes but
restores content the index already indexed is recognized as **no real change** — the git blob SHA matches
what we've seen — so no re-hash storm and no spurious reindex. Reconciliation runs at startup when `.git`
exists; absent git, the stat-gate + Merkle path stands alone.

### §4 — 3-tier freshness check (vital set from ADR-006)

Three check cadences, escalating in cost:
- **mtime heartbeat** — cheap, frequent: the §2 stat-gate.
- **vital pre-flight** — before serving, re-verify the **vital file set**: the high-centrality / god-object
  files **auto-derived from ADR-006's scoring**. These are the files whose drift does the most damage, so
  they get checked more often. Fallback when ADR-006 output is unavailable: a `vital_paths` glob in
  `indexer.toml`.
- **full backstop** — periodic complete Merkle rebuild as the correctness backstop.

### §5 — Root-hash drift alarm (human↔AI drift)

The index records the root hash it last reconciled to. If a scan computes a **different root hash with no
indexer-initiated write in between**, an out-of-band edit happened — fire a **drift alarm** via
`src/MCPServer.py` (a startup reconciliation step + a drift-alarm tool). This is the capability that makes
silent human↔AI divergence *visible* instead of latent until the next full scan.

### §6 — Optional FastCDC sub-file localization

Optionally ([32] FastCDC), content-defined chunking can localize *which region* of a changed large file
moved, not just *that* it changed — enabling sub-file incremental work. Listed as **optional**; whether the
granularity is worth it now is an Open Question.

### §7 — Hash standardization (amend-ADR-005, folded in)

ADR-005 currently uses a mix of MD5/SHA-256 for change detection. Since we're introducing XXH3 leaves here,
**standardize all change-detection hashing onto XXH3** (`xxhash`) while we're in this code. (Cryptographic
hashing, if ever needed for integrity rather than change-detection, stays a separate concern.)

## Consequences

**Better:**
- Unchanged subtrees skip in O(1) on a hash compare; the common run gets cheaper, not just correct.
- Git checkouts stop triggering reindex storms (SHA reconciliation), removing a real day-to-day annoyance.
- The root-hash alarm makes out-of-band human↔AI drift **observable** — a genuinely new capability, not an
  optimization.
- The vital tier auto-focuses freshness effort on the highest-blast-radius files using ADR-006 centrality
  we already compute.
- One hash (XXH3) replaces the MD5/SHA-256 split — faster and simpler (closes amend-ADR-005).

**Worse:**
- New state to persist and keep consistent (`merkle.json` / `dir_hashes`); a corrupted or stale Merkle file
  must degrade safely to a full rebuild.
- New dependency `xxhash` (and optional `fastcdc`).
- Git reconciliation adds a `subprocess` git dependency on the path where `.git` exists.
- The vital tier couples drift detection to ADR-006; the config-glob fallback keeps it functional when
  ADR-006 output is missing, but that coupling must be documented.

**Neutral:**
- Sits *in front of* the existing diff pipeline rather than replacing it — the current `compute_diff` stays
  as the back-end once the Merkle layer decides *what* to look at.
- Prolly tree is a labeled future option, not a commitment.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep the scan-and-diff approach unchanged | No cheap subtree skip, no checkout-storm fix, no drift alarm — the three things this layer exists to add. |
| Prolly tree now instead of Merkle | No mature Python library (merkle doc §7); the rebalancing advantage doesn't justify building one. Kept as a future option. |
| SHA-256 leaves | Slower than XXH3 for change-detection with no benefit here (this isn't an integrity/security hash). XXH3 is the right tool; standardizing also closes amend-ADR-005. |
| Skip the `(mtime,size)` gate, hash everything | Wastes hashing on the overwhelmingly common unchanged case; the stat-gate is the accelerator that keeps the hot path cheap. |
| Hard-code a `vital_paths` list | Static and goes stale; ADR-006 centrality derives the vital set from real structure. The glob remains only as a fallback. |
| FastCDC sub-file localization as core | Adds complexity whose payoff is unproven at our file sizes; kept optional pending a need. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `xxhash` dependency; standardize ADR-005 change-detection hashing onto XXH3 (closes amend-ADR-005).
- [ ] Merkle build in `src/incremental_indexer.py` (`scan_disk` → merkle); persist `.code-index/merkle.json` (or `dir_hashes` in `src/db.py`).
- [ ] Mandatory `(mtime,size)` fast-gate ahead of hashing.
- [ ] Git-SHA reconciliation (§6.8) via `subprocess`, active when `.git` present.
- [ ] 3-tier check; vital set auto-derived from ADR-006 centrality, `vital_paths` glob fallback in `indexer.toml`.
- [ ] Root-hash drift alarm: startup reconciliation + drift-alarm tool in `src/MCPServer.py`.
- [ ] (Optional) FastCDC sub-file localization, pending the granularity decision.
- [ ] `[drift]` config block in `indexer.toml`.

**Notes:**
<!-- 2026-06-18: Wave 2 robustness; folds in amend-ADR-005 (hash standardization → XXH3). Defaults: structure = Merkle (Prolly later); hash = XXH3; (mtime,size) gate mandatory; git-SHA reconciliation on when .git present; vital set auto-derived from ADR-006 centrality with vital_paths glob fallback. Gate: cold-start reconciliation works, git checkout causes no re-hash storm, root-hash alarm fires on out-of-band edits. Open: Merkle vs Prolly final call; persistence location; CDC granularity worth it now. -->
