"""ADR-021: baseline CALLS-edge resolution + graph-traversal consumption.

Populates a CodeDB directly (no embedder) with files/symbols/edges, then asserts
resolve_call_edges() writes resolved_target only for provably-unique targets and that
get_call_graph() (CTE now COALESCEs resolved_target) walks the resolved edges.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB              # noqa: E402
from call_resolver import resolve_call_edges  # noqa: E402


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


def _add_edge(db, source_fqn, target, kind="CALLS", resolved_target=None):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind, resolved_target)"
            " VALUES (?, ?, ?, ?)",
            (source_fqn, target, kind, resolved_target),
        )


def _resolved(db, source_fqn, target):
    row = db._conn.execute(
        "SELECT resolved_target FROM edges WHERE source_fqn = ? AND target = ? AND kind='CALLS'",
        (source_fqn, target),
    ).fetchone()
    return row[0] if row else None


@pytest.fixture
def db(tmp_path):
    d = CodeDB(str(tmp_path / "graph.db"))
    yield d
    d.close()


def test_unique_name_resolves(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_symbol(db, "b.py::lowerBound", "lowerBound", b)
    _add_edge(db, "a.py::caller", "lowerBound")

    stats = resolve_call_edges(db)
    assert stats["resolved"] == 1
    assert _resolved(db, "a.py::caller", "lowerBound") == "b.py::lowerBound"


def test_external_call_stays_unresolved(db):
    a = _add_file(db, "a.py")
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_edge(db, "a.py::caller", "push")  # no in-repo symbol named push

    stats = resolve_call_edges(db)
    assert stats["external"] == 1
    assert _resolved(db, "a.py::caller", "push") is None


def test_name_collision_stays_unresolved(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    c = _add_file(db, "c.py")
    _add_symbol(db, "b.py::foo", "foo", b)
    _add_symbol(db, "c.py::foo", "foo", c)
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_edge(db, "a.py::caller", "foo")  # 2 candidates, caller imports neither

    stats = resolve_call_edges(db)
    assert stats["ambiguous"] == 1
    assert _resolved(db, "a.py::caller", "foo") is None


def test_same_file_tiebreak(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::bar", "bar", a)
    _add_symbol(db, "b.py::bar", "bar", b)
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_edge(db, "a.py::caller", "bar")  # prefer the same-file bar

    resolve_call_edges(db)
    assert _resolved(db, "a.py::caller", "bar") == "a.py::bar"


def test_import_scoped_tiebreak(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    c = _add_file(db, "c.py")
    _add_symbol(db, "b.py::baz", "baz", b)
    _add_symbol(db, "c.py::baz", "baz", c)
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_edge(db, "a.py::caller", "baz")
    # a.py imports b.py only → the b.py candidate wins
    _add_edge(db, "a.py", "b", kind="IMPORTS", resolved_target="b.py")

    resolve_call_edges(db)
    assert _resolved(db, "a.py::caller", "baz") == "b.py::baz"


def test_demotion_when_name_becomes_ambiguous(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_symbol(db, "b.py::qux", "qux", b)
    _add_edge(db, "a.py::caller", "qux")

    resolve_call_edges(db)
    assert _resolved(db, "a.py::caller", "qux") == "b.py::qux"

    # A second qux appears → the call is now ambiguous → demoted back to NULL.
    c = _add_file(db, "c.py")
    _add_symbol(db, "c.py::qux", "qux", c)
    stats = resolve_call_edges(db)
    assert stats["ambiguous"] == 1
    assert _resolved(db, "a.py::caller", "qux") is None


def test_get_call_graph_walks_resolved_edge(db):
    a = _add_file(db, "a.py")
    b = _add_file(db, "b.py")
    _add_symbol(db, "a.py::caller", "caller", a)
    _add_symbol(db, "b.py::lowerBound", "lowerBound", b)
    _add_edge(db, "a.py::caller", "lowerBound")
    resolve_call_edges(db)

    # Forward: caller's callee is the resolved symbol, WITH a real file_path.
    callees = db.get_callees("a.py::caller")
    lb = [n for n in callees if n.fqn == "b.py::lowerBound"]
    assert lb and lb[0].file_path == "b.py"

    # Reverse: lowerBound's caller is found via the resolved target.
    callers = db.get_callers("b.py::lowerBound")
    assert any(n.fqn == "a.py::caller" and n.file_path == "a.py" for n in callers)
