"""B-037: analyze_blast_radius / find_dead_code import detection.

The tools used `(import|require).*?['"].*?NAME.*?['"]` with DOTALL over each file's
joined chunk text. On a file that did not import NAME it backtracked across the whole
text: ~20 s for db.py, 10-20 min per tool call on this repo. It also could not match a
Python import (no quotes). These tests pin the replacement: IMPORTS edges plus a linear
scan of quoted specifiers, matched on whole module names.

Model-free: a CodeDB wired in as MCPServer._db, as in test_verdict_edge_evidence.py.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB  # noqa: E402
import MCPServer as M  # noqa: E402


@pytest.fixture
def wired_db(tmp_path, monkeypatch):
    db = CodeDB(str(tmp_path / "graph.db"))
    monkeypatch.setattr(M, "_db", lambda: db)
    yield db
    db.close()


def _add_import(db, source, target, resolved=None):
    with db._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind, resolved_target)"
            " VALUES (?, ?, 'IMPORTS', ?)",
            (source, target, resolved),
        )


@pytest.mark.parametrize("spec, stem", [
    ("./lib/foo.js", "foo"),
    ("../foo", "foo"),
    ("@/lib/bar/index", "bar"),
    ("./bar/index.ts", "bar"),
    ("scan_policy", "scan_policy"),
    ("adapters.base", "base"),
    (".sibling", "sibling"),
    ("src/scan_policy.py", "scan_policy"),
    ("src\\Widgets\\Grid.TSX", "grid"),
    ("./foo.component", "foo.component"),
    ("foo.component.ts", "foo.component"),
])
def test_module_stem(spec, stem):
    assert M._module_stem(spec) == stem


def test_specifier_forms_the_ts_adapter_does_not_record():
    text = (
        "const a = require('./alpha');\n"
        "const b = await import(\"../beta.js\");\n"
        "export { g } from './gamma/index';\n"
        "import {\n  x,\n  y,\n} from '@/delta';\n"
    )
    assert M._specifier_stems(text) == {"alpha", "beta", "gamma", "delta"}


def test_specifier_scan_is_linear_on_text_that_used_to_backtrack():
    """Prose full of 'import'/'required' and quotes, with no import of the anchor:
    the shape that took the old pattern ~20 s at 150K chars."""
    line = 'It is important that "required" fields are \'quoted\' when we import them.\n'
    text = line * 4000          # ~300K chars, the size of MCPServer.py's chunk text
    t0 = time.perf_counter()
    M._specifier_stems(text)
    assert time.perf_counter() - t0 < 1.0


def test_python_importers_come_from_edges(wired_db):
    """Python imports have no quotes; only the IMPORTS edges can see them."""
    _add_import(wired_db, "src/MCPServer.py", "scan_policy")
    _add_import(wired_db, "src/incremental_indexer.py", "scan_policy")
    _add_import(wired_db, "src/scan_policy.py", "config")
    _add_import(wired_db, "src/core.py", "os")
    texts = {p: "" for p in ("src/MCPServer.py", "src/incremental_indexer.py",
                             "src/scan_policy.py", "src/core.py", "src/config.py")}

    importers, imported = M._import_relations("scan_policy.py", texts)

    assert importers == {"src/MCPServer.py", "src/incremental_indexer.py"}
    assert "config" in imported


def test_commonjs_importers_come_from_text(wired_db):
    texts = {
        "src/utils/sheet.js": "function read() {}",
        "src/main.js": "const sheet = require('./utils/sheet');\nread();",
        "src/other.js": "const s = require('./utils/sheetHelpers');",   # not the anchor
    }
    importers, _ = M._import_relations("src/utils/sheet.js", texts)
    assert importers == {"src/main.js"}


def test_whole_name_match_not_substring(wired_db):
    """The old test matched 'foo' inside './foobar'."""
    _add_import(wired_db, "a.ts", "./foobar")
    texts = {"a.ts": "import x from './foobar';", "foo.ts": ""}
    importers, _ = M._import_relations("foo.ts", texts)
    assert importers == set()


def test_anchor_is_not_its_own_importer(wired_db):
    texts = {"src/sheet.js": "// re-export\nexport * from './sheet';"}
    importers, _ = M._import_relations("sheet.js", texts)
    assert importers == set()
