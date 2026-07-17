# ADR-025: Per-File Content-Change Timestamps & Index Freshness Metadata

**Status:** proposed
**Date:** 2026-07-16
**Branch:** `feature/adr-025-index-freshness-metadata`
**Reviewer:** @ethanblauw21
**Depends on:** none — the design is deliberately hash-agnostic and consumes only `files.content_hash`, whatever algorithm currently populates it. See *Relationship to adjacent ADRs* below for non-blocking overlaps (ADR-005, ADR-010, ADR-012) that carry no obligation either direction.
**Depended on by:** none yet.

> Closes [#20](https://github.com/ethanblauw21/codebase-indexer/issues/20) — *"Per-file content-change timestamp: reliable 'changed since T' delta"*.
> Downstream consumer: the `project_router` **agent context hub** in `C:\Users\edb\Documents\segmem`
> (`project_router\docs\agent-context-hub-design.md` §7, lines 141–167).

## Context

### The consumer, and why it cannot ask us anything

`segmem`'s `project_router` aggregates several projects into one context hub. Its `codemap` connector
(`project_router\connectors\codemap.py`) reads this repo's index by opening
`<project>/.code-index/graph.db` **read-only, offline, stdlib `sqlite3` only**. That is not an
implementation detail — it is a written invariant (`project_router\docs\ADRs.md:24-28`):

> **Offline / read-already-built.** A connector reads a tool's *already-materialized* local artifact
> (a SQLite index, a manifest file). It never runs the tool, holds no live connection, and makes no
> network call.

The hub **polls; projects never push** (`agent-context-hub-design.md` §1/§8), so this repo stays
hub-ignorant by design. The consequence for us is decisive: **our public contract with segmem is the
SQLite schema of `graph.db`, not an MCP tool signature.** The connector already runs
`SELECT MAX(indexed_at) FROM files` (`codemap.py:48-60`). Anything we want segmem to know must be
answerable by a read-only SQL query against `graph.db`.

Two traps follow. First, `project_router\connectors\__init__.py:45` swallows every connector
exception, and each query in `codemap.py:95-116` is individually wrapped in
`try/except sqlite3.Error` returning a default — so **schema drift degrades silently rather than
erroring.** If we break its queries, segmem gets quieter, not louder, and nobody finds out.
All changes here are therefore strictly **additive**; every existing `files`/`symbols`/`edges` query
must keep answering. Second, FAISS cannot help: the tier indexes are `IndexIDMap(IndexFlatIP)`
(`src/core.py:138-159`) storing vectors and int64 IDs with **no payload**, so there is nowhere to hang
a per-vector timestamp. Since a file's chunks are purged and reinserted together, per-file granularity
carries exactly the information a per-vector stamp would.

### The bug

`files.indexed_at` already exists (`src/db.py:74-79`) and is already correct **for incremental runs** —
change detection is MD5 content hashing (`src/incremental_indexer.py:124-208`), so only genuinely
changed files are re-ingested and restamped.

It is wrong for **full rebuilds**. `reindex(changed_files_only=False)` executes `DELETE FROM files`
(`src/MCPServer.py:1200-1207`) before re-ingesting, so every row reinserts with a fresh `DEFAULT`
timestamp regardless of whether a byte changed. A single full reindex restamps the entire corpus, and
`WHERE indexed_at > ?` then returns every file in the repo. This is exactly what #20 names as the gap,
and it is what gates the hub's Phase-2 session-event correlation table
(`agent-context-hub-design.md:159-167`).

Deleting `.code-index/` entirely — which `src/CLAUDE.md` **requires** when swapping the embedder, and
which ADR-009 §P1 already did once — is the same failure with no prior rows to recover from.

### The freshness signal that has never fired

A git-staleness check exists at `src/MCPServer.py:1176-1198`: it compares a commit hash stored in
`.code-index/last_indexed_commit.txt` against HEAD and reports diverged files. It is written **only by
the MCP `reindex` tool** (`src/MCPServer.py:1231-1239`) — a CLI `code-indexer` run reindexes without
recording HEAD — so the two entry points disagree about what is indexed. The file **does not currently
exist** in `.code-index/`, so the `os.path.exists` guard skips the check outright; the surrounding
`except Exception: pass` (`src/MCPServer.py:1197`) hides non-git-repo, no-commits, and git-absent alike.
The report has, in practice, likely never run.

Worse, it is invisible to the only consumer that needs it. segmem cannot run `scan_disk`, so it can
never learn staleness the way we do; a stored-HEAD-vs-actual-HEAD comparison is precisely the cheap,
offline signal it *could* use — but a `.txt` file in `.code-index/` is unreachable from a connector that
opens `graph.db`. The signal exists and is addressed to nobody.

The live evidence, from `indexer\.claude\context\codemap.json` at the time of writing:

```json
"summary": "Codebase index (indexer / .code-index): 59 files, 601 symbols, 2948 edges. ... Last indexed 2026-07-07T19:09:59Z.",
"updated": "2026-07-13T19:10:26+00:00"
```

`updated` is **harvest** time, not index freshness; the real index age is buried in prose and
unparseable; and `graph.db`'s mtime was later still. The hub surfaces `source · updated <ts>`
(`router_mcp.py:72-75`), so the model reads the harvest time and reasonably mistakes it for freshness.

### Relationship to adjacent ADRs (non-blocking, no obligations)

- **ADR-005** (chunk provenance versioning) owns the MD5→**XXH3** change-detection migration via its
  2026-06-18 amendment (Kit 12). This ADR compares `files.content_hash` to itself and never inspects the
  algorithm, so it is correct before and after that migration. No dependency either direction.
- **ADR-010** (content-addressed drift detection) solves an adjacent problem at a different layer:
  Merkle subtree hashes to *skip work*, including the "git checkout looks like everything changed"
  re-hash storm. This ADR is a *timestamp contract for an external reader*. Both may exist; ADR-010
  changes how we decide what to re-index, not what a stamp means once written.
- **ADR-012** (cross-repository graph) owns the project/repo node and cross-repo symbol identity, and
  states plainly that no project node exists today. Freshness aggregation needs none of that: segmem
  keys projects by absolute folder path (`project_router\router.py:50-60`) and knows which folder it
  crawled. This ADR introduces **no** project identity and must not pre-empt ADR-012's schema.
- **ADR-016 / ADR-005** own symbol- and chunk-level provenance. Per-symbol change stamps are explicitly
  deferred to them (see *Alternatives*).

## Decision

Ship a **trustworthy per-file content-change delta, readable offline from `graph.db`**, and relocate the
git-staleness signal into the database where its consumer can reach it. File granularity only.

### §1 — `files.content_changed_at` and `files.authored_at` (additive)

Two new nullable columns on `files`. `indexed_at` is **left untouched and keeps its current meaning**
("when the indexer last wrote this row"), which preserves segmem's existing `MAX(indexed_at)` query and
keeps an honest debugging signal.

| Column | Meaning | Source |
|---|---|---|
| `content_changed_at` | When this file's **content** last changed. The authoritative delta field. | git **committer** time (`%cI`), else `now()`, else NULL — per §2 |
| `authored_at` | When the content was **authored**. Diagnostic provenance only. | git **author** time (`%aI`) |

`authored_at` is recorded because it is free (same git pass) and because "authored vs landed" is a useful
throughput meter. **Nothing reads it automatically and no logic couples the two.** They diverge in ~2% of
this repo's commits (rebases, cherry-picks); no rule is defined for divergence because none is correct —
for "what changed in the last day", committer time is always the right answer.

### §2 — Back-date from git on first index

On first index of a file, `content_changed_at` is seeded from git rather than `now()`, so a from-scratch
rebuild reconstructs real change history instead of spiking the whole corpus.

**One git pass, not per-file.** A single `git log --format='%cI|%aI' --name-only --no-merges` walk builds
`{path → (committer_time, author_time)}` for every tracked path. Measured at **~85 ms** over this repo's
full history (174 tracked files, 56 commits). Per-file `git log -1` invocations are explicitly rejected.

Resolution rules, in order:

1. **Tracked and clean vs HEAD** → git committer time.
2. **Tracked but dirty** (uncommitted working-tree edits) → `now()`. Git's stamp describes the *committed*
   version, but we indexed the *dirty* version; back-dating would claim content came from a time it never
   existed at, and `changes_since()` would **miss real, recent work** — a false negative, strictly worse
   than the false positive we are fixing, and invisible because segmem swallows errors.
3. **No git history** (untracked, git-ignored, or no repo) → **NULL**. `WHERE content_changed_at > ?`
   naturally excludes NULLs, so segmem ignores files it cannot reason about instead of being told a
   fabricated time. Not hypothetical: `IGNORE_DIRS` (`src/incremental_indexer.py:92-97`) does not exclude
   `venv`/`site-packages`, so vendored `.py` files git has never seen do get indexed.

Back-dating applies **only on first index**. Thereafter the file is stamped when its hash changes.

### §3 — Preserve stamps across full rebuilds

Before any wipe that destroys `files` rows, capture `{path → (content_hash, content_changed_at, authored_at)}`.
After re-ingest, restore the stamps for every path whose `content_hash` matches. Files whose hash differs
are genuinely changed and stamp normally. This makes `reindex(changed_files_only=False)` timestamp-neutral
and satisfies #20's second acceptance criterion.

A from-scratch rebuild (deleted `.code-index/`) has no rows to preserve; §2's back-dating reconstructs
history from git instead, which is why back-dating is load-bearing rather than a nicety.

### §4 — `index_meta` key/value table (new)

A small `(key TEXT PRIMARY KEY, value TEXT)` table — the delivery vehicle for run-level facts segmem
cannot otherwise obtain. Precedent exists and segmem **already knows how to read this shape**: its
`filesystem` connector reads exactly such a table from the sibling rust indexer
(`project_router\connectors\filesystem.py:95-96`).

| Key | Value | Why |
|---|---|---|
| `last_indexed_commit` | HEAD at last verification | Lets segmem answer "is this stale?" with one git call, no indexer run |
| `last_verified_at` | ISO-8601 UTC of last completed run | Distinguishes "fresh index over quiet code" from "week-old index". Fixes the harvest-time-mistaken-for-freshness bug |
| `files_total` | count of indexed files | Lets a reader sanity-check the rollup it is about to serve |
| `schema_version` | `"1"` | Because segmem degrades **silently**, a reader needs a way to detect drift rather than quietly serving a stale shape |

`last_verified_at` is written on **every** completed run **including no-ops**. This is honest: the early
return at `src/incremental_indexer.py:513-516` fires only *after* `scan_disk` has hashed every indexable
file and found nothing changed. "At commit X, at time T, we verified the index matches the code" is a
true and strictly more informative statement than segmem has today. Recording only after real work would
mean a docs-only commit never records HEAD, leaving the staleness check reporting `README.md` as
"changed since the index was built" **forever**.

### §5 — Retire `last_indexed_commit.txt`; record in `run_incremental()`

All four index triggers (CLI, MCP `reindex`, watchdog, server startup) funnel through
`run_incremental()` (`src/incremental_indexer.py:472`). Writing `index_meta` there — rather than in the
MCP tool — is what makes CLI and MCP agree. The `.txt` file and its read/write blocks
(`src/MCPServer.py:1175-1198`, `1231-1239`) are removed; the staleness *report* is retained, now sourced
from `index_meta` and honest about failure instead of `except Exception: pass`.

### §6 — `index_status` MCP tool (12th tool)

For interactive agents, **not** for segmem — segmem reads SQL and is forbidden from calling us. Roughly:

```python
@mcp.tool()
def index_status(since: str = "1d") -> str:
    """Report index freshness and which files changed recently."""
```

Returns `last_verified_at`, `last_indexed_commit` vs HEAD, `files_total`, and files with
`content_changed_at > now-since`, with full paths and absolute ISO timestamps (agent-parseable, not
prose). No TUI entry: `src/tui/tools.py` is a presentational menu for a human surface
(`ToolDef` carries `label`/`shortcut`/`Param` placeholders — `src/tui/tools.py:5-19`), and this tool is
agent-facing. The lists differing is a legitimate state, not drift.

### §7 — Document the schema contract

Document the `files` columns and `index_meta` keys as the stable, read-only contract segmem consumes, so
the `codemap` connector can implement `changes_since(folder, ts)` — the named additive seam at
`agent-context-hub-design.md:141-143` — against it. Satisfies #20's fourth criterion.

### Out of scope

- **Per-symbol change stamps** (#20 stretch) — deferred; see *Alternatives*.
- **mtime as a fallback layer** — deferred; see *Alternatives*.
- **Project/repo identity** — ADR-012's. segmem keys by folder path and needs nothing from us.
- **CLI surface** — explicitly deferred by the requester. §1–§4 live in `CodeDB`, so a CLI reader is a
  thin later addition, not a rewrite.

## Consequences

**Better:**
- `WHERE content_changed_at > ?` returns exactly the real change-set after both full and incremental runs
  — unblocks segmem's Phase-2 correlation table, closing #20.
- A from-scratch rebuild reconstructs real history from git instead of claiming the corpus changed today.
- Index freshness becomes readable offline, by a read-only reader, without running the indexer.
- CLI and MCP stop disagreeing about what commit is indexed; a staleness check that has never fired starts
  firing, from the one chokepoint all four triggers share.
- `authored_at` gives an authored-vs-landed throughput signal for free.

**Worse:**
- Two nullable columns, one new table, and a migration on an existing `graph.db`.
- Full rebuilds carry a capture/restore step — more moving parts on the riskiest path, and `run_incremental`'s
  documented crash-safety ordering (`src/incremental_indexer.py:474-485`) must be re-verified, not assumed.
- A git subprocess enters the indexing path. It must degrade to NULL, never raise, on non-git trees.
- `indexed_at` and `content_changed_at` coexisting invites future confusion; §7 documentation is the only
  guard.
- The ~85 ms git pass is measured on a 56-commit repo. `git log --name-only` scales with history; a repo
  with 100k commits will not be 85 ms and may need `--since` bounding.

**Neutral:**
- `indexed_at` semantics are unchanged, so segmem's current query keeps working during rollout.
- Type inconsistency persists across the family: our `indexed_at` is an ISO string, the rust indexer's is
  an epoch float (`project_router\connectors\filesystem.py:49`). New columns follow **this** repo's ISO
  convention; unifying is segmem's call, not ours.
- `schema_version` starts at `"1"` and asserts nothing about the past.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| **Redefine `indexed_at` in place** (what #20's wording implies) | Silently changes the meaning of a column segmem already queries, in a consumer that swallows errors — the breakage would be invisible. Additive columns preserve the existing contract and keep "when did we write this row" as a real debugging signal. |
| **Accept the from-scratch corpus spike; document it** | Cheapest, and rare (embedder swaps only). Rejected: it writes a falsehood into the data, and segmem cannot detect it. Back-dating costs one 85 ms git pass. |
| **Back-date dirty files from git anyway** | Would stamp indexed dirty content with the *committed* version's time, making `changes_since()` miss real recent work — a false negative, worse than the false positive being fixed. |
| **mtime fallback with a >95%-match clone heuristic** | Deferred, and needs rescoping first. Files reaching a fallback are those *without* git history; a clone produces only *tracked* files, which back-date from git and never reach it. Meanwhile `pip install` writes `site-packages` in one burst, so those mtimes cluster tightly — the heuristic would read that as "cloned" and discard the one signal that was approximately honest. It **inverts** on the only case that reaches it. Genuinely useful for a **non-git tree** copied wholesale; scope it there if revisited. |
| **Automated committer-vs-author cross-check** | No correct action exists. "Authored 3 weeks ago, landed today" makes both true, and committer time remains the right answer for recency. A rule firing on ~2% of commits would make `changes_since()` non-deterministic over identical history. Both stored; neither coupled. |
| **Per-symbol `changed_at` (#20 stretch)** | Deferred. Git tracks files, not symbols: back-dating needs `git log -L` per symbol (expensive) or inherits the file's stamp — giving all 601 symbols in a file one identical time, structurally weaker than the file-level feature. It is also ADR-005/ADR-016 turf (provenance versioning, persisted symbol tree). segmem's named seam needs `changed_files` only. |
| **Keep `last_indexed_commit.txt`, just also write it from the CLI** | Fixes the divergence but not the invisibility: segmem's connector opens `graph.db` and cannot see a `.txt` file. The signal would remain addressed to nobody. |
| **Expose freshness via the MCP tool for segmem to call** | Violates segmem's offline/never-run-the-tool invariant (`project_router\docs\ADRs.md:24-28`). The hub polls artifacts; it does not call servers. |
| **Write a JSON manifest into `.code-index/`** | A third artifact to keep in sync, and segmem's `codemap` connector already opens `graph.db` and nothing else. The DB is zero-friction; a manifest needs new connector code. |
| **Wait for ADR-012's project node** | ADR-012 is Wave-3 reach work depending on ADR-008 and ADR-011. Freshness needs no cross-repo identity — segmem keys by folder path. Blocking on it would stall #20 indefinitely. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] **§1** Add `content_changed_at` / `authored_at` (nullable) to the `files` DDL in `src/db.py`; idempotent `ALTER TABLE ... ADD COLUMN` migration (`_migrate_files_freshness`) for existing `graph.db` files.
- [x] **§4** Add the `index_meta` DDL + `meta_get`/`meta_set` accessors in `src/db.py`; seed `schema_version = "1"` via INSERT OR IGNORE (`_seed_index_meta`).
- [x] **§2** Add `git_change_times(repo_path) -> dict[str, tuple[str, str]]` (one `git log` pass, sentinel-prefixed `--format`) in `src/incremental_indexer.py`; returns `{}` on any git failure, never raises.
- [x] **§2** Add `git_dirty_paths()` (`git diff --name-only HEAD`) so rule 2 can fire; git failure → empty set → files fall through to NULL, not `now()`.
- [x] **§2** Wire back-dating via `_content_stamp()` in `run_incremental`'s loop; `upsert_file` leaves `content_changed_at` untouched when the hash is unchanged (early return) — covered by `test_unchanged_leaves_stamp`.
- [x] **§3** Capture `{path → (hash, content_changed_at, authored_at)}` before the `DELETE FROM files` wipe in `src/MCPServer.py reindex()`; restore on hash match after re-ingest.
- [x] **§3** Crash-safety ordering re-verified: capture is a read before the existing wipe transaction; restore is a separate post-reindex transaction. A crash mid-run leaves files absent → re-indexed as `new` next run (unchanged from the documented ordering).
- [x] **§4/§5** Write `last_indexed_commit`, `last_verified_at`, `files_total` from `run_incremental()` via `_write_index_meta`, on every completed run **including the no-op early return**.
- [x] **§5** Retire `last_indexed_commit.txt` (both read + write blocks removed); staleness report now reads `index_meta`; blanket `except Exception: pass` replaced with narrow `FileNotFoundError` (git absent) vs `subprocess.CalledProcessError` (not a repo / bad ref).
- [x] **§1** One-time backfill (`_backfill_null_stamps`) fills legacy NULL rows from the git pass, runs before the no-op check so a quiet repo still backfills; idempotent.
- [x] **§6** Add the `index_status` MCP tool in `src/MCPServer.py`. No `src/tui/tools.py` entry (agent-facing).
- [x] **§7** Documented in `docs/index-schema-contract.md` (the `files` columns + `index_meta` keys + the delta query); notes the ISO-vs-epoch divergence from the rust indexer.
- [x] Tests: `tests/test_index_freshness.py`, 17 tests — full-rebuild neutrality (restore-on-hash-match), dirty→`now()`, untracked→NULL, non-git tree doesn't raise, migration preserves legacy rows, `MAX(indexed_at)` still answers, the `WHERE content_changed_at > ?` delta query, backfill idempotency. All pass; whole 196-test suite green. Pure SQLite + git, no model load.
- [x] Manually verify the MCP server starts cleanly (CONTRIBUTING §5): `import MCPServer` clean with `CUDA_VISIBLE_DEVICES=""`, `index_status` + `reindex` registered, `upsert_file`/git helpers wired. Migration proven on a populated old-schema DB (legacy row survives with NULL stamps).
- [ ] **BLOCKED (GPU):** end-to-end `reindex` run — a real full/incremental reindex embeds files and would pin the GPU (kernel-crash risk on this machine; see `[[feedback-no-gpu-workloads]]`). The DB + git layers are fully unit-verified; the embed path is unchanged by this ADR. Run this once the GPU is usable, or on a CPU-forced box, to confirm timestamps end-to-end.
- [ ] Verify against the real consumer: run segmem's `codemap` connector against a rebuilt `graph.db` and confirm it still produces a rollup (fails **silently** — absence of an error is not a pass). Deferred with the reindex above (needs a populated post-ADR index).
- [ ] Resolve every downstream obligation listed in **Depended on by** (none) before setting status to `accepted`.

**Notes:**
<!-- 2026-07-16: Requester initially asked for timestamps on the vectors themselves. Not possible — FAISS IndexIDMap(IndexFlatIP) holds vectors + int64 IDs, no payload (src/core.py:138-159). Per-file is informationally equivalent since a file's chunks are purged and reinserted as a set. -->
<!-- 2026-07-17: Implemented §1-§7 in code + tests. Two deviations worth recording: (a) modified files (already in the index, hash changed) stamp now() rather than back-dating, matching the ADR's "back-date on first index, stamp-on-change thereafter" — this means the same file's stamp can differ by run type (full rebuild back-dates it from git; incremental stamps now()), but both land in the same "changed since T" window, satisfying #20. (b) The §1 backfill gives legacy dirty files committer time rather than now() — a one-time historical reconciliation; their next real change restamps correctly. (c) End-to-end reindex verification is GPU-blocked and deferred; every layer this ADR actually changes (schema, migration, git helpers, meta, capture/restore SQL) is unit-verified without a model load. -->
<!-- 2026-07-16: Assumed early on that MD5 change detection already made indexed_at content-accurate. Wrong — true for incremental runs, false for the full-rebuild path (DELETE FROM files, src/MCPServer.py:1200-1207), which is exactly what #20 documents. -->
<!-- 2026-07-16: Discovered mid-design that segmem's codemap connector already exists and already queries MAX(indexed_at). The integration is live, not hypothetical, and the DB — not an MCP tool — is the contract. -->
<!-- 2026-07-16: Registry "drift" between MCPServer.py and tui/tools.py was investigated and dismissed — tui/tools.py is a presentational menu, not a second source of truth. -->
<!-- 2026-07-16: segmem's projects.json registers this project as folder `...\indexer\src` (name "src") because discover() anchors on CLAUDE.md, while the codemap connector detects `<folder>/.code-index/graph.db` at the repo root. The registry entry and the harvested index point at different folders. Out of scope here (segmem-side), but confirm before anything folder-scoped is hung off it. -->
