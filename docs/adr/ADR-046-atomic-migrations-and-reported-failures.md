# ADR-046: Schema Migrations Are Atomic, and Swallowed Failures Are Reported

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-046-atomic-migrations`
**Reviewer:** @edb
**Backlog:** [B-059](../backlog.md#b-059), from GitHub #55 and #54
**Depends on:** none
**Depended on by:** none yet

## Context

**#55: a crash mid-migration could empty the edges graph.**
- `CodeDB` opens SQLite in autocommit mode (`isolation_level=None`).
- The two table-swap migrations (`_migrate_edges`, `_migrate_edge_kinds`) ran as one `executescript`, and that commits statement by statement.
- A process killed between `DROP TABLE edges` and `ALTER TABLE edges_vN RENAME TO edges` left the rows in `edges_vN` and no `edges` table.
- The next open's `CREATE TABLE IF NOT EXISTS edges` then made an empty table. Any later swap would begin with `DROP TABLE IF EXISTS edges_vN`, deleting the only copy.
- File hashes were unchanged, so no incremental run ever refilled it. The index would answer every graph question with nothing, and say nothing.

A second hazard came from the same code. Every process opens `CodeDB`: the server, the CLI and the watchdog. Each one checked "is this migration done?" and then ran it, with no lock between the check and the run. The additive `ADD COLUMN` migrations would crash the loser of that race with "duplicate column". The swaps copy only the columns they knew about, so repeating `_migrate_edges` after `_migrate_edge_kinds` would have dropped `resolved_target`, `confidence` and the rest.

**#54: three places turned a failure into silence.**
- `adapters/_treesitter.run_query` caught every exception and returned `[]`. A grammar upgrade that broke one query would extract zero symbols or edges for that construct. Only the conformance scorecard would notice, and only for the features it covers.
- `core.py` set `warnings.filterwarnings("ignore")` at import. That silenced every warning in each process that imports it: the server, the indexer and the model host.
- `MCPServer` had two `except sqlite3.Error: pass` blocks:
  - The server instructions' index_meta read turned an unreadable database into "no finished build yet".
  - The reindex stamp restore left files with the rebuild's git-backdated stamps without saying so.

## Decision

1. **Every migration runs through `CodeDB._migrate(done, script)`** (`src/db.py`).
   - It checks `done()`; if the work is needed, it takes `BEGIN IMMEDIATE`, then checks `done()` again under the lock.
   - It runs each statement of the script on one cursor and commits.
   - Any error rolls back.
   - SQLite DDL is transactional, so a swap either completes or leaves the old table untouched.
   - `_split_sql` splits a script with `sqlite3.complete_statement`, because `executescript` would commit around each statement.
   - `PRAGMA optimize` moves out of the scripts and runs after the commit.
2. **The migration done-checks are unchanged:** the `ADD COLUMN` checks, the two swaps and the files-freshness columns keep their existing conditions.
   - The one exception is the `symbol_locations` backfill, which ran unconditionally. It now checks for a symbol with no location first, so opening an up-to-date index doesn't take the write lock for it.
3. **A database already broken by the old code is repaired on open.** `_recover_interrupted_edges_swap` runs before the DDL: if there is no `edges` table but an `edges_v3` or `edges_v2` exists, it renames the newer one into place and says so on stderr.
4. **`run_query` still returns `[]`, but reports each failing (language, query) once on stderr:** the exception and the first line of the query. Once, because the adapters run the same query on every file.
5. **`core.py` has no process-wide warnings filter.** With `-W always`, importing every server module, loading and running the embedder (bge-code-v1), and loading and running the summarizer (Qwen2.5-Coder-1.5B) on CPU emitted no warnings. So nothing needed hiding, and it is removed rather than narrowed. The two environment variables that quiet transformers and tokenizers stay.
6. **Both `sqlite3.Error` handlers in `MCPServer` report the error.** The server instructions' read goes to stderr, because it runs at startup. The stamp restore prints into the reindex output, so the caller sees it.

## Consequences

**Better:**
- A crash, a kill or a power loss during a migration can no longer empty the graph.
- Two processes opening an index at once can no longer crash each other or repeat a swap.
- Any index the old code had already broken is repaired the next time it is opened.
- A broken tree-sitter query, a library warning, or a failed stamp restore now says so.

**Worse:**
- A migration that has work to do waits up to Python's default 5 s connect timeout if another process holds the write lock, then fails with "database is locked". It used to fail the same way partway through, which was worse.
- A warning from a dependency upgrade will now appear in server and indexer stderr. That is the intent, but it may be noise until someone looks at it.

**Neutral:**
- Opening an up-to-date database runs the same checks as before and no migration: 3 ms on a copy of GanttWebApp's index, with all 6,718 edges intact.
- `_seed_index_meta`'s `INSERT OR IGNORE` still takes the write lock briefly on every open, as before. #55's note that read-only callers run migrations is otherwise answered by (1): they only take the lock when there is work.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Put `BEGIN IMMEDIATE; … COMMIT;` inside each `executescript` string | Atomic, but the done-check can't be repeated under the lock, so two processes could still both swap. |
| Keep the migrations and add a file lock around `CodeDB()` | SQLite already has a lock that is released on crash; a second lock adds a way to deadlock. |
| Narrow the warnings filter to known categories and modules | Nothing currently warns, so there is nothing to name. A filter for warnings that don't exist would only hide future ones. |
| Make `run_query` raise | One broken query would stop indexing every file of that language. Reporting it and extracting the rest is the better failure. |

## Implementation Log

- [x] `CodeDB._migrate`, `_columns`, `_split_sql`; all seven migrations routed through it; `PRAGMA optimize` after commit
- [x] `_recover_interrupted_edges_swap` before the DDL
- [x] `_treesitter.run_query` reports once per (language, query)
- [x] `core.py` blanket filter removed. Checked first with `-W always`: imports, embedder load and encode, and summarizer load and generate on CPU emitted none.
- [x] `MCPServer`: both `except sqlite3.Error: pass` report the error
- [x] Tests, `tests/test_migration_atomicity.py` (8): swap keeps every column; a crash between DROP and RENAME rolls back and the next open finishes; a database left mid-swap is recovered; the re-check under the lock; a failed migration leaves no open transaction; `_split_sql`; a failed query reported once; no blanket filter. Full suite 649 passed, 1 skipped; flake8 clean.
- [x] Opened a copy of GanttWebApp's real `graph.db`: no migration ran, 6,718 → 6,718 edges, same schema, 3 ms.
