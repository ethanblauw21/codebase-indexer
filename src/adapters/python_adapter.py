"""PythonAdapter — tree-sitter Python parser."""
from __future__ import annotations

import re

from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_python as tspython

from adapters.base import Edge, ParseResult, Reference, Symbol, TestConventions, build_fqn
from adapters._treesitter import (
    merge_adjacent_same_fqn, node_text, run_query, skeleton_with_lines,
)
from category_tagger import tag_symbol


_GRAMMAR = Language(tspython.language())

_IMPORT_QUERY = """
(import_statement name: (dotted_name) @path)
(import_statement name: (aliased_import name: (dotted_name) @path))
(import_from_statement module_name: (dotted_name) @path)
"""

# ADR-044 §1: `from .x import y` parses its module as a relative_import, which the query
# above never matched, so every relative import was dropped.
_RELATIVE_IMPORT_QUERY = "(import_from_statement module_name: (relative_import) @rel)"

_CALL_QUERY = """
(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ])
"""


_OVERLOAD_DECORATOR = re.compile(r"@(?:[\w.]+\.)?overload\b")


def _is_overload(node: Node, src: bytes) -> bool:
    """Is this function_definition an `@overload` stub? Its decorators are siblings under a
    decorated_definition, outside the symbol's own text."""
    parent = node.parent
    return (parent is not None and parent.type == "decorated_definition"
            and any(_OVERLOAD_DECORATOR.match(node_text(c, src))
                    for c in parent.children if c.type == "decorator"))


def _relative_import_targets(root: Node, src: bytes) -> list[str]:
    """Import targets for relative imports, as written: `.x`, `..a.b` (ADR-044 §1).

    Dots only (`from . import a, b as c`) gives one target per imported name (`.a`, `.b`),
    because Python binds the submodule when one exists and a bare `.` names nothing
    specific. A wildcard (`from . import *`) has no names and keeps the bare prefix.
    """
    targets: list[str] = []
    for rel, _ in run_query(_GRAMMAR, _RELATIVE_IMPORT_QUERY, root):
        spec = node_text(rel, src)
        if any(c.type == "dotted_name" for c in rel.children):
            targets.append(spec)
            continue
        names = []
        for n in rel.parent.children_by_field_name("name"):
            if n.type == "aliased_import":
                n = n.child_by_field_name("name")
            names.append(spec + node_text(n, src))
        targets.extend(names or [spec])
    return targets


def _import_bindings(root: Node, src: bytes) -> dict[str, Optional[str]]:
    """This file's `local dotted name -> import specifier` map (ADR-044 §1).

    The specifier is the IMPORTS edge target the name came from, so the resolver can look
    that edge up. `import a.b` binds `a.b` and `a`; `import a.b as x` binds `x`;
    `from m import n as k` binds `k` to `m`. A name bound by two different imports maps to
    None: it is still an import, but which one is unknown. Wildcards bind nothing.
    """
    # A name a statement binds outright beats the `a` that `import a.b` implies.
    exact: dict[str, Optional[str]] = {}
    implied: dict[str, Optional[str]] = {}

    def bind(name: str, spec: str, is_exact: bool = True) -> None:
        tier = exact if is_exact else implied
        tier[name] = spec if tier.get(name, spec) == spec else None

    def walk(node: Node) -> None:
        if node.type == "import_statement":
            for n in node.children_by_field_name("name"):
                if n.type == "aliased_import":
                    path, alias = n.child_by_field_name("name"), n.child_by_field_name("alias")
                    if path is not None and alias is not None:
                        bind(node_text(alias, src), node_text(path, src))
                elif n.type == "dotted_name":
                    spec = node_text(n, src)
                    bind(spec, spec)
                    parts = spec.split(".")
                    for i in range(1, len(parts)):
                        bind(".".join(parts[:i]), spec, is_exact=False)
            return
        if node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            if mod is None:
                return
            spec = node_text(mod, src)
            dots_only = (mod.type == "relative_import"
                         and not any(c.type == "dotted_name" for c in mod.children))
            for n in node.children_by_field_name("name"):
                local = n
                if n.type == "aliased_import":
                    local = n.child_by_field_name("alias")
                    n = n.child_by_field_name("name")
                if local is None or n is None:
                    continue
                # `from . import a` is its own edge, `.a` (see _relative_import_targets).
                bind(node_text(local, src), spec + node_text(n, src) if dots_only else spec)
            return
        for child in node.children:
            walk(child)

    walk(root)
    return {**implied, **exact}


def _dotted(node: Node, src: bytes) -> Optional[str]:
    """`a` or `a.b.c` when the node is a plain identifier chain, else None (`f().x`, `a[0]`)."""
    if node.type == "identifier":
        return node_text(node, src)
    if node.type == "attribute":
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        head = _dotted(obj, src) if obj is not None else None
        if head is not None and attr is not None:
            return f"{head}.{node_text(attr, src)}"
    return None


_UNBOUND = object()


def _bound_spec(name: Optional[str], bindings: dict[str, Optional[str]]):
    """The binding of the longest prefix of `name` that is an import, else _UNBOUND."""
    if name is None:
        return _UNBOUND
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in bindings:
            return bindings[prefix]
    return _UNBOUND


def _extract_calls(
    node: Node, src: bytes, bindings: dict[str, Optional[str]]
) -> list[tuple[str, Optional[str], bool]]:
    """(callee name, bound_module, member_call) per distinct callee, in first-seen order.

    ADR-044 §1: one CALLS edge collapses every site with the same callee name, so
    `bound_module` is kept only when every site binds through the same import, and
    `member_call` is True when any site calls a method on something that is not an import.
    """
    order: list[str] = []
    sites: dict[str, list[tuple[object, bool]]] = {}
    for n, _ in run_query(_GRAMMAR, _CALL_QUERY, node):
        name = node_text(n, src)
        parent = n.parent
        if parent is not None and parent.type == "attribute":
            recv = parent.child_by_field_name("object")
            spec = _bound_spec(_dotted(recv, src) if recv is not None else None, bindings)
            site = (spec, spec is _UNBOUND)
        else:
            site = (_bound_spec(name, bindings), False)
        if name not in sites:
            order.append(name)
            sites[name] = []
        sites[name].append(site)

    result = []
    for name in order:
        specs = {spec for spec, _ in sites[name]}
        only = next(iter(specs)) if len(specs) == 1 else _UNBOUND
        bound = only if isinstance(only, str) else None
        result.append((name, bound, any(member for _, member in sites[name])))
    return result


class PythonAdapter:
    language_id = "python"
    extensions  = frozenset({".py"})

    def parse(self, path: str, src: bytes) -> ParseResult:
        root = Parser(_GRAMMAR).parse(src).root_node

        import_edges = [
            Edge(source_fqn=path, target=node_text(n, src), kind="import")
            for n, _ in run_query(_GRAMMAR, _IMPORT_QUERY, root)
        ] + [
            Edge(source_fqn=path, target=t, kind="import")
            for t in _relative_import_targets(root, src)
        ]

        symbols, call_edges = self._extract_symbols(root, src, path, _import_bindings(root, src))
        references          = self._extract_references(root, src, symbols)

        return ParseResult(
            symbols    = symbols,
            edges      = import_edges + call_edges,
            references = references,
            symbol_types = [],
        )

    def analyze_tags(
        self,
        path: str,
        src: bytes,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        fqn_tags: dict[str, list[str]] = {}
        for sym in symbols:
            cat_tags = tag_symbol(sym.name, sym.text)
            if cat_tags:
                fqn_tags[sym.fqn] = cat_tags
        return [], fqn_tags

    def test_conventions(self):
        return TestConventions(
            file_suffixes=["_test.py"],
            in_file_markers=["def test_", "class Test", "@pytest.mark"],
            file_globs=["test_*.py"],   # pytest's default is a prefix; a suffix cannot say it
        )

    def project_resolver(self):
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_symbols(
        self, root: Node, src: bytes, file_path: str,
        bindings: Optional[dict[str, Optional[str]]] = None,
    ) -> tuple[list[Symbol], list[Edge]]:
        bindings = bindings or {}
        symbols: list[Symbol] = []
        rank_of: dict[int, tuple[int, int]] = {}
        edges:   list[Edge]   = []

        def walk(node: Node, class_ctx: Optional[str]) -> None:
            t = node.type

            if t == "class_definition":
                name_node = next((c for c in node.children if c.type == "identifier"), None)
                if name_node:
                    name = node_text(name_node, src)
                    fqn  = build_fqn(file_path, None, name)
                    sym  = Symbol(
                        fqn           = fqn,
                        kind          = "class",
                        name          = name,
                        class_context = None,
                        start_line    = node.start_point[0] + 1,
                        end_line      = node.end_point[0] + 1,
                        text          = "",
                    )
                    sym.text, sym.line_map = skeleton_with_lines(
                        node, src, {"function_definition", "decorated_definition"})
                    symbols.append(sym)
                    # Inheritance: `class Dog(Animal, base.Mixin):` -> extends edges.
                    # The superclass list is an `argument_list` child; each positional
                    # base is an identifier (Animal) or attribute (base.Mixin -> Mixin,
                    # matching the call convention of the final identifier). Keyword args
                    # (metaclass=...) are not base classes and are skipped.
                    supers = next((c for c in node.children if c.type == "argument_list"), None)
                    if supers:
                        for base in supers.children:
                            if base.type == "identifier":
                                base_name = node_text(base, src)
                            elif base.type == "attribute":
                                attr = base.child_by_field_name("attribute")
                                base_name = node_text(attr, src) if attr else None
                            else:
                                base_name = None
                            if base_name:
                                edges.append(Edge(source_fqn=fqn, target=base_name, kind="extends"))
                    body = next((c for c in node.children if c.type == "block"), None)
                    if body:
                        for child in body.children:
                            walk(child, name)
                return

            if t == "function_definition":
                name_node = next((c for c in node.children if c.type == "identifier"), None)
                if name_node:
                    name = node_text(name_node, src)
                    fqn  = build_fqn(file_path, class_ctx, name)
                    sym  = Symbol(
                        fqn           = fqn,
                        kind          = "method" if class_ctx else "function",
                        name          = name,
                        class_context = class_ctx,
                        start_line    = node.start_point[0] + 1,
                        end_line      = node.end_point[0] + 1,
                        text          = node_text(node, src),
                    )
                    # A merged run (ADR-034 §3): `@overload` stubs last, then the member with
                    # the most code first, so the implementation stays inside the embedder's
                    # window whether a property's logic is in its getter or its setter.
                    rank_of[id(sym)] = (1 if _is_overload(node, src) else 0, -len(sym.text))
                    symbols.append(sym)
                    for call_name, bound, member in _extract_calls(node, src, bindings):
                        edges.append(Edge(source_fqn=fqn, target=call_name, kind="call",
                                          bound_module=bound, member_call=member))
                    if class_ctx:
                        class_fqn = build_fqn(file_path, None, class_ctx)
                        edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
                return

            for child in node.children:
                walk(child, class_ctx)

        walk(root, None)
        # `@overload` stubs are signatures without bodies, and the implementation's own
        # signature subsumes them; merging them in only diluted its vector (ADR-034 arm 3).
        # They are dropped when an implementation of the same name exists, kept otherwise.
        stubs = {id(s) for s in symbols if rank_of.get(id(s), (0, 0))[0] == 1}
        implemented = {(s.fqn, s.class_context) for s in symbols if id(s) not in stubs}
        symbols = [s for s in symbols
                   if id(s) not in stubs or (s.fqn, s.class_context) not in implemented]
        merged = [m for m, _ in merge_adjacent_same_fqn(
            symbols, lambda s: rank_of.get(id(s), (0, 0)), merge_top_level=True)]
        return merged, edges

    def _extract_references(
        self, root: Node, src: bytes, symbols: list[Symbol]
    ) -> list[Reference]:
        sorted_syms = sorted(symbols, key=lambda s: s.start_line)

        def find_context_fqn(line: int) -> Optional[str]:
            for sym in sorted_syms:
                if sym.start_line <= line <= sym.end_line:
                    return sym.fqn
            return None

        refs: list[Reference] = []
        for n, _ in run_query(_GRAMMAR, _CALL_QUERY, root):
            name = node_text(n, src)
            if not name or len(name) <= 1:
                continue
            line = n.start_point[0] + 1
            refs.append(Reference(
                symbol_name = name,
                symbol_fqn  = None,
                line        = line,
                ref_kind    = "CALL",
                context_fqn = find_context_fqn(line),
            ))
        return refs
