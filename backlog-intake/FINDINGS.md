# backlog-intake — findings

How this repo files wants: `docs/backlog.md` (index table row + a `### B-NNN — title` section with
Source / Status / Size, Want, Done when, Related). Wants only; ADRs are born on their build branch
(`CONTRIBUTING.md` §4.1). Check `git branch -r | grep backlog` and the table tail for the next free ID
before numbering: a filed-but-unmerged branch can hold the next ID (B-072 did on 2026-11-12).

**Notes location caveat (2026-11-12):** a resident that has entered a worktree cannot write to the
shared checkout (the harness rejects it), and the notes folder IS the shared checkout. These two files
were written in the worktree `.claude/worktrees/backlog-pr-agent-feedback` instead, uncommitted. If the
next resident finds no `RESIDENT-NOTES.md` at the repo root, look there, or in `git worktree list`.

## 2026-11-12 — claude-conductor-3e-1747 (from claude-conductor-3e): PR-agent install feedback

Ask: file six points from a session that wired codebase-indexer into a local PR agent on another PC
(@edb: "Pass those on to sessions in those projects"). Don't build, don't push.

Answer: filed as **B-073..B-078**, one commit `30663fb` on local branch `docs/backlog-pr-agent-feedback`
(worktree `.claude/worktrees/backlog-pr-agent-feedback`, based on master a195bf9, which already had
B-072 via #100). Not pushed, no PR.

| ID | Point | Verified by reading |
|---|---|---|
| B-073 | `format="json"` on `semantic_code_search` / `find_test_coverage` | header at `MCPServer.py:393`; open question: other tools too? |
| B-074 | `find_test_coverage` basename-only | `MCPServer.py:1594` splits to the last segment; Tier 1 matches by basename |
| B-075 | split `MCPServer.py` | 3,540 lines, 17 `@_tool()`; 20+ test files mention it; entry point `MCPServer:main` |
| B-076 | stale comments, `jina_tokenizer` | `MCPServer.py:369-371`, `core.py:361-362`; name at `core.py:170`, 22 refs in 10 files; tokenizer follows `embed_model_id()` |
| B-077 | minimal per-repo `indexer.toml` | `[eval]` = lines 195-258, read by `tools/coir_eval.py` not `src/config.py`; `test_config_drift.py` has `_EVAL_KEYS_OWNED_BY_TOOLS` |
| B-078 | first-install weight | ~3 GB embedder + ~3 GB summarizer, reranker off by default; `summarization.enabled = true` shipped; ties to B-065/ADR-030 |

Not run: nothing was executed, all by code reading. The "summarizer doubles the first build" figure is
the asker's estimate (they hadn't run a build).

## 2026-11-12 — claude-conductor-3e-1804 (from claude-conductor-3e): push + PR, B-073 leaning

Ask: @edb said "1: Yes." (push the branch, open a PR) and, on B-073's open question, "It should
probably give json output for other tools too." Amend B-073, commit, push, open a PR, don't merge.

Answer: new commit `7134abb` on `docs/backlog-pr-agent-feedback` (on top of `30663fb`): B-073 now records
the leaning as "probably", not a ruling; which tools (all 17 or the lookup/search ones) is left to the
builder; schema cost kept as "cost to weigh" (one short shared `format` description, measure `tools/list`
before/after); Done-when covers every tool that takes the argument; Size S–M → M (index row and section).
Pushed, **PR #101** https://github.com/ethanblauw21/codebase-indexer/pull/101 (open, not merged, mergeable).

Gotchas hit:
- Push needed `gh auth switch --user ethanblauw21` first (remote URL names it), then
  `GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c credential.helper='!gh auth git-credential' push -u origin <branch>`,
  then switch back to `ethanblauw-hub` (was the active account at the start; restored). A worktree-isolated
  session refuses the one-line chain (`switch && git … push; switch`), so run them as separate Bash calls.
- Entering the worktree by `path` works for an existing one (`EnterWorktree path=…`).
- Entries B-070+ use no `<a id>` anchors (auto heading anchors); don't add them.
