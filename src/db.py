"""
db.py — SQLite persistence layer for the Code Intelligence Engine.

Schema (six tables)
--------------------
  files            — one row per indexed source file; content_hash for incremental re-indexing
  symbols          — every named entity extracted by the AST parser; keyed by FQN
  chunks           — embeddable text segments with tier label; keyed by (file_id, scope, tier)
  edges            — directed dependencies between FQNs or module paths
                     kind ∈ {IMPORTS, CALLS, INSTANTIATES, OWNS,
                             PROVIDES_CONTEXT, CONSUMES_CONTEXT, EXTENDS, IMPLEMENTS}
  symbol_references — exact usage locations for every symbol (find-references support)
  symbol_types     — TypeScript type annotation metadata per symbol

Read profile
------------
The dominant read pattern is graph traversal: "who calls X?" and "what does X call?".
Every write path is a single atomic transaction.  Every traversal query has a
covering index.  WAL mode lets the indexer writer and MCP reader run concurrently.

Cycle prevention in get_call_graph
-----------------------------------
SQLite recursive CTEs terminate when the working table produces no new rows.
With UNION ALL that is not guaranteed for cyclic graphs (A→B→A loops forever).
We track a visited-path string using char(31) (ASCII Unit Separator, never
present in FQNs) so each node is expanded at most once per traversal branch.
The :max_depth guard is a secondary hard cap.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ast_chunker import Chunk, Edge, Reference, Symbol, SymbolType

# ---------------------------------------------------------------------------
# Return type for graph queries
# ---------------------------------------------------------------------------

@dataclass
class CallGraphNode:
    fqn: str
    depth: int
    direction: str          # "root" | "calls" | "called_by"
    symbol_kind: Optional[str]
    file_path: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    candidate: bool = False   # True when every edge reaching this node is
    # candidate (name-based / unresolved). MIN over reaching edges, so a node
    # with any resolved edge in this direction is verified. Drives the
    # safe-direction verdict rule (ADR-017 §7).


# ---------------------------------------------------------------------------
# DDL — PRAGMAs are separated out; executescript() handles both blocks
# ---------------------------------------------------------------------------

_PRAGMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA synchronous   = NORMAL;
PRAGMA foreign_keys  = ON;
PRAGMA cache_size    = -65536;
PRAGMA temp_store    = MEMORY;
PRAGMA mmap_size     = 268435456;
PRAGMA threads       = 4;
"""

_DDL_SQL = """\
-- -------------------------------------------------------------------------
-- files
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT    UNIQUE NOT NULL,
    content_hash TEXT    NOT NULL,
    indexed_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -------------------------------------------------------------------------
-- symbols
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY,
    fqn           TEXT    UNIQUE NOT NULL,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    class_context TEXT,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    text          TEXT    NOT NULL
);

-- -------------------------------------------------------------------------
-- symbol_locations — one row per (symbol, file) location.
-- Supports C# partial classes and C++ header/impl split (Phase 2+).
-- For Python/TS/JS (Phase 1), each symbol has exactly one location and
-- the row mirrors symbols.file_id / start_line / end_line / text.
-- ON DELETE CASCADE from symbols ensures cleanup when a symbol is removed.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbol_locations (
    id         INTEGER PRIMARY KEY,
    symbol_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES files(id)   ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    UNIQUE(symbol_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_symloc_symbol ON symbol_locations(symbol_id);
CREATE INDEX IF NOT EXISTS idx_symloc_file   ON symbol_locations(file_id);

-- -------------------------------------------------------------------------
-- chunks
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    scope      TEXT    NOT NULL,
    tier       INTEGER NOT NULL DEFAULT 1,
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line   INTEGER NOT NULL DEFAULT 0,
    text       TEXT    NOT NULL,
    tags       TEXT    NOT NULL DEFAULT '',
    UNIQUE(file_id, scope, tier)
);
CREATE INDEX IF NOT EXISTS idx_chunks_tags     ON chunks(tags) WHERE tags != '';

-- -------------------------------------------------------------------------
-- chunk_summaries — LLM extraction cache keyed by MD5(chunk_text)
-- Survives file moves and renames: same code → same hash → same cached summary.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk_summaries (
    text_hash  TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -------------------------------------------------------------------------
-- edges — directed dependencies (migrated to expanded schema in _migrate_edges)
-- Placeholder CREATE so _DDL_SQL is always safe on a fresh DB.
-- The migration in _init_db immediately upgrades this to the full schema.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY,
    source_fqn      TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    resolved_target TEXT,
    candidate       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_fqn, target, kind)
);

-- -------------------------------------------------------------------------
-- symbol_references — exact usage locations for find-references + density scoring
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbol_references (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    symbol_name TEXT    NOT NULL,
    symbol_fqn  TEXT,
    line        INTEGER NOT NULL,
    ref_kind    TEXT    NOT NULL DEFAULT 'CALL',
    context_fqn TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_name    ON symbol_references(symbol_name);
CREATE INDEX IF NOT EXISTS idx_refs_fqn     ON symbol_references(symbol_fqn);
CREATE INDEX IF NOT EXISTS idx_refs_file    ON symbol_references(file_id);
CREATE INDEX IF NOT EXISTS idx_refs_context ON symbol_references(context_fqn);

-- -------------------------------------------------------------------------
-- symbol_types — TypeScript type annotations per symbol
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbol_types (
    id           INTEGER PRIMARY KEY,
    symbol_fqn   TEXT    NOT NULL UNIQUE,
    return_type  TEXT,
    params_json  TEXT,
    type_params  TEXT,
    is_async     INTEGER NOT NULL DEFAULT 0,
    is_generator INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symtypes_fqn ON symbol_types(symbol_fqn);

-- -------------------------------------------------------------------------
-- Indices — optimised for graph traversal and FQN point-lookups
-- -------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_symbols_fqn        ON symbols(fqn);
CREATE INDEX IF NOT EXISTS idx_symbols_file        ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name        ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind        ON symbols(kind);

CREATE INDEX IF NOT EXISTS idx_edges_source        ON edges(source_fqn);
CREATE INDEX IF NOT EXISTS idx_edges_target        ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_source_kind   ON edges(source_fqn, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target_kind   ON edges(target, kind);
CREATE INDEX IF NOT EXISTS idx_edges_resolved      ON edges(resolved_target)
    WHERE resolved_target IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chunks_scope        ON chunks(scope);
CREATE INDEX IF NOT EXISTS idx_chunks_file_tier    ON chunks(file_id, tier);
"""

# ---------------------------------------------------------------------------
# Recursive CTE — bidirectional call-graph traversal with cycle guard
# ---------------------------------------------------------------------------

_CALL_GRAPH_SQL = """\
WITH RECURSIVE
call_graph(fqn, depth, direction, visited, candidate) AS (

    SELECT
        :fqn                                AS fqn,
        0                                   AS depth,
        'root'                              AS direction,
        char(31) || :fqn || char(31)        AS visited,
        0                                   AS candidate

    UNION ALL

    SELECT
        COALESCE(e.resolved_target, e.target),
        cg.depth + 1,
        'calls',
        cg.visited || COALESCE(e.resolved_target, e.target) || char(31),
        e.candidate
    FROM   edges      e
    JOIN   call_graph cg  ON cg.fqn = e.source_fqn
    WHERE  e.kind         = 'CALLS'
      AND  cg.depth       < :max_depth
      AND  instr(cg.visited, char(31) || COALESCE(e.resolved_target, e.target) || char(31)) = 0

    UNION ALL

    SELECT
        e.source_fqn,
        cg.depth + 1,
        'called_by',
        cg.visited || e.source_fqn || char(31),
        e.candidate
    FROM   edges      e
    JOIN   call_graph cg  ON cg.fqn = COALESCE(e.resolved_target, e.target)
    WHERE  e.kind         = 'CALLS'
      AND  cg.depth       < :max_depth
      AND  instr(cg.visited, char(31) || e.source_fqn || char(31)) = 0
)
SELECT
    cg.fqn,
    MIN(cg.depth)   AS depth,
    cg.direction,
    s.kind          AS symbol_kind,
    f.path          AS file_path,
    s.start_line,
    s.end_line,
    MIN(cg.candidate) AS candidate
FROM   call_graph  cg
LEFT   JOIN symbols s  ON s.fqn = cg.fqn
LEFT   JOIN files   f  ON f.id  = s.file_id
GROUP  BY cg.fqn, cg.direction
ORDER  BY depth, cg.direction, cg.fqn
"""

# Parser's lowercase edge kinds → DB canonical uppercase
_EDGE_KIND_MAP: dict[str, str] = {
    "import":           "IMPORTS",
    "call":             "CALLS",
    "instantiates":     "INSTANTIATES",
    "owns":             "OWNS",
    "provides_context": "PROVIDES_CONTEXT",
    "consumes_context": "CONSUMES_CONTEXT",
    "extends":          "EXTENDS",
    "implements":       "IMPLEMENTS",
}


def _normalise_edge_kind(raw: str) -> str:
    return _EDGE_KIND_MAP.get(raw.lower(), raw.upper())


# ---------------------------------------------------------------------------
# CodeDB
# ---------------------------------------------------------------------------

class CodeDB:
    """
    Thin wrapper around a SQLite connection for the Code Intelligence Engine.

    Usage
    -----
    with CodeDB("path/to/index.db") as db:
        changed = db.upsert_file(path, hash, symbols, edges, chunks)
        graph   = db.get_call_graph("src/api/auth.ts::AuthService.login", max_depth=3)
    """

    def __init__(self, db_path: str | Path = ".code-index/graph.db") -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._graph_cache: dict[str, list[CallGraphNode]] = {}
        self._adjacency_cache: Optional[dict[str, list[str]]] = None
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CodeDB":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Initialisation + migrations
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._conn.executescript(_PRAGMA_SQL)
        self._conn.executescript(_DDL_SQL)
        self._migrate_edges()
        self._migrate_edge_candidate()
        self._migrate_symbol_locations()

    def _migrate_edge_candidate(self) -> None:
        """
        Idempotent additive migration: add the `candidate` column to edges on
        DBs that predate it (ADR-017 §3). A plain ADD COLUMN suffices — unlike
        the kind CHECK-constraint change, no table swap is needed. No-ops once
        the column exists.
        """
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(edges)").fetchall()
        }
        if "candidate" not in cols:
            self._conn.execute(
                "ALTER TABLE edges ADD COLUMN candidate INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_edges(self) -> None:
        """
        Idempotent migration: expand edges CHECK constraint to include new
        ownership/context edge kinds, and add the resolved_target column.

        Uses table-swap pattern because SQLite cannot ALTER TABLE to change
        a CHECK constraint.  Safe to run on every startup — no-ops if already done.
        """
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(edges)").fetchall()
        }
        if "resolved_target" in cols:
            # Check constraint was already expanded; run PRAGMA optimize once
            try:
                self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            return

        # Drop any leftover temp table from a previous partial migration, then swap
        self._conn.executescript("""
        DROP TABLE IF EXISTS edges_v2;
        CREATE TABLE edges_v2 (
            id              INTEGER PRIMARY KEY,
            source_fqn      TEXT    NOT NULL,
            target          TEXT    NOT NULL,
            kind            TEXT    NOT NULL CHECK(kind IN (
                'IMPORTS','CALLS','INSTANTIATES',
                'OWNS','PROVIDES_CONTEXT','CONSUMES_CONTEXT',
                'EXTENDS','IMPLEMENTS'
            )),
            resolved_target TEXT,
            candidate       INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_fqn, target, kind)
        );
        INSERT OR IGNORE INTO edges_v2(id, source_fqn, target, kind)
            SELECT id, source_fqn, target, kind FROM edges;
        DROP TABLE edges;
        ALTER TABLE edges_v2 RENAME TO edges;
        CREATE INDEX IF NOT EXISTS idx_edges_source      ON edges(source_fqn);
        CREATE INDEX IF NOT EXISTS idx_edges_target      ON edges(target);
        CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source_fqn, kind);
        CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target, kind);
        CREATE INDEX IF NOT EXISTS idx_edges_resolved    ON edges(resolved_target)
            WHERE resolved_target IS NOT NULL;
        PRAGMA optimize;
        """)

    def _migrate_symbol_locations(self) -> None:
        """
        Idempotent additive migration: back-fill symbol_locations from any
        symbols rows that were written before this table existed.

        For Python/TS/JS (Phase 1), each symbol has exactly one location, so
        this is a 1-to-1 copy of (file_id, start_line, end_line, text) from
        symbols.  Phase 2 (C# partial classes) will write multiple rows per
        symbol directly via upsert_file.
        """
        self._conn.executescript("""
        INSERT OR IGNORE INTO symbol_locations
            (symbol_id, file_id, start_line, end_line, text)
        SELECT s.id, s.file_id, s.start_line, s.end_line, s.text
        FROM   symbols s
        WHERE  NOT EXISTS (
            SELECT 1 FROM symbol_locations sl WHERE sl.symbol_id = s.id
        );
        """)

    # ------------------------------------------------------------------
    # Transaction helper
    # ------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # Graph cache management
    # ------------------------------------------------------------------

    def invalidate_graph_cache(self) -> None:
        """Clear all cached graph traversal results after any write to edges."""
        self._graph_cache.clear()
        self._adjacency_cache = None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @staticmethod
    def hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def file_is_unchanged(self, path: str, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row is not None and row[0] == content_hash

    def upsert_file(
        self,
        path: str,
        content_hash: str,
        symbols: list[Symbol],
        edges: list[Edge],
        chunks_by_tier: Optional[dict[int, list[Chunk]]] = None,
        references: Optional[list[Reference]] = None,
        symbol_types: Optional[list[SymbolType]] = None,
    ) -> bool:
        """
        Atomically replace all data for `path`.

        Parameters
        ----------
        chunks_by_tier
            Mapping of tier number → chunk list:
              {1: tier1_chunks, 2: tier2_chunks, 3: tier3_chunks}
        references
            Symbol usage locations from ast_chunker.Reference.
        symbol_types
            Type annotation metadata from ast_chunker.SymbolType.

        Returns True if data was written (file was new or modified).
        """
        if self.file_is_unchanged(path, content_hash):
            return False

        chunks_by_tier = chunks_by_tier or {}
        references = references or []
        symbol_types = symbol_types or []

        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO files(path, content_hash)
                VALUES(?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    indexed_at   = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
                (path, content_hash),
            )
            file_id: int = cur.execute(
                "SELECT id FROM files WHERE path = ?", (path,)
            ).fetchone()[0]

            # ── Stale data removal ──────────────────────────────────────────
            # For shared symbols (C# partial classes / C++ header+impl), the
            # same FQN may appear in multiple files.  We delete the per-file
            # location entries first, then only remove symbols that have no
            # remaining locations (i.e. are not claimed by another file).
            #
            # For non-shared symbols (Python, JS, TS, C# members), each
            # symbol has exactly one location, so deleting that location row
            # and then removing the orphaned symbol is equivalent to the
            # previous DELETE WHERE file_id = ? pattern.

            cur.execute("DELETE FROM symbol_locations WHERE file_id = ?", (file_id,))
            cur.execute(
                "DELETE FROM symbols WHERE file_id = ? "
                "AND id NOT IN (SELECT DISTINCT symbol_id FROM symbol_locations)",
                (file_id,),
            )
            cur.execute("DELETE FROM chunks            WHERE file_id = ?", (file_id,))
            cur.execute("DELETE FROM symbol_references WHERE file_id = ?", (file_id,))

            # symbol_types are keyed by fqn; remove by matching file's symbols
            non_shared_fqns = [s.fqn for s in symbols if not getattr(s, "shared", False)]
            old_fqns        = [s.fqn for s in symbols]
            if old_fqns:
                ph = ",".join("?" * len(old_fqns))
                cur.execute(f"DELETE FROM symbol_types WHERE symbol_fqn IN ({ph})", old_fqns)

            # Outgoing edges for non-shared symbols + file-level import edges.
            # Shared-type edges (EXTENDS, IMPLEMENTS, OWNS from C# types) are
            # NOT deleted here — they accumulate via INSERT OR IGNORE and are
            # only fully cleaned on a full re-index.  This avoids clobbering
            # edges contributed by other files that share the same type FQN.
            old_source_fqns = non_shared_fqns + [path]
            if old_source_fqns:
                ph = ",".join("?" * len(old_source_fqns))
                cur.execute(
                    f"DELETE FROM edges WHERE source_fqn IN ({ph})",
                    old_source_fqns,
                )

            # ── Symbols insert ──────────────────────────────────────────────
            # Shared symbols (partial-class type declarations):
            #   INSERT OR IGNORE — leave the existing row if another file
            #   already claimed this FQN; symbol_locations accumulates below.
            # Non-shared symbols:
            #   INSERT OR REPLACE — always overwrite with the fresh parse.
            shared_rows = [
                (s.fqn, file_id, s.kind, s.name, s.class_context,
                 s.start_line, s.end_line, s.text)
                for s in symbols if getattr(s, "shared", False)
            ]
            non_shared_rows = [
                (s.fqn, file_id, s.kind, s.name, s.class_context,
                 s.start_line, s.end_line, s.text)
                for s in symbols if not getattr(s, "shared", False)
            ]
            if shared_rows:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO symbols
                        (fqn, file_id, kind, name, class_context, start_line, end_line, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    shared_rows,
                )
            if non_shared_rows:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO symbols
                        (fqn, file_id, kind, name, class_context, start_line, end_line, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    non_shared_rows,
                )
            # symbol_locations — one row per (symbol, file); enables multi-location
            # merge for C# partial classes and C++ header/impl split (Phase 2+).
            if symbols:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO symbol_locations
                        (symbol_id, file_id, start_line, end_line, text)
                    SELECT s.id, ?, ?, ?, ?
                    FROM   symbols s WHERE s.fqn = ?
                    """,
                    [
                        (file_id, s.start_line, s.end_line, s.text, s.fqn)
                        for s in symbols
                    ],
                )

            # Chunks
            for tier_num, chunk_list in chunks_by_tier.items():
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO chunks
                        (file_id, scope, tier, start_line, end_line, text, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (file_id, c.scope, tier_num,
                         c.start_line, c.end_line, c.text,
                         " ".join(c.tags))
                        for c in chunk_list
                    ],
                )

            # Edges
            cur.executemany(
                """
                INSERT OR IGNORE INTO edges(source_fqn, target, kind, resolved_target, candidate)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (e.source_fqn, e.target, _normalise_edge_kind(e.kind),
                     getattr(e, "resolved_target", None),
                     int(getattr(e, "candidate", False)))
                    for e in edges
                ],
            )

            # Symbol references
            if references:
                cur.executemany(
                    """
                    INSERT INTO symbol_references
                        (file_id, symbol_name, symbol_fqn, line, ref_kind, context_fqn)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (file_id, r.symbol_name, r.symbol_fqn, r.line,
                         r.ref_kind, r.context_fqn)
                        for r in references
                    ],
                )

            # Symbol types
            if symbol_types:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO symbol_types
                        (symbol_fqn, return_type, params_json, type_params,
                         is_async, is_generator)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (st.fqn, st.return_type,
                         json.dumps(st.params) if st.params else None,
                         st.type_params,
                         int(st.is_async), int(st.is_generator))
                        for st in symbol_types
                    ],
                )

        self.invalidate_graph_cache()
        return True

    def delete_file(self, path: str) -> None:
        """
        Remove a file and all its symbols, chunks, and outgoing edges.
        ON DELETE CASCADE handles symbols, chunks, and symbol_references;
        edges need explicit cleanup because they are not FK-constrained.
        """
        with self._tx() as cur:
            row = cur.execute(
                "SELECT id FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return
            file_id = row[0]

            fqns = [
                r[0] for r in cur.execute(
                    "SELECT fqn FROM symbols WHERE file_id = ?", (file_id,)
                ).fetchall()
            ]
            if fqns:
                ph = ",".join("?" * len(fqns))
                cur.execute(f"DELETE FROM edges WHERE source_fqn IN ({ph})", fqns)
                cur.execute(f"DELETE FROM symbol_types WHERE symbol_fqn IN ({ph})", fqns)
            cur.execute("DELETE FROM edges WHERE source_fqn = ?", (path,))
            cur.execute("DELETE FROM files WHERE id = ?", (file_id,))

        self.invalidate_graph_cache()

    # ------------------------------------------------------------------
    # Graph traversal
    # ------------------------------------------------------------------

    def get_call_graph(
        self,
        fqn: str,
        max_depth: int = 2,
    ) -> list[CallGraphNode]:
        """
        Bidirectional call-graph traversal rooted at `fqn`.
        Results are cached per (fqn, max_depth) key; call invalidate_graph_cache()
        after any write if fresh results are required.
        """
        cache_key = f"{fqn}::{max_depth}"
        if cache_key in self._graph_cache:
            return self._graph_cache[cache_key]

        rows = self._conn.execute(
            _CALL_GRAPH_SQL, {"fqn": fqn, "max_depth": max_depth}
        ).fetchall()
        result = [
            CallGraphNode(
                fqn=r["fqn"],
                depth=r["depth"],
                direction=r["direction"],
                symbol_kind=r["symbol_kind"],
                file_path=r["file_path"],
                start_line=r["start_line"],
                end_line=r["end_line"],
                candidate=bool(r["candidate"]),
            )
            for r in rows
        ]
        self._graph_cache[cache_key] = result
        return result

    def get_callers(self, fqn: str) -> list[CallGraphNode]:
        return [n for n in self.get_call_graph(fqn, max_depth=1) if n.direction == "called_by"]

    def get_callees(self, fqn: str) -> list[CallGraphNode]:
        return [n for n in self.get_call_graph(fqn, max_depth=1) if n.direction == "calls"]

    def get_importers(self, module_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT source_fqn FROM edges WHERE target = ? AND kind = 'IMPORTS'",
            (module_path,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_importers_resolved(self, canonical_path: str) -> list[str]:
        """
        Return file paths that import `canonical_path`, checking resolved_target
        first (set by ImportResolver) and falling back to raw target match.
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT source_fqn FROM edges
            WHERE kind = 'IMPORTS'
              AND (resolved_target = ? OR target = ?)
            """,
            (canonical_path, canonical_path),
        ).fetchall()
        return [r[0] for r in rows]

    def get_owned_by(self, fqn: str) -> list[str]:
        """Return FQNs of symbols owned by `fqn` (OWNS edges where source=fqn)."""
        rows = self._conn.execute(
            "SELECT target FROM edges WHERE source_fqn = ? AND kind = 'OWNS'",
            (fqn,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_owners(self, fqn: str) -> list[str]:
        """Return FQNs of symbols that own `fqn` (OWNS edges where target=fqn)."""
        rows = self._conn.execute(
            "SELECT source_fqn FROM edges WHERE target = ? AND kind = 'OWNS'",
            (fqn,),
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Reference queries
    # ------------------------------------------------------------------

    def get_reference_count(self, symbol_name: str) -> int:
        """How many reference locations exist for this bare symbol name."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM symbol_references WHERE symbol_name = ?",
            (symbol_name,),
        ).fetchone()
        return row[0] if row else 0

    def get_references(self, symbol_name: str, limit: int = 200) -> list[dict]:
        """All reference locations for `symbol_name`, ordered by file+line."""
        rows = self._conn.execute(
            """
            SELECT sr.symbol_name, sr.symbol_fqn, sr.line, sr.ref_kind,
                   sr.context_fqn, f.path
            FROM   symbol_references sr
            JOIN   files f ON f.id = sr.file_id
            WHERE  sr.symbol_name = ?
            ORDER  BY f.path, sr.line
            LIMIT  ?
            """,
            (symbol_name, limit),
        ).fetchall()
        return [
            {
                "symbol_name": r["symbol_name"],
                "symbol_fqn":  r["symbol_fqn"],
                "line":        r["line"],
                "ref_kind":    r["ref_kind"],
                "context_fqn": r["context_fqn"],
                "file":        r["path"],
            }
            for r in rows
        ]

    def get_reference_density(self, fqns: list[str]) -> dict[str, int]:
        """
        Batch reference count lookup keyed by FQN (using bare symbol name).
        Used by the reranker to boost frequently-referenced symbols.
        """
        if not fqns:
            return {}
        # Extract bare names from FQNs (last component after :: and .)
        name_to_fqns: dict[str, list[str]] = {}
        for fqn in fqns:
            parts = fqn.split("::")
            bare = parts[-1].split(".")[-1] if parts else fqn
            name_to_fqns.setdefault(bare, []).append(fqn)

        ph = ",".join("?" * len(name_to_fqns))
        rows = self._conn.execute(
            f"""
            SELECT symbol_name, COUNT(*) AS cnt
            FROM   symbol_references
            WHERE  symbol_name IN ({ph})
            GROUP  BY symbol_name
            """,
            list(name_to_fqns.keys()),
        ).fetchall()

        name_counts = {r["symbol_name"]: r["cnt"] for r in rows}
        result: dict[str, int] = {}
        for name, mapped_fqns in name_to_fqns.items():
            cnt = name_counts.get(name, 0)
            for fqn in mapped_fqns:
                result[fqn] = cnt
        return result

    # ------------------------------------------------------------------
    # Graph adjacency snapshot (for bulk fan-out queries)
    # ------------------------------------------------------------------

    def get_adjacency_snapshot(self) -> dict[str, list[str]]:
        """
        Load the full edges table into a Python dict for session-level
        fan-out queries (e.g. blast-radius analysis).  Cached per session;
        cleared by invalidate_graph_cache().
        """
        if self._adjacency_cache is not None:
            return self._adjacency_cache

        rows = self._conn.execute(
            "SELECT source_fqn, target FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
        adj: dict[str, list[str]] = {}
        for r in rows:
            adj.setdefault(r[0], []).append(r[1])
        self._adjacency_cache = adj
        return adj

    def get_graph_edges(self) -> list[tuple[str, str, str]]:
        """Return every edge as ``(source_fqn, target, kind)`` across all kinds.

        Unlike :meth:`get_adjacency_snapshot` (CALLS only), this exposes the full
        typed edge set so structural analytics (ADR-006) can weight CALLS /
        EXTENDS / IMPLEMENTS / IMPORTS / INSTANTIATES / OWNS / *_CONTEXT
        differently.  ``resolved_target`` is preferred over the raw ``target``
        when present, so edges point at canonical FQNs.  Read-only; reflects the
        current committed graph on the shared connection.
        """
        rows = self._conn.execute(
            "SELECT source_fqn, COALESCE(resolved_target, target) AS tgt, kind FROM edges"
        ).fetchall()
        return [(r["source_fqn"], r["tgt"], r["kind"]) for r in rows]

    # ------------------------------------------------------------------
    # Point lookups and search
    # ------------------------------------------------------------------

    def get_symbol(self, fqn: str) -> Optional[Symbol]:
        row = self._conn.execute(
            "SELECT fqn, kind, name, class_context, start_line, end_line, text "
            "FROM symbols WHERE fqn = ?",
            (fqn,),
        ).fetchone()
        if row is None:
            return None
        return Symbol(
            fqn=row["fqn"],
            kind=row["kind"],
            name=row["name"],
            class_context=row["class_context"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            text=row["text"],
        )

    def search_symbols(
        self,
        name_prefix: str,
        kind: Optional[str] = None,
        limit: int = 20,
    ) -> list[Symbol]:
        if kind:
            rows = self._conn.execute(
                "SELECT fqn, kind, name, class_context, start_line, end_line, text "
                "FROM symbols WHERE name LIKE ? AND kind = ? LIMIT ?",
                (name_prefix + "%", kind, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT fqn, kind, name, class_context, start_line, end_line, text "
                "FROM symbols WHERE name LIKE ? LIMIT ?",
                (name_prefix + "%", limit),
            ).fetchall()
        return [
            Symbol(
                fqn=r["fqn"], kind=r["kind"], name=r["name"],
                class_context=r["class_context"],
                start_line=r["start_line"], end_line=r["end_line"],
                text=r["text"],
            )
            for r in rows
        ]

    def get_file_symbols(self, path: str) -> list[Symbol]:
        rows = self._conn.execute(
            """
            SELECT s.fqn, s.kind, s.name, s.class_context, s.start_line, s.end_line, s.text
            FROM   symbols s
            JOIN   files   f ON f.id = s.file_id
            WHERE  f.path = ?
            ORDER  BY s.start_line
            """,
            (path,),
        ).fetchall()
        return [
            Symbol(
                fqn=r["fqn"], kind=r["kind"], name=r["name"],
                class_context=r["class_context"],
                start_line=r["start_line"], end_line=r["end_line"],
                text=r["text"],
            )
            for r in rows
        ]

    def get_chunks(self, path: str, tier: int = 1) -> list[Chunk]:
        rows = self._conn.execute(
            """
            SELECT c.scope, c.start_line, c.end_line, c.text, c.tags
            FROM   chunks c
            JOIN   files  f ON f.id = c.file_id
            WHERE  f.path = ? AND c.tier = ?
            ORDER  BY c.start_line
            """,
            (path, tier),
        ).fetchall()
        return [
            Chunk(
                text=r["text"],
                file=path,
                start_line=r["start_line"],
                end_line=r["end_line"],
                scope=r["scope"],
                tags=r["tags"].split() if r["tags"] else [],
            )
            for r in rows
        ]

    def search_chunks_by_tag(
        self,
        tag: str,
        tier: int = 1,
        limit: int = 200,
    ) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT c.scope, c.tier, f.path, c.tags
            FROM   chunks c
            JOIN   files  f ON f.id = c.file_id
            WHERE  c.tier = ? AND c.tags LIKE ?
            LIMIT  ?
            """,
            (tier, f"%{tag}%", limit),
        ).fetchall()
        return [
            {"scope": r["scope"], "tier": r["tier"], "file": r["path"], "tags": r["tags"]}
            for r in rows
        ]

    def get_chunk_metadata_for_files(
        self, paths: list[str]
    ) -> list[tuple[str, int, str]]:
        """
        Return (scope, tier_number, file_path) for every chunk belonging to
        any of the given file paths.  Used by the incremental indexer for
        stale FAISS vector removal.
        """
        if not paths:
            return []
        ph = ",".join("?" * len(paths))
        rows = self._conn.execute(
            f"""
            SELECT c.scope, c.tier, f.path
            FROM   chunks c
            JOIN   files  f ON f.id = c.file_id
            WHERE  f.path IN ({ph})
            """,
            paths,
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ------------------------------------------------------------------
    # Summary cache
    # ------------------------------------------------------------------

    def get_cached_summaries(self, text_hashes: list[str]) -> dict[str, str]:
        """Return {text_hash: summary} for every hash that has a cached extraction."""
        if not text_hashes:
            return {}
        ph = ",".join("?" * len(text_hashes))
        rows = self._conn.execute(
            f"SELECT text_hash, summary FROM chunk_summaries WHERE text_hash IN ({ph})",
            text_hashes,
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def cache_summaries(self, pairs: list[tuple[str, str]]) -> None:
        """Persist (text_hash, summary) pairs. OR IGNORE — first write wins."""
        if not pairs:
            return
        with self._tx() as cur:
            cur.executemany(
                "INSERT OR IGNORE INTO chunk_summaries(text_hash, summary) VALUES (?, ?)",
                pairs,
            )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        tables = ("files", "symbols", "chunks", "edges", "symbol_references", "symbol_types")
        return {
            t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables
        }
