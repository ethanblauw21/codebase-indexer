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

Measured with Inspector's `tools/list` (Implementation Log). Trimmed only where a description is
clearly bloated, keeping the when-to-call guidance.

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

**Neutral:**
- A client that called `reindex()` bare expecting a full rebuild now gets an incremental one, and
  its result says "Incremental".
- Error text from a tool that raises is not redacted: it carries paths and messages, not chunk text.

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

- [ ] B-057: `_snapshot_index` restores a leftover backup; atomic snapshot; copying restore; backup
      removed only after a finished save or a finished restore
- [ ] B-057: `reindex` defaults to incremental; docstring; TUI fallback
- [ ] B-057: CONTRIBUTING §5 line
- [ ] B-045: annotations derived in `_tool`
- [ ] B-018: `src/redact.py` and the pass in `_tool`'s wrapper
- [ ] B-021: routing line in both instruction branches; length before and after
- [ ] B-019: `tools/list` measured; table; trims; totals before and after
- [ ] Tests, gates, Inspector

**Notes:**
