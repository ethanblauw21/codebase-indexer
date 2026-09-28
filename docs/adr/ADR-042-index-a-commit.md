# ADR-042: Index a Commit, Not a Folder

**Status:** proposed
**Date:** 2026-09-28
**Branch:** `feature/adr-042-index-a-commit`
**Reviewer:** @edb
**Backlog:** [B-052](../backlog.md#b-052) option 4 (the index covers one folder, so worktree work isn't indexed) · [B-054](../backlog.md#b-054) (line-ending-only changes count as changes; this mode doesn't have that problem)
**Depends on:** [ADR-038](./ADR-038-one-writer-and-one-watchdog-per-index.md) (the writer and watch locks, which move with the index), [ADR-025](./ADR-025-freshness-metadata.md) (freshness stamps, redefined here for a commit)
**Depended on by:** a later overlay ADR (B-052 option 3), reserved in §8.

## Context

Go-live on InventoryApp-V2 (2026-09-28) showed that the indexer indexes a **folder**. It covers
whatever is checked out in one working directory:
- The indexed folder was on `fix/closed-jobs-sync`, 55 commits behind `main`. Forking the index onto
  a clean `main` reprocessed 239 files: 88 new, 124 changed, and 27 that differed only in line
  endings.
- The workaround is a second, detached `origin/main` worktree (`InventoryApp-index`), with a
  launcher that `chdir`s into it, an opt-in line in its `indexer.toml`, and manual
  `fetch` + `checkout --detach` to move it. @edb: a separate folder needing separate upkeep is
  annoying.
- InventoryApp runs 27 worktrees. Sessions in them get no index at all.

Everything the build reads comes from the folder. Searches read SQLite only.

| Build step | Reads the folder through |
|---|---|
| Scan and diff | `scan_disk`: `os.walk` plus a raw-byte MD5 per file (`incremental_indexer.py:141`) |
| Summarize pass, embed pass | `open(repo_path/rel)` (lines 872 and 1121) |
| Import edges | `import_resolver`: reads `tsconfig.json` and barrel files, calls `isfile` to infer extensions |
| Freshness | `git log` / `git diff HEAD` (dirty paths); `last_indexed_commit` = `HEAD` |

## Decision

Decisions taken with @edb on 2026-09-28: working-tree edits are **ignored for now, with an overlay
reserved** (§8); the index lives in the **git common directory**; it **follows the local ref**, and
the server never fetches; the content hash is the **git blob SHA**.

### 1. A source layer with two implementations

A `Source` gives the build three things: `list() -> {rel_path: content_hash}`,
`read(rel_path) -> str` and `exists(rel_path) -> bool`. Every read in the table above goes through it.

- **`WorkingTreeSource`** is today's behavior, unchanged, and stays the default. This repo and every
  other current user see no difference.
- **`GitCommitSource(ref)`** resolves the ref to a commit X once per run, so the whole run sees one
  tree even if the ref moves partway through.
  - `list()`: `git ls-tree -r -z X`. It returns every path with its blob SHA, and the SHA is the
    content hash, so scanning and diffing read no file contents. It keeps regular files only (mode
    `100644`/`100755`); symlinks (`120000`) and submodules (`160000`) are skipped. The `[ignore]`
    policy (`scan_policy.is_scannable`) filters paths exactly as it does today. Untracked and
    gitignored files never appear, which suits an index of `main`.
  - `read()`: one long-lived `git cat-file --batch` process per run. Contents are decoded as UTF-8
    with `errors="ignore"` and `\r\n` becomes `\n`, the same as the text-mode `open` used today.
    Git LFS pointer files are skipped.
  - `exists()`: membership in the `ls-tree` listing.

### 2. Configuration

`[indexer] source = "worktree"` (default, drift-tested) or `"git:<ref>"`, for example
`"git:origin/main"`. `index_meta` records `source`. A run whose configured source differs from the
recorded one says so, as the `embed_layout` check does (ADR-030 §5), and then proceeds. Every content
hash differs between the two modes, so the first run after switching re-embeds every file. Summaries
come from the cache: the chunker already reads in text mode, so chunk text has no line-ending
differences. Expected cost on InventoryApp: about 5–7 min on the GPU, not 35.

**Where a linked worktree finds its config:** `indexer.toml` is usually untracked (InventoryApp
lists it in `.git/info/exclude`), so a linked worktree doesn't have one. When the walk up from the
working directory finds none, config lookup falls back to the main worktree's root
(`git rev-parse --git-common-dir`, then its parent).

### 3. The index lives in the git common directory

In git mode the index directory is `<git-common-dir>/code-index/` (for example
`InventoryApp-V2/.git/code-index/`). There is one per repository, and it is the same directory from
the main folder and from every linked worktree. Git ignores unknown directories under `.git`.

This needs a real resolver: `.code-index` is spelled out in 11 places across 7 `src` modules
(`core`, `db`, `incremental_indexer`, `MCPServer`, `hybrid_retriever`, `scan_policy`,
`graph_viz`). They move to one `index_dir()` that returns `.code-index` in worktree mode and the
common-directory path in git mode. That also wires `[indexer] index_dir`, which `KNOWN_INERT` in
`test_config_drift.py` lists today.

### 4. Updates follow the local ref

The server holding `watch.lock` (ADR-038) runs no file watcher in git mode. Instead it polls
`git rev-parse <ref>` every `ref_poll_s` (default 60 s; a local call of about 10 ms). When the ref
moves from X to Y, it runs an ordinary incremental under `write.lock`: `list()` at Y against the
`files` table gives new, modified and deleted by blob SHA. Nothing touches the network. The index
follows `origin/main` as soon as anything on the machine fetches.

Checkouts, branch switches and edits in V2 no longer trigger any work.

### 5. Freshness is about the commit, not HEAD

- `last_indexed_commit` = X, the resolved ref, not `HEAD`.
- `content_changed_at` / `authored_at` come from `git log X`. There are no dirty paths.
- `index_status` reports the ref's current commit against X ("index current" or "N commits
  behind"). It also reports how the caller's `HEAD` relates to X, with the exact command for listing
  the files to `Read`: `git diff --name-only X...HEAD`.
- The server `instructions` (ADR-038 §6) name the ref and X instead of a folder.

### 6. Linked worktrees read and may maintain the index

ADR-038 refuses writes from linked worktrees because in worktree mode each folder's index is
different work. In git mode the work doesn't depend on the folder. Any session's server computes the
same update from the same commit, and `write.lock` / `watch.lock` sit in the shared index directory,
so N sessions across any number of worktrees still produce one update. **The worktree refusal
therefore applies to worktree mode only.** @edb's requirement, that parallel worktree sessions must
not queue duplicate work, holds by construction.

### 7. The import resolver reads through the source

`ImportResolver` takes the `Source` and calls `exists()` / `read()` instead of `os.path.isfile` and
`open`. Its path arithmetic stays the same, done on repo-relative paths. `.csproj` / `.sln` parsing
already receives content as a string.

### 8. Reserved for the overlay (not built here)

A later ADR may index a branch's working-tree differences from X into a small per-worktree overlay
and merge it with the base at query time (B-052 option 3). To keep that possible:
- The base index stays self-contained and records X in `index_meta`. An overlay records the X it
  was diffed against.
- Retrieval will need "hide base hits for paths the overlay contains". `stable_id` doesn't encode
  the source, so the overlay needs its own FAISS files, not shared ones.
- Nothing in this ADR stores working-tree state in the base index.

## Consequences

- **InventoryApp needs no second folder.** `indexer.toml` in V2 gets `source = "git:origin/main"`.
  The existing index moves to `.git/code-index/` (or is rebuilt), and the launcher, the opt-in line
  and `InventoryApp-index` are all retired.
- **Every worktree session can search the index**, provided it loads the server. `.mcp.json` is
  untracked, so worktrees don't have it. Rollout needs either a user-scope registration that serves
  whichever repo the session is in (and has no index to offer outside indexed repos) or a copy in
  each worktree. Decided at rollout; out of this ADR's code.
- Scans get faster: one `ls-tree` instead of hashing every file.
- B-054 disappears in git mode, since blob SHAs don't depend on the checkout's line endings. It
  still applies to worktree mode.
- Agents still `Read` the files their branch changed. The index shows `main` until the overlay
  exists. The instructions and `index_status` hand them the exact list.
- Costs: a subprocess-backed reader to maintain, and the `index_dir()` refactor across 7 modules.
- The default stays worktree mode, so this repo's dogfood index, CI and the eval harness are
  unaffected.

## Alternatives Considered

- **Keep the `InventoryApp-index` worktree** (today's workaround). It works, but the extra folder
  needs manual upkeep and worktree sessions get nothing.
- **A git checkout into a hidden cache folder** (`git worktree add` managed by the indexer). It
  keeps the folder-reading code, but the folder is still there, still needs moving, and still ends
  up as CRLF under `autocrlf`. Reading objects directly is simpler once the source layer exists.
- **MD5 of normalized content in both modes.** One hash scheme would fix B-054 for worktree mode
  too, but every scan would read every blob. The blob SHA is free, and B-054 can be fixed separately
  for worktree mode.
- **The server runs `git fetch` on a timer.** It would track the remote with no help, but it's
  network activity from a background process, and credential prompts can hang it (ADR-036's stdin
  lesson). Rejected in favor of following the local ref.
- **The index in `V2/.code-index`.** The same as today for the main folder, but worktree sessions
  couldn't find it.

## Implementation Plan

1. `Source` protocol, `WorkingTreeSource` (a pure refactor; the whole suite passes unchanged), and
   `index_dir()` replacing the 11 hard-coded spellings.
2. `GitCommitSource` plus `[indexer] source`, config fallback for linked worktrees, and `index_meta`
   `source`. Tests use a real temporary repo: add, modify, delete, rename, a CRLF blob, a symlink, a
   submodule entry, an LFS pointer.
3. Freshness (§5) and `index_status` / instructions wording.
4. The server: ref poller instead of the file watcher in git mode, and locks in the shared directory.
   Worktree refusal applies to worktree mode only.
5. Rollout on InventoryApp: switch the config, move or seed the index, measure the one-time
   re-embed, retire `InventoryApp-index`. MCP Inspector checks on the changed tools.

## Implementation Log

(empty)
