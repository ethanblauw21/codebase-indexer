"""ADR-050: get_file_outline, get_symbol_source and changed_symbols, model-free.

Each fixture parses real files with the real adapters and persists them through the
real `CodeDB.upsert_file` (the path `tools/resolution_eval.py` uses), with no chunks,
so nothing is embedded. `MCPServer._db` is pointed at that database, as in
test_what_writes. The diff tests build a temporary git repository and commit inside
the test.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import anyio
import pytest

import MCPServer as M
from ast_chunker import parse_file
from db import CodeDB
from source import md5_file

MOD_PY = '''\
import os


class Greeter:
    """Says hi."""

    def __init__(self, name: str) -> None:
        self.name = name

    @staticmethod
    def hello(
        x: int,
        y: int = 2,
    ) -> str:
        return "hi"


def top(a, b):
    return a + b


async def run():
    pass
'''

SVC_TS = '''\
export class Svc extends Base implements Api {
  async login(user: string, pw: string): Promise<boolean> {
    return true;
  }
}
export const arrow = (a: number) => a + 1;
export function run(): void {}
'''

EMPTY_PY = 'X = 1\nprint(X)\n'


def git(repo, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}"
    return done.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _index(db: CodeDB, root: Path, rels: list[str], hashes: dict[str, str] | None = None,
           texts: dict[str, str] | None = None) -> None:
    """Parse each file and upsert it, hashed the way the folder source hashes it."""
    for rel in rels:
        text = texts[rel] if texts else (root / rel).read_text(encoding="utf-8")
        result = parse_file(rel, text)
        h = hashes[rel] if hashes else md5_file(str(root / rel))
        db.upsert_file(rel, h, result.symbols, result.edges,
                       references=result.references)


def _wire(monkeypatch, db: CodeDB, texts: dict[str, str] | None = None) -> None:
    monkeypatch.setattr(M, "_db", lambda: db)
    monkeypatch.setattr(M, "_ensure_indexes", lambda: None)
    monkeypatch.setattr(M, "_texts_by_file", lambda: dict(texts or {}))


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    """A folder-mode index of three files, the server's cwd at the repo root."""
    repo = tmp_path / "repo"
    _write(repo, "pkg/mod.py", MOD_PY)
    _write(repo, "pkg/empty.py", EMPTY_PY)
    _write(repo, "web/svc.ts", SVC_TS)
    _write(repo, "notes/readme.txt", "not indexed\n")
    db = CodeDB(str(tmp_path / "graph.db"))
    _index(db, repo, ["pkg/mod.py", "pkg/empty.py", "web/svc.ts"])
    db.meta_set("source", "worktree")
    monkeypatch.chdir(repo)
    _wire(monkeypatch, db)
    yield repo, db
    db.close()


# ── get_file_outline (B-017) ─────────────────────────────────────────────────

def test_outline_lists_classes_methods_and_signatures_without_bodies(indexed):
    out = M.get_file_outline("pkg/mod.py")

    assert "OUTLINE: pkg/mod.py (5 symbol(s))" in out
    lines = out.splitlines()
    assert "L4-15  class  class Greeter" in lines
    assert "  L7-8  method  def __init__(self, name: str) -> None" in lines
    assert "  L11-15  method  def hello(x: int, y: int = 2) -> str" in lines
    assert "L18-19  function  def top(a, b)" in lines
    assert "L22-23  function  async def run()" in lines
    assert "return a + b" not in out and "self.name = name" not in out
    assert "changed on disk" not in out


def test_outline_of_a_typescript_file(indexed):
    out = M.get_file_outline("web/svc.ts")
    assert "L1-5  class  class Svc extends Base implements Api" in out
    assert "  L2-4  method  async login(user: string, pw: string): Promise<boolean>" in out
    assert "L6-6  arrow_function  const arrow = (a: number)" in out


@pytest.mark.parametrize("spelling", [
    "pkg\\mod.py", "./pkg/mod.py", "mod.py", "PKG/MOD.PY", "<abs>",
])
def test_outline_accepts_either_separator_relative_and_absolute_paths(indexed, spelling):
    repo, _ = indexed
    if spelling == "<abs>":
        spelling = str(repo / "pkg" / "mod.py")
    assert "OUTLINE: pkg/mod.py" in M.get_file_outline(spelling)


def test_outline_of_an_indexed_file_with_no_symbols_says_so(indexed):
    out = M.get_file_outline("pkg/empty.py")
    assert "(0 symbol(s))" in out
    assert "indexed but defines no symbols" in out


def test_outline_of_an_unknown_file_is_an_error_naming_the_case(indexed):
    with pytest.raises(LookupError, match="is not in the index"):
        M.get_file_outline("pkg/nope.py")
    with pytest.raises(LookupError, match="The file exists"):
        M.get_file_outline("notes/readme.txt")
    with pytest.raises(LookupError, match="Indexed files with that name"):
        M.get_file_outline("other/mod.py")
    with pytest.raises(ValueError, match="file is required"):
        M.get_file_outline("  ")


def test_outline_with_an_ambiguous_tail_lists_the_files(indexed, monkeypatch):
    repo, db = indexed
    _write(repo, "lib/mod.py", "def other():\n    pass\n")
    _index(db, repo, ["lib/mod.py"])
    with pytest.raises(ValueError, match="matches 2 indexed files") as exc:
        M.get_file_outline("mod.py")
    assert "lib/mod.py" in str(exc.value) and "pkg/mod.py" in str(exc.value)


def test_outline_flags_a_file_changed_on_disk_since_indexing(indexed):
    repo, _ = indexed
    (repo / "pkg" / "mod.py").write_text("# new first line\n" + MOD_PY, encoding="utf-8")
    assert "changed on disk since it was indexed" in M.get_file_outline("pkg/mod.py")


def test_every_tool_fails_on_an_empty_index(tmp_path, monkeypatch):
    db = CodeDB(str(tmp_path / "graph.db"))
    monkeypatch.chdir(tmp_path)
    _wire(monkeypatch, db)
    for call in (lambda: M.get_file_outline("a.py"),
                 lambda: M.get_symbol_source("top"),
                 lambda: M.changed_symbols("main")):
        with pytest.raises(RuntimeError, match="No index here"):
            call()
    db.close()


@pytest.mark.parametrize("text, path, sig", [
    ("[Serializable]\n  public partial class Order : IOrder {\n  }", "Order.cs",
     "public partial class Order : IOrder"),
    ("int f(int x) const;", "w.cpp", "int f(int x) const"),
    ("template <typename T> T id(T v) { return v; }", "w.cpp", "template <typename T> T id(T v)"),
    ("type Alias = string;", "a.ts", "type Alias = string"),
    ("def f(x={}):  # trailing\n    return x", "a.py", "def f(x={})"),
    ("class A(Base, metaclass=M):\n    x: int = 3\n", "a.py", "class A(Base, metaclass=M)"),
])
def test_signature_is_the_declaration_up_to_the_body(text, path, sig):
    assert M._signature(text, path) == sig


# ── get_symbol_source (B-016) ────────────────────────────────────────────────

def test_symbol_source_returns_just_that_span_with_its_lines(indexed):
    out = M.get_symbol_source("top")
    assert out.splitlines() == [
        "--- pkg/mod.py::top  [function]  pkg/mod.py:18-19 ---",
        "   18  def top(a, b):",
        "   19      return a + b",
    ]


def test_symbol_source_of_a_class_is_its_real_text_not_the_skeleton(indexed):
    out = M.get_symbol_source("Greeter")
    assert 'return "hi"' in out and "self.name = name" in out
    assert " ..." not in out


def test_symbol_source_lists_candidates_for_an_ambiguous_name(indexed):
    out = M.get_symbol_source("run")
    assert "'run' is ambiguous: 2 symbols match" in out
    assert "pkg/mod.py::run  [function]  pkg/mod.py:22-23" in out
    assert "web/svc.ts::run  [function]  web/svc.ts:7-7" in out
    assert "pass" not in out.split("\n", 1)[1]           # no body was guessed

    narrowed = M.get_symbol_source("run", file="web\\svc.ts")
    assert narrowed.splitlines()[1] == "    7  export function run(): void {}"
    assert "Greeter.hello" not in M.get_symbol_source("Greeter.hello").split("\n", 1)[1]
    assert "def hello(" in M.get_symbol_source("Greeter.hello")
    assert "def hello(" in M.get_symbol_source("pkg/mod.py::Greeter.hello")


def test_unknown_symbol_is_an_error(indexed):
    with pytest.raises(LookupError, match="No symbol 'nosuch'"):
        M.get_symbol_source("nosuch")
    with pytest.raises(LookupError, match="in web/svc.ts"):
        M.get_symbol_source("top", file="web/svc.ts")


def test_an_edit_elsewhere_in_the_file_keeps_the_span_and_says_so(indexed):
    repo, _ = indexed
    (repo / "pkg" / "mod.py").write_text(MOD_PY.replace("pass", "return 1"), encoding="utf-8")
    out = M.get_symbol_source("top")
    assert "has changed since it was indexed, but this symbol's lines have not" in out
    assert "   18  def top(a, b):" in out


def test_a_moved_symbol_is_served_from_its_new_lines(indexed):
    repo, _ = indexed
    (repo / "pkg" / "mod.py").write_text("# one\n# two\n" + MOD_PY, encoding="utf-8")
    out = M.get_symbol_source("top")
    assert "indexed at lines 18-19, now at 20-21" in out
    assert "   20  def top(a, b):" in out and "   21      return a + b" in out


def test_a_symbol_changed_since_indexing_is_refused_not_served_stale(indexed):
    repo, _ = indexed
    (repo / "pkg" / "mod.py").write_text(MOD_PY.replace("return a + b", "return a - b"),
                                         encoding="utf-8")
    with pytest.raises(RuntimeError, match="has changed since it was indexed") as exc:
        M.get_symbol_source("top")
    assert "a + b" not in str(exc.value)


def test_a_deleted_file_is_an_error(indexed):
    repo, _ = indexed
    (repo / "pkg" / "mod.py").unlink()
    with pytest.raises(RuntimeError, match="could not be read"):
        M.get_symbol_source("top")


def test_git_mode_reads_the_indexed_commit_not_the_working_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _write(repo, "pkg/mod.py", MOD_PY)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    commit = git(repo, "rev-parse", "HEAD")
    db = CodeDB(str(tmp_path / "graph.db"))
    _index(db, repo, ["pkg/mod.py"], hashes={"pkg/mod.py": git(repo, "rev-parse", "HEAD:pkg/mod.py")})
    db.meta_set("source", "git:main")
    db.meta_set("last_indexed_commit", commit)
    monkeypatch.chdir(repo)
    _wire(monkeypatch, db)
    # The working tree moves on; the index is the commit.
    (repo / "pkg" / "mod.py").write_text(MOD_PY.replace("return a + b", "return a * b"),
                                         encoding="utf-8")

    out = M.get_symbol_source("top")
    assert f"(from commit {commit[:10]} (git:main): the index's source, not your working tree)" in out
    assert "   19      return a + b" in out
    assert "changed" not in out
    assert f"as of commit {commit[:10]}" in M.get_file_outline("pkg/mod.py")
    db.close()


# ── changed_symbols (B-020) ──────────────────────────────────────────────────

LIB_V1 = '''\
def alpha(x):
    y = x + 1
    return y


def beta(x):
    return x * 2


def gamma(x):
    a = 1
    b = 2
    return a + b + x


class K:
    def m(self):
        return 1

    def n(self):
        return 2
'''

# beta's body changed, one line deleted inside gamma, one blank line deleted between
# alpha and beta, K.m's body changed; alpha and K.n untouched.
LIB_V2 = '''\
def alpha(x):
    y = x + 1
    return y

def beta(x):
    return x * 3


def gamma(x):
    a = 1
    return a + x


class K:
    def m(self):
        return 10

    def n(self):
        return 2
'''

USE_PY = '''\
from pkg.lib import beta


def user():
    return beta(1)
'''


@pytest.fixture
def diffed(tmp_path, monkeypatch):
    """main at v1; a branch commits v2, adds pkg/new.py, deletes pkg/gone.py and edits
    a README. A folder-mode index of the branch's working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _write(repo, "pkg/lib.py", LIB_V1)
    _write(repo, "pkg/use.py", USE_PY)
    _write(repo, "pkg/gone.py", "def gone():\n    pass\n")
    _write(repo, "README.md", "v1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "v1")
    git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "pkg/lib.py", LIB_V2)
    _write(repo, "pkg/new.py", "def fresh():\n    return 0\n")
    (repo / "pkg" / "gone.py").unlink()
    _write(repo, "README.md", "v2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v2")

    rels = ["pkg/lib.py", "pkg/use.py", "pkg/new.py"]
    db = CodeDB(str(tmp_path / "graph.db"))
    _index(db, repo, rels)
    from call_resolver import resolve_call_edges
    from import_resolver import resolve_python_imports
    resolve_python_imports(db)
    resolve_call_edges(db)
    db.meta_set("source", "worktree")
    monkeypatch.chdir(repo)
    _wire(monkeypatch, db, {r: (repo / r).read_text(encoding="utf-8") for r in rels})
    yield repo, db
    db.close()


def _symbols(out: str) -> set[str]:
    return {line.split()[-1] for line in out.splitlines() if line.startswith("  L")}


def test_changed_symbols_reports_the_changed_function_not_the_untouched_one(diffed):
    out = M.changed_symbols("main")

    assert _symbols(out) == {"pkg/lib.py::beta", "pkg/lib.py::gamma", "pkg/lib.py::K.m",
                             "pkg/new.py::fresh"}
    assert "pkg/lib.py (modified)" in out and "pkg/new.py (added)" in out
    assert "Deleted since the merge base (their symbols are not in the index): pkg/gone.py" in out
    assert "Changed but not indexed (1): README.md" in out
    assert "on disk since" not in out


def test_changed_symbols_diffs_from_the_merge_base(diffed):
    """main moving on after the branch left it does not show up as this branch's change."""
    repo, _ = diffed
    git(repo, "checkout", "-q", "main")
    _write(repo, "pkg/lib.py", LIB_V1.replace("return 2", "return 22"))
    git(repo, "commit", "-q", "-am", "main moves")
    git(repo, "checkout", "-q", "work")
    assert "pkg/lib.py::K.n" not in M.changed_symbols("main")


def test_changed_symbols_includes_uncommitted_and_untracked_work(diffed):
    repo, db = diffed
    _write(repo, "pkg/lib.py", LIB_V2.replace("y = x + 1", "y = x + 100"))
    _write(repo, "pkg/extra.py", "def later():\n    pass\n")
    _write(repo, "build/out.txt", "not indexed, not reported\n")
    _index(db, repo, ["pkg/extra.py"])
    db.delete_file("pkg/lib.py")
    _index(db, repo, ["pkg/lib.py"])

    out = M.changed_symbols("HEAD")
    assert _symbols(out) == {"pkg/lib.py::alpha", "pkg/extra.py::later"}
    assert "pkg/extra.py (untracked)" in out
    assert "build/out.txt" not in out


def test_changed_symbols_flags_files_the_index_has_not_caught_up_with(diffed):
    repo, _ = diffed
    _write(repo, "pkg/lib.py", LIB_V2.replace("return x * 3", "return x * 4"))
    out = M.changed_symbols("main")
    assert "1 file(s) changed on disk since they were indexed" in out
    assert "pkg/lib.py" in out.split("on disk since", 1)[1].splitlines()[0]


def test_changed_symbols_with_blast_radius_names_the_callers(diffed):
    out = M.changed_symbols("main", blast_radius=True)
    beta = out.split("pkg/lib.py::beta", 1)[1].splitlines()[1]
    assert "callers:" in beta and "pkg/use.py::user" in beta
    assert "imported by 1: pkg/use.py" in out


def test_changed_symbols_limit_caps_the_listing_not_the_count(diffed):
    out = M.changed_symbols("main", limit=1)
    assert "4 symbol(s) in 2 file(s)" in out
    assert len(_symbols(out)) == 1
    assert "3 more symbol(s); pass limit=0" in out


def test_a_bad_ref_fails_clearly(diffed):
    with pytest.raises(ValueError, match="does not name a commit"):
        M.changed_symbols("no-such-branch")
    with pytest.raises(ValueError, match="ref is required"):
        M.changed_symbols("")


@pytest.mark.parametrize("ref", ["--output=x", "-p", "main --output=x", "ma\nin"])
def test_an_option_like_or_malformed_ref_is_rejected_before_git_sees_it(diffed, ref):
    repo, _ = diffed
    with pytest.raises(ValueError, match="is not a git ref"):
        M.changed_symbols(ref)
    assert not (repo / "x").exists()


def test_the_ref_is_checked_before_the_index_or_git(tmp_path, monkeypatch):
    db = CodeDB(str(tmp_path / "graph.db"))
    monkeypatch.chdir(tmp_path)
    _wire(monkeypatch, db)
    with pytest.raises(ValueError, match="is not a git ref"):
        M.changed_symbols("--output=x")
    db.close()


def test_changed_symbols_outside_a_git_repo_fails_clearly(indexed, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(Path.cwd().parent))
    with pytest.raises(RuntimeError, match="needs a git repository"):
        M.changed_symbols("main")


def test_git_mode_diffs_against_the_indexed_commit(diffed):
    repo, db = diffed
    db.meta_set("source", "git:work")
    db.meta_set("last_indexed_commit", git(repo, "rev-parse", "work"))
    _write(repo, "pkg/lib.py", LIB_V2.replace("y = x + 1", "y = x + 100"))   # uncommitted
    out = M.changed_symbols("main")
    assert "→ indexed commit" in out
    assert "pkg/lib.py::alpha" not in _symbols(out)
    assert "pkg/lib.py::beta" in _symbols(out)


def test_hunk_parsing_ignores_deleted_lines_that_look_like_headers():
    diff = (
        "diff --git a.sql a.sql\n--- a.sql\n+++ a.sql\n"
        "@@ -3,2 +2,0 @@\n--- a comment\n-x\n"
        "@@ -9 +8,3 @@\n-y\n+++ not a header\n+b\n+c\n"
    )
    [f] = M._diff_hunks(diff)
    assert f["new"] == "a.sql" and f["points"] == [2] and f["ranges"] == [(8, 10)]


# ── through the MCP layer ────────────────────────────────────────────────────

def _call(name: str, args: dict):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def main():
        async with create_connected_server_and_client_session(M.mcp._mcp_server) as client:
            return await client.call_tool(name, args)
    return anyio.run(main)


def test_the_tools_are_registered_as_read_tools():
    names = {t.name for t in M.mcp._tool_manager.list_tools()}
    assert {"get_file_outline", "get_symbol_source", "changed_symbols"} <= names
    for name in ("get_file_outline", "get_symbol_source", "changed_symbols"):
        doc = M.mcp._tool_manager.get_tool(name).description
        assert len(doc) < 800, name


@pytest.mark.parametrize("name, args", [
    ("get_file_outline", {"file": "pkg/nope.py"}),
    ("get_symbol_source", {"symbol": "nosuch"}),
    ("changed_symbols", {"ref": "--output=x"}),
    ("get_file_outline", {}),
])
def test_failures_reach_the_client_as_errors(indexed, name, args):
    result = _call(name, args)
    assert result.isError, result


def test_success_reaches_the_client_as_a_result(indexed):
    result = _call("get_file_outline", {"file": "pkg/mod.py"})
    assert not result.isError
    assert "class Greeter" in result.content[0].text
    result = _call("get_symbol_source", {"symbol": "run"})
    assert not result.isError and "ambiguous" in result.content[0].text
