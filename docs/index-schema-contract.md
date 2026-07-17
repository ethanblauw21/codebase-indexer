# `graph.db` freshness contract (ADR-025)

This is the **stable, read-only contract** that an external consumer — segmem's
`project_router` `codemap` connector — may query against `<project>/.code-index/graph.db`.
The connector opens the SQLite file **read-only, offline, stdlib `sqlite3` only**; it never
runs the indexer or the MCP server. Everything below is answerable by a plain `SELECT`.

Only the columns and keys documented here are contract. Everything else in the schema is
internal and may change without notice.

## `files` — per-file timestamps

| Column | Type | Meaning | May be NULL? |
|---|---|---|---|
| `path` | TEXT | Repo-relative path, **forward slashes** on every OS. | no |
| `content_hash` | TEXT | Change-detection digest. Algorithm-opaque — compare it to itself, never parse it. | no |
| `indexed_at` | TEXT (ISO-8601 UTC, `…Z`) | When the indexer last **wrote this row**. Unchanged meaning since before ADR-025. | no |
| `content_changed_at` | TEXT (ISO-8601 UTC, `…Z`) | When the file's **content** last changed. **The authoritative "changed since T" field.** | **yes** |
| `authored_at` | TEXT (ISO-8601 UTC, `…Z`) | When the content was authored (git author time). Diagnostic provenance only; nothing couples it to `content_changed_at`. | **yes** |

### The delta query

```sql
SELECT path, content_changed_at
FROM   files
WHERE  content_changed_at IS NOT NULL
  AND  content_changed_at > :since
ORDER  BY content_changed_at DESC;
```

- `content_changed_at` is **NULL** for files with no git history (untracked, git-ignored,
  vendored, or no repo). The `IS NOT NULL` guard makes the reader **ignore** those rather
  than be handed a fabricated time. `> :since` already excludes NULLs in SQLite, but keep
  the explicit guard for clarity.
- Stamps are **back-dated from git** on first index, so a from-scratch rebuild reconstructs
  real history instead of spiking the whole corpus to "now". A dirty (uncommitted) file is
  stamped `now()`, never back-dated — so recent uncommitted work is never missed.
- `indexed_at` is **not** a change signal — a full rebuild rewrites every row's `indexed_at`
  while leaving `content_changed_at` accurate. Use `content_changed_at` for "what changed".

## `index_meta` — run-level facts

A `(key TEXT PRIMARY KEY, value TEXT)` table. Same shape the sibling rust indexer exposes,
so the connector reads it with existing code.

| Key | Value | Use |
|---|---|---|
| `last_indexed_commit` | HEAD hash at last completed run | Answer "is this stale?" with one `git` call, no indexer run: compare to the folder's current HEAD. |
| `last_verified_at` | ISO-8601 UTC of the last completed run | Distinguishes "fresh index over quiet code" from "week-old index". Written on **every** completed run, **including no-ops** — a docs-only commit still advances it. |
| `files_total` | count of indexed files | Sanity-check the rollup before serving it. |
| `schema_version` | `"1"` | Detect drift. If this key is absent or an unexpected value, treat the freshness columns as unavailable rather than trusting a stale shape. |

```sql
SELECT value FROM index_meta WHERE key = 'last_indexed_commit';
SELECT value FROM index_meta WHERE key = 'last_verified_at';
```

## Compatibility notes

- **Additive only.** `MAX(indexed_at)` and every prior `files` / `symbols` / `edges` query
  keep answering. ADR-025 added columns and one table; it changed no existing column's
  meaning. This matters because the connector **swallows errors** — a broken query degrades
  silently, so nothing here may break one.
- **Timestamp type divergence across the family is intentional.** This indexer writes ISO-8601
  strings (`indexed_at`, `content_changed_at`, `authored_at`); the rust indexer writes an
  epoch float. New columns follow **this** repo's ISO convention. Unifying the two is the
  consumer's call, not this indexer's.

## Interactive counterpart

Human/agent callers (not segmem, which is forbidden from calling the server) can get the same
facts from the `index_status` MCP tool, which reads exactly these columns and keys and adds a
live HEAD-vs-`last_indexed_commit` staleness comparison.
