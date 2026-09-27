# Handoff — codebase-indexer — 2026-09-25 (evening)

**Read this first, then `CLAUDE.md`.** Everything here is the delta between the repo and what the
last session knew. Where this doc and the code disagree, the code wins. Flag it and move on.

The earlier handoff from today (summarizer / ADR-027–035 work) was never committed. It is kept
beside this file as `HANDOFF-2026-09-25-summarizer.md`. Two older ones sit beside it too.

## Where things stand

The user's top priority is the **code indexer running live with the watchdog daemon**, for their
TypeScript and Apps Script work. The daemon now works end to end, but four PRs hold the pieces and
**none is merged**. The embedder and chunk shape are settled by a 21-arm study. Two small blockers
stand between this and handing the repo to anyone else: B-036 (mcp 2.x) and CI that cannot fail on
tests.

**Tree state:**
- **This checkout:** `feature/adr-028-central-model-host`, clean apart from the untracked files that
  were here before (`Egan_LX_files/` is customer code, never read it).
- **Pushed:** all four PR branches, 0 unpushed.
- **Suite:** 408 passed on this branch, run at the end of the session.
- **Combined tree:** #40 + #41 + #42 + #43 merged onto master in a scratch worktree merge cleanly.
  The suite there was not re-run after the ADR-036 test fix below.

| PR | Branch (worktree) | What |
|---|---|---|
| #40 | `feature/adr-036-one-reindex-at-a-time` (`../indexer-036`) | daemon fixes: one reindex at a time, cp1252 stdout, stdin-pipe hang; per-save timing |
| #41 | `feature/adr-028-central-model-host` (this checkout) | model host, off by default; leak fix; the summary-hold fix c6352f1 |
| #42 | `docs/chunk-shape-study` (`../indexer-study`) | the study, its data, and backlog B-027/B-010/B-034 updates plus **new B-035 and B-036** |
| #43 | `feature/adr-037-heal-missing-vectors` (`../indexer-037`) | B-035 fix: every run reconciles FAISS ids with chunk rows |

## What happened this session

- **ADR-036 (#40): the watchdog had never worked under Claude Code on Windows.**
  - Every save crashed on a `━━` print to a cp1252 stdout.
  - With summaries on, the summarizer's worker hung on the MCP stdin pipe.
  - Both are fixed, and the fix was verified end to end over a real stdio launch.
- **Per-save timing** (the daemon baseline's step 4): a save is searchable **14–22 s** later on
  p-queue, with summaries on and models in-process.
- **ADR-028 host (#41), leak:** unloading left 3.5 GB pinned by cuBLAS workspaces (7508ead); an idle
  host now holds 119 MiB.
- **ADR-028 host (#41), timing:** the first daemon timing through the host gave 61 s per save. Each
  save's summaries held ~45 s behind the previous save's own index embeds. Fixed in c6352f1: only a
  `query` embed holds summaries. After the fix, the host **ties** in-process at 14–22 s, and does
  not beat it. With one model on the card at a time, each save still swaps the summarizer in, then
  the embedder. The user said: *"I am completely fine with the speed for now."*
- **Study (#42), 21 arms: nothing beats the shipped setup.**
  - bge-code-v1 bf16 with its query instruction stays. Qwen3-0.6B, jina-1.5b, CodeRank and C2LLM all
    lose intent by 0.11–0.30.
  - fp32 is no better than bf16.
  - B-027's outline-per-file is rejected (whole-file −0.24).
  - Both slice tiers matter, and so does the 500-token tier-1 cap.
- **ADR-037 (#43), B-035:** a real GPU run killed partway, then resumed. master ended at 24 of 94
  tier-1 vectors, the branch at 94/94. It also caught a second bug: a killed *modify* duplicated
  vectors.
- **707f709 (#40):** ADR-036's own test file had a syntax error from commit 50eeecc. A shell heredoc
  turned `\n` in `b'...\n'` into a real newline. Fixed and pushed.

## Next steps

1. **Fix B-036 before pointing anyone at the repo.** It's on #42's branch, in `docs/backlog.md`.
   - Pin `mcp[cli]>=1.28,<2` in both `pyproject.toml` and `requirements.txt`.
   - Add a *blocking* `pytest --co -q` step to `.github/workflows/ci.yml`.
   - Done means: a CI run on the branch shows pytest actually collecting and running tests (it has
     run **zero** on master; see Critical context), and `Test + Mutate` goes red if collection fails.
   - Small. It's its own branch/ADR per the repo's process, or a waiver if the user prefers.
2. **Get the user to review and merge #40 → #43 → #42, then #41 if wanted.** They merge cleanly in
   any order; that was checked. After #40 and #43 are on master, re-run the full suite there.
3. **Go live** once #40 and #43 are merged:
   - register the MCP server for the user's TS / Apps Script project;
   - run a first full index;
   - count `chunk_summaries` rows, not the "done" line.
   - `.gs` files aren't mapped in `src/adapters/__init__.py`; only clasp's `.js` output indexes. Ask
     the user which they use.
4. **Deferred, not started:**
   - B-033: two MCP servers writing one index; atomic FAISS saves.
   - B-034: re-embedding unchanged chunks.
   - Both-models-resident host: unmeasured, and it would reverse ADR-028 Decision 1.

## Critical context

- **CI's green checks don't mean the tests passed.**
  - The `Run pytest` step has `continue-on-error: true` (deliberate, ADR-004) and pipes into `tee`
    under `bash -e` with no pipefail.
  - Master's run 36191999364 shows `Interrupted: 2 errors during collection` (mcp 2.x on CI) with a
    green job. #40's run on 00c2ccd had a third error (a SyntaxError) and was green too.
  - **Until B-036 lands, verify tests locally. Never read the checks.**
- **The model host is not a speed feature.** Its value is sharing the 8 GB card between Claude Code
  sessions. Don't re-run the timing hoping for a win without changing the one-model rule.
- **Don't re-propose** an embedder swap, outline chunks, dropping a slice tier, a different tier-1
  cap, or rewording the query instruction. All are measured in #42's study doc.
- **Before trusting any eval number, check that vectors equal chunk rows.** A killed build faked a
  −0.21 fp32 "regression" during the study. `index_status` shows the counts.
- **In a Bash heredoc on this machine, `\n` inside a string becomes a real newline.** It broke
  `shape_study.py` once and the ADR-036 test once (it reached a pushed commit). Write files that
  contain `\n` with the Edit or Write tool.
- **Found by MCP Inspector, not fixed, not filed** (both recorded in ADR-037's notes):
  - no tool declares `readOnlyHint`;
  - `index_status(since="garbage")` silently reports 0 changed files. `_parse_since` passes it
    through as a text cutoff.
- **`gh`:** only `ethanblauw21` can push. The session switched to it for every push. See
  `reference_github_push_from_agent_shell` in memory.

## Verified this session

- #43 heals a real killed run: master 24/94 against the branch's 94/94 tier-1 vectors.
  `[checked: gpu-crash-repro/kill_heal_e2e.py; recount of telemetry/kill_heal/*/index]`
- Per-save timing: in-process 14.6/13.6/21.7 s; host 61.7/61.3/68.2 s before c6352f1 and
  14.7/14.0/22.1 s after. `[checked: gpu-crash-repro/telemetry/watchdog_timing_*.json]`
- The four PRs merge cleanly onto master. `[checked: sequential git merge in ../indexer-daemon]`
- CI never ran tests on master. `[checked: gh run view 36191999364 job log]`
- The local mcp is 1.28.1; the latest release is 2.2.0. `[checked: pip show mcp; pip index versions mcp]`
- Suite: 408 on this branch; 389 plus the 6 known snapshot failures on #43's worktree; 5/5
  `test_reindex_serial.py` after 707f709. `[checked: pytest, this session]`

## Assumed, NOT verified

- **That pinning `mcp<2` is all CI needs to collect tests.** Only the first error in each file was
  read. Confirm with a CI run after the pin.
- **That the full suite passes on master + all four PRs.** The combined tree was last run before
  707f709 and stopped at that collection error. Re-run after merging.
- **Where the extra ~8 s per save on the large file goes** (tier-1 re-embed vs the longer tier-3
  summary). The daemon log has no timestamps. B-034 is the likely lever.

## Landmines

- **`../indexer-daemon`** is a detached scratch worktree holding a local merge of all four PRs. Don't
  commit from it. Remove it with `git worktree remove --force ../indexer-daemon` when done. The same
  goes for `../indexer-master` and `../indexer-034base`, detached checkouts the study used.
- **`gpu-crash-repro/` is untracked**, and the study harness, timing scripts and all telemetry live
  there. Deleting it loses every raw number behind today's PR text. The study's own results are
  committed in #42.
- **The model host outlives its client** (`idle_exit_s` 1800). Timing runs kill it by command line
  (`*model_host*`). Check for a stray `python.exe` with `model_host` in its command line before
  reading GPU memory. None was running at the end of this session.
- **`CUDA_VISIBLE_DEVICES=-1` is set at User scope on purpose.** Unset it per command
  (`env -u CUDA_VISIBLE_DEVICES`), never globally.
