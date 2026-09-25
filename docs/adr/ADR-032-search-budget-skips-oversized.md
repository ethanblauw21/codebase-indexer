# ADR-032: Search Output Skips a Result That Does Not Fit, Instead of Stopping

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-032-search-budget-skips-oversized`
**Reviewer:** @edb
**Backlog:** [B-030](../backlog.md#b-030) — MCP search output stops at the first chunk that does not fit the token budget
**Depends on:** none
**Depended on by:** B-026 Stage 2 (skeletons at the head of a group make large chunks likelier)

## Context

`semantic_code_search` packs up to 15 ranked chunks into a 4,000-token budget. At the first chunk
that did not fit, it appended "Further context truncated" and `break`. One large chunk, such as a
long docstring or a big class-body part, therefore hid every result ranked below it, even when
those would have fit. The caller could not tell what was missing.

## Decision

Packing moves into `_pack_results(header, chunks, count_tokens, max_tokens)` in `MCPServer.py`.
It walks the chunks in rank order, adds each one that fits, and **skips** one that does not
rather than stopping. After the results, a note says how many were left out and lists each one's
`file | scope`, so the caller can open it directly.

The budget (4,000) and the `<` comparison are unchanged. Only `semantic_code_search` had this
pattern; the other `break`s in the file are unrelated.

## Consequences

**Better:** a lower-ranked result that fits is always shown, and a skipped one is named rather
than silently lost.
**Worse:** the output can now include results ranked below one that was skipped, so rank order
in the text has a gap the note has to explain.
**Neutral:** no change to retrieval, ranking or the budget.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Shorten an oversized chunk to its header or signature | Needs a per-language notion of "signature" for raw chunk text; worth doing with B-026's skeletons, not before |
| Raise the budget | Moves the cliff without removing it |

## Implementation Log

- [x] `_pack_results`, skip-and-continue, with a note naming skipped results
- [x] `tests/test_search_budget.py`: an oversized chunk in the middle, nothing skipped, rank order
- [x] MCP Inspector: `semantic_code_search` over stdio (GPU embedder) returns results plus the skipped list; a missing `query` returns `isError: true`
