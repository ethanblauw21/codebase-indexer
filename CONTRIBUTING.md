# Contributing to codebase-indexer

## 1. Change Classification

Before starting any work, classify your change. This determines how much process applies.

| Classification | Definition | Process |
|---------------|-----------|---------|
| **Minor** | Docs, comments, README, CLAUDE.md, ADR prose — no changes to `src/` | Commit directly to `master`. No ADR, no issue, no PR required. |
| **Major** | Any change to `src/` — new or modified MCP tools, retrieval logic, indexing pipeline, AST chunking, graph schema, TUI, or project config | Branch required. ADR required. PR required. |

When in doubt, treat it as Major.

## 2. Branch Naming

| Pattern | When to use |
|--------|-------------|
| `feature/adr-XXX-short-name` | New feature tied to an ADR |
| `fix/issue-XXX-short-name` | Bug fix tied to a GitHub issue |
| `fix/short-name` | Minor bug fix with no issue |
| `chore/short-name` | Tooling, deps, config, governance |

## 3. Bug Reporting

**Non-trivial bugs** (root cause unknown, or fix touches shared indexing/retrieval code) must be filed as a GitHub issue using the Bug Report template before a fix is started. Include root cause hypothesis and proposed solution if known.

**Minor bugs** (obvious one-liner fix, isolated to a single function, no shared code touched) can be fixed directly with a descriptive commit message.

## 4. Architecture Decision Records (ADRs)

ADRs are required for all Major changes.

- **Location:** `docs/adr/`
- **Template:** `docs/adr/ADR-000-template.md`
- **Numbering:** Sequential. Check existing ADRs and increment.
- **Lifecycle:**
  1. Write the ADR before implementation begins. Commit it to `master` with status `proposed`.
  2. Work on the feature branch. Update the Implementation Log as you go — record deviations, surprises, and in-the-moment decisions.
  3. Update status to `accepted` and commit the final ADR as part of the PR.
- **AI agents:** Use `/grill-plan` to draft an ADR before implementing non-trivial features.

## 5. Pull Requests

- Use the PR template (`.github/pull_request_template.md`).
- **Merge strategy:** Merge commits only. No squash, no rebase. Branch history is preserved.
- Manually verify the MCP server starts cleanly and `reindex` runs without error before requesting review.
- Update the ADR Implementation Log before merging (Major changes only).

## 6. Commit Messages

Imperative mood, present tense. Describe what the commit does, not what you did.

```
Add hybrid reranker fallback when CrossEncoder is unavailable
Fix FAISS ID collision on files with identical content
Update MCPServer tool docstrings for AI consumption clarity
```

## 7. Git Hooks

Commit tagging is handled by `.githooks/commit-msg`. Activate it once per clone:

```
git config core.hooksPath .githooks
```

On ADR branches (`feature/adr-XXX-*`), the hook appends `[ADR-XXX src/file.py]` to each commit message — sourcing the ADR number from the branch name and the staged `src/` files from the diff. The hook is a no-op on non-ADR branches and skips if the tag is already present.

To retrieve the full commit trail for any ADR:

```
git log --oneline --grep='\[ADR-003\]'
```
