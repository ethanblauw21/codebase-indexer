# Study: jCodeMunch MCP, and what's worth taking from it

> **Source:** https://github.com/jgravelle/jcodemunch-mcp, v1.108.319 @ `7dfa8bb`. It was
> desk-reviewed from source on 2026-09-24 in `Documents/Trying Out Tools/tools/jcodemunch-mcp/`,
> not installed or run.
>
> **Why this matters here:** jCodeMunch is a shipping, heavily polished tree-sitter + SQLite code
> index over MCP. That's the same family as this repo. It was **rejected as a tool** for Egan use
> (reasons below), but it does a few things indexer doesn't, and those are the point of this doc.
> The wants it produced are filed in [`backlog.md`](./backlog.md) as **B-016 to B-021**.

> ⚠️ **License: ideas only, no code.** jCodeMunch's `LICENSE` (§3, and the commercial-use list at
> lines ~72-76) forbids use "within a for-profit organization to support revenue-generating
> activities", and it names "internal tooling that supports revenue-generating operations". indexer
> is used at Egan. **Don't copy, port or closely paraphrase its source.** Re-implement from the
> behavior described here, against indexer's own schema.

---

## 1. What it is

A Python MCP server with 90+ tools (with a `"counter"` mode that exposes a small subset). Tree-sitter
parses 70+ languages into SQLite under `~/.code-index/`, storing symbols with **byte offsets**,
imports, and a copy of each file's content. Search is BM25 plus PageRank over symbols, with optional
general-text MiniLM embeddings and optional AI summaries. It also has git- and PR-aware tools
(changed symbols, PR risk, hotspots), blast radius and importers, freshness flags with optional
git-SHA verification, and **secret redaction before output**. An optional `init` installs Claude
Code hooks that steer native Read and Grep toward the index.

## 2. How it compares with indexer

| | jCodeMunch | indexer |
|---|---|---|
| Semantic retrieval | BM25 by default; optional **general-text** MiniLM | **Code-specific** `bge-code-v1` + jina reranker, 3-tier RRF |
| Retrieval-quality eval | None published. Its benchmark "does not measure answer quality" (`benchmarks/METHODOLOGY.md`) | Conformance P/R, CoIR, ADR-019 real-repo eval |
| Egan-specific | None | L5X and C# adapters |
| Languages | 70+ | 5 |
| **Get one symbol's exact source** | ✅ `get_symbol_source`, by byte offset | ❌ no tool. Spans are stored as `start_line`/`end_line` (`src/db.py:52-53`) but not served on their own |
| **File outline** | ✅ `get_file_outline` (signatures and ranges, no bodies) | ❌ no tool |
| **Secret redaction on output** | ✅ | ❌ (none found in `src/`) |
| Git and PR awareness | changed symbols, PR risk, hotspots | `index_status` freshness and "files changed since the index was built" (ADR-025). No diff-scoped tools |
| Steering the agent toward the index | Optional PreToolUse hook on Read/Grep/Glob/Bash; strict mode *denies* native reads | None |
| Tool-schema context cost | `tool_surface: "counter"` to shrink it | 14 tools, not measured |
| GPU needed | No | For a real reindex (and the GPU is off limits for now) |

**Bottom line:** indexer wins where it matters most for its purpose: code-aware semantic retrieval,
measured quality, and Egan adapters. jCodeMunch wins on **cheap, exact, structural reads**: "give
me just this function", "what's in this file". Those are the calls an agent makes most often, and
they're where token savings actually come from.

## 3. What to take (→ backlog)

1. **B-016: a single-symbol source tool.** Return exactly one symbol's code, from the span indexer
   already stores. Pair it with ADR-025 freshness: if the file's content changed after indexing,
   re-read the span from disk or say it's stale. jCodeMunch's quiet failure mode is serving a stale
   stored copy.
2. **B-017: a file outline tool.** Symbols with kind, signature and line range, and no bodies. It
   answers "what's in this file" for a fraction of a Read.
3. **B-018: redact secrets in tool output.** indexer returns raw chunks, so a key committed in an
   indexed repo gets pasted straight into the agent's context. This has already bitten @edb once
   (a recursive grep printed a production private key). Of the six, this is the one that's
   **security**, not convenience.
4. **B-019: measure and trim the tool-schema budget.** Every connected session pays for 14 tool
   descriptions. Measure it, and consider a compact surface. Same finding as segmem_mcp's
   2,892-char `search_notes` description, found with MCP Inspector on 2026-09-24.
5. **B-020: diff-scoped tools.** "Symbols changed since `<ref>`" and a blast radius over a diff,
   building on ADR-025's timestamps and the existing `analyze_blast_radius`.
6. **B-021: a hint that steers Read toward the index (raw, contentious).** Only as an opt-in,
   project-scope hint, **never a deny**. See the Gate 1 caveat in the backlog entry.

## 4. What not to take

- **Its token-savings accounting.** `get_symbol_source` credits the whole file size minus the
  returned bytes as "saved" (`tools/get_symbol.py:628-637`), and an outline of a file with no
  symbols credits the whole file. The vendor's own A/B shows **5.7% end-to-end** cost reduction
  (15–25% at the tool layer), not the advertised 96%. If indexer ever reports savings, measure a
  real counterfactual (Read with offset/limit, or `Grep -n`).
- **Strict-mode denial of native Read and Grep.** A stale or wrong index then blocks the fallback.
  That's the opposite of "fail loud, never silent".
- **Default-on telemetry** (`JCODEMUNCH_SHARE_SAVINGS` defaults to on).
- **Writing to user-scope `~/.claude/CLAUDE.md` and `settings.json` from an installer.**
