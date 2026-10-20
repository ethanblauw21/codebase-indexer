# ADR-049: A Safe Reindex, Read-Only Hints, Redacted Output and a Cheaper Tool Surface

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-049-safe-reindex-and-tool-output`
**Reviewer:** @edb
**Backlog:** [B-057](../backlog.md#b-057) (main), [B-045](../backlog.md#b-045),
[B-018](../backlog.md#b-018) (the output half), [B-019](../backlog.md#b-019),
[B-021](../backlog.md#b-021) (point 3 only)
**Depends on:** none
**Depended on by:** none yet. ADR-050 (symbols, built in parallel) gets `readOnlyHint` and redaction
on its new tools through `_tool` with no change of its own; @edb adds its tool names to the routing
line after both merge.

## Context

**B-057, the 2026-09-29 incident.** An agent called `reindex` through MCP Inspector twice with no
arguments. `changed_files_only` defaulted to `False`, so each call was a full rebuild, and Inspector's
fixed 60 s timeout killed the server partway through both:

1. Run 1: `_snapshot_index` copied the good index to `.pre-full-reindex/`, the index was wiped, and
   the rebuild was killed.
2. Run 2: `_snapshot_index` began with `shutil.rmtree(backup_dir)` ("left by a killed earlier run"),
   which deleted the only good copy, then snapshotted run 1's half-built index and was killed too.

Two independent defects: the backup is deleted on the assumption that it is stale, and a bare call is
the destructive one.

**B-045.** No tool declares MCP tool annotations. Every tool but `reindex` only reads, and a client
that sees `readOnlyHint` can auto-approve them.

**B-018.** Tools return raw chunk text. A credential committed to an indexed repo (a service-account
JSON, a token in a config file) goes straight into the agent's context and its transcript. There is
no redaction anywhere in `src/`.

**B-019.** Every session with the server connected pays for the tool listing and the server
instructions before its first call. Nobody has measured how much that is.

**B-021 point 3.** The server instructions say when the index is stale but never when to prefer it
over Grep, and they are the one text every connected session already receives.

## Decision

### 1. A backup on disk is the good copy (B-057, defect 1)

- **`_snapshot_index` never deletes `.pre-full-reindex/`.** If it exists when a full rebuild starts,
  the run that made it never finished, so the live index is suspect and the backup is the copy that
  run was protecting. The new run **restores it first**, then reuses it as its own backup (the live
  index now equals it, so there is nothing new to copy).
- **A snapshot becomes the backup only once it is complete.** It is copied into
  `.pre-full-reindex.partial/` and renamed to `.pre-full-reindex/` in one `os.replace`. The wipe comes
  after the rename. So a `.partial/` left behind was killed while copying, before anything was wiped:
  the live index is still good, and the partial copy is the one thing that is safe to delete.
- **`_restore_index` copies; it never moves files out of the backup or deletes it.** The old one
  moved the `.faiss` files with `os.replace`, so a kill mid-restore left a backup missing files, and
  the next restore would have deleted the live copies of the ones already moved.
- **The backup is removed in exactly two places, both after the live index is known good:** when the
  rebuild has finished and saved (`run_incremental` returned), and when a failed rebuild's restore has
  finished. A kill at any point before either leaves it in place for the next full rebuild.

### 2. A full rebuild has to be asked for (B-057, defect 2)

`reindex(changed_files_only: bool = True)`. The parameter keeps its name, so a saved call that passes
it means what it meant before; only the bare call changes, from a full rebuild to an incremental one.
The docstring now leads with the full rebuild being slow (tens of minutes) and destructive, and says
never to run one against an index you need from a client with a request timeout. Every caller in the
repo (the TUI, the tests) already passed the argument; the TUI's fallback when it can't read its own
field is now incremental too.

### 3. CONTRIBUTING §5

"`reindex` runs without error" now says: on a scratch index or a test target, never the live one,
because Inspector's 60 s timeout kills any full rebuild.

### 4. Tool annotations derived in `_tool` (B-045)

`_tool` passes `ToolAnnotations` to FastMCP for every tool it registers. `readOnlyHint` is
`reads_index`, so every read tool, including ones other branches are adding now, gets
`readOnlyHint: true` without its author doing anything. A writer declares the rest at its decorator:
`reindex` is `@_tool(reads_index=False, destructiveHint=True, idempotentHint=False)`.

- `destructiveHint=True`: a full rebuild wipes the index first.
- `idempotentHint=False`, deliberately. An incremental run is close to idempotent, but a client may
  retry an idempotent call that timed out, and a retried full rebuild after a timeout is the incident.
- `openWorldHint` is left unset: it would add bytes to every tool's listing for no client behavior we
  rely on.

`mcp` is pinned `>=1.28,<2`. `FastMCP.tool(annotations=...)` and `mcp.types.ToolAnnotations` exist
throughout that range.

### 5. Redaction at the MCP boundary (B-018, output half)

`_tool`'s async wrapper runs every tool's returned text through `redact.redact()` before FastMCP sees
it. The plain module functions, which the TUI and the tests call, are unchanged, and the index stays
faithful. `src/redact.py` holds the patterns and replaces each match with `[REDACTED:<kind>]`:

| Kind | Matches |
|---|---|
| `private-key-field` | the string value of a `"private_key"` / `'private_key'` key (service-account JSON, Python dicts) |
| `private-key` | a PEM `-----BEGIN … PRIVATE KEY-----` block through its END line, or to the end of the text when a chunk cuts it off |
| `github-token` | `ghp_` / `gho_` / `ghs_` / `ghu_` / `ghr_` + 36, `github_pat_` + 50 or more |
| `slack-token` | `xox[abprs]-` + 10 or more |
| `aws-access-key` | `AKIA` / `ASIA` + 16 uppercase letters or digits |
| `google-api-key` | `AIza` + 35 |
| `anthropic-key` | `sk-ant-` + 20 or more |
| `openai-key` | `sk-proj-` / `sk-svcacct-` / `sk-admin-` + 20 or more, or `sk-` + 20 or more letters and digits with no hyphen |

False positives are kept low by requiring each prefix's real length and alphabet and a word boundary
in front: `sk_` names, `sk-` kebab-case class names, short hex, and `AKIA` in prose pass through.
Not matching every token format is accepted: this catches the families that leak in practice, and
the scan-policy half of B-018 (not indexing `.env*` and key files at all) is another branch's.

### 6. The routing line (B-021 point 3)

Both branches of `_server_instructions` gain one sentence: for how code works, where a symbol is used,
what writes a field, or blast radius, call `semantic_code_search`, `trace_data_flow`, `what_writes` or
`analyze_blast_radius` before Grep; use Grep for exact strings and for files your branch changed.

### 7. Measure the tool surface, trim only the bloated (B-019)

Measured with Inspector's `tools/list` (table in the Implementation Log). Three changes, smallest
risk first:

- **`_tool` sends each docstring dedented** (`inspect.cleandoc`). FastMCP sends `__doc__` as is, so
  every line carried four spaces of indentation: 634 characters across 14 tools, with no content.
- **`_tool` registers with `structured_output=False`.** For a `-> str` tool FastMCP generates an
  `outputSchema` of `{"result": string}` (about 150 characters per tool) and returns the text twice,
  once as content and once as `structuredContent`. The schema tells an agent nothing.
- **Three descriptions are trimmed:** `what_writes` (a support-call narrative), `investigate_architecture`
  (the retrieval pipeline's internals, which no caller acts on) and `index_status` (implementation
  detail). Each keeps its when-to-call and when-not-to-call lines. The rest are left alone: none is
  bloated once dedented, and `trace_data_flow` and `map_module_communities` belong to ADR-053.

## Consequences

**Better:**
- No sequence of killed full rebuilds can lose the last good index: the backup outlives every kill
  and is removed only once the live index is good again.
- A bare `reindex()` costs seconds and destroys nothing.
- Clients can auto-approve every read tool, and every future one gets the hint for free.
- A committed credential no longer reaches an agent's context through this server.

**Worse:**
- A full rebuild that starts on a leftover backup first copies it back, which is more disk I/O
  before the wipe. Restoring now copies the `.faiss` files instead of moving them.
- If an incremental run finished the half-built index after a kill, the leftover backup is now
  older than the live index. The next full rebuild restores it before starting, and if that rebuild
  is killed too, the fallback is the older, but finished, index. An incremental catches it up.
- A backup written by the old code was not renamed into place, so one left from a kill *during* its
  snapshot can be partial. That window existed only before this ADR, and only during the copy.
- A redacted chunk no longer shows its literal. That is the point, but a test fixture that looks
  like a key will read as `[REDACTED:…]` in tool output.

- Tool results no longer carry `structuredContent`. A client that read the `result` field instead of
  the text content would have to switch. None of ours does (Claude Code reads the content).

**Neutral:**
- A client that called `reindex()` bare expecting a full rebuild now gets an incremental one, and
  its result says "Incremental".
- Error text from a tool that raises is not redacted: it carries paths and messages, not chunk text.
- The routing line makes the instructions 215 characters longer, and the listing is still 3,392
  characters shorter than before (Implementation Log).

## Open Questions for @edb

1. **`structured_output=False`.** It is a one-argument revert in `_tool` if any client turns out to
   read `structuredContent`. Keep it?
2. **Other secret families.** AWS *secret* keys (40 characters of base64 with no prefix), Stripe
   `sk_live_`, and generic `password = "…"` assignments are not matched: each has no prefix that
   separates it from ordinary code, or (for Stripe) collides with the `sk_` names this ADR keeps
   untouched. Add any of them, accepting the false positives?
3. **An incremental run that completes a half-built index** leaves the old backup in place (only a
   full rebuild removes it). Should a finished incremental run, which means the watchdog's, also
   remove it? That code is in `_ReindexDebouncer`, outside this ADR's functions.
4. **The routing line** names four tools. Add `get_file_outline` / `get_symbol_source` after ADR-050
   merges, as planned.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Refuse a full rebuild while a backup exists | Safe, but it leaves a wiped live index serving until someone restores by hand. Restoring is what the human would do anyway. |
| Keep the leftover backup and skip the restore | Same end state for the rebuild, but the live index stays half-built for the length of the new run, and the "live equals backup at start" invariant is lost. |
| Rename the parameter to `full: bool = False` | Clearer, but every saved call that passes `changed_files_only` would break against the schema. |
| Redact at index time | The index would no longer be faithful, and the TUI's chunk browser and tests would see altered text. Redacting where text leaves the process covers every tool at one point. |
| Hand-written list of read-only tools | Goes stale the moment another branch adds a tool. |
| `idempotentHint=True` on `reindex` | Invites a client's automatic retry of a timed-out full rebuild. |

## Implementation Log

- [x] B-057: `_snapshot_index` restores a leftover backup; atomic snapshot; copying restore; backup
      removed only after a finished save or a finished restore
- [x] B-057: `reindex` defaults to incremental; docstring; TUI fallback
- [x] B-057: CONTRIBUTING §5 line
- [x] B-045: annotations derived in `_tool`
- [x] B-018: `src/redact.py` and the pass in `_tool`'s wrapper
- [x] B-021: routing line in both instruction branches; length before and after
- [x] B-019: `tools/list` measured; table; trims; totals before and after
- [x] Tests, gates, Inspector
- [ ] ADR-050's tool names in the routing line, after both merge (@edb)

**Stack for every measurement below:** Linux cloud container, Python 3.12.3, `mcp` 1.30.0, MCP
Inspector 2.8.0, CPU only, no embedder or model downloaded (Hugging Face unreachable). Before is
`871b55c`; after is this branch.

**B-019: the tool listing.** Characters of compact JSON from `tools/list --strict --format json`,
run from an empty directory. "Whole entry" is the tool's full listing object (name, description,
input and output schemas, annotations).

| Tool | Description before | after | Input schema before | after | Annotations after | Whole entry before | after |
|---|---:|---:|---:|---:|---:|---:|---:|
| `what_writes` | 1,564 | 961 | 199 | 199 | 21 | 1,992 | 1,272 |
| `map_module_communities` | 1,222 | 1,157 | 299 | 299 | 21 | 1,759 | 1,576 |
| `investigate_architecture` | 1,071 | 687 | 223 | 223 | 21 | 1,535 | 1,025 |
| `index_status` | 1,122 | 741 | 177 | 177 | 21 | 1,529 | 1,031 |
| `find_unabstracted_collection_reads` | 906 | 848 | 277 | 277 | 21 | 1,444 | 1,255 |
| `verify_candidate_edges` | 894 | 840 | 207 | 207 | 21 | 1,336 | 1,163 |
| `reindex` | 859 | 800 | 144 | 143 | 68 | 1,207 | 1,090 |
| `find_test_coverage` | 724 | 674 | 222 | 222 | 21 | 1,171 | 1,006 |
| `find_dead_code` | 725 | 679 | 200 | 200 | 21 | 1,143 | 986 |
| `semantic_code_search` | 561 | 527 | 135 | 135 | 21 | 922 | 771 |
| `detect_pattern_violations` | 249 | 231 | 340 | 340 | 21 | 817 | 677 |
| `analyze_blast_radius` | 287 | 265 | 227 | 227 | 21 | 733 | 594 |
| `find_similar_code` | 203 | 189 | 153 | 153 | 21 | 567 | 439 |
| `trace_data_flow` | 91 | 81 | 154 | 154 | 21 | 451 | 329 |
| **Total** | **10,478** | **8,680** | **2,957** | **2,956** | **341** | **16,606** | **13,214** |

Where the 3,392 characters (−20%) went: 634 of indentation, about 2,100 of output schemas, 1,151
from the three trimmed descriptions, less the 341 of annotations added. The input schemas are
unchanged (`reindex` is one character shorter: `true` for `false`). The largest description is
`map_module_communities` at 1,157, which is ADR-053's to trim; no description comes near the
2,892 characters found on segmem_mcp.

**B-021: the server instructions.** Rendered for a folder index and a git-mode index, each with a
recorded commit: folder mode **600 → 815** characters, git mode **570 → 785**. The routing sentence
replaces "Use the index for how the rest of the codebase works", which it says more precisely.

**Notes:**
- 2026-10-20: **"Byte-identical" needs one qualification for `graph.db`.** The restore uses SQLite's
  backup API (as `_snapshot_index` always has, to read a consistent snapshot through the WAL), and
  the backup API bumps three 4-byte counters in the database header of the file it writes: the
  file change counter (offset 24), the schema cookie (40) and version-valid-for (92). The replay
  test compares the `.faiss` files byte for byte and `graph.db` byte for byte with those twelve
  header bytes masked, every page included. A raw file copy would match exactly but could miss
  pages still in the WAL.
- 2026-10-20: **How the tests simulate a kill.** A `Killed(BaseException)` raised from the embedder
  inside a real `run_incremental` (stubbed embedder, character-level tokenizer, summarizer off, a
  one-text embed window so part of the repo is written first). `_reindex` catches `BaseException`
  and restores, which a killed process never gets to do, so the fixture makes that handler's
  restore die too. The replay fails on `871b55c` (7 of the 9 new reindex tests) and passes here.
- 2026-10-20: the commit-msg hook is not executable in a fresh clone (`git` ignores it), so the
  `[ADR-049 …]` tags on this branch's commits were added by hand in the hook's format.
- 2026-10-20: `pyproject.toml`'s `py-modules` list does not include `redact` (nor `config`,
  `index_lock`, `scan_policy` and nine other modules already in `src/`). The editable install
  and the tests don't need it; a non-editable install of every one of those is already broken the
  same way. Not changed here: not this ADR's file.
- 2026-10-20: **Tests.** New: `tests/test_safety_reindex.py` (9), `tests/test_safety_redact.py` (32),
  `tests/test_safety_tool_surface.py` (9). In this container the suite is 654 passed, 48 failed,
  2 skipped on `871b55c`, and 704 passed, 48 failed, 2 skipped here: the same 48 fail on both, all
  because the embedder's tokenizer cannot be fetched from Hugging Face here. flake8 on `src` is
  clean; `conformance_eval`, `resolution_eval` and `l5x_fixture_teeth` pass on both.
- 2026-10-20: **MCP Inspector 2.8.0**, from an empty `/tmp/insp`, never calling `reindex`:
  - `tools/list --strict`: exit 0. Every tool but `reindex` declares `readOnlyHint: true`;
    `reindex` declares `readOnlyHint: false, destructiveHint: true, idempotentHint: false`; no tool
    has an `outputSchema`.
  - `index_status since=1d`: exit 0, reports no recorded build. `index_status since=bogus`: exit 5,
    `isError: true`, names the accepted forms.
  - `semantic_code_search` with no `query`: exit 5, `isError: true` (validation error).
    With `query=parser`: exit 5, `isError: true`, because no embedder can load here. Its success
    path, with redaction applied, is covered in pytest
    (`test_tool_output_is_redacted_at_the_mcp_boundary`).
