"""ADR-044: imports are captured and resolved, and a call resolves only through the scope its
shape allows.

Adapter half: Python relative imports become edges; each CALLS edge carries `bound_module`
(the import its callee or receiver is bound to, when every collapsed site agrees) and
`member_call`. Import half: `ImportResolver.classify` (TS/JS) and `resolve_python_imports`
(resolution + `external`). Resolver half: the §3 rules, including the legacy rule.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.base import Edge                      # noqa: E402
from adapters.python_adapter import PythonAdapter   # noqa: E402
from adapters.ts_adapter import TypeScriptAdapter   # noqa: E402
from call_resolver import resolve_call_edges        # noqa: E402
from db import CodeDB                               # noqa: E402
from import_resolver import (                       # noqa: E402
    ImportResolver, PythonModuleIndex, resolve_python_imports,
)


def _calls(result) -> dict[str, tuple]:
    return {e.target: (e.bound_module, e.member_call) for e in result.edges if e.kind == "call"}


def _imports(result) -> list[str]:
    return [e.target for e in result.edges if e.kind == "import"]


# ---------------------------------------------------------------------------
# Python adapter
# ---------------------------------------------------------------------------

_PY = b'''
import json
import os.path
import numpy as np
from .store import DocumentStore, get as g
from . import helpers, other as o
from ..pkg import mod
from .star import *

def f(self, d):
    json.loads(d)
    os.getcwd()
    os.path.join("a")
    np.array([])
    g()
    helpers.run()
    o.go()
    mod.work()
    d.get("x")
    self.save()
    local()
    DocumentStore()
'''


def test_python_relative_imports_become_edges():
    got = _imports(PythonAdapter().parse("pkg/sub/a.py", _PY))
    assert sorted(got) == sorted(["json", "os.path", "numpy",
                                  ".store", ".helpers", ".other", "..pkg", ".star"])
    dots_star = b"from . import *\n"
    assert _imports(PythonAdapter().parse("p/a.py", dots_star)) == ["."]


def test_python_call_shape():
    calls = _calls(PythonAdapter().parse("pkg/sub/a.py", _PY))
    assert calls["loads"] == ("json", False)
    assert calls["getcwd"] == ("os.path", False)        # `import os.path` binds `os`
    assert calls["join"] == ("os.path", False)          # the longest imported prefix
    assert calls["array"] == ("numpy", False)           # an alias
    assert calls["g"] == (".store", False)              # a bare call through `from … import`
    assert calls["run"] == (".helpers", False)          # dots only: one edge per name
    assert calls["go"] == (".other", False)
    assert calls["work"] == ("..pkg", False)
    assert calls["DocumentStore"] == (".store", False)
    assert calls["get"] == (None, True)                 # a method on a parameter
    assert calls["save"] == (None, True)
    assert calls["local"] == (None, False)              # an unbound bare call


def test_python_mixed_sites_drop_the_binding():
    src = b"import json\ndef f(d):\n    json.loads(d)\n    loads(d)\n    x.dumps()\n    dumps()\n"
    calls = _calls(PythonAdapter().parse("a.py", src))
    assert calls["loads"] == (None, False)     # bound + unbound bare → unbound
    assert calls["dumps"] == (None, True)      # any member site on a non-import → member


def test_python_name_bound_by_two_imports_is_unknown():
    src = b"from a import x\nfrom b import x\ndef f():\n    x()\n"
    assert _calls(PythonAdapter().parse("m.py", src))["x"] == (None, False)


def test_python_explicit_import_beats_implied_prefix():
    src = b"import a.b\nimport a\ndef f():\n    a.g()\n    a.b.h()\n"
    calls = _calls(PythonAdapter().parse("m.py", src))
    assert calls["g"] == ("a", False)
    assert calls["h"] == ("a.b", False)


# ---------------------------------------------------------------------------
# TS adapter
# ---------------------------------------------------------------------------

_TS = b'''import { z } from "zod";
import * as ns from "./lib/ns";
import def, { helper as h } from "@/util";
export function f(store) {
  z.object({});
  z.string().min(1);
  ns.run();
  h();
  def();
  store.get("x");
  this.save();
  local();
}
'''


def test_ts_call_shape():
    calls = _calls(TypeScriptAdapter().parse("src/a.ts", _TS))
    assert calls["object"] == ("zod", False)
    assert calls["string"] == ("zod", False)
    assert calls["min"] == (None, True)       # receiver is a call result, not the import
    assert calls["run"] == ("./lib/ns", False)
    assert calls["h"] == ("@/util", False)
    assert calls["def"] == ("@/util", False)
    assert calls["get"] == (None, True)
    assert calls["save"] == (None, True)
    assert calls["local"] == (None, False)


def test_ts_classify(tmp_path):
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
    r = ImportResolver(str(tmp_path))
    assert r.classify("zod") is True
    assert r.classify("node:fs") is True
    assert r.classify("@scope/pkg") is True
    assert r.classify("./x") is False
    assert r.classify("../x") is False
    assert r.classify("@/lib/x") is False
    assert r.classify("/abs/x") is None
    assert r.classify("anything", resolved="src/x.ts") is False


def test_ts_alias_keeps_its_slash(tmp_path):
    """`"@/*"` is the prefix `@/`. It used to load as `@`, so `@/lib/x` expanded to the
    absolute `/lib/x` and never resolved, and `@scope/pkg` looked like an alias."""
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "x.ts").write_text("export const x = 1;")
    r = ImportResolver(str(tmp_path))
    assert r.resolve("@/lib/x", "src/a.ts") == "src/lib/x.ts"
    assert r.resolve("@scope/pkg", "src/a.ts") is None


# ---------------------------------------------------------------------------
# Python import resolution
# ---------------------------------------------------------------------------

def test_python_module_index():
    idx = PythonModuleIndex(["src/pkg/__init__.py", "src/pkg/mod.py", "src/pkg/sub/a.py",
                             "src/config.py", "tests/fixtures/x/config.py", "src/tool.py"])
    assert idx.resolve("pkg.mod", "src/x.py") == "src/pkg/mod.py"
    assert idx.resolve("pkg", "src/x.py") == "src/pkg/__init__.py"
    assert idx.resolve("config", "src/x.py") == "src/config.py"        # the shallowest
    assert idx.resolve("..mod", "src/pkg/sub/a.py") == "src/pkg/mod.py"
    assert idx.resolve(".name", "src/pkg/mod.py") == "src/pkg/__init__.py"  # an attribute
    assert idx.resolve("....up", "src/pkg/sub/a.py") is None           # above the root
    assert idx.resolve("json", "src/x.py") is None


@pytest.fixture
def db(tmp_path):
    d = CodeDB(str(tmp_path / "graph.db"))
    yield d
    d.close()


def _add_file(db, path):
    with db._tx() as cur:
        cur.execute("INSERT OR IGNORE INTO files(path, content_hash) VALUES (?, ?)",
                    (path, "h:" + path))
        return cur.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def _add_symbol(db, fqn, name, file_id):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO symbols"
            "(fqn, file_id, kind, name, class_context, start_line, end_line, text)"
            " VALUES (?, ?, 'function', ?, NULL, 1, 2, '')",
            (fqn, file_id, name),
        )


def _add_edge(db, source, target, kind="CALLS", resolved_target=None,
              bound_module=None, member_call=None, external=None):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind, resolved_target,"
            " bound_module, member_call, external) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, target, kind, resolved_target, bound_module, member_call, external),
        )


def _import_row(db, source, target):
    return tuple(db._conn.execute(
        "SELECT resolved_target, external FROM edges"
        " WHERE source_fqn = ? AND target = ? AND kind = 'IMPORTS'", (source, target),
    ).fetchone())


def _resolved(db, source, target):
    row = db._conn.execute(
        "SELECT resolved_target FROM edges WHERE source_fqn = ? AND target = ? AND kind='CALLS'",
        (source, target),
    ).fetchone()
    return row[0] if row else None


def test_resolve_python_imports_resolves_and_classifies(db):
    for p in ("src/app.py", "src/store.py", "src/a/config.py", "src/b/config.py"):
        _add_file(db, p)
    for target in ("json", "numpy", "store", ".store", ".missing", "config"):
        _add_edge(db, "src/app.py", target, kind="IMPORTS")
    _add_edge(db, "src/app.ts", "zod", kind="IMPORTS", external=1)   # not Python: untouched

    stats = resolve_python_imports(db)
    assert _import_row(db, "src/app.py", "json") == (None, 1)        # stdlib
    assert _import_row(db, "src/app.py", "numpy") == (None, 1)       # absent from the repo
    assert _import_row(db, "src/app.py", "store") == ("src/store.py", 0)
    assert _import_row(db, "src/app.py", ".store") == ("src/store.py", 0)
    assert _import_row(db, "src/app.py", ".missing") == (None, 0)    # relative: in-repo
    assert _import_row(db, "src/app.py", "config") == (None, None)   # two at one depth
    assert _import_row(db, "src/app.ts", "zod") == (None, 1)
    assert stats == {"resolved": 2, "unresolved": 4, "external": 2}


def test_upsert_file_round_trips_the_new_columns(db):
    edges = [Edge("a.py", "json", "import", external=True),
             Edge("a.py::f", "loads", "call", bound_module="json", member_call=False),
             Edge("a.py::f", "get", "call", member_call=True),
             Edge("a.py::f", "x", "call")]
    db.upsert_file("a.py", "h", [], edges)
    rows = dict(((t, (b, m, x)) for t, b, m, x in db._conn.execute(
        "SELECT target, bound_module, member_call, external FROM edges")))
    assert rows == {"json": (None, None, 1), "loads": ("json", 0, None),
                    "get": (None, 1, None), "x": (None, None, None)}


# ---------------------------------------------------------------------------
# call_resolver §3
# ---------------------------------------------------------------------------

def test_bound_to_dependency_never_resolves_to_an_in_repo_namesake(db):
    a, s = _add_file(db, "a.py"), _add_file(db, "store.py")
    _add_symbol(db, "store.py::loads", "loads", s)       # the repo's only `loads`
    _add_symbol(db, "a.py::f", "f", a)
    _add_edge(db, "a.py", "json", kind="IMPORTS", external=1)
    _add_edge(db, "a.py::f", "loads", bound_module="json", member_call=0)
    stats = resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "loads") is None
    assert stats["bound_external"] == 1


def test_bound_to_in_repo_file_scopes_to_it(db):
    a, b, c = _add_file(db, "a.ts"), _add_file(db, "b.ts"), _add_file(db, "c.ts")
    _add_symbol(db, "b.ts::run", "run", b)
    _add_symbol(db, "c.ts::run", "run", c)
    _add_symbol(db, "a.ts::f", "f", a)
    _add_edge(db, "a.ts", "./c", kind="IMPORTS", resolved_target="c.ts", external=0)
    _add_edge(db, "a.ts::f", "run", bound_module="./c", member_call=0)
    stats = resolve_call_edges(db)
    assert _resolved(db, "a.ts::f", "run") == "c.ts::run"
    assert stats["bound_scoped"] == 1


def test_bound_to_a_barrel_falls_back_to_unique_repo_wide_only(db):
    a, idx, h = _add_file(db, "a.ts"), _add_file(db, "util/index.ts"), _add_file(db, "util/h.ts")
    _add_symbol(db, "util/h.ts::helper", "helper", h)
    _add_symbol(db, "a.ts::f", "f", a)
    _add_edge(db, "a.ts", "./util", kind="IMPORTS", resolved_target="util/index.ts", external=0)
    _add_edge(db, "a.ts::f", "helper", bound_module="./util", member_call=0)
    resolve_call_edges(db)
    assert _resolved(db, "a.ts::f", "helper") == "util/h.ts::helper"


def test_bound_to_a_barrel_with_a_collision_stays_unresolved(db):
    a, idx = _add_file(db, "a.ts"), _add_file(db, "util/index.ts")
    h, k = _add_file(db, "util/h.ts"), _add_file(db, "other/k.ts")
    _add_symbol(db, "util/h.ts::helper", "helper", h)
    _add_symbol(db, "other/k.ts::helper", "helper", k)
    _add_symbol(db, "a.ts::f", "f", a)
    _add_edge(db, "a.ts", "./util", kind="IMPORTS", resolved_target="util/index.ts", external=0)
    _add_edge(db, "a.ts::f", "helper", bound_module="./util", member_call=0)
    resolve_call_edges(db)
    assert _resolved(db, "a.ts::f", "helper") is None


def test_bound_to_an_unknown_import_stays_unresolved(db):
    a, c = _add_file(db, "a.py"), _add_file(db, "src/config.py")
    _add_symbol(db, "src/config.py::load", "load", c)
    _add_symbol(db, "a.py::f", "f", a)
    _add_edge(db, "a.py", "config", kind="IMPORTS")                    # external NULL
    _add_edge(db, "a.py::f", "load", bound_module="config", member_call=0)
    resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "load") is None


def _two_gets(db, src):
    a, s, o = _add_file(db, f"a.{src}"), _add_file(db, f"store.{src}"), _add_file(db, f"o.{src}")
    _add_symbol(db, f"store.{src}::get", "get", s)
    _add_symbol(db, f"o.{src}::get", "get", o)
    _add_symbol(db, f"a.{src}::f", "f", a)
    _add_edge(db, f"a.{src}", "store", kind="IMPORTS", resolved_target=f"store.{src}", external=0)


def test_unbound_member_call_skips_import_scoping(db):
    _two_gets(db, "py")
    _add_edge(db, "a.py::f", "get", member_call=1)       # `d.get()` on a dict
    resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "get") is None


def test_unbound_bare_call_keeps_import_scoping(db):
    _two_gets(db, "py")
    _add_edge(db, "a.py::f", "get", member_call=0)
    resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "get") == "store.py::get"


def test_legacy_python_row_skips_import_scoping(db):
    _two_gets(db, "py")
    _add_edge(db, "a.py::f", "get")                      # member_call NULL: written pre-ADR
    resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "get") is None


def test_legacy_ts_row_resolves_as_before(db):
    _two_gets(db, "ts")
    _add_edge(db, "a.ts::f", "get")
    resolve_call_edges(db)
    assert _resolved(db, "a.ts::f", "get") == "store.ts::get"


def test_unbound_member_call_keeps_unique_repo_wide(db):
    a, s = _add_file(db, "a.py"), _add_file(db, "store.py")
    _add_symbol(db, "store.py::flush", "flush", s)
    _add_symbol(db, "a.py::f", "f", a)
    _add_edge(db, "a.py::f", "flush", member_call=1)
    resolve_call_edges(db)
    assert _resolved(db, "a.py::f", "flush") == "store.py::flush"   # unchanged by ADR-044
