"""ADR-053 §1 (B-043): the community graph holds only nodes the repo defines.

A small Python package goes through the real pipeline the indexer runs, model-free:
`parse_file` → `CodeDB.upsert_file` → `resolve_python_imports` → `resolve_call_edges`
(the path `tools/resolution_eval.py` uses). Then `get_graph_edges()`, `analyze()` and the
`map_module_communities` tool read it. Before ADR-053, every unresolved `append()`,
`bool()`, `ArgumentParser()`, `import os` and `class X(Protocol)` became a community member.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ast_chunker import parse_file  # noqa: E402
from call_resolver import resolve_call_edges  # noqa: E402
from db import CodeDB  # noqa: E402
from import_resolver import resolve_python_imports  # noqa: E402
import graph_analytics  # noqa: E402
import MCPServer as M  # noqa: E402

SOURCES = {
    "pkg/__init__.py": "",
    "pkg/store.py": (
        "import os\n"
        "from typing import Protocol\n"
        "\n"
        "class Reader(Protocol):\n"
        "    def read(self): ...\n"
        "\n"
        "class Base:\n"
        "    def save(self, rows):\n"
        "        rows.append(1)\n"
        "        return bool(rows)\n"
        "\n"
        "class Store(Base):\n"
        "    def load(self):\n"
        "        return os.listdir('.')\n"
        "\n"
        "def open_store():\n"
        "    return Store()\n"
    ),
    "pkg/cli.py": (
        "from argparse import ArgumentParser\n"
        "from pkg.store import open_store\n"
        "\n"
        "def main():\n"
        "    parser = ArgumentParser()\n"
        "    store = open_store()\n"
        "    helper(store)\n"
        "\n"
        "def helper(store):\n"
        "    return len(store)\n"
    ),
    # Two unrelated files each define `Animal`: a `Dog(Animal)` in a third file is ambiguous.
    "zoo/a.py": "class Animal:\n    pass\n",
    "zoo/b.py": "class Animal:\n    pass\n",
    "zoo/dog.py": "class Dog(Animal):\n    pass\n",
    "zoo/cat.py": "class Animal:\n    pass\n\nclass Cat(Animal):\n    pass\n",
}

EXTERNAL = {"append", "bool", "ArgumentParser", "listdir", "len", "os", "argparse", "typing",
            "Protocol", "Animal"}


def _build(db_path) -> CodeDB:
    db = CodeDB(str(db_path))
    for rel, text in SOURCES.items():
        result = parse_file(rel, text)
        db.upsert_file(rel, "h:" + rel, result.symbols, result.edges)
    resolve_python_imports(db)
    resolve_call_edges(db)
    return db


@pytest.fixture
def graph_db(tmp_path):
    db = _build(tmp_path / "graph.db")
    yield db
    db.close()


def test_the_raw_edges_do_hold_external_names(graph_db):
    """The premise: without the filter these names would be nodes."""
    raw = {t for (t,) in graph_db._conn.execute("SELECT COALESCE(resolved_target, target) FROM edges")}
    assert {"append", "bool", "ArgumentParser", "os", "Protocol"} <= raw


def test_every_edge_ends_at_a_symbol_or_file_of_the_repo(graph_db):
    nodes = ({f for (f,) in graph_db._conn.execute("SELECT fqn FROM symbols")}
             | {p for (p,) in graph_db._conn.execute("SELECT path FROM files")})
    edges = graph_db.get_graph_edges()
    assert edges
    assert {t for _, t, _ in edges} <= nodes


def test_resolved_import_links_survive(graph_db):
    edges = graph_db.get_graph_edges()
    assert ("pkg/cli.py", "pkg/store.py", "IMPORTS") in edges
    assert not any(t in ("os", "argparse", "typing") for _, t, _ in edges)


def test_resolved_calls_and_ownership_survive(graph_db):
    edges = graph_db.get_graph_edges()
    assert ("pkg/cli.py::main", "pkg/store.py::open_store", "CALLS") in edges
    assert ("pkg/cli.py::main", "pkg/cli.py::helper", "CALLS") in edges
    assert ("pkg/store.py::Store", "pkg/store.py::Store.load", "OWNS") in edges


def test_heritage_binds_to_an_in_repo_type_and_drops_the_rest(graph_db):
    edges = graph_db.get_graph_edges()
    heritage = {(s, t) for s, t, k in edges if k == "EXTENDS"}
    assert ("pkg/store.py::Store", "pkg/store.py::Base") in heritage
    assert ("zoo/cat.py::Cat", "zoo/cat.py::Animal") in heritage      # same file wins
    assert not any(s == "zoo/dog.py::Dog" for s, _ in heritage)          # three Animals: ambiguous
    assert not any(t == "Protocol" for _, t in heritage)                 # not a repo type


def test_communities_contain_no_unresolved_external_names(graph_db):
    analysis = graph_analytics.analyze(graph_db)
    members = {m for c in analysis.communities for m in c.members}
    assert members
    assert not members & EXTERNAL
    assert {"pkg/cli.py", "pkg/store.py"} <= members    # the import link keeps both files


def test_map_module_communities_reports_no_external_name(tmp_path, monkeypatch):
    import incremental_indexer
    db = _build(tmp_path / "graph.db")
    db.meta_set("last_verified_at", "2026-10-20T00:00:00Z")
    db.close()
    monkeypatch.setattr(incremental_indexer, "INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(M, "_ensure_indexes", lambda: None)

    out = M.map_module_communities(min_community_size=1)

    assert "open_store" in out
    for name in ("append", "ArgumentParser", "Protocol", "listdir"):
        assert name not in out
