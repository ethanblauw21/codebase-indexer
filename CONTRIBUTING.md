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

## 4. Working Lists — Backlog, Roadmap, ADRs

Three documents, three jobs, no overlap. **Do not let one hold another's content.**

| Where | Holds | Never holds |
|---|---|---|
| `docs/backlog.md` | **wants** — problems and requests, with a source and a date | how you'll solve them, or ADR build state |
| `docs/roadmap.md` | **order** — sequencing and dependency edges | wants, or build state |
| `docs/adr/` | **decisions** — one committed solution per file, plus its Implementation Log | wants you haven't committed to |

The one-line test: **a backlog item asserts a problem, an ADR asserts a solution you are building.**
A backlog item can sit unresolved forever without lying; an unbuilt ADR is a lie the moment it lands
on `master`.

Three rules follow:

1. **Not every item becomes an ADR.** Most work is just work. An item earns an ADR only when there is a real decision — alternatives, consequences you accept.
2. **One item can become several ADRs.** If shaping an item produces four independently-reversible decisions, that is four ADRs, not one 700-line file.
3. **No ADR without an item behind it.** The backlog is the intake; the ADR is the outcome. An ADR header carries `**Backlog:** B-NNN` back to its origin.

An accepted ADR's open checkboxes are **owned by that ADR** and are never copied into the backlog —
`roadmap.md` may summarise them, but the Implementation Log is the truth for its own build.

> **Adopted 2026-07-27.** The old process had no intake document, so every want was written as a
> `proposed` ADR on `master` and nothing could ever remove one. 19 of 25 ADRs read `proposed`: 6 were
> actually built and 9 had never been started. The ADR set had become the backlog.

## 4.1 Architecture Decision Records (ADRs)

ADRs are required for all Major changes.

- **Location:** `docs/adr/`
- **Template:** `docs/adr/ADR-000-template.md`
- **Numbering:** Sequential. Check existing ADRs and increment. **Numbers are never reused.**
- **Lifecycle — the ADR is born on its branch:**
  1. Cut the feature branch. **Create the ADR in that branch's first commit**, with status `proposed`. Never commit a `proposed` ADR to `master`.
  2. Build. Update the Implementation Log as you go — record deviations, surprises, and in-the-moment decisions.
  3. Set status to `accepted` in the PR. **Reaching `master` means `accepted`.**

  `proposed` is a **branch-only status**. A decision you think about and abandon dies with its branch
  instead of accumulating as a permanent file. If you want to think out loud before committing to
  build, that is a backlog item, not an ADR.

- **Cross-references (REQUIRED for multi-ADR changes):** When one ADR depends on another, the link must be recorded in *both* ADRs via the header fields, and kept in sync in the same PR. A one-directional link is a defect.
  - **Downstream ADR → `Depends on:`** name the upstream ADR and the *exact* artifact/decision/confirmation needed from it, so an implementor knows to **wait** and knows *what for* before starting.
  - **Upstream ADR → `Depended on by:`** name each consumer ADR and what it needs. On completing the upstream implementation, **resolve those obligations** (answer the open questions, confirm the contracts) at that point — while the context is freshest — rather than leaving the downstream implementor to rediscover them later.
  - **An obligation to an unbuilt ADR does not block `accepted`.** Record it as an open checkbox and ship. *(Amended 2026-07-27: the original rule said resolve every obligation "before setting status to `accepted`", which left ADR-011 — merged, tested and measured — stuck at `proposed` because ADR-012, which will likely never be built, had not confirmed a contract. A built thing must not be described as unbuilt.)*
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
