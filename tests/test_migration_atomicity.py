"""ADR-046: schema migrations are atomic (#55), and extraction failures are reported (#54)."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db as db_mod                      # noqa: E402
from db import CodeDB                    # noqa: E402

# The edges table as it stood before ADR-013 added READS/WRITES/ALIAS_OF: every
# column present, so only _migrate_edge_kinds (a table swap) has work to do.
_PRE_ADR013_EDGES = """
CREATE TABLE edges (
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
    confidence      REAL,
    receiver_type   TEXT,
    UNIQUE(source_fqn, target, kind)
);
INSERT INTO edges(source_fqn, target, kind, resolved_target, confidence)
    VALUES ('a.py::f', 'g', 'CALLS', 'b.py::g', 0.9);
"""


def _pre_adr013_db(tmp_path):
    path = str(tmp_path / "graph.db")
    con = sqlite3.connect(path)
    con.executescript(_PRE_ADR013_EDGES)
    con.close()
    return path


def _edges(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT source_fqn, target, kind, resolved_target, confidence "
                           "FROM edges").fetchall()
    finally:
        con.close()


def _table_sql(path, name):
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                          (name,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


_ROW = [("a.py::f", "g", "CALLS", "b.py::g", 0.9)]


def test_swap_migrates_and_keeps_every_column(tmp_path):
    path = _pre_adr013_db(tmp_path)
    CodeDB(path).close()
    assert "'READS'" in _table_sql(path, "edges")
    assert _edges(path) == _ROW


def test_crash_between_drop_and_rename_loses_nothing(tmp_path, monkeypatch):
    """The #55 failure: the process dies after `DROP TABLE edges`, before the rename."""
    path = _pre_adr013_db(tmp_path)
    real = db_mod._split_sql

    def crash_after_drop(script):
        out = []
        for stmt in real(script):
            out.append(stmt)
            if stmt == "DROP TABLE edges;":
                out.append("SELECT crash_here();")      # no such function: raises
        return out

    monkeypatch.setattr(db_mod, "_split_sql", crash_after_drop)
    with pytest.raises(sqlite3.OperationalError):
        CodeDB(path)
    assert _edges(path) == _ROW                          # rolled back: old table intact
    assert _table_sql(path, "edges_v3") is None

    monkeypatch.setattr(db_mod, "_split_sql", real)
    CodeDB(path).close()                                  # the next open finishes the job
    assert "'READS'" in _table_sql(path, "edges")
    assert _edges(path) == _ROW


def test_a_database_left_mid_swap_is_recovered(tmp_path, capsys):
    """A crash before this fix left the rows in edges_v3 and no edges table."""
    path = _pre_adr013_db(tmp_path)
    CodeDB(path).close()
    con = sqlite3.connect(path)
    con.executescript("ALTER TABLE edges RENAME TO edges_v3;")
    con.close()

    CodeDB(path).close()
    assert _edges(path) == _ROW
    assert _table_sql(path, "edges_v3") is None
    assert "interrupted migration" in capsys.readouterr().err


def test_migration_rechecks_under_the_lock(tmp_path):
    """A second process that waited for the lock must not repeat a finished swap."""
    db = CodeDB(str(tmp_path / "graph.db"))
    try:
        checks = iter([False, True])       # not done before the lock, done once held
        ran = db._migrate(lambda: next(checks), "DROP TABLE edges;")
        assert ran is False
        assert db._columns("edges")        # the table is still there
        assert not db._conn.in_transaction
    finally:
        db.close()


def test_failed_migration_leaves_no_open_transaction(tmp_path):
    db = CodeDB(str(tmp_path / "graph.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            db._migrate(lambda: False, "CREATE TABLE t(x);\nSELECT crash_here();")
        assert not db._conn.in_transaction
        assert "x" not in db._columns("t")
    finally:
        db.close()


def test_split_sql_rejects_an_unterminated_statement():
    assert db_mod._split_sql("CREATE TABLE t(x);\nDROP TABLE t;") == \
        ["CREATE TABLE t(x);", "DROP TABLE t;"]
    with pytest.raises(ValueError):
        db_mod._split_sql("CREATE TABLE t(x)")


def test_failed_tree_sitter_query_is_reported_once(capsys):
    from adapters import _treesitter
    from tree_sitter import Language, Parser
    import tree_sitter_python

    lang = Language(tree_sitter_python.language())
    tree = Parser(lang).parse(b"x = 1\n")
    bad = "(no_such_node_type) @x"
    assert _treesitter.run_query(lang, bad, tree.root_node) == []
    assert _treesitter.run_query(lang, bad, tree.root_node) == []
    err = capsys.readouterr().err
    assert err.count("[tree-sitter] query failed") == 1
    assert "no_such_node_type" in err


def test_core_installs_no_process_wide_warnings_filter():
    import warnings
    import core                                           # noqa: F401
    blanket = [f for f in warnings.filters
               if f[0] == "ignore" and f[1] is None and f[2] is Warning and f[3] is None]
    assert blanket == []
