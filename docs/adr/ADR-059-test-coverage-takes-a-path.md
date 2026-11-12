# ADR-059: `find_test_coverage` Takes a Repo-Relative Path, and the Folder Narrows the Direct Tier

**Status:** proposed
**Date:** 2026-11-12
**Branch:** `feature/adr-059-test-coverage-path`
**Reviewer:** @edb
**Backlog:** [B-074](../backlog.md#b-074)
**Depends on:** none
**Depended on by:** none yet

## Context

`find_test_coverage` reduced `source_file` to its last path segment, and Tier 1 ("direct") then
accepted any indexed test whose own name was `<base><test suffix>`, wherever it sat. In a Next.js
app every route folder has a `page.tsx`, so `app/orders/page.tsx` reported every `page.test.tsx` in
the repo as direct coverage, and so did `app/users/page.tsx`. The docstring asked for a filename;
the code accepted a path and dropped the folder silently.

## Decision

- `source_file` may be a repo-relative path (`./`, backslashes and an absolute path under the
  server's folder are normalised). **A bare filename keeps today's behaviour exactly.**
- With a folder, a name-matched test counts as direct only when its folder relates to the source's
  (`_test_dir_relates()`, next to `_get_test_patterns()`):
  - the same folder (`app/orders/page.test.tsx`);
  - a test folder beside it: `__tests__`, `tests`, `test` or `spec` (`app/orders/__tests__/`);
  - a top-level test tree, either flat (`tests/test_scan_policy.py` for `src/scan_policy.py`) or
    mirroring the source's trailing folders (`tests/orders/`, `tests/app/orders/`).
- Same-named tests in unrelated folders are **listed, not counted**, on one line under the direct
  tier. C# keeps tests in a sibling project (`MyApp.Tests/…`), which no folder rule above relates;
  listing them keeps that answer visible without calling it direct.
- The docstring says a path is accepted and why to prefer it.

## Consequences

**Better:** a path gives a per-folder answer in repos full of same-named files.
**Worse:** a caller passing a path whose tests live in an unrelated tree (C# test projects, a
`tests/unit/` split) loses them from the direct tier; they show on the "not counted" line, and a
bare name still finds them as before.
**Neutral:** the semantic tier is unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Rank direct matches by folder distance, keep all | still reports `app/users/page.test.tsx` as direct for `app/orders/page.tsx` |
| Per-language folder rules in each adapter's `test_conventions()` | the relations above are language-neutral; no adapter needs a different one yet |
| Drop unrelated same-named tests silently | hides C# test projects from a path caller with no hint |

## Implementation Log

- [x] `_test_dir_relates()` and path normalisation in `find_test_coverage`.
- [x] Tests in `tests/test_find_test_coverage.py`: two `page.tsx`, one beside test; bare name lists
      both; the folder relation table; `./` and backslash paths. The four existing tests pass unchanged.
- [x] MCP Inspector `tools/call` with a path and a bare name.

**Notes:**
