"""ADR-017 §3 Phase 1: Edge.candidate field — data-model round-trip + migration.

Populates a CodeDB directly (no embedder) with candidate and resolved edges,
then asserts the flag survives the write path, propagates through the call-graph
CTE onto CallGraphNode (MIN over reaching edges), and that the additive column
migration back-fills a pre-candidate DB.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB  # noqa: E402
from adapters.base import Edge  # noqa: E402


def _add_file(db, path):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO files(path, content_hash) VALUES (?, ?)",
            (path, "h:" + path),
        )
        return cur.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def _add_symbol(db, fqn, name, file_id, kind="function"):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO symbols"
            "(fqn, file_id, kind, name, class_context, start_line, end_line, text)"
            " VALUES (?, ?, ?, ?, NULL, 1, 2, ?)",
            (fqn, file_id, kind, name, f"def {name}(): ..."),
        )


def _add_edge(db, source_fqn, target, kind="CALLS", resolved_target=None, candidate=0):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges"
            "(source_fqn, target, kind, resolved_target, candidate) VALUES (?, ?, ?, ?, ?)",
            (source_fqn, target, kind, resolved_target, candidate),
        )


@pytest.fixture
def db(tmp_path):
    d = CodeDB(str(tmp_path / "graph.db"))
    yield d
    d.close()


def test_edge_dataclass_defaults_false():
    assert Edge("a::f", "g", "call").candidate is False


def test_upsert_file_writes_candidate(db):
    """The Edge.candidate flag survives db.upsert_file's edge write path."""
    _add_file(db, "a.py")
    db.upsert_file(
        "a.py", "hash-a", symbols=[],
        edges=[
            Edge("a.py::caller", "solid", "call", resolved_target="b.py::solid"),
            Edge("a.py::caller", "loose", "call", resolved_target="b.py::loose", candidate=True),
        ],
    )
    rows = {
        r[0]: r[1]
        for r in db._conn.execute(
            "SELECT target, candidate FROM edges WHERE source_fqn = 'a.py::caller'"
        ).fetchall()
    }
    assert rows == {"solid": 0, "loose": 1}


def test_candidate_propagates_to_call_graph_node(db):
    """A callee reached by a candidate edge is flagged; a resolved edge is not."""
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::caller_solid", "caller_solid", a)
    _add_symbol(db, "a.py::caller_loose", "caller_loose", a)
    _add_symbol(db, "b.py::solid_target", "solid_target", b)
    _add_symbol(db, "b.py::loose_target", "loose_target", b)
    _add_edge(db, "a.py::caller_solid", "solid_target",
              resolved_target="b.py::solid_target", candidate=0)
    _add_edge(db, "a.py::caller_loose", "loose_target",
              resolved_target="b.py::loose_target", candidate=1)
    db.invalidate_graph_cache()

    solid_callers = db.get_callers("b.py::solid_target")
    loose_callers = db.get_callers("b.py::loose_target")
    assert len(solid_callers) == 1 and solid_callers[0].candidate is False
    assert len(loose_callers) == 1 and loose_callers[0].candidate is True


def test_min_semantics_any_resolved_edge_verifies(db):
    """A node reached by both a resolved and a candidate edge is verified (MIN=0)."""
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_symbol(db, "b.py::t", "t", b)
    # Same caller→callee via a candidate INSTANTIATES-shaped and a resolved CALLS;
    # both are CALLS here but differ only by candidate — UNIQUE(source,target,kind)
    # allows one CALLS row, so use two distinct callers reaching the same target.
    _add_symbol(db, "a.py::caller2", "caller2", a)
    _add_edge(db, "a.py::caller", "t", resolved_target="b.py::t", candidate=1)
    _add_edge(db, "a.py::caller2", "t", resolved_target="b.py::t", candidate=0)
    db.invalidate_graph_cache()

    callers = {n.fqn: n.candidate for n in db.get_callers("b.py::t")}
    assert callers == {"a.py::caller": True, "a.py::caller2": False}


def test_additive_migration_backfills_pre_candidate_db(tmp_path):
    """A DB whose edges table predates the candidate column gets it, defaulting 0."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_fqn TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            resolved_target TEXT,
            UNIQUE(source_fqn, target, kind)
        );
        INSERT INTO edges(source_fqn, target, kind) VALUES ('x::f', 'g', 'CALLS');
        """
    )
    conn.commit()
    conn.close()

    db = CodeDB(path)  # runs _migrate_edge_candidate on init
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edges)").fetchall()}
        assert "candidate" in cols
        row = db._conn.execute(
            "SELECT candidate FROM edges WHERE source_fqn = 'x::f'"
        ).fetchone()
        assert row[0] == 0
    finally:
        db.close()
