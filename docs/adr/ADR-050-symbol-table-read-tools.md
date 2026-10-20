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
- **Staleness: re-read and verify, refuse when it can't be verified.**
  - If the file still has its indexed hash, the indexed span is exact and is served.
  - Otherwise the stored `text` is the witness. It is split into blocks, at ADR-034 skeleton stubs
    (` ...`, which replace a class's member bodies) and at blank lines (a merged overload set is its
    parts reordered). Each block is stripped, which absorbs the first line's missing indentation and an
    `export` the node doesn't include. Then:
    1. Every block is still inside the indexed span → served with its line numbers, and a note that the
       file changed elsewhere but this symbol's lines did not.
    2. Exactly one same-length span starting where the first block now occurs holds every block →
       served from there, with a note that it moved from the indexed lines and that other line numbers
       in the file may have shifted.
    3. Neither → **refused with an error**: the symbol changed since indexing; here are the indexed
       lines; Read the file. Serving the re-read span here would show whatever now sits on those lines,
       which may be a different function. That is the backlog's trap in a new form.
  - The hash is the cheap "file changed" signal; the text comparison decides what is served. So a change
    to how `content_hash` is computed (ADR-051 is touching keys on a sibling branch) can at worst add a
    spurious "file changed" note, never serve wrong text.
  - Served text is always the real source. A class comes back with its member bodies, not its skeleton.
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
  - Folder mode: the indexed side is the working tree (`git diff <base>`), and the merge base is taken
    against `HEAD`. Untracked files are added only when the index holds them; their symbols are all new.
    A file whose disk hash no longer matches the index is flagged: its ranges may be off until the
    watchdog catches up.
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

- New `CodeDB` methods, appended at the end of the class and read-only: `has_files`,
  `get_file_record`, `find_file_paths`, `get_file_symbol_locations`, `find_symbol_locations`.
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

- [x] `CodeDB` read methods (`src/db.py`, appended to the class)
- [x] `get_file_outline` + tests
- [x] `get_symbol_source` + tests (unique, ambiguous, edited elsewhere, moved, changed, deleted, git mode)
- [x] `changed_symbols` + tests (temp git repo: changed vs untouched function, merge base, uncommitted and
  untracked work, index behind disk, blast radius, bad ref, option-like refs, outside a repo, git mode)
- [x] Each tool's failure path is `isError` through the MCP layer (pytest, in-memory FastMCP client session)
- [x] MCP Inspector `tools/list` and one good and one bad call per tool
- [x] Tool lists in `src/CLAUDE.md` and `README.md`
- [ ] Once ADR-049 merges: confirm the three tools list `readOnlyHint` and their output goes through its
  redaction. They are registered with the plain `@_tool()`, so both should arrive with no change here.

**Open questions for @edb:**

1. **A class whose method body changed is refused.** Its span grew or shrank, so no same-length span
   holds its skeleton. The answer is honest, but for a class it is coarse. Accept it, or verify a class
   by its skeleton blocks in order and extend its end to the next sibling?
2. **Server instructions.** Suggested sentence for ADR-049's `_server_instructions` (not edited here):
   "For what a file contains, call `get_file_outline` before Reading it; for one function, call
   `get_symbol_source`."
3. **Gitignored but indexed files** are invisible to `changed_symbols` in folder mode: the indexer
   follows `scan_policy`, not `.gitignore`, and `git diff` follows git. Rare in practice; worth a note?
4. **No raw-diff mode.** `changed_symbols` always diffs from the merge base. A `--no-merge-base` switch
   is easy if review against a moving tip is ever wanted.

**Notes:**

<!-- 2026-10-20 --> **What the adapters store, checked before building §2.** Python, TS, C# and C++ symbols
store the node's text, which starts at the node's column and leaves out a TS `export` and a Python
decorator (`start_line` is the `def` line). Class symbols store an ADR-034 skeleton, and merged
overload sets join their parts in implementation-first order. So "the stored text occurs in the span"
is false for every class. The verification is block-wise (§2) rather than whole-text.

<!-- 2026-10-20 --> **C++ declaration and definition share an FQN** (`ns::W::f(int)` at line 4 in the class
and line 6 out of line). `symbols` keeps one row per FQN, and `symbol_locations` keeps one location per
(symbol, file). The outline shows one of the two, the declaration. That is the existing schema, not
something this ADR changes.

<!-- 2026-10-20 --> **Inspector caught noise pytest didn't.** Against a scratch repo whose `.code-index/` was
not gitignored, `changed_symbols` listed `graph.db`, `-shm` and `-wal` as "changed but not indexed".
Untracked files now count only when the index holds them (test added). It also showed that the ref was
checked after the index, so an option-like ref against an empty directory said "No index here". The
shape check now runs first.

<!-- 2026-10-20 --> **Tests.** Stack: Linux container, Python 3.12.3, git 2.43.0, mcp 1.30.0, no GPU and no
model (HuggingFace offline). The suite on `871b55c` was 654 passed, 48 failed, 2 skipped; on this branch
it is 702 passed, 48 failed, 2 skipped. The 48 are the same tests on both: each loads `BAAI/bge-code-v1`
or its tokenizer, which can't be downloaded here. `tests/test_symbol_tools.py` adds 48 tests.
`tests/test_index_state.py`'s registered-tool count moves from 14 to 17, the one line changed outside
this ADR's files. flake8, `pytest --co`, `conformance_eval --check-baseline`, `resolution_eval
--check-baseline` and `l5x_fixture_teeth --check` all pass.

<!-- 2026-10-20 --> **MCP Inspector 2.8.0** (`--cli`, same stack):
- `tools/list --strict`: exit 0, 17 tools. Description lengths: `get_file_outline` 587,
  `get_symbol_source` 598, `changed_symbols` 637 characters. No tool on this branch declares
  `readOnlyHint`: ADR-049 adds it inside `_tool`.
- From an empty directory, each tool fails with `isError: true` and "No index here". A missing required
  argument and an option-like ref (`--output=x`) also fail with `isError: true`, the ref before any
  index or git work.
- Against a scratch git repo with a SQLite-only index (parse and upsert, no embedder, no FAISS vectors):
  `get_file_outline pkg/mod.py`, `get_symbol_source K.m` and `changed_symbols HEAD~1` succeed. An
  unknown file, an unknown symbol, a missing `symbol`, `ref=no-such-branch` and `ref=--output=x` each
  return `isError: true` with a message naming the case. The Inspector CLI won't send an empty-string
  argument, so `symbol=""` is covered in pytest instead.
