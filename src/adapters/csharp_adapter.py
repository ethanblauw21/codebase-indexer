"""CSharpAdapter — tree-sitter C# parser + .csproj/.sln project resolver.

FQN format (D3, permanent — bakes into stable IDs):
  Types:   Namespace.Type                          (no file_path prefix)
  Members: Namespace.Type.Member/arity             (methods/constructors)
           Namespace.Type.Member                   (properties, events)
  Nested:  Namespace.Outer+Inner                   (CLR + convention)

Rationale for dropping file_path prefix on C# symbols:
  C# namespaces enforce global uniqueness within a project, so
  file_path is not required for identity — unlike Python/JS where
  the same class name legitimately appears in multiple modules.
  Dropping the prefix allows partial-class merge: two .cs files that
  declare `partial class Foo` produce the SAME FQN and share one
  symbols row via the symbol_locations multi-location schema (ADR-003).

Partial classes: the first file to be indexed claims the symbols row
  (shared=True, INSERT OR IGNORE); subsequent files add symbol_locations
  rows.  Cross-file OWNS/EXTENDS edges from shared types are NOT cleaned
  on incremental re-index — full re-index always produces a correct state.
  Documented limit; acceptable because inheritance changes are infrequent.

Arity suffix: always applied to methods and constructors, not properties.
  Stable regardless of whether overloads exist elsewhere; prevents FQN
  collision when two overloads appear in the same file.

Documented limits (Phase 2):
  - Extension methods: resolve to candidates only (no receiver-type inference)
  - LINQ query syntax: indexed as text at chunk level, not as call edges
  - `dynamic`: invisible to the graph
  - Base-list extends/implements: first entry → EXTENDS, rest → IMPLEMENTS
    (cannot distinguish base class vs interface without type resolution)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import re
import os
from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_c_sharp as tscs

from adapters.base import Edge, ParseResult, Reference, Symbol, TestConventions
from adapters._treesitter import node_text, run_query
from category_tagger import tag_symbol

_GRAMMAR = Language(tscs.language())

# ---------------------------------------------------------------------------
# Tree-sitter queries
# ---------------------------------------------------------------------------

_USING_QUERY = """
(using_directive [(identifier) (qualified_name)] @path)
"""

_CALL_QUERY = """
(invocation_expression
  function: [
    (identifier) @name
    (member_access_expression member: (identifier) @name)
  ])
"""

# Security-relevant attributes → tag strings for analyze_tags
_SECURITY_ATTRS: dict[str, str] = {
    "Authorize":                 "[CS_AUTHORIZE]",
    "AllowAnonymous":            "[CS_ALLOW_ANON]",
    "HttpPost":                  "[CS_HTTP_MUTATE]",
    "HttpPut":                   "[CS_HTTP_MUTATE]",
    "HttpDelete":                "[CS_HTTP_MUTATE]",
    "HttpGet":                   "[CS_HTTP_READ]",
    "HttpPatch":                 "[CS_HTTP_MUTATE]",
    "ValidateAntiForgeryToken":  "[CS_CSRF_GUARD]",
}

# ---------------------------------------------------------------------------
# Type node name constants
# ---------------------------------------------------------------------------

_TYPE_DECL_KINDS = frozenset({
    "class_declaration",
    "interface_declaration",
    "struct_declaration",
    "record_declaration",
    "enum_declaration",
})

_SYMBOL_KIND_MAP = {
    "class_declaration":     "class",
    "interface_declaration": "interface",
    "struct_declaration":    "struct",
    "record_declaration":    "record",
    "enum_declaration":      "enum",
}

# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _decl_name(node: Node, src: bytes) -> Optional[str]:
    """Return the declared name via the 'name' field of any declaration node."""
    n = node.child_by_field_name("name")
    return node_text(n, src) if n else None


def _qualified_or_identifier(node: Node, src: bytes) -> Optional[str]:
    """Namespace name — may be bare identifier or dotted qualified_name."""
    for c in node.children:
        if c.type in ("identifier", "qualified_name"):
            return node_text(c, src)
    return None


def _param_count(node: Node) -> int:
    pl = next((c for c in node.children if c.type == "parameter_list"), None)
    if pl is None:
        return 0
    return sum(1 for c in pl.children if c.type == "parameter")


def _is_partial(node: Node, src: bytes) -> bool:
    return any(
        node_text(c, src) == "partial"
        for c in node.children
        if c.type == "modifier"
    )


def _base_types(node: Node, src: bytes) -> tuple[Optional[str], list[str]]:
    """Return (first_base, rest) extracted from a base_list child."""
    items: list[str] = []
    for child in node.children:
        if child.type == "base_list":
            for item in child.children:
                if item.type in ("identifier", "qualified_name", "generic_name"):
                    items.append(node_text(item, src))
    if not items:
        return None, []
    return items[0], items[1:]


def _cs_skeletonize(node: Node, src: bytes) -> str:
    """
    Return a class/struct/record/interface with method/constructor bodies
    replaced by { ... }.  Attributes, modifiers, and signatures are kept.
    Properties and events are kept verbatim (already compact).
    """
    decl_list = next(
        (c for c in node.children if c.type == "declaration_list"), None
    )
    if decl_list is None:
        return node_text(node, src)

    _stub_kinds = frozenset({
        "method_declaration",
        "constructor_declaration",
        "destructor_declaration",
    })

    parts: list[str] = [
        src[node.start_byte:decl_list.start_byte].decode("utf-8", errors="replace")
    ]
    last = decl_list.start_byte

    for member in decl_list.children:
        if member.type in _stub_kinds:
            body = next(
                (c for c in member.children if c.type == "block"), None
            )
            if body:
                parts.append(
                    src[last:body.start_byte].decode("utf-8", errors="replace")
                )
                parts.append(" { ... }\n")
                last = body.end_byte

    parts.append(src[last:decl_list.end_byte].decode("utf-8", errors="replace"))
    parts.append(src[decl_list.end_byte:node.end_byte].decode("utf-8", errors="replace"))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Project file resolver (.csproj / .sln)
# ---------------------------------------------------------------------------

# MSBuild XML namespace used in newer SDK-style projects
_MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"

# Pattern for project references in .sln files
_SLN_PROJ_RE = re.compile(
    r'Project\s*\([^)]*\)\s*=\s*"[^"]*",\s*"([^"]+\.(?:csproj|vbproj|fsproj))"',
    re.IGNORECASE,
)


class CsprojResolver:
    """
    Parses .csproj and .sln files to extract project-level dependency edges.

    .csproj:
      <ProjectReference Include="..." />  → import edge (inter-project dep)
      <PackageReference Include="..."  Version="..." /> → import edge (NuGet)

    .sln:
      Project("{...}") = "Name", "Path/To/Project.csproj" → import edge per project
    """

    def parse(self, path: str, src: bytes) -> ParseResult:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".sln":
            return self._parse_sln(path, src)
        return self._parse_csproj(path, src)

    def _parse_csproj(self, path: str, src: bytes) -> ParseResult:
        edges: list[Edge] = []
        text = src.decode("utf-8", errors="replace")
        try:
            # Strip XML namespace for uniform iteration (handles both SDK-style
            # and legacy projects with explicit xmlns= attributes)
            text_no_ns = re.sub(r'\s+xmlns="[^"]+"', '', text)
            root = ET.fromstring(text_no_ns)

            for ref in root.iter("ProjectReference"):
                include = (ref.get("Include") or "").replace("\\", "/")
                if include:
                    edges.append(Edge(source_fqn=path, target=include, kind="import"))

            for ref in root.iter("PackageReference"):
                include = ref.get("Include") or ""
                version = ref.get("Version") or ref.findtext("Version") or ""
                if include:
                    target = f"{include}/{version}" if version else include
                    edges.append(Edge(source_fqn=path, target=target, kind="import"))

        except ET.ParseError:
            pass

        return ParseResult(symbols=[], edges=edges, references=[], symbol_types=[])

    def _parse_sln(self, path: str, src: bytes) -> ParseResult:
        edges: list[Edge] = []
        for m in _SLN_PROJ_RE.finditer(src.decode("utf-8", errors="replace")):
            proj_path = m.group(1).replace("\\", "/")
            edges.append(Edge(source_fqn=path, target=proj_path, kind="import"))
        return ParseResult(symbols=[], edges=edges, references=[], symbol_types=[])


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

_CSPROJ_RESOLVER = CsprojResolver()

_TEST_CONVENTIONS = TestConventions(
    file_suffixes=["Tests.cs", "Test.cs"],
    in_file_markers=["[Fact]", "[Theory]", "[Test]", "[TestMethod]",
                     "[Fact(", "[Theory(", "[Test(", "[TestMethod("],
)


class CSharpAdapter:
    language_id = "csharp"
    extensions  = frozenset({".cs", ".csproj", ".sln"})

    def parse(self, path: str, src: bytes) -> ParseResult:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".csproj", ".sln"):
            return _CSPROJ_RESOLVER.parse(path, src)
        return self._parse_cs(path, src)

    def _parse_cs(self, path: str, src: bytes) -> ParseResult:
        root = Parser(_GRAMMAR).parse(src).root_node

        import_edges: list[Edge] = [
            Edge(source_fqn=path, target=node_text(n, src), kind="import")
            for n, _ in run_query(_GRAMMAR, _USING_QUERY, root)
        ]

        symbols: list[Symbol] = []
        edges: list[Edge]     = list(import_edges)

        self._walk(root, src, path, ns=None, outer_local=None,
                   symbols=symbols, edges=edges)

        references = self._extract_references(root, src, symbols)

        return ParseResult(
            symbols      = symbols,
            edges        = edges,
            references   = references,
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
            for attr_name, tag in _SECURITY_ATTRS.items():
                if (f"[{attr_name}]" in sym.text or f"[{attr_name}(" in sym.text) \
                        and tag not in tags:
                    tags.append(tag)
            if tags:
                fqn_tags[sym.fqn] = tags
        return [], fqn_tags

    def test_conventions(self) -> TestConventions:
        return _TEST_CONVENTIONS

    def project_resolver(self) -> CsprojResolver:
        return _CSPROJ_RESOLVER

    # ------------------------------------------------------------------
    # Internal — AST walk
    # ------------------------------------------------------------------

    def _walk(
        self,
        node: Node,
        src: bytes,
        file_path: str,
        ns: Optional[str],
        outer_local: Optional[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        t = node.type

        if t == "namespace_declaration":
            ns_name = _qualified_or_identifier(node, src)
            decl = next((c for c in node.children if c.type == "declaration_list"), None)
            if decl:
                for child in decl.children:
                    self._walk(child, src, file_path,
                               ns=ns_name, outer_local=None,
                               symbols=symbols, edges=edges)
            return

        # Modern C# file-scoped namespace: `namespace Foo.Bar;`
        if t == "file_scoped_namespace_declaration":
            ns_name = _qualified_or_identifier(node, src)
            for child in node.children:
                self._walk(child, src, file_path,
                           ns=ns_name, outer_local=None,
                           symbols=symbols, edges=edges)
            return

        if t in _TYPE_DECL_KINDS:
            self._handle_type(node, src, file_path, ns, outer_local, symbols, edges)
            return

        for child in node.children:
            self._walk(child, src, file_path, ns, outer_local, symbols, edges)

    def _handle_type(
        self,
        node: Node,
        src: bytes,
        file_path: str,
        ns: Optional[str],
        outer_local: Optional[str],
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        t    = node.type
        name = _decl_name(node, src)
        if not name:
            return

        # Local qualifier within namespace; uses + for nested types (D3)
        local_qualifier = f"{outer_local}+{name}" if outer_local else name

        # Full FQN — no file_path prefix (C# namespace uniqueness is global)
        fqn = f"{ns}.{local_qualifier}" if ns else local_qualifier

        kind = _SYMBOL_KIND_MAP.get(t, "class")
        if kind == "class" and _is_partial(node, src):
            kind = "partial_class"

        text = node_text(node, src) if t == "enum_declaration" else _cs_skeletonize(node, src)

        symbols.append(Symbol(
            fqn           = fqn,
            kind          = kind,
            name          = name,
            class_context = outer_local,
            start_line    = node.start_point[0] + 1,
            end_line      = node.end_point[0] + 1,
            text          = text,
            shared        = True,   # type symbols may span multiple partial-class files
        ))

        # Base list edges (first → EXTENDS, rest → IMPLEMENTS)
        base, rest = _base_types(node, src)
        if base:
            edges.append(Edge(source_fqn=fqn, target=base, kind="extends"))
        for iface in rest:
            edges.append(Edge(source_fqn=fqn, target=iface, kind="implements"))

        # Walk declaration_list for members
        decl_list = next(
            (c for c in node.children if c.type == "declaration_list"), None
        )
        if decl_list is None:
            return

        for member in decl_list.children:
            self._handle_member(
                member, src, file_path, ns, fqn, local_qualifier, symbols, edges
            )

    def _handle_member(
        self,
        node: Node,
        src: bytes,
        file_path: str,
        ns: Optional[str],
        owner_fqn: str,
        owner_local: str,
        symbols: list[Symbol],
        edges: list[Edge],
    ) -> None:
        t = node.type

        # Nested type — recurse with updated outer_local
        if t in _TYPE_DECL_KINDS:
            self._handle_type(
                node, src, file_path, ns,
                outer_local=owner_local,
                symbols=symbols, edges=edges,
            )
            return

        class_ctx = f"{ns}.{owner_local}" if ns else owner_local

        if t == "method_declaration":
            name = _decl_name(node, src)
            if not name:
                return
            arity      = _param_count(node)
            member_fqn = f"{owner_fqn}.{name}/{arity}"
            symbols.append(Symbol(
                fqn           = member_fqn,
                kind          = "method",
                name          = name,
                class_context = class_ctx,
                start_line    = node.start_point[0] + 1,
                end_line      = node.end_point[0] + 1,
                text          = node_text(node, src),
                shared        = False,
            ))
            edges.append(Edge(source_fqn=owner_fqn, target=member_fqn, kind="owns"))
            for n, _ in run_query(_GRAMMAR, _CALL_QUERY, node):
                call_name = node_text(n, src)
                if call_name:
                    edges.append(Edge(source_fqn=member_fqn, target=call_name, kind="call"))

        elif t == "constructor_declaration":
            name = _decl_name(node, src)
            if not name:
                return
            arity      = _param_count(node)
            member_fqn = f"{owner_fqn}.{name}/{arity}"
            symbols.append(Symbol(
                fqn           = member_fqn,
                kind          = "constructor",
                name          = name,
                class_context = class_ctx,
                start_line    = node.start_point[0] + 1,
                end_line      = node.end_point[0] + 1,
                text          = node_text(node, src),
                shared        = False,
            ))
            edges.append(Edge(source_fqn=owner_fqn, target=member_fqn, kind="owns"))
            for n, _ in run_query(_GRAMMAR, _CALL_QUERY, node):
                call_name = node_text(n, src)
                if call_name:
                    edges.append(Edge(source_fqn=member_fqn, target=call_name, kind="call"))

        elif t == "property_declaration":
            name = _decl_name(node, src)
            if not name:
                return
            member_fqn = f"{owner_fqn}.{name}"
            symbols.append(Symbol(
                fqn           = member_fqn,
                kind          = "property",
                name          = name,
                class_context = class_ctx,
                start_line    = node.start_point[0] + 1,
                end_line      = node.end_point[0] + 1,
                text          = node_text(node, src),
                shared        = False,
            ))
            edges.append(Edge(source_fqn=owner_fqn, target=member_fqn, kind="owns"))

    # ------------------------------------------------------------------
    # Internal — reference extraction
    # ------------------------------------------------------------------

    def _extract_references(
        self, root: Node, src: bytes, symbols: list[Symbol]
    ) -> list[Reference]:
        sorted_syms = sorted(symbols, key=lambda s: s.start_line)

        def find_context_fqn(line: int) -> Optional[str]:
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
                ref_kind    = "CALL",
                context_fqn = find_context_fqn(line),
            ))
        return refs
