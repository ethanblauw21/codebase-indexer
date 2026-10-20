# ADR-050: Answer from the Symbol Table: File Outline, One Symbol's Source, Changed Symbols

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-050-symbol-tools`
**Reviewer:** @edb
**Backlog:** [B-017](../backlog.md#b-017) (outline), [B-016](../backlog.md#b-016) (one symbol's source), [B-020](../backlog.md#b-020) (diff-scoped), all from the [jCodeMunch study](../study-jcodemunch-mcp.md) §3.1, §3.2, §3.5
**Depends on:** none. It reads what ADR-025 (`files.content_hash`, `content_changed_at`, `index_meta`) and ADR-042 (`source.py`, `index_meta.source`) already store.
**Depended on by:** none yet. B-021 (steer Read toward the index) would point at `get_file_outline`.

## Context

The index already stores every symbol's kind, name, FQN and line span (`symbols`, `symbol_locations`),
each file's content hash (`files.content_hash`), and the commit a git-mode index was built from
(`index_meta.last_indexed_commit`, ADR-042). No MCP tool serves those directly, so an agent that wants
to know what is in a file, or to see one function, Reads the whole file.

The jCodeMunch study (§3) names three tools worth having, and one trap: jCodeMunch serves its stored
copy of a symbol even when the file has changed since it was indexed, so the agent gets old code
presented as current. The study's license warning applies: everything here is built from the behavior
described in the backlog, against this repo's schema. No jCodeMunch source was read for it.

Facts the decisions rest on, checked on `871b55c`:

- **No signature is stored.** `symbols` holds `kind`, `name`, `class_context`, the span and the raw
  `text`. `symbol_types` has return and parameter types, but only for TypeScript.
- **`files.content_hash` is whatever `Source.list()` returned**: an MD5 of the raw bytes for the
  folder, the blob SHA for a git commit (ADR-042 §1).
- **`content_changed_at` says when the content last changed before it was indexed** (git committer
  time), not whether it changed since. It can't detect a stale index; only the hash can.
- **A symbol's stored `text` is its node's text**: it starts at the node's column, so its first line
  lacks the indentation that the file's line has.

## Decision

Three read tools, registered with `@_tool()` like the other read tools, in one block of `MCPServer.py`,
over new read-only `CodeDB` methods. They do no retrieval and load no model.

### 1. `get_file_outline(file)` (B-017)

- Each symbol in the file, as `L<start>-<end>  <kind>  <signature>`, indented by nesting (a method under
  its class), straight from `symbol_locations` joined to `symbols`. `symbol_locations` rather than
  `symbols.file_id` so a C# partial class lists its part in each file it spans.
- **The signature is derived, not stored**: the declaration text of the stored symbol up to its body.
  Read from the start of `text`, skip decorator and attribute lines, and stop at the first `{` outside
  brackets, or at a `:` that ends a line outside brackets (Python). Whitespace is collapsed to one line
  and capped at 200 characters. If no body opener is found, the first line is used. A parse-time
  signature column was rejected (below): it means a schema change and a full reindex for a display
  string.
- **Two different empty answers**, both explicit:
  - the file is indexed but has no symbols → a success that says so (and that the file is in the index);
  - the file isn't in the index → an error that says it isn't, with near-miss paths when there are any.
- **Paths:** `\` or `/`, a leading `./`, or an absolute path inside the repo are all normalised to the
  repo-relative POSIX path the index stores. A bare tail (`db.py`, `src/db.py`) that matches exactly one
  indexed path is accepted; several matches are an error listing them.
- **Freshness note:** when the file on disk no longer matches its indexed hash (folder mode), the
  outline says so, because its line numbers may be off.

### 2. `get_symbol_source(symbol, file="")` (B-016)

- `symbol` is a bare name (`login`), a qualified tail (`AuthService.login`) or a full FQN. `file`
  narrows it like `get_file_outline`'s argument.
- **Ambiguous → list, don't guess.** Several matches return the candidates (FQN, file, lines) as a
  successful answer that says how to narrow. No match is an error.
- **Text comes from the source the index was built from, through `source.py`, never from the stored
  copy and never from `open()`.**
  - Folder mode: `WorkingTreeSource.read`.
  - Git mode (`index_meta.source` starts with `git:`): `GitCommitSource` at the indexed commit
    (`index_meta.last_indexed_commit`). That is the exact text the symbol table describes. The answer
    names the commit and says it is not the caller's working tree, the same caveat the server
    instructions give.
- **Staleness: re-read and verify, refuse when it can't be verified.** The stored `text` is the
  witness. The span read from the source is compared with it (both stripped, which absorbs the
  first-line indentation):
  1. It matches → served with exact line numbers. If the file's hash differs from the indexed one, the
     answer says the file changed elsewhere but this symbol's span did not.
  2. It doesn't, but the stored text occurs exactly once elsewhere in the current file → served from
     its new position, and the answer says it moved from the indexed lines.
  3. Neither → **refused with an error**: the symbol changed since indexing, here are the indexed
     lines, Read the file. Serving the re-read span here would show whatever now sits on those lines,
     which may be a different function. That is the backlog's trap in a new form.
  The hash is the cheap "file changed" signal; the text comparison decides what is served. So a change
  to how `content_hash` is computed (ADR-051 is touching keys on a sibling branch) can at worst add a
  spurious "file changed" note, never serve wrong text.
- **Skeleton texts.** A class whose stored `text` is an ADR-034 skeleton (stubbed bodies) can't be
  matched line for line. A class span is verified by its declaration line alone, and its hash. See the
  Implementation Log for what the adapters actually store.
- Spans longer than 400 lines are cut, with a note pointing at `get_file_outline`.

### 3. `changed_symbols(ref, blast_radius=False, limit=100)` (B-020)

- **Shell out to git; don't read `content_changed_at`.** A timestamp says when a file last changed, not
  which lines, and it can't express "since `<ref>`". Only a diff gives line ranges to intersect with
  symbol spans.
- **Hardening:**
  - `ref` is rejected before git sees it if it is empty, starts with `-` (`--output=x` would be an
    option), or holds whitespace or control characters;
  - then `git rev-parse --verify --quiet --end-of-options <ref>^{commit}` must name a commit;
  - every call uses a list of arguments (never `shell=True`), `stdin=DEVNULL` and a 30 s timeout;
  - outside a git repository, or with no git binary, the tool fails with an error naming that.
- **What it diffs.** The question is "what did this work change since it left `<ref>`", so the base is
  `git merge-base <ref> <indexed side>`, falling back to `<ref>` itself when they share no history. When
  `<ref>` is an ancestor (`HEAD~3`) the merge base is `<ref>`, so nothing changes for that case.
  - Git mode: the indexed side is `index_meta.last_indexed_commit`. The diff is commit to commit, and its
    new-side line numbers are exactly the index's.
  - Folder mode: the indexed side is the working tree (`git diff <base>`, plus untracked indexed files,
    whose symbols are all new), and the merge base is taken against `HEAD`. A file whose disk hash no
    longer matches the index is flagged: its ranges may be off until the watchdog catches up.
- **Intersection.** `git diff -U0` gives the new-side lines each hunk added or changed. A pure deletion is
  a point between two lines. A symbol is reported when a changed line falls in its *own* lines: its span
  minus its nested symbols' spans. A one-line edit inside a method reports the method, not the method
  and its class. A deletion counts only when it is strictly inside a symbol (`start <= after < end`), so
  removing the blank lines between two functions reports neither.
- **What it can't see:** a symbol deleted since `<ref>` isn't in the index, which describes only the new
  side. Deleted files are listed by name, and so are changed files the index doesn't hold (docs, config).
- **Blast radius** (`blast_radius=True`) reuses `analyze_blast_radius`'s internals by calling them,
  unmodified: `_caller_evidence` per changed symbol (verified callers, and a count of candidate ones,
  under the ADR-017 §7 split) and `_import_relations` per changed file (its importers). Capped at the
  first 25 symbols, which the answer says.

### Shared

- New `CodeDB` methods, appended at the end of the class and read-only: `get_file_record`,
  `find_file_paths`, `get_file_symbol_locations`, `find_symbol_locations`.
- An index with no files at all (a server started where nothing was built) fails each tool with
  "no index here", not "file not indexed".
- Every failure is raised, so the MCP client sees `isError: true`, as `reindex` already does.

## Consequences

**Better:**
- "What's in this file" and "show me this function" cost a few hundred tokens, not a whole Read.
- An agent can't get old code presented as current: the answer is verified against the source, moved
  with a note, or refused.
- Pre-merge review gets the changed symbols, and optionally who calls them, in one call.

**Worse:**
- Three more tool descriptions in every session's context (17 tools, from 14; B-019 is the measuring
  item).
- `changed_symbols` runs git from the server process. It is bounded by timeouts and argument lists, but
  it is a subprocess per call.
- The derived signature is a heuristic. It is right for the declaration shapes of the five languages
  the tests cover; an exotic declaration falls back to its first line.

**Neutral:**
- No schema change and no reindex. The tools read only what the index already holds.
- In folder mode, symbols in files the watchdog hasn't re-indexed yet are flagged, not hidden.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Store a `signature` column at parse time | A schema migration and a full reindex for a display string. The derived one reads the same stored text. Worth revisiting if adapters start emitting a real signature. |
| Serve the stored `symbols.text` | It is exactly the stale copy the backlog warns about, and for classes it may be a skeleton. |
| On a changed file, re-read the indexed line span and warn | The lines may now hold a different symbol. Re-reading is only safe when the stored text confirms what is there; otherwise refuse. |
| Refuse whenever the file's hash changed | Throws away the common case: an edit elsewhere in the file leaves this symbol intact. The text check keeps it. |
| B-020 from `content_changed_at` | File-level timestamps: no line ranges, no notion of a ref. |
| B-020 diffing `<ref>` directly, not the merge base | With a `<ref>` that moved on (main after a merge), the answer would include everything main did since, reversed. The merge base is what a reviewer means. |
| Use GitPython or pygit2 | A new runtime dependency. The repo already shells out to git with list arguments (`source.py`, `index_location.py`). |
| Report every symbol whose span intersects | A one-line change in a method also reports its class, and every enclosing namespace in C#. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `CodeDB` read methods
- [ ] `get_file_outline` + tests
- [ ] `get_symbol_source` + tests (unique, ambiguous, changed on disk, git mode)
- [ ] `changed_symbols` + tests (temp git repo: changed vs untouched function, bad ref, option-like ref)
- [ ] Each tool's failure path is `isError` through the MCP layer (pytest, FastMCP client session)
- [ ] MCP Inspector `tools/list` and one good and one bad call per tool
- [ ] Tool lists in `src/CLAUDE.md` and `README.md`

**Open questions for @edb:**

**Notes:**
