"""ADR-034 §3 and §4: same-FQN siblings merge; Python skeletons stub decorated methods."""
from ast_chunker import chunk_file_ast, parse_file

PY = '''import typing as t


class A:
    @property
    def x(self) -> int:
        return self._x

    @x.setter
    def x(self, v):
        self._x = v

    @t.overload
    def f(self, a: int) -> int: ...
    @t.overload
    def f(self, a: str) -> str: ...
    def f(self, a):
        return a

    @staticmethod
    def s():
        return "static body"


@t.overload
def g(a: int) -> int: ...
def g(a):
    return a
'''

TS = '''class Q {
  set c(v: number) { this.x = v; this.#drain(); }
  get c(): number { return 1; }
}

describe("a", () => {
  it("one", () => { const useStore = () => 1; });
  it("two", () => { const useStore = () => 2; });
});
'''


def _syms(path, src):
    return [s for s in parse_file(path, src).symbols]


def test_python_property_and_setter_merge_longer_first():
    x = [s for s in _syms("a.py", PY) if s.fqn == "a.py::A.x"]
    assert len(x) == 1
    assert x[0].text.startswith("def x(self) -> int:")
    assert "def x(self, v):" in x[0].text
    assert (x[0].start_line, x[0].end_line) == (6, 11)


def test_python_overload_stubs_are_dropped_when_implemented():
    f = [s for s in _syms("a.py", PY) if s.fqn == "a.py::A.f"]
    assert len(f) == 1
    assert f[0].text.startswith("def f(self, a):")
    assert f[0].text.count("def f(") == 1


def test_python_overload_stubs_without_implementation_are_kept():
    stubs = '''import typing as t

@t.overload
def h(a: int) -> int: ...
@t.overload
def h(a: str) -> str: ...
'''
    h = [s for s in _syms("s.py", stubs) if s.fqn == "s.py::h"]
    assert len(h) == 1 and h[0].text.count("def h(") == 2


def test_python_module_level_overloads_merge():
    g = [s for s in _syms("a.py", PY) if s.fqn == "a.py::g"]
    assert len(g) == 1 and g[0].text.startswith("def g(a):")


def test_python_skeleton_stubs_decorated_methods():
    skel = next(s for s in _syms("a.py", PY) if s.fqn == "a.py::A").text
    assert "static body" not in skel
    assert "return self._x" not in skel
    assert "@staticmethod" in skel


def test_python_function_text_leaves_decorators_out():
    """Decorators such as long @pytest.mark.parametrize tables would push members past
    tier 1's 500 tokens (ADR-034 arm 3); they are read from the tree for ordering only."""
    s = next(s for s in _syms("a.py", PY) if s.fqn == "a.py::A.s")
    assert s.text.startswith("def s():")


def test_ts_accessor_pair_merges_more_code_first_with_getter_type():
    result = parse_file("q.ts", TS)
    c = [s for s in result.symbols if s.fqn == "q.ts::Q.c"]
    assert len(c) == 1
    assert c[0].text.startswith("set c(")
    assert "get c()" in c[0].text
    types = [t for t in result.symbol_types if t.fqn == "q.ts::Q.c"]
    assert len(types) == 1 and types[0].return_type == "number"


def test_ts_scattered_top_level_redeclarations_do_not_merge():
    stores = [s for s in _syms("q.test.ts", TS) if s.name == "useStore"]
    assert len(stores) == 2


def test_merged_pair_is_one_chunk():
    scopes = [c.scope for c in chunk_file_ast("q.ts", TS)]
    assert scopes.count("q.ts::Q.c") == 1
