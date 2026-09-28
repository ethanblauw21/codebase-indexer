# Handoff — codebase-indexer — 2026-09-28

**Read this first, then `CLAUDE.md`.** Everything here is the delta between the repo and what the
last session knew. Where this doc and the code disagree, the code wins. Flag it and move on.

## Where things stand

The indexer went live on @edb's real projects on 2026-09-28, both in git mode (ADR-042):
- **InventoryApp-V2:** `source = "git:origin/main"`, 479 files.
- **GanttWebApp:** `source = "git:refs/code-index/target"`. That is a local alias for the current
  `origin/integration/staging-<date>` branch. It is re-pointed each staging cycle and never pushed.
  269 files, `_ds` ignored.

Each project's `CLAUDE.local.md` (untracked) holds its usage rules and upkeep commands.

**Tree state:**
- `master` at 89c325f. #73–#78 are merged.
- The open branch `chore/cleanup-and-docs` holds the triage cleanup: #58, #60, #61, #64, #65, plus a
  packaging fix.
- Suite 645 passed and flake8 is clean on the branch; MCP Inspector `tools/list --strict` exits 0 with 14 tools.

## What happened this session

- **ADR-042 (#76): index a commit, not a folder.**
  - The index lives in `<git common dir>/code-index` and is shared by every worktree.
  - A ref poller follows the ref, and the content hash is the git blob SHA.
- **#77, tool output:**
  - `investigate_architecture` evidence is now a focused, line-numbered snippet instead of the
    first 200 characters.
  - Honest `semantic_code_search` and `reindex` descriptions.
  - `index_status` caps its recent list at 20 (`limit=0` for all).
  - Content dates are kept when an index switches between worktree and git mode.
  - An alias ref is shown with its target.
- **#78 (B-055): pass 2 overlaps embedding with preparing the next window.**
  - `[indexer] embed_overlap`, default true.
  - The build prints a `Pass 2 timing` line.
  - `tools/pass2_bench.py` times three arms: `old`, `b050` and `b055`.
- **Triage of the 21 open issues.** All were still true on master. Next are #55, #54 and #52/#53,
  and #47/#48 are up for discussion.

## Next steps

1. **Bench done** (ADR-040 Measurement): pass 2 334 s → 266 s (B-050) → 194 s (B-055), −42%; now GPU-bound.
2. **Review and merge `chore/cleanup-and-docs`.**
3. **Talk through #47 (mutation gate) and #48 (branch protection).** Both are @edb's decisions.
4. **Then #55 + #54 + small fixes, and #52 + #53** (index swap mid-call, sync tools blocking the
   event loop). Both matter more now that the watchdog swaps the index on every fetch.
5. **Waiting on @edb:**
   - remove the `InventoryApp-index` worktree and V2's old `.code-index`
   - delete merged worktrees (`indexer-adr042`, `indexer-ci`, `indexer-polish`, `indexer-overlap`,
     and the agent worktrees under `.claude/worktrees`); ask before deleting branches
   - delete the remote branch `fix/conformance-eval-ntpath-basename` (@edb runs remote deletes)
   - a user-scope server registration, so sessions in `wt-*` / `GanttWebApp-wg*` worktrees load
     the indexer
   - SOPCentral: still on the CPU-only VectorEnv registration, with a half-built CPU index

## Critical context

- **The summarizer is on, deliberately.** ADR-030's separate summary index measurably helps
  whole-file questions (+0.24). If summaries look like the cause of a slowdown, the lever is the
  model host, not a switch.
- **The GPU works.** `CUDA_VISIBLE_DEVICES` is unset. @edb runs GPU builds and benchmarks in their
  own terminal; don't start one from the agent.
- **Only the terminal starts a first build.** The watchdog never starts one. After deleting an index,
  sessions do nothing until `code-indexer` finishes.
- **"dropped N chunk(s) with a duplicate scope"** lines in a build are informational (B-028). A test
  file that redeclares a helper by the same name keeps only the last copy's tier-1 chunk.
- **Pass 1 on a big project looks stalled but isn't.** Slices go longest first, so the first batches
  take minutes each. Check the NVIDIA "shared usage" counter for WDDM paging before blaming the model.
- **In a Bash heredoc on this machine, backslashes and `\n` get mangled.** Write a scratch `.py` with
  the Write tool, or use Edit.
- **`gh`: only ethanblauw21 can push.** Switch to it, push with
  `git -c credential.helper='!gh auth git-credential' push`, and switch back to the account that was
  active before.

## Landmines

- **`gpu-crash-repro/` is untracked and not gitignored.** It holds the raw telemetry behind several
  PRs, and it contains system logs and the machine's service tag. Don't delete it, and don't push it.
- **`Egan_LX_files/` is customer code.** Scripts may run against it; never read it or echo
  identifiers from it.
- **Windows PowerShell 5.1 decodes piped native output with the console code page.** Set
  `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` before piping the indexer's output,
  or `—` and `→` turn into mojibake.
