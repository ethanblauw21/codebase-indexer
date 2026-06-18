# Evaluation: A Merkle Tree for Drift Handling

> **Status:** design evaluation + proposal (could become an ADR if pursued). Not committed work.
> **Date:** 2026-06-18
> **Question evaluated:** should this project add a Merkle tree to detect drift (especially human↔AI drift) and
> drive the daemon's reindexing?
> **Companion to:** [design-research-informed-improvements.md](./design-research-informed-improvements.md),
> [references-code-intelligence.md](./references-code-intelligence.md).

## Verdict (read this first)

**Yes — but with one essential refinement and one honesty caveat.**

- **Essential refinement:** a Merkle tree alone does *not* save the cost of hashing files. To make it faster than
  what exists today you must gate leaf re-hashing on a cheap `(mtime, size)` stat check, reusing cached leaf
  hashes for unchanged files. Without that gate you've added structure but still read every byte every run.
- **Honesty caveat:** the project is a **git repo**, and git *is already a Merkle DAG*. For tracked files,
  `git diff --name-only` gives you the changed set from git's own Merkle tree for free. A custom Merkle tree
  earns its place only for the cases git doesn't cover (below) — not as a wholesale replacement.
- **Where it genuinely wins:** cold-start/offline reconciliation, catching dropped file-watcher events, and a
  compact **"repo state id" that makes human↔AI drift detectable** — which is exactly your stated goal.

Net: worth building as a *reconciliation and drift-alarm layer*, scoped honestly against git and the existing
watchdog. Medium effort, high value for correctness/robustness, modest value for the steady-state hot path.

---

## 0. What this fixes vs. what it doesn't (scope on the record)

To keep expectations honest, the value falls into three buckets:

**Genuine fixes to current weaknesses**
- **Full-tree re-hash every run** — `scan_disk()` reads + hashes every file's full bytes on every incremental
  pass (O(total bytes)). The mandatory `(mtime, size)` gate stops re-reading unchanged files. *Scales with repo
  size.*
- **No reconciliation after daemon downtime** — the watchdog only catches events while running (and these get
  orphaned/killed on Windows). A startup root-compare catches everything changed while it was off.
- **Silently dropped watcher events** — periodic root reconciliation is a cheap self-healing audit for events
  missed under load or on network drives.
- **git-checkout wasted work** — SHA-diff reconciliation (§6.8) avoids the mtime-storm re-hash after branch
  switches.

**New capability (a guard, not a bug fix)**
- **Human↔AI drift detection** — nothing today flags out-of-band changes while an agent reasons. The root hash
  as a "repo state id" + the vital pre-flight (§6.3) is new. *This — plus O(log n) change localization — is what
  the Merkle structure itself buys; most of the speed win above comes from the mtime gate, not the tree.*

**Out of scope (don't expect these from the Merkle layer)**
- **Extraction accuracy** — that's the conformance/precision work (Bucket B / ADR-007). The tree detects *that* a
  file changed, not whether its extracted symbols are correct.
- **Reindex granularity** — still file-level; a whitespace edit still re-extracts that file.
- **The MD5-vs-SHA256 split** in the current code — not fixed by the tree, but standardize on one hash (XXH3)
  while building it.

> **Net:** worth it *because of the drift use case*, not purely as a speedup. The mtime gate alone delivers most
> of the reindex speedup; the Merkle tree is justified by cheap, exact **drift detection and localization**.

---

## 1. Audit — how drift/change-detection works today

The current machinery is **flat, per-file content hashing with a full-tree rescan every run**:

- `incremental_indexer.py:scan_disk()` (line 133) walks the entire repo and computes **MD5 of every indexable
  file's full bytes on every incremental run** — `result[rel_path] = md5_file(full_path)` (line 160). Cost is
  **O(total bytes on disk)** each pass, regardless of how little changed.
- `md5_file()` (line 114) block-reads each file; MD5 chosen for speed, not integrity (line 119).
- `compute_diff()` (line 178) does a three-way Python set diff of `{path: md5}` (disk) vs the SQLite `files`
  table (`content_hash` column) → `DiffResult(new, modified, deleted)` (line 172).
- `db.py` stores one `content_hash` per file (`files` table, `db.py:77`) and `file_is_unchanged()` (line 422)
  compares it. The hash here is **SHA-256** (`hash_content`, `db.py:419`) — note the MD5/SHA-256 split between
  the scanner and the DB writer (minor inconsistency, not a bug).
- Stale FAISS vectors are reconstructed from `stable_id()` and removed surgically (`get_stale_ids`, line 205) —
  this part is already efficient and a Merkle tree does **not** change it.
- The daemon (`MCPServer.py`, watchdog `Observer`) triggers reindex on file events; the README also mentions
  "git-staleness detection" in `reindex`.

**The gap a Merkle tree addresses:** there is no directory-level short-circuiting and no `(mtime, size)` gate.
Every reindex re-reads and re-hashes the whole tree, and there is no compact whole-repo fingerprint to compare
two states (human vs AI, or now vs last-index) cheaply. ADR-005's versioning handles *method/version* drift;
nothing handles *content* drift more cheaply than a full rescan.

---

## 2. Literature

### 2a. Merkle trees *for vector databases* — they exist, but solve a different problem
You asked specifically for Merkle-over-vector-DB work. It exists and is active, **but it targets verifiability /
integrity against a malicious or untrusted server — not drift detection or reindexing**:

- **VeriANN — "Practical and Verifiable Encrypted Vector Search for Retrieval-Augmented Generation"**
  (IACR ePrint 2026/923). First encrypted ANN retrieval framework with query privacy + DB confidentiality +
  **verifiability**: it "reduces client-side verification to a single hash check against the published **Merkle
  root**." https://eprint.iacr.org/2026/923
- **Authenticated kNN / range queries & outsourced-DB integrity via Merkle** (Berkeley CS261 reading; IJSAT 2025
  survey of optimized Merkle structures for authenticated queries). The Merkle root + authentication paths give
  O(log n) membership/result proofs.

> **Implication for us:** this literature would let the indexer *prove* a query result came from an untampered
> index (a nice-to-have if the index is ever served remotely — and it composes with the supply-chain concerns in
> the competitor's security section). It does **not** address drift/reindexing. So the vector-DB Merkle answer
> is: "yes, it's a real field, but it's a *second, optional* benefit (authenticated retrieval), not the tool for
> your stated goal." Treat it as a bonus the same structure could later unlock, not the justification.

### 2b. Merkle trees for change detection / sync — this *is* the right foundation
The applicable, mature body of work is file-sync and change detection:

- **sync-mht — "Fast incremental file transfer using Merkle-Hash-Trees"** (Haskell package). Folder comparison
  where "synchronization time and communication complexity grow only **logarithmically** with the size of the
  directories (assuming the actual difference is small)." https://hackage.haskell.org/package/sync-mht
- **Filesystem-embedded Merkle trees** (US patents 11,741,067 and 11,704,295) — Merkle trees maintained as a
  native filesystem structure for change verification.
- **Established practice:** O(n)→O(log n) change detection; **children hashed in deterministic order (sorted by
  filename) or hashes become unstable**; incremental recompute propagates leaf→root on any file change. (Multiple
  engineering write-ups corroborate the exact design you proposed.)
- **Git** is the canonical production example: a Merkle DAG of tree + blob objects, with a stat (`mtime`,`size`)
  index cache that lets it skip re-hashing unchanged files — the very refinement called out in the Verdict.

**Conclusion:** your proposed design matches established sync/change-detection practice. The vector-DB angle is
real but orthogonal (verifiability, not drift).

---

## 3. Evaluation of your proposed design

> *"A Merkle tree … parse from leaf to root, sorting each directory by a standard filter so the hashing is
> determinate and able to spot changes easily … help the daemon with its reindexing."*

**What's correct and well-aligned with the literature:**
- **Leaf→root construction** with **deterministic per-directory ordering** is exactly right and *required* — an
  unsorted directory yields unstable hashes (§2b). Your "standard filter" = a canonical sort key.
- Using it to "spot changes easily" is the core Merkle property: one root comparison answers "did anything
  change?", and a top-down descent localizes *what* changed without scanning everything.
- Feeding the daemon's reindexing is the right consumer — it slots in front of the existing `compute_diff` →
  stale-vector-removal → re-embed pipeline.

**Critical refinements (these make or break it):**

1. **Gate leaf hashing on `(mtime, size)` — the most important point.** A Merkle tree by itself still needs leaf
   hashes to compute directory hashes, so naively it re-reads every file (no faster than today). Cache each
   leaf as `(path, mtime, size, hash)`; on rescan, if `mtime`+`size` match the cache, **reuse the cached hash
   and skip the read**. Only then do unchanged subtrees become free, and the whole thing beats the current
   full-rehash. (This is precisely git's index cache.)

2. **Reconcile against git, don't duplicate it.** For tracked files, `git status --porcelain` already derives
   the changed set from git's Merkle DAG and respects `.gitignore`. The custom tree should *own* only what git
   doesn't: non-git repos, untracked/ignored-but-indexed files, and the working-tree state at the moment of
   indexing. Practical stance: **use git as the fast path when present; use the Merkle tree as the
   git-independent reconciliation layer.**

3. **Build the tree over the *indexable* set, with the same filters as `scan_disk`.** Apply `IGNORE_DIRS`,
   `_ALL_SCAN_EXTS`, and the monster-line/role rules so the tree reflects what actually gets indexed — otherwise
   the root churns on files you never index. Include each file's role/extension in its leaf so a rename that
   flips indexability is caught.

4. **Hash choice is a fork:** for pure drift detection use a fast non-crypto hash (**XXH3**, consistent with
   ADR-005 and ~30 GB/s) at the leaves. If you *also* want the §2a verifiability benefit later, use **SHA-256**
   (slower) so the same root is usable as an authentication commitment. Decide which goal dominates; don't pay
   for crypto strength you won't use.

5. **Granularity mismatch to acknowledge:** leaves are *files*, but the index's real unit is *chunks/symbols*. A
   whitespace-only edit changes the file hash (→ reindex) even if no symbol changed — same as today, so no
   regression, but the Merkle tree won't make reindexing *finer*, only *detection cheaper*. Content-drift
   (this tree) and method-drift (ADR-005) are orthogonal axes that compose.

**On "drift between human and AI work" specifically:** this is the strongest motivation and it's well-founded.
The root hash is a compact **repo state id**. Capture the root at the moment of indexing; if the live tree's
root later diverges (a human edited while the AI worked, or an agent wrote files out-of-band), a single
root comparison flags it and a descent localizes the drifted subtrees — O(log n + changes). That's a concrete,
cheap **drift alarm** the current per-file scan can't provide without a full pass.

---

## 4. Proposed design for this project

A purely additive **reconciliation + drift-alarm layer** in front of the existing diff pipeline. **No change to
FAISS, `stable_id`, or the chunk schema.**

**Storage** (alongside `graph.db`): a `.code-index/merkle.json` (or `dir_hashes` + `file_stat` tables) holding
- per file: `(path, mtime, size, leaf_hash)`
- per directory: `dir_hash`
- the `root_hash` and the commit/timestamp it was captured at (the "last-indexed state id").

**Build** (`src/merkle_index.py`, new):
1. Walk with the existing `scan_disk` filters.
2. Per file: if `(mtime,size)` match the cache → reuse `leaf_hash`; else hash bytes (XXH3) and update.
3. Fold child leaf/dir hashes **sorted by normalized name** up to `dir_hash`, up to `root_hash`.

**Diff** (replaces the front of `compute_diff`): top-down descent comparing stored vs new `dir_hash`; recurse
only into differing subtrees → produces the same `DiffResult(new, modified, deleted)` the pipeline already
consumes, in O(changed + depth) instead of O(all files).

**Daemon integration** (`MCPServer.py`):
- **Startup reconciliation:** compare persisted `root_hash` to a freshly built one → catch everything changed
  while the daemon was down (relevant on Windows, where the watchdog process can be orphaned/killed).
- **Hot path unchanged:** keep watchdog for live events, but update affected leaves→root incrementally per event
  so the persisted root stays current.
- **Periodic reconciliation tick:** a cheap audit that catches dropped/missed watcher events (network drives,
  load).
- **Drift alarm:** expose `root_hash` as a repo state id via a log line / lightweight MCP read tool; surface when
  the live root diverges from the last-indexed root — the human↔AI drift signal.

**Scope guard:** when `.git` is present, prefer `git diff --name-only HEAD` + untracked listing as the fast path,
and use the Merkle tree for reconciliation and for ignored-but-indexed content. Build it git-independent so
non-git trees still work.

---

## 5. Pros / cons / recommendation

**Pros**
- Cheap whole-repo "did anything change?" (one comparison) and O(log n) change localization once the `(mtime,
  size)` gate is in place.
- Robust **cold-start and dropped-event reconciliation** — self-healing against silent drift the watchdog misses.
- A real **human↔AI drift alarm** via the root state id — your primary goal, delivered.
- Additive: front-ends the existing diff; no schema/ID/FAISS churn. Portable to the planned Rust indexer.
- Same structure later unlocks **verifiable retrieval** (VeriANN-style) if the index is ever served remotely.

**Cons / costs**
- Partially overlaps git for tracked files — must be scoped to avoid reinventing it.
- Without the `(mtime,size)` gate it's no faster than today (the trap to avoid).
- Adds a persisted side-structure to keep consistent with the index (a new failure mode if it desyncs — mitigate
  by treating a missing/garbage Merkle store as "rebuild from scratch," never as truth).
- Steady-state benefit is modest because watchdog already reports live changes; the win is reconciliation +
  drift detection, not the hot path.

**Recommendation:** build it as a **reconciliation/drift layer**, not a hot-path replacement. Make the
`(mtime,size)` leaf gate mandatory, scope against git, and ship the root-hash drift alarm first (cheapest, most
aligned with the stated goal). This is ADR-worthy if pursued — it touches `incremental_indexer.py` and adds
persistence.

---

## 6. Extension — three-tier drift checking (mtime / vital / full)

**Proposal:** a 3-tier hash scheme "to match the chunking" — (1) mtime only, (2) vital path (core functionality
and tool files), (3) full path.

**Verdict: folds in well, with one reframing and one auto-derivation.**

### 6.1 It's a different axis than the chunking (name it honestly)
The chunking tiers (surgical/component/architectural) vary **granularity of the same content**. These hash tiers
vary **cost and cadence of checking**. So this is an **escalation ladder**, not a structural mirror of the
chunker — a useful 3-tier mnemonic, but the tiers mean different things. (If you actually want a tier scheme that
*does* mirror the chunker, see §6.5.)

### 6.2 Tier 1 — mtime: it's the gate for *all* tiers, and a cheap heartbeat (with a backstop)
`mtime` is not a content hash; it's the `(mtime, size)` skip-gate from §3 that every tier already relies on. As a
standalone "tier" it's a legitimate **continuous liveness/heartbeat check** (stat is ~free), but mtime is
unreliable in two opposite directions:

- **False negatives (dangerous — missed change):** mtime unchanged but content changed. Causes: tools that
  preserve/restore timestamps (`touch -d`, some archive extractors, rsync `--times`), sub-second-resolution
  races, network/virtual filesystems with unreliable mtime. A missed change *is* the drift you're preventing.
- **False positives (wasteful, but self-correcting):** mtime newer but content identical. The dominant cause is
  **git checkout/switch**, which writes touched files with `mtime = now` — see §6.8. This triggers a re-hash but
  the content-hash layer then finds no change, so **no incorrect reindex results** — only wasted reads.

Because of the false-negative case, **mtime-only must never be the only check that ever runs**: it's the frequent
cheap pass; Tier 3 is the periodic backstop that catches what mtime missed (same reconciliation argument as §4).
The false-positive case is handled by git reconciliation in §6.8.

### 6.3 Tier 2 — vital path: the strong idea — *auto-derive it from ADR-006*
The valuable tier. "Vital" = files whose change has the largest **reindex blast radius and drift cost**. Don't
hand-maintain that list (it drifts itself). **Derive it from the graph-analytics layer (ADR-006):** the
**god-objects / high-betweenness / high-fan-in** symbols already computed there *are* the vital set — a change to
a high-fan-in file invalidates many dependents' context. This is a clean fold between the Merkle layer and the
analytics layer: ADR-006 tells the drift checker which subtree to watch most closely.

Tier 2 then does **double duty**: (a) a fast **pre-flight** check ("has the structural foundation moved?") on a
short cadence, and (b) a **reindex priority** signal — when a vital file changes, reindex it and eagerly
invalidate its dependents first.

Fallback when ADR-006 isn't built yet: a `vital_paths` glob in `indexer.toml` (e.g. `src/core.py`,
`src/db.py`, `src/MCPServer.py`, the adapters) — manual, but explicit.

### 6.4 Tier 3 — full path: the backstop (already designed in §4)
The complete-tree reconciliation from §4. Runs on cold start, on demand, and on a slow periodic tick to catch
Tier-1 false negatives and dropped watcher events.

### 6.5 How it folds into the Merkle structure
Merkle trees are already hierarchical, so this needs **no new mechanism** — just **nested sub-roots checkable at
different cadences**:
- `full_root` — hash over the whole indexable set (Tier 3).
- `vital_root` — a sub-root over just the vital subset (Tier 2), recomputed independently and cheaply.
- the `(mtime,size)` gate (Tier 1) feeds both, deciding which leaves to actually re-hash.

Cadence mapping (the real payoff — sub-second pre-flight without a full scan):

| Tier | Mechanism | When it runs | Cost |
|------|-----------|--------------|------|
| 1 — mtime | stat the (vital or full) set, no reads | continuous / every watchdog idle tick | ~free |
| 2 — vital | recompute `vital_root` (gated by stat) | before an AI agent starts work; short interval | low |
| 3 — full | recompute `full_root` (gated by stat) | cold start, periodic audit, on demand | bounded by changes |

**Human↔AI drift mapping:** Tier 1 = continuous heartbeat; **Tier 2 = pre-flight before an agent acts** ("is the
core still what I last indexed?"); Tier 3 = periodic deep audit. The vital pre-flight is the highest-value
addition — it's a cheap "your foundation is unchanged" guarantee that a full scan is too slow to give on every
agent turn.

### 6.6 Alternative if you meant "mirror the chunker" literally
A scheme that genuinely parallels the chunking tiers would hash by **graph level**, not by check-cost:
symbol/chunk-hash (≈ surgical) → file/module-hash (≈ component) → directory/system-hash (≈ architectural). That
is a true Merkle hierarchy mirroring the chunker and would let drift be localized to the same granularity the
index is built at. It's a cleaner conceptual match but a bigger build (leaves become chunks, not files) — worth
considering only if chunk-level (not file-level) drift localization is actually needed. The mtime/vital/full
ladder above is the more practical near-term choice.

### 6.7 Risks specific to the 3-tier scheme
- **Complexity:** three cadences and a vital-set definition vs. one full pass. Justified only if you want
  frequent low-latency pre-flight checks; on small repos the §3 gated full scan is already cheap enough.
- **mtime false negatives** (§6.2) — Tier 3 backstop is mandatory, not optional.
- **Vital-set staleness** — mitigated by auto-deriving from ADR-006 rather than a static list.

**Recommendation:** adopt the ladder as **detection cadence + reindex priority**, implemented as a `vital_root`
nested in `full_root`, with mtime as the shared gate. Ship Tier 2 (vital pre-flight, auto-derived from ADR-006)
as the headline feature — it's the cheapest high-value drift guard for agent work. Keep Tier 3 as the
non-negotiable backstop.

### 6.8 Reconciling git checkouts (the mtime-reset storm)

**The problem.** Default `git checkout` / `git switch` writes every file it changes with **`mtime = now`** (it
does *not* restore historical mtimes). After switching branches, all files that differ between the two commits
get a fresh timestamp, so the Tier-1 mtime gate flags them — a large flag for files whose *content* may be
identical to what's already indexed. (Files that don't differ between the commits are untouched and keep their
mtime, so the storm size = number of files differing between old and new HEAD.)

**Why it is not actually dangerous.** This is a **false positive** (§6.2), and the two-level design absorbs it:
the mtime bump causes a *re-hash*, the content hash then matches the stored leaf, and **no re-embedding / FAISS
write occurs.** Worst case of ignoring the problem entirely = one wasted re-hash pass after a checkout, with zero
incorrect reindexing. So this is a *performance* concern, not a *correctness* one.

**The robust fix — trust git's Merkle tree, not the timestamp.** Record the **indexed commit SHA** in the Merkle
store (not a wall-clock checkout time). On startup/periodic reconcile:

1. `current HEAD == indexed SHA` and `git status --porcelain` clean → nothing changed; **skip all re-hashing**,
   mtime storm ignored entirely.
2. HEAD moved → `git diff --name-only <indexed_SHA>..<current_SHA>` yields the **exact** changed tracked files;
   add `git status --porcelain` for uncommitted working-tree edits. Reconcile *only* that set. Every other
   checked-out file is known-unchanged regardless of its new mtime.

This is exact and order-independent (survives multiple checkouts, rebases, and daemon downtime), because git
already computed the content delta as a Merkle-tree operation — which is precisely the question we're asking.

**Why "save the checkout timestamp" is the weaker form of the right instinct.** Recording *state* on checkout is
correct; recording *wall-clock time* is a racy heuristic — a genuine human edit moments after a checkout also has
`mtime ≈ now`, so a timestamp value can't distinguish "git wrote this" from "a human edited this." Recording the
**commit SHA** and diffing is exact where a timestamp is a guess. A `post-checkout` hook (it receives old HEAD,
new HEAD, branch-flag) is a fine *event-driven optimization* to invalidate eagerly, but **startup HEAD
reconciliation is the more robust backbone** because it reconstructs the truth from git state even if the hook
never ran.

**Scope.** `git diff` covers *tracked* files only. Untracked files and ignored-but-indexed files (the §3 scope
note) still go through the Merkle/mtime path. Clean division of labor: **git is the authority for tracked-content
drift across HEAD moves; Merkle + mtime + content-hash handle untracked/ignored content and non-git repos.**

> Prior art: this is exactly how git itself handles its own stat cache — stat (mtime/size/ctime/inode) is a
> *hint*, the content (blob) hash is *truth*, and HEAD/index is authoritative for tracked files; git even has
> explicit "racy git" handling for sub-second mtime races. We are reusing that proven layering rather than
> reinventing it.

---

## 7. Alternative structures and the research angle

### 7.1 Is the feature, as specified, a paper? No — it's engineering
A plain Merkle tree for change detection is well-trodden (sync-mht, git itself), and the vector-DB Merkle work
(VeriANN, §2a) targets *verifiability*, not drift. The design in §0–§6 is sound engineering on established
structures — not a novel contribution on its own.

### 7.2 More supportive structures (genuine upgrades over a plain Merkle tree)
- **Prolly Trees** (Probabilistic B-trees; Dolt/Noms) — "a cross between B-trees and Merkle trees." Better-fitted
  to this project's goals than a plain Merkle tree:
  - **History-independence:** identical logical content → identical tree shape *regardless of edit order*. This
    is the determinism §3/§6 wanted, achieved structurally (and at chunk granularity) rather than by sorting
    directory entries.
  - **Fast diffs** in O(differences) between two versions — exactly the "diff human state vs AI state" operation.
  - **Structural sharing** — keep last-indexed and current snapshots cheaply.
  - Prior art for the exact pairing: DoltHub, *"How we can Build a Vector Index from Prolly Trees"* — so a
    versioned vector index on Prolly Trees is already a demonstrated direction.
  *(Caveat: Prolly Trees are an engineering construct from Noms/Dolt, not a peer-reviewed paper; cite the DoltHub
  docs/blogs.)*
- **Content-Defined Chunking (FastCDC, USENIX ATC'16)** — fixes the §0 "reindex granularity is file-level"
  limitation. CDC sets chunk boundaries by content, so a small edit doesn't shift all downstream boundaries →
  localize *which sub-file regions* changed and re-extract only those. A more supportive structure for **sub-file
  drift localization**.

### 7.3 Where the added context *does* open paper options — agentic drift
The richer framing (human↔AI drift, centrality-tiered checking, vector-index coupling) lands in an actively
forming 2026 research area:
- **Codified Context: Infrastructure for AI Agents in a Complex Codebase** (arXiv:2602.20478) — **closest prior
  art.** Uses knowledge tiers with *different update frequencies* (≈ the mtime/vital/full ladder) and a *"context
  drift detector" parsing git commits against a subsystem-to-file mapping* (≈ the §6.8 git reconciliation). A
  paper here must differentiate from this.
- **Scaling Human-AI Coding Collaboration Requires a Governable Consensus Layer** (arXiv:2604.17883);
  **Human-AI Synergy in Agentic Code Review** (arXiv:2603.15911).

**The paper-shaped contribution** (not found published as a combination): drift detection as a **content-addressed
structural layer (Prolly/Merkle) with a centrality-derived "vital" tier (from ADR-006) used as an agent
pre-flight against stale-index drift**. Codified Context does this at the *spec/doc* level; doing it structurally
+ importance-weighted + agentic is the open gap. Conclusion: **not a paper as-is; a credible one with the
Prolly/CDC structure plus the agentic framing.**

---

## Sources
- VeriANN — Practical and Verifiable Encrypted Vector Search for RAG (IACR ePrint 2026/923): https://eprint.iacr.org/2026/923
- Prolly Trees (Dolt storage engine): https://docs.dolthub.com/architecture/storage-engine/prolly-tree · Vector index on Prolly Trees: https://www.dolthub.com/blog/2024-10-08-how-to-build-a-vector-index-with-prolly-trees/
- FastCDC — A Fast and Efficient Content-Defined Chunking Approach for Data Deduplication (USENIX ATC 2016): https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia
- Codified Context: Infrastructure for AI Agents in a Complex Codebase (arXiv:2602.20478): https://arxiv.org/abs/2602.20478
- Scaling Human-AI Coding Collaboration Requires a Governable Consensus Layer (arXiv:2604.17883): https://arxiv.org/abs/2604.17883
- Human-AI Synergy in Agentic Code Review (arXiv:2603.15911): https://arxiv.org/abs/2603.15911
- Providing Authentication and Integrity in Outsourced Databases using Merkle (UC Berkeley): https://people.eecs.berkeley.edu/~raluca/cs261-f15/readings/merkleodb.pdf
- A Survey of Optimized Merkle Tree Structures for Query Authentication (IJSAT 2025): https://www.ijsat.org/papers/2025/3/6844.pdf
- sync-mht — Fast incremental file transfer using Merkle-Hash-Trees: https://hackage.haskell.org/package/sync-mht
- Filesystem embedded Merkle trees (US Patents 11,741,067; 11,704,295)
- Merkle Trees for change detection (engineering references): https://jsdevspace.substack.com/p/using-merkle-trees-to-efficiently
- See also XXH3 / content-hash incremental in [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md) (§4.6) and ADR-005.
