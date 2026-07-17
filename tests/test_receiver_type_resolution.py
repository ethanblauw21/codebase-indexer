"""ADR-011 Stage 1 — C# receiver-type inference + typed call resolution.

Two halves, both CPU-only (tree-sitter + SQLite, no embedding, no GPU):

* Inference — parse real C# via `parse_file` and assert the adapter stamps the right
  `Edge.receiver_type` for each exact strategy (param / explicit-local / `var`+`new` / field /
  `this`), and leaves it None for bare calls, un-typeable receivers, and generic/external types
  (prefer-unknown, ADR-011 §2).

* Resolution — seed a CodeDB directly and assert `resolve_call_edges` uses the hint to
  disambiguate a name several classes share, grades the hit above the verdict floor (§3), and
  refuses to fall back to a positional guess when the hint matches nothing (§2).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ast_chunker import parse_file            # noqa: E402
from db import CodeDB, EDGE_CONFIDENCE_FLOOR  # noqa: E402
from call_resolver import resolve_call_edges, _TYPED_CONFIDENCE  # noqa: E402


# --------------------------------------------------------------------------- #
# Inference (whole-adapter, via parse_file)
# --------------------------------------------------------------------------- #

_CS = """
namespace Demo
{
    public class Repo
    {
        public void Save() { }
    }

    public class Service
    {
        private Repo _repo;

        public void DoParam(Repo r)          { r.Save(); }
        public void DoLocalExplicit()        { Repo x = Make(); x.Save(); }
        public void DoVarNew()               { var y = new Repo(); y.Save(); }
        public void DoField()                { _repo.Save(); }
        public void DoThis()                 { this.Helper(); }
        public void DoBare()                 { Helper(); }
        public void DoExternalParam(Widget w){ w.Save(); }
        public void DoChain()                { GetThing().Save(); }
        public void DoGeneric()              { var list = new System.Collections.Generic.List<Repo>(); list.Add(_repo); }

        public Repo Make()   { return new Repo(); }
        private void Helper(){ }
    }
}
"""


@pytest.fixture(scope="module")
def cs_edges():
    return [e for e in parse_file("Demo.cs", _CS).edges if e.kind == "call"]


def _recv(edges, method: str, target: str):
    """receiver_type of the call to `target` inside method `method` (by fqn substring)."""
    for e in edges:
        if f".{method}/" in e.source_fqn and e.target == target:
            return e.receiver_type
    raise AssertionError(f"no call edge {method} → {target}")


def test_param_type_inferred(cs_edges):
    assert _recv(cs_edges, "DoParam", "Save") == "Repo"


def test_explicit_local_type_inferred(cs_edges):
    assert _recv(cs_edges, "DoLocalExplicit", "Save") == "Repo"


def test_var_new_object_creation_inferred(cs_edges):
    assert _recv(cs_edges, "DoVarNew", "Save") == "Repo"


def test_field_type_inferred(cs_edges):
    assert _recv(cs_edges, "DoField", "Save") == "Repo"


def test_this_resolves_to_enclosing_type(cs_edges):
    assert _recv(cs_edges, "DoThis", "Helper") == "Service"


def test_bare_call_has_no_receiver_type(cs_edges):
    assert _recv(cs_edges, "DoBare", "Helper") is None


def test_external_param_type_still_named(cs_edges):
    # The receiver's *declared* type is known syntactically even if not in-repo — inference
    # reports it; resolution (below) is what decides in-repo-ness and leaves it unresolved.
    assert _recv(cs_edges, "DoExternalParam", "Save") == "Widget"


def test_chained_receiver_is_unknown(cs_edges):
    # GetThing().Save() — receiver is an invocation, not a simple identifier → prefer-unknown.
    assert _recv(cs_edges, "DoChain", "Save") is None


def test_generic_typed_local_is_unknown(cs_edges):
    # var list = new List<Repo>() — generic_name is not a simple type → None.
    assert _recv(cs_edges, "DoGeneric", "Add") is None


def test_behaviour_preserved_bare_targets_unchanged(cs_edges):
    # The bare callee names are exactly what the old query captured — no regression.
    names = {e.target for e in cs_edges}
    assert {"Save", "Helper", "Make", "Add"} <= names


# --------------------------------------------------------------------------- #
# Resolution (seeded CodeDB, via resolve_call_edges)
# --------------------------------------------------------------------------- #

def _file(db, path):
    with db._tx() as cur:
        cur.execute("INSERT OR IGNORE INTO files(path, content_hash) VALUES (?, ?)",
                    (path, "h:" + path))
        return cur.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def _sym(db, fqn, name, fid, kind="method"):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO symbols"
            "(fqn, file_id, kind, name, class_context, start_line, end_line, text)"
            " VALUES (?, ?, ?, ?, NULL, 1, 2, ?)",
            (fqn, fid, kind, name, name),
        )


def _edge(db, source_fqn, target, kind="CALLS", receiver_type=None):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind, receiver_type)"
            " VALUES (?, ?, ?, ?)",
            (source_fqn, target, kind, receiver_type),
        )


def _row(db, source_fqn, target):
    return db._conn.execute(
        "SELECT resolved_target, confidence FROM edges "
        "WHERE source_fqn=? AND target=? AND kind='CALLS'",
        (source_fqn, target),
    ).fetchone()


@pytest.fixture
def db(tmp_path):
    d = CodeDB(str(tmp_path / "graph.db"))
    yield d
    d.close()


def _seed_two_saves(db):
    """Two classes, each with a Save() — a name a bare-name resolver can't settle."""
    f = _file(db, "svc.cs")
    _sym(db, "Foo", "Foo", f, kind="class")
    _sym(db, "Bar", "Bar", f, kind="class")
    _sym(db, "Foo.Save/0", "Save", f)
    _sym(db, "Bar.Save/0", "Save", f)
    _sym(db, "Svc.M/0", "M", f)
    _edge(db, "Foo", "Foo.Save/0", kind="OWNS")
    _edge(db, "Bar", "Bar.Save/0", kind="OWNS")
    return f


def test_receiver_type_disambiguates_shared_name(db):
    _seed_two_saves(db)
    _edge(db, "Svc.M/0", "Save", receiver_type="Foo")

    stats = resolve_call_edges(db)
    assert stats["typed"] == 1
    resolved, conf = _row(db, "Svc.M/0", "Save")
    assert resolved == "Foo.Save/0"         # picked Foo's Save, not Bar's
    assert conf == _TYPED_CONFIDENCE


def test_typed_resolution_clears_floor_and_walks_graph(db):
    _seed_two_saves(db)
    _edge(db, "Svc.M/0", "Save", receiver_type="Foo")
    resolve_call_edges(db)

    # Graded above the floor → reads as verified, and the CTE walks the resolved target.
    callee = [n for n in db.get_callees("Svc.M/0") if n.fqn == "Foo.Save/0"]
    assert callee, "resolved typed edge not traversed"
    assert callee[0].confidence == _TYPED_CONFIDENCE
    assert callee[0].confidence >= EDGE_CONFIDENCE_FLOOR


def test_bare_shared_name_stays_ambiguous_without_hint(db):
    """Control: the SAME graph without a receiver hint cannot resolve — proving the hint,
    not some positional accident, is what disambiguates."""
    _seed_two_saves(db)
    _edge(db, "Svc.M/0", "Save")            # no receiver_type

    stats = resolve_call_edges(db)
    assert stats["ambiguous"] == 1
    resolved, _ = _row(db, "Svc.M/0", "Save")
    assert resolved is None


def test_hint_matching_nothing_is_unknown_not_guessed(db):
    """Prefer-unknown: a receiver type with no in-repo owner match leaves the edge
    unresolved — it does NOT fall back to a positional guess (§2)."""
    _seed_two_saves(db)
    _edge(db, "Svc.M/0", "Save", receiver_type="Baz")   # no class named Baz

    stats = resolve_call_edges(db)
    assert stats["typed"] == 0
    resolved, conf = _row(db, "Svc.M/0", "Save")
    assert resolved is None
    assert conf is None                     # stays below the floor (derives from candidate)


def test_hint_but_external_name_counts_external(db):
    """receiver_type set but the callee name has zero in-repo symbols → external, unresolved."""
    _seed_two_saves(db)
    _edge(db, "Svc.M/0", "Persist", receiver_type="Foo")   # nothing named Persist

    stats = resolve_call_edges(db)
    assert stats["external"] == 1
    resolved, _ = _row(db, "Svc.M/0", "Persist")
    assert resolved is None


def test_bare_path_unchanged_regression(db):
    """A unique bare name still resolves exactly as ADR-021 did — the new regime is additive."""
    a = _file(db, "a.cs")
    b = _file(db, "b.cs")
    _sym(db, "a.cs::caller", "caller", a)
    _sym(db, "b.cs::onlyOne", "onlyOne", b)
    _edge(db, "a.cs::caller", "onlyOne")

    stats = resolve_call_edges(db)
    assert stats["resolved"] == 1
    resolved, _ = _row(db, "a.cs::caller", "onlyOne")
    assert resolved == "b.cs::onlyOne"
