# ADR-057: Name the Embedder's Tokenizer for What It Follows, and Say What the Output Cap Is For

**Status:** accepted
**Date:** 2026-11-12
**Branch:** `feature/adr-057-embed-tokenizer-name`
**Reviewer:** @edb
**Backlog:** [B-076](../backlog.md#b-076)
**Depends on:** none
**Depended on by:** none yet

## Context

A first-install report from a PR-agent session (B-073 to B-078) named two kinds of drift:

1. `core.jina_tokenizer` is a `TokenizerManager` that loads
   `AutoTokenizer.from_pretrained(embed_model_id())`. The embedder has been `BAAI/bge-code-v1` since
   ADR-009, so the name claims a model the index no longer uses. It had 22 uses in 10 files.
2. `semantic_code_search` justified its 4,000-token cap as "protecting your 8GB local model's VRAM",
   and `core.pack_context_safely()` called it "a conservative limit for 8k-window 8B models". No
   local model reads tool output: the text goes to whichever agent called the tool.

## Decision

- Rename the singleton to `embed_tokenizer`, the name of the thing it follows (`embed_model_id()`),
  everywhere in `src/`, `tests/` and `benchmarks/`. No alias is kept: nothing outside this repo
  imports it, and an alias would keep the wrong name alive.
- Rewrite the two comments: the cap is a budget for the calling agent's context, not a VRAM guard.
- No behaviour change. Historical mentions of jina in ADRs and benchmark write-ups stay; they record
  what was measured.

## Consequences

**Better:** the name says what it loads, so a future embedder swap can't leave a lying name behind.
**Worse:** any out-of-tree script importing `jina_tokenizer` breaks (none known).
**Neutral:** `tests/test_embed_tokenizer_name.py` fails if the old name reappears in code.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep `jina_tokenizer` as an alias | keeps the misleading name importable; no external caller needs it |
| Name it after bge (`bge_tokenizer`) | repeats the mistake at the next embedder swap |

## Implementation Log

- [x] Rename across 10 files (22 uses).
- [x] Rewrite the cap comments in `src/MCPServer.py` and `src/core.py`.
- [x] Regression test `tests/test_embed_tokenizer_name.py`.

**Notes:**
<!-- 2026-11-12: core.py:342's docstring first line made the same 8B claim; rewritten too. -->
