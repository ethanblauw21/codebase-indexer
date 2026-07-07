"""PythonAdapter — tree-sitter Python parser."""
from __future__ import annotations

from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_python as tspython

from adapters.base import Edge, ParseResult, Reference, Symbol, TestConventions, build_fqn
from adapters._treesitter import node_text, run_query, skeletonize
from category_tagger import tag_symbol


_GRAMMAR = Language(tspython.language())

_IMPORT_QUERY = """
(import_statement name: (dotted_name) @path)
(import_statement name: (aliased_import name: (dotted_name) @path))
(import_from_statement module_name: (dotted_name) @path)
"""

_CALL_QUERY = """
(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ])
"""


def _extract_calls(node: Node, src: bytes) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for n, _ in run_query(_GRAMMAR, _CALL_QUERY, node):
        name = node_text(n, src)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


class PythonAdapter:
    language_id = "python"
    extensions  = frozenset({".py"})

    def parse(self, path: str, src: bytes) -> ParseResult:
        root = Parser(_GRAMMAR).parse(src).root_node

        import_edges = [
            Edge(source_fqn=path, target=node_text(n, src), kind="import")
            for n, _ in run_query(_GRAMMAR, _IMPORT_QUERY, root)
        ]

        symbols, call_edges = self._extract_symbols(root, src, path)
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
            file_suffixes=["_test.py", "test_.py"],
            in_file_markers=["def test_", "class Test", "@pytest.mark"],
        )

    def project_resolver(self):
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_symbols(
        self, root: Node, src: bytes, file_path: str
    ) -> tuple[list[Symbol], list[Edge]]:
        symbols: list[Symbol] = []
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
                        text          = skeletonize(node, src, {"function_definition"}),
                    )
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
                    symbols.append(sym)
                    for call_name in _extract_calls(node, src):
                        edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
                    if class_ctx:
                        class_fqn = build_fqn(file_path, None, class_ctx)
                        edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
                return

            for child in node.children:
                walk(child, class_ctx)

        walk(root, None)
        return symbols, edges

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
