# ADR-XXX: [Short Title]

> **Create this file in the first commit of the branch that builds it** — never on `master`
> beforehand. `proposed` is a branch-only status; reaching `master` means `accepted`. If you are not
> building it yet, it is a `docs/backlog.md` item. See CONTRIBUTING §4.

**Status:** proposed | accepted | superseded by ADR-XXX | deprecated
**Date:** YYYY-MM-DD
**Branch:** `feature/short-description`
**Reviewer:** @username
**Backlog:** B-NNN — *the want this came from (`docs/backlog.md`)* | none
**Depends on:** none | ADR-XXX — *what this ADR needs from it (the specific artifact, decision, or confirmation)*
**Depended on by:** none yet | ADR-XXX — *what that ADR needs from this one (resolve at completion — see CONTRIBUTING §4.1)*

## Context

What situation or problem prompted this decision? Include any constraints, prior art, or relevant system state.

## Decision

What did we decide to do? State it directly.

## Consequences

**Better:** what this enables or improves.
**Worse:** what gets harder, slower, or more complex as a result.
**Neutral:** notable side effects that are neither good nor bad.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Option A | brief reason |
| Option B | brief reason |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Task or milestone one
- [ ] Task or milestone two
- [ ] Resolve the downstream obligations listed in **Depended on by** (answer/confirm what each consumer ADR needs) while the context is fresh. An obligation owed to an **unbuilt** ADR does not block `accepted` — leave it as an open checkbox and ship.

**Notes:**
<!-- Add dated comments as you go -->
<!-- 2026-06-11: Discovered X assumption was wrong; pivoted to Y approach instead -->
