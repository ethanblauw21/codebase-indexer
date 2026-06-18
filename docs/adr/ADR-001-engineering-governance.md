# ADR-001: Engineering Governance Standards

**Status:** accepted
**Date:** 2026-06-11
**Branch:** `chore/governance-scaffolding`
**Reviewer:** @ethanblauw21

## Context

The codebase-indexer project needed a lightweight, enforceable contribution process to keep changes legible and architectural decisions traceable as the codebase grows. Without this, changes accumulate without context, bugs lack root-cause trails, and future contributors (human or AI) have no map of why things are the way they are.

## Decision

Establish a minimal governance layer covering change classification, branch naming, ADRs, bug tracking, feature requests, PR process, and merge strategy. All standards live in `CONTRIBUTING.md` as the single source of truth.

## Consequences

**Better:** Every `src/` change has a traceable decision record. PRs carry enough context to review without chasing the author. AI agents working in this repo have a clear process to follow.
**Worse:** Major changes require upfront ADR writing before implementation begins — small overhead, but real.
**Neutral:** Doc-only and comment-only changes remain zero-process; governance only activates when `src/` is touched.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| No formal process | Works until it doesn't — first time someone asks "why is the retrieval pipeline structured this way?" there's no answer |
| Full GitHub Projects + milestones | Too heavy for a solo/small-team project at this stage |
| Squash-merge only | Loses branch history; harder to bisect retrieval regressions and understand the evolution of the index schema |

## Implementation Log

> This ADR is itself the implementation — all items are complete upon merge.

- [x] `docs/adr/ADR-000-template.md` — canonical ADR template
- [x] `.github/ISSUE_TEMPLATE/bug_report.yml` — structured bug report form
- [x] `.github/ISSUE_TEMPLATE/feature_request.yml` — structured feature request form
- [x] `.github/pull_request_template.md` — PR template with project-accurate checklist
- [x] `CONTRIBUTING.md` — single source of truth for all contribution standards
- [x] `src/CLAUDE.md` updated — reference to CONTRIBUTING.md and /grill-plan added

**Notes:**
<!-- 2026-06-11: Initial governance scaffolding. Minor/Major split defined around src/ changes rather than "presentation layer" since this is a backend Python tool with no UI layer. -->
<!-- 2026-06-18: Extended the ADR standard with a bidirectional cross-reference rule for multi-ADR changes (CONTRIBUTING.md §4 + ADR-000-template.md "Depends on" / "Depended on by" fields). Rationale: downstream ADRs can sequence/wait correctly, and upstream ADRs resolve downstream obligations at completion time (freshest context) instead of forcing rediscovery. Motivated by the docs/adr-backlog.md multi-ADR plan. -->
