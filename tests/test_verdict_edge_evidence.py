"""ADR-023 §3 / ADR-017 §7: the edge-aware verdict primitives.

Model-free: builds a CodeDB directly (candidate + resolved CALLS edges) and drives
MCPServer's verdict helpers against it via a monkeypatched ``_db``. Covers FQN
resolution (name → path::symbol, anchor-scoped) and the verified/candidate caller
split with the safe-direction dedup (a resolved sighting beats a candidate one).
The full verdict tools route candidate generation through ``_search`` (needs the
embedder) and are covered by the live golden-diff, not here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB  # noqa: E402
import MCPServer as M  # noqa: E402


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


def _add_edge(db, source_fqn, target, resolved_target=None, candidate=0):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges"
            "(source_fqn, target, kind, resolved_target, candidate)"
            " VALUES (?, ?, 'CALLS', ?, ?)",
            (source_fqn, target, resolved_target, candidate),
        )


@pytest.fixture
def wired_db(tmp_path, monkeypatch):
    """A populated CodeDB wired in as MCPServer._db()."""
    db = CodeDB(str(tmp_path / "graph.db"))
    monkeypatch.setattr(M, "_db", lambda: db)
    yield db
    db.close()


def test_resolve_symbol_fqns_scopes_by_anchor(wired_db):
    """Same symbol name in two files → anchor basename picks the right FQN."""
    a = _add_file(wired_db, "pkg/target.py")
    b = _add_file(wired_db, "pkg/other.py")
    _add_symbol(wired_db, "pkg/target.py::widget", "widget", a)
    _add_symbol(wired_db, "pkg/other.py::widget", "widget", b)

    assert set(M._resolve_symbol_fqns("widget")) == {
        "pkg/target.py::widget", "pkg/other.py::widget",
    }
    assert M._resolve_symbol_fqns("widget", "target.py") == ["pkg/target.py::widget"]
    # Unknown anchor → fall back to all matches rather than returning nothing.
    assert set(M._resolve_symbol_fqns("widget", "nope.py")) == {
        "pkg/target.py::widget", "pkg/other.py::widget",
    }


def test_caller_evidence_splits_verified_and_candidate(wired_db):
    """Resolved callers land in verified; candidate-only callers in candidate."""
    a = _add_file(wired_db, "a.py")
    t = _add_file(wired_db, "t.py")
    _add_symbol(wired_db, "a.py::solid", "solid", a)
    _add_symbol(wired_db, "a.py::loose", "loose", a)
    _add_symbol(wired_db, "t.py::target", "target", t)
    _add_edge(wired_db, "a.py::solid", "target", resolved_target="t.py::target", candidate=0)
    _add_edge(wired_db, "a.py::loose", "target", resolved_target="t.py::target", candidate=1)
    wired_db.invalidate_graph_cache()

    verified, candidate = M._caller_evidence("target", "t.py")
    assert {n.fqn for n in verified} == {"a.py::solid"}
    assert {n.fqn for n in candidate} == {"a.py::loose"}


def test_caller_evidence_resolved_sighting_wins_dedup(wired_db):
    """A caller reaching the symbol via both a candidate and a resolved edge
    (across two same-named target FQNs) is verified, never double-listed."""
    a = _add_file(wired_db, "a.py")
    t1 = _add_file(wired_db, "t1.py")
    t2 = _add_file(wired_db, "t2.py")
    # Symbol name "target" resolves to two FQNs.
    _add_symbol(wired_db, "t1.py::target", "target", t1)
    _add_symbol(wired_db, "t2.py::target", "target", t2)
    _add_symbol(wired_db, "a.py::caller", "caller", a)
    # Same caller: candidate edge into t1.target, resolved edge into t2.target.
    # Distinct raw `target` strings so both survive UNIQUE(source, target, kind)
    # — the resolution to the same-named symbol happens via resolved_target.
    _add_edge(wired_db, "a.py::caller", "target_c", resolved_target="t1.py::target", candidate=1)
    _add_edge(wired_db, "a.py::caller", "target_r", resolved_target="t2.py::target", candidate=0)
    wired_db.invalidate_graph_cache()

    verified, candidate = M._caller_evidence("target")
    assert {n.fqn for n in verified} == {"a.py::caller"}
    assert candidate == []


def test_caller_evidence_empty_for_unknown_symbol(wired_db):
    assert M._caller_evidence("does_not_exist") == ([], [])
