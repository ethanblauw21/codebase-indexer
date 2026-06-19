# ADR-010: Content-Addressed Drift Detection & Incremental Reindexing

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-010-content-addressed-drift-detection`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-005 — **extends** its `recheck` / self-healing loop and its provenance-versioning model; this ADR is the content-hash change-detection layer ADR-005 anticipated. It also **consumes** the XXH3 hash standardization **owned by the ADR-005 amendment (Kit 12)**: ADR-010's Merkle leaves use XXH3 consistent with that amendment, but migrating the existing MD5/SHA-256 change-detection hashes is the amendment's task, not this ADR's (Rule A).
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

### §7 — Hash standardization (owned by the ADR-005 amendment; consumed here)

This ADR's Merkle **leaves** are XXH3, consistent with the hash-standardization **amendment to ADR-005**
(Kit 12), which **owns** migrating the existing change-detection hashes onto XXH3. Per Rule A, standardizing
hashing inside ADR-005's change-detection boundary is an *amendment to ADR-005*, not a fold-in here — ADR-010
simply uses the same hash.

**Mantra-4 carve-out (binding on the amendment and on this ADR).** "Standardize change-detection hashing"
means exactly two call sites: `md5_file` (`src/incremental_indexer.py`) and `hash_content` (`src/db.py`,
currently SHA-256). It does **NOT** include:
- **`src/stable_id.py`'s MD5** — that is the **ID formula**, not change-detection; altering it orphans every
  FAISS index (Mantra 4). Strictly off-limits.
- the **`chunk_summaries` MD5 cache key** — swapping it silently invalidates the summary cache; treat as a
  separate, deliberate one-time-invalidation decision, not part of the sweep.

(Cryptographic hashing, if ever needed for integrity rather than change-detection, stays a separate concern.)

### §8 — Limits & honest caveats (current state)

- **The `(mtime,size)` gate is an accelerator, not an authority.** An edit that preserves *both* mtime and
  size — a tool that restores mtime, or an in-place equal-length byte swap — slips the gate. The periodic
  **full backstop** (§4) is the correctness net that eventually catches it; the gate trades a vanishingly
  rare miss for a cheap hot path. The Merkle hash, never the stat-gate, is the authority.
- **The drift alarm needs a baseline.** It fires on root-hash *divergence*, so it requires a previously
  reconciled root hash to compare against. On a cold start there is nothing to diverge from — the first run
  *establishes* the baseline rather than alarming.
- **Git-SHA reconciliation requires `.git`.** Non-git working copies fall back to stat-gate + Merkle — still
  correct, just without the checkout-storm optimization.

## Consequences

**Better:**
- Unchanged subtrees skip in O(1) on a hash compare; the common run gets cheaper, not just correct.
- Git checkouts stop triggering reindex storms (SHA reconciliation), removing a real day-to-day annoyance.
- The root-hash alarm makes out-of-band human↔AI drift **observable** — a genuinely new capability, not an
  optimization.
- The vital tier auto-focuses freshness effort on the highest-blast-radius files using ADR-006 centrality
  we already compute.
- ADR-010's Merkle leaves use XXH3, aligned with the ADR-005 hash-standardization amendment (Kit 12) that
  replaces the MD5/SHA-256 split — faster and simpler change detection.

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
| SHA-256 leaves | Slower than XXH3 for change-detection with no benefit here (this isn't an integrity/security hash). XXH3 is the right tool, aligned with the ADR-005 hash-standardization amendment (Kit 12). |
| Skip the `(mtime,size)` gate, hash everything | Wastes hashing on the overwhelmingly common unchanged case; the stat-gate is the accelerator that keeps the hot path cheap. |
| Hard-code a `vital_paths` list | Static and goes stale; ADR-006 centrality derives the vital set from real structure. The glob remains only as a fallback. |
| FastCDC sub-file localization as core | Adds complexity whose payoff is unproven at our file sizes; kept optional pending a need. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `xxhash` dependency; build the Merkle leaves on XXH3. *(Migrating the existing `md5_file` / `hash_content` change-detection hashes — and **excluding** `stable_id.py` + the summary cache key — is the **ADR-005 amendment (Kit 12)**'s task; align with it.)*
- [ ] Merkle build in `src/incremental_indexer.py` (`scan_disk` → merkle); persist `.code-index/merkle.json` (or `dir_hashes` in `src/db.py`).
- [ ] Mandatory `(mtime,size)` fast-gate ahead of hashing.
- [ ] Git-SHA reconciliation (§6.8) via `subprocess`, active when `.git` present.
- [ ] 3-tier check; vital set auto-derived from ADR-006 centrality, `vital_paths` glob fallback in `indexer.toml`.
- [ ] Root-hash drift alarm: startup reconciliation + drift-alarm tool in `src/MCPServer.py`.
- [ ] (Optional) FastCDC sub-file localization, pending the granularity decision.
- [ ] `[drift]` config block in `indexer.toml`.

**Notes:**
<!-- 2026-06-18: Wave 2 robustness; uses XXH3 leaves aligned with the ADR-005 amendment (Kit 12), which OWNS the hash standardization. stable_id.py MD5 + chunk_summaries cache key are carved OUT (Mantra 4). Defaults: structure = Merkle (Prolly later); hash = XXH3; (mtime,size) gate mandatory; git-SHA reconciliation on when .git present; vital set auto-derived from ADR-006 centrality with vital_paths glob fallback. Gate: cold-start reconciliation works, git checkout causes no re-hash storm, root-hash alarm fires on out-of-band edits. Open: Merkle vs Prolly final call; persistence location; CDC granularity worth it now. -->
