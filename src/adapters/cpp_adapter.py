"""CppAdapter — tree-sitter C++ parser and project resolver.

FQN format (permanent — bakes into stable IDs):
  Types:    ns::ClassName
  Nested:   ns::Outer::Inner  (:: separator — C++ convention, not CLR +)
  Methods:  ns::ClassName::method(type1, type2)
  Free fns: ns::funcName(type1, type2)
  Typedefs: ns::AliasName
  Enums:    ns::EnumName

Header/impl unification: shared=True for ALL C++ symbols.
  A declaration (.h) and its definition (.cpp) produce the same FQN.
  INSERT OR IGNORE means the first-indexed file (usually .h) wins the primary
  symbol row; the .cpp definition adds a symbol_locations row.
  See ADR-003 §2.3.

Parameter type normalization rules (pinned in conformance suite):
  1. Taken syntactically as written — no canonicalization.
  2. Collapse multiple whitespace → single space.
  3. No space before * or & (e.g. "T &" → "T&", "T *" → "T*").
  4. Parameter names stripped; unnamed params kept as-is.
  5. cv-qualifiers on the function itself (trailing const/noexcept) NOT in FQN.

Documented blind spots (ADR-003 §2.3):
  - Preprocessor macros: macro-generated functions are invisible to the graph.
  - Template instantiations: definitions index fine; instantiations are invisible.
  - Function pointers and virtual dispatch: resolve to declared type only.
  - Operator overloads: indexed as symbols but rarely earn call edges.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_cpp as tscpp

from adapters.base import Edge, ParseResult, Reference, Symbol, TestConventions
from adapters._treesitter import node_text, run_query
from category_tagger import tag_symbol

_GRAMMAR = Language(tscpp.language())

# ---------------------------------------------------------------------------
# Tree-sitter queries
# ---------------------------------------------------------------------------

# Matches direct function calls: foo(), ns::foo(), obj.method()
_CALL_QUERY = """
(call_expression
  function: [
    (identifier) @name
    (qualified_identifier name: (identifier) @name)
    (field_expression field: (field_identifier) @name)
  ])
"""

# ---------------------------------------------------------------------------
# C++ security markers for analyze_tags
# ---------------------------------------------------------------------------

_UNSAFE_BUF_CALLS = frozenset({
    "strcpy", "strncpy", "strcat", "strncat",
    "sprintf", "vsprintf", "gets", "scanf", "sscanf",
    "memcpy", "memmove",
})
_EXEC_CALLS = frozenset({
    "system", "popen", "execl", "execlp", "execle",
    "execv", "execvp", "execve",
})

# ---------------------------------------------------------------------------
# Type string normalization helpers
# ---------------------------------------------------------------------------

_MULTI_SPACE_RE = re.compile(r'\s+')
_SPACE_BEFORE_REFPTR_RE = re.compile(r'\s+([*&])')


def _normalize_cpp_type(s: str) -> str:
    """
    Normalize a C++ type string for FQN inclusion.

    Rules (pinned in conformance suite, permanent):
      1. Collapse multiple whitespace → single space.
      2. No space before * or & (e.g. "T &" → "T&").
      3. Strip leading/trailing whitespace.
    """
    s = _MULTI_SPACE_RE.sub(' ', s).strip()
    s = _SPACE_BEFORE_REFPTR_RE.sub(r'\1', s)
    return s


def _find_inner_id(node: Node, src: bytes) -> Optional[str]:
    """Find the innermost identifier inside a declarator chain."""
    for child in node.children:
        if child.type == 'identifier':
            return node_text(child, src)
        if child.type in ('reference_declarator', 'pointer_declarator',
                          'rvalue_reference_declarator'):
            found = _find_inner_id(child, src)
            if found:
                return found
    return None


def _find_param_name(param: Node, src: bytes) -> Optional[str]:
    """Find the variable name in a parameter_declaration node."""
    for child in reversed(param.children):
        t = child.type
        if t == 'identifier':
            return node_text(child, src)
        if t in ('reference_declarator', 'pointer_declarator',
                 'rvalue_reference_declarator',
                 'abstract_reference_declarator', 'abstract_pointer_declarator'):
            found = _find_inner_id(child, src)
            if found:
                return found
    return None


def _extract_param_type(param: Node, src: bytes) -> str:
    """
    Extract the type portion of a parameter_declaration, stripping the name.

    E.g. "const std::string& msg" → "const std::string&",
         "T& out" → "T&",  "int count" → "int".
    """
    full = node_text(param, src).strip()
    name = _find_param_name(param, src)
    if name:
        last = full.rfind(name)
        if last > 0 and full[last - 1] in (' ', '\t', '*', '&'):
            full = full[:last].rstrip()
    return _normalize_cpp_type(full)


def _extract_params_text(param_list: Optional[Node], src: bytes) -> str:
    """
    Build the parameter type list string for a FQN.
    E.g. parameter_list for (int a, const T& b) → "int, const T&".
    """
    if param_list is None:
        return ''
    parts: list[str] = []
    for child in param_list.children:
        if child.type == 'parameter_declaration':
            parts.append(_extract_param_type(child, src))
        elif child.type == 'variadic_parameter':
            parts.append('...')
    return ', '.join(parts)


def _get_fn_declarator(node: Node) -> Optional[Node]:
    """
    Find a function_declarator child in a declaration, function_definition,
    or field_declaration, including through one level of pointer/ref wrapper.
    """
    for child in node.children:
        if child.type == 'function_declarator':
            return child
        if child.type in ('pointer_declarator', 'reference_declarator',
                          'abstract_pointer_declarator'):
            for gc in child.children:
                if gc.type == 'function_declarator':
                    return gc
    return None


def _get_fn_name_and_qualifier(
    fn_decl: Node, src: bytes
) -> tuple[Optional[str], Optional[str]]:
    """
    Extract (function_name, class_qualifier) from a function_declarator.

    Returns ("method", "MyClass") for qualified out-of-class definitions like
    `void MyClass::method()`.  Returns ("push", None) for in-class declarations.
    Handles destructors (~Foo) and operator overloads (operator+).
    """
    name_node = next(
        (c for c in fn_decl.children
         if c.type in ('identifier', 'field_identifier',
                       'qualified_identifier', 'destructor_name',
                       'operator_name')),
        None,
    )
    if name_node is None:
        return None, None

    if name_node.type == 'qualified_identifier':
        full = node_text(name_node, src)
        last = full.rfind('::')
        return (full[last + 2:], full[:last]) if last >= 0 else (full, None)

    return node_text(name_node, src), None


def _get_param_list(fn_decl: Node) -> Optional[Node]:
    """Find the parameter_list child of a function_declarator."""
    return next((c for c in fn_decl.children if c.type == 'parameter_list'), None)


def _get_type_name(node: Node, src: bytes) -> Optional[str]:
    """Get the declared name of a class, struct, or enum specifier."""
    for c in node.children:
        if c.type == 'type_identifier':
            return node_text(c, src)
    return None


def _get_base_types(node: Node, src: bytes) -> list[str]:
    """Extract base class type names from a base_class_clause."""
    for child in node.children:
        if child.type == 'base_class_clause':
            return [
                node_text(item, src)
                for item in child.children
                if item.type in ('type_identifier', 'qualified_identifier', 'template_type')
            ]
    return []


def _cpp_skeletonize(node: Node, src: bytes) -> str:
    """
    Return class/struct text with inline method bodies replaced by { ... }.
    Declarations, access specifiers, and field declarations are kept verbatim.
    """
    fdl = next((c for c in node.children if c.type == 'field_declaration_list'), None)
    if fdl is None:
        return node_text(node, src)

    parts: list[str] = [src[node.start_byte:fdl.start_byte].decode('utf-8', errors='replace')]
    last = fdl.start_byte

    for member in fdl.children:
        if member.type == 'function_definition':
            body = next((c for c in member.children if c.type == 'compound_statement'), None)
            if body:
                parts.append(src[last:body.start_byte].decode('utf-8', errors='replace'))
                parts.append(' { ... }\n')
                last = body.end_byte

    parts.append(src[last:node.end_byte].decode('utf-8', errors='replace'))
    return ''.join(parts)


# ---------------------------------------------------------------------------
# Project file resolver (compile_commands.json)
# ---------------------------------------------------------------------------


class CppProjectResolver:
    """
    Parses compile_commands.json to extract per-translation-unit include paths.

    Emits import edges from each source file to the include roots configured
    for it, representing build-system-level include dependencies.
    """

    def parse(self, path: str, src: bytes) -> ParseResult:
        edges: list[Edge] = []
        try:
            entries = json.loads(src.decode('utf-8', errors='replace'))
            if not isinstance(entries, list):
                return ParseResult([], [], [], [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                file_path = entry.get('file') or ''
                cmd       = entry.get('command') or ''
                args_list = entry.get('arguments') or []
                args_str  = cmd if cmd else ' '.join(str(a) for a in args_list)
                for m in re.finditer(r'-(?:I|isystem)\s*([^\s]+)', args_str):
                    edges.append(
                        Edge(source_fqn=file_path, target=m.group(1), kind='import')
                    )
        except (json.JSONDecodeError, ValueError):
            pass
        return ParseResult(symbols=[], edges=edges, references=[], symbol_types=[])


_CPP_PROJECT_RESOLVER = CppProjectResolver()

# ---------------------------------------------------------------------------
# Test conventions
# ---------------------------------------------------------------------------

_TEST_CONVENTIONS = TestConventions(
    file_suffixes=[
        '_test.cpp', '_unittest.cpp', '_test.cc', '_unittest.cc',
        'Test.cpp', 'Tests.cpp',
    ],
    in_file_markers=[
        'TEST(', 'TEST_F(', 'TEST_P(',            # GoogleTest
        'TEST_CASE(', 'SECTION(',                  # Catch2
        'DOCTEST_TEST_CASE(',                      # doctest
        'BOOST_AUTO_TEST_CASE(',                   # Boost.Test
    ],
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CppAdapter:
    language_id = 'cpp'
    extensions  = frozenset({
        '.cpp', '.cc', '.cxx', '.c',
        '.h', '.hpp', '.hxx', '.inl',
    })

    def parse(self, path: str, src: bytes) -> ParseResult:
        root     = Parser(_GRAMMAR).parse(src).root_node
        symbols: list[Symbol] = []
        edges:   list[Edge]   = []

        self._extract_includes(root, src, path, edges)
        self._walk(root, src, path, ns_parts=[], class_parts=[], symbols=symbols, edges=edges)

        references = self._extract_references(root, src, symbols)
        return ParseResult(
            symbols    = symbols,
            edges      = edges,
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
            tags: list[str] = list(tag_symbol(sym.name, sym.text))
            if any(f'{fn}(' in sym.text for fn in _UNSAFE_BUF_CALLS):
                if '[CPP_UNSAFE_BUF]' not in tags:
                    tags.append('[CPP_UNSAFE_BUF]')
            if any(f'{fn}(' in sym.text for fn in _EXEC_CALLS):
                if '[CPP_EXEC_CMD]' not in tags:
                    tags.append('[CPP_EXEC_CMD]')
            if tags:
                fqn_tags[sym.fqn] = tags
        return [], fqn_tags

    def test_conventions(self) -> TestConventions:
        return _TEST_CONVENTIONS

    def project_resolver(self) -> CppProjectResolver:
        return _CPP_PROJECT_RESOLVER

    # ------------------------------------------------------------------
    # Internal — include edge extraction
    # ------------------------------------------------------------------

    def _extract_includes(
        self, root: Node, src: bytes, file_path: str, edges: list[Edge]
    ) -> None:
        """Emit import edges for all #include directives in the translation unit."""
        for child in root.children:
            if child.type != 'preproc_include':
                continue
            for gc in child.children:
                if gc.type == 'string_literal':
                    # Local include: #include "path/to/file.h"
                    content = next(
                        (c for c in gc.children if c.type == 'string_content'), None
                    )
                    if content:
                        edges.append(
                            Edge(source_fqn=file_path,
                                 target=node_text(content, src),
                                 kind='import')
                        )
                elif gc.type == 'system_lib_string':
                    # System include: #include <vector>
                    edges.append(
                        Edge(source_fqn=file_path,
                             target=node_text(gc, src),
                             kind='import')
                    )

    # ------------------------------------------------------------------
    # Internal — recursive AST walker
    # ------------------------------------------------------------------

    def _walk(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        t = node.type

        if t == 'namespace_definition':
            ns_id = next((c for c in node.children if c.type == 'namespace_identifier'), None)
            ns_name  = node_text(ns_id, src) if ns_id else ''
            decl_list = next((c for c in node.children if c.type == 'declaration_list'), None)
            if decl_list:
                new_ns = ns_parts + ([ns_name] if ns_name else [])
                for child in decl_list.children:
                    self._walk(child, src, path, new_ns, class_parts, symbols, edges)
            return

        if t in ('class_specifier', 'struct_specifier'):
            self._handle_type(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'template_declaration':
            inner = next(
                (c for c in node.children
                 if c.type in ('class_specifier', 'struct_specifier',
                               'function_definition', 'declaration')),
                None,
            )
            if inner is not None:
                if inner.type in ('class_specifier', 'struct_specifier'):
                    self._handle_type(
                        inner, src, path, ns_parts, class_parts,
                        symbols, edges, template_node=node,
                    )
                else:
                    self._handle_fn_node(inner, src, path, ns_parts, class_parts,
                                         symbols, edges)
            return

        if t == 'function_definition':
            self._handle_fn_node(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'declaration':
            fn_decl = _get_fn_declarator(node)
            if fn_decl is not None:
                self._handle_fn_node(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'enum_specifier':
            self._handle_enum(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'type_definition':
            self._handle_typedef(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'alias_declaration':
            self._handle_alias(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        # Default: recurse for translation_unit, declaration_list, comments, etc.
        for child in node.children:
            self._walk(child, src, path, ns_parts, class_parts, symbols, edges)

    # ------------------------------------------------------------------
    # Internal — class / struct declarations
    # ------------------------------------------------------------------

    def _handle_type(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
        template_node: Optional[Node] = None,
    ) -> None:
        name = _get_type_name(node, src)
        if not name:
            return

        kind      = 'class' if node.type == 'class_specifier' else 'struct'
        fqn       = '::'.join(ns_parts + class_parts + [name])
        context   = '::'.join(ns_parts + class_parts) or None

        class_text = _cpp_skeletonize(node, src)
        if template_node is not None:
            tmpl_head = src[template_node.start_byte:node.start_byte].decode(
                'utf-8', errors='replace'
            )
            class_text = tmpl_head + class_text

        start_line = (template_node or node).start_point[0] + 1
        end_line   = (template_node or node).end_point[0] + 1

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = kind,
            name          = name,
            class_context = context,
            start_line    = start_line,
            end_line      = end_line,
            text          = class_text,
            shared        = True,
        ))

        for base in _get_base_types(node, src):
            edges.append(Edge(source_fqn=fqn, target=base, kind='extends'))

        fdl = next((c for c in node.children if c.type == 'field_declaration_list'), None)
        if fdl is None:
            return

        new_class = class_parts + [name]
        for member in fdl.children:
            self._handle_class_member(member, src, path, ns_parts, new_class, fqn,
                                      symbols, edges)

    def _handle_class_member(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        owner_fqn: str,
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        t = node.type

        if t in ('class_specifier', 'struct_specifier'):
            self._handle_type(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'enum_specifier':
            self._handle_enum(node, src, path, ns_parts, class_parts, symbols, edges)
            return

        if t == 'template_declaration':
            inner = next(
                (c for c in node.children
                 if c.type in ('class_specifier', 'struct_specifier',
                               'function_definition', 'field_declaration', 'declaration')),
                None,
            )
            if inner is not None:
                if inner.type in ('class_specifier', 'struct_specifier'):
                    self._handle_type(inner, src, path, ns_parts, class_parts, symbols, edges,
                                      template_node=node)
                else:
                    self._handle_method_node(inner, src, ns_parts, class_parts, owner_fqn,
                                             symbols, edges)
            return

        if t == 'function_definition':
            self._handle_method_node(node, src, ns_parts, class_parts, owner_fqn,
                                     symbols, edges)
            return

        if t in ('field_declaration', 'declaration'):
            fn_decl = _get_fn_declarator(node)
            if fn_decl is not None:
                self._handle_method_node(node, src, ns_parts, class_parts, owner_fqn,
                                         symbols, edges)

    # ------------------------------------------------------------------
    # Internal — method declarations and definitions within a class body
    # ------------------------------------------------------------------

    def _handle_method_node(
        self,
        node: Node,
        src: bytes,
        ns_parts: list[str],
        class_parts: list[str],
        owner_fqn: str,
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        fn_decl = _get_fn_declarator(node)
        if fn_decl is None:
            return

        fn_name, _qualifier = _get_fn_name_and_qualifier(fn_decl, src)
        if not fn_name:
            return

        params_text = _extract_params_text(_get_param_list(fn_decl), src)
        fqn         = f'{owner_fqn}::{fn_name}({params_text})'

        class_name = class_parts[-1] if class_parts else ''
        if fn_name.startswith('~'):
            kind = 'destructor'
        elif fn_name == class_name:
            kind = 'constructor'
        else:
            kind = 'method'

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = kind,
            name          = fn_name,
            class_context = owner_fqn,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = node_text(node, src),
            shared        = True,
        ))
        edges.append(Edge(source_fqn=owner_fqn, target=fqn, kind='owns'))

        body = next((c for c in node.children if c.type == 'compound_statement'), None)
        if body:
            for call_node, _ in run_query(_GRAMMAR, _CALL_QUERY, body):
                call_name = node_text(call_node, src)
                if call_name and len(call_name) > 1:
                    edges.append(Edge(source_fqn=fqn, target=call_name, kind='call'))

    # ------------------------------------------------------------------
    # Internal — free functions and out-of-class method definitions
    # ------------------------------------------------------------------

    def _handle_fn_node(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        fn_decl = _get_fn_declarator(node)
        if fn_decl is None:
            return

        fn_name, qualifier = _get_fn_name_and_qualifier(fn_decl, src)
        if not fn_name:
            return

        params_text = _extract_params_text(_get_param_list(fn_decl), src)

        if qualifier:
            # Out-of-class method definition (e.g. `void MyClass::method() { ... }`)
            owner_fqn  = '::'.join(ns_parts + [qualifier])
            fqn        = f'{owner_fqn}::{fn_name}({params_text})'
            class_name = qualifier.split('::')[-1]
            if fn_name.startswith('~'):
                kind = 'destructor'
            elif fn_name == class_name:
                kind = 'constructor'
            else:
                kind = 'method'
            context = owner_fqn
            edges.append(Edge(source_fqn=owner_fqn, target=fqn, kind='owns'))
        else:
            # Free function
            ns_prefix = '::'.join(ns_parts)
            fqn       = f'{ns_prefix}::{fn_name}({params_text})' if ns_prefix else f'{fn_name}({params_text})'
            kind      = 'function'
            context   = None

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = kind,
            name          = fn_name,
            class_context = context,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = node_text(node, src),
            shared        = True,
        ))

        body = next((c for c in node.children if c.type == 'compound_statement'), None)
        if body:
            for call_node, _ in run_query(_GRAMMAR, _CALL_QUERY, body):
                call_name = node_text(call_node, src)
                if call_name and len(call_name) > 1:
                    edges.append(Edge(source_fqn=fqn, target=call_name, kind='call'))

    # ------------------------------------------------------------------
    # Internal — enums, typedefs, using aliases
    # ------------------------------------------------------------------

    def _handle_enum(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        name = _get_type_name(node, src)
        if not name:
            return

        fqn     = '::'.join(ns_parts + class_parts + [name])
        context = '::'.join(ns_parts + class_parts) or None

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = 'enum',
            name          = name,
            class_context = context,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = node_text(node, src),
            shared        = True,
        ))

    def _handle_typedef(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        # typedef ... AliasName ;  — name is the last type_identifier
        name_node = next(
            (c for c in reversed(node.children) if c.type == 'type_identifier'), None
        )
        if name_node is None:
            return

        name    = node_text(name_node, src)
        fqn     = '::'.join(ns_parts + class_parts + [name])
        context = '::'.join(ns_parts + class_parts) or None

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = 'typedef',
            name          = name,
            class_context = context,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = node_text(node, src),
            shared        = True,
        ))

    def _handle_alias(
        self,
        node: Node,
        src: bytes,
        path: str,
        ns_parts: list[str],
        class_parts: list[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        # using AliasName = ... ;  — name is the first type_identifier
        name_node = next((c for c in node.children if c.type == 'type_identifier'), None)
        if name_node is None:
            return

        name    = node_text(name_node, src)
        fqn     = '::'.join(ns_parts + class_parts + [name])
        context = '::'.join(ns_parts + class_parts) or None

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = 'type_alias',
            name          = name,
            class_context = context,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = node_text(node, src),
            shared        = True,
        ))

    # ------------------------------------------------------------------
    # Internal — reference extraction
    # ------------------------------------------------------------------

    def _extract_references(
        self, root: Node, src: bytes, symbols: list[Symbol]
    ) -> list[Reference]:
        sorted_syms = sorted(symbols, key=lambda s: s.start_line)

        def find_ctx(line: int) -> Optional[str]:
            ctx: Optional[str] = None
            for sym in sorted_syms:
                if sym.start_line <= line <= sym.end_line:
                    ctx = sym.fqn
            return ctx

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
                ref_kind    = 'CALL',
                context_fqn = find_ctx(line),
            ))
        return refs
