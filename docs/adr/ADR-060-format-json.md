# ADR-060: An Optional `format="json"` on the Search and Lookup Tools

**Status:** proposed
**Date:** 2026-11-12
**Branch:** `feature/adr-060-json-format`
**Reviewer:** @edb
**Backlog:** [B-073](../backlog.md#b-073)
**Depends on:** [ADR-059](./ADR-059-test-coverage-takes-a-path.md) (branched from it: both change `find_test_coverage`)
**Depended on by:** none yet

## Context

Every tool returns text written for a model. A program calling them (the PR agent that asked for
this) had to regex `--- FILE: … | SCOPE: … ---` out of `semantic_code_search` and `- {file} ({scope})`
lines out of `find_test_coverage`, and nothing on this side treated that wording as an interface.
@edb's leaning was JSON on other tools too, with the scope left to whoever built it.

## Decision

- **Five tools take `format`: `"text"` (default) or `"json"`.** The two asked for,
  `semantic_code_search` and `find_test_coverage`, and the three lookups a program is most likely to
  call: `get_symbol_source`, `get_file_outline`, `what_writes`.
- **The other twelve do not, for now.** The analysis tools (`investigate_architecture`,
  `analyze_blast_radius`, `trace_data_flow`, …) build their text from mixed sections and verdict
  prose; each would need its own shape, and no caller has asked for one. Adding one later is the same
  one argument.
- **One argument type, described once** (`OutputFormat`, a `Literal["text", "json"]` with a one-line
  description). The schema refuses any other value, so an MCP client gets `isError`; a direct Python
  call gets the same `ValueError` from `_check_format()`.
- **The JSON is a string** a caller `json.loads`. `semantic_code_search` returns
  `{query, warning, hits, max_tokens, skipped}`, each hit `file, scope, score, source, tier,
  start_line, end_line, text`. It goes through the same token budget as the text form:
  `_fit_budget()` is now shared by `_pack_results()` and `_pack_json()`, and both count the same text
  block, so they keep the same hits.
- **Text output is unchanged.** `_pack_results()` and the four other tools build exactly the string
  they built before; the JSON branch returns before the text is assembled.
- Errors stay errors in both forms (unknown file, unknown symbol, a symbol changed since indexing):
  they raise, so they are `isError` either way.

## Consequences

**Better:** a program gets stable keys instead of parsing prose; the hits match the text form's.
**Worse:** five schemas grow by one argument each (measured below); the JSON keys are now an
interface, so renaming one is a breaking change.
**Neutral:** the text form, and every other tool, is unchanged.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| JSON on all 17 tools now | the analysis tools need bespoke shapes nobody has asked for; 12 more schema entries for no caller |
| MCP structured output (`outputSchema` + `structuredContent`) | B-019 removed the generated output schemas for listing cost; per-tool schemas would cost more than one argument |
| A separate `*_json` tool per tool | doubles the listing |

## Implementation Log

- [x] `OutputFormat`, `_check_format()`, `_json()`; `_fit_budget()` / `_pack_json()` split out of `_pack_results()`.
- [x] `format` on the five tools.
- [x] Tests: JSON shape per tool, same hits under the budget, text still the default, unknown format refused
      (`test_search_budget.py`, `test_find_test_coverage.py`, `test_symbol_tools.py`, `test_what_writes.py`).
- [x] MCP Inspector 2.8.0, 2026-11-12, against a five-file fixture repo indexed on CPU with summaries
      off. `tools/list --strict` exits 0 with 17 tools, all read-only tools still `readOnlyHint`.
      **Listing size: 17,063 characters on master, 17,473 with B-074 (its docstring), 18,378 here:
      +905 (+5.3%), 181 per tool, all of it the `format` schema entry; no description grew.**
      `tools/call` for each of the five: without `format`, text byte-identical to the base branch's;
      `format=json` parses (`semantic_code_search` → `query`, `warning`, `hits`, `max_tokens`,
      `skipped`; `get_file_outline` → `file`, `as_of`, `changed_since_indexed`, `symbols`; …);
      `format=xml` → `isError: true` (exit 5).
- [x] Gates, 2026-11-12, CPU only (`CODE_INDEXER_DEVICE=cpu HF_HUB_OFFLINE=1`): flake8, collect,
      conformance, resolution and l5x teeth checks exit 0; 1018 passed, 2 skipped.

**Notes:**
- `semantic_code_search`'s new JSON line calls `jina_tokenizer.count_tokens`, which #102 (B-076)
  renames without an alias. Whichever of the two merges second renames that one call on rebase.
