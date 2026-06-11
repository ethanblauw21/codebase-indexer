"""
TypeScript and JavaScript adapters.

TypeScriptAdapter  — .ts and .tsx files
JavaScriptAdapter  — .js and .jsx files

Both share the same symbol-extraction and tag-analysis logic via _WebAdapter;
the only difference is which tree-sitter grammar is used per extension.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_typescript as tstypescript
import tree_sitter_javascript as tsjavascript

from adapters.base import Edge, ParseResult, Reference, Symbol, SymbolType, TestConventions, build_fqn
from adapters._treesitter import node_text, run_query, skeletonize
from category_tagger import tag_symbol


# ---------------------------------------------------------------------------
# Framework tag constants (JS/TS only; propagated to every chunk in the file
# or to the specific symbol chunk, depending on the tag type)
# ---------------------------------------------------------------------------

TAG_USE_CLIENT    = "[USE_CLIENT]"
TAG_USE_SERVER    = "[USE_SERVER]"
TAG_REACT_COMP    = "[REACT_COMPONENT]"
TAG_USER_PRIV     = "[USER_PRIVILEGE]"
TAG_ELEVATED_PRIV = "[ELEVATED_PRIVILEGE]"
TAG_ZUSTAND       = "[ZUSTAND_STORE]"
TAG_HOOK              = "[HOOK]"
TAG_CONTEXT_PROVIDER  = "[CONTEXT_PROVIDER]"
TAG_CONTEXT_CONSUMER  = "[CONTEXT_CONSUMER]"
TAG_MUTATION          = "[FIRESTORE_MUTATION]"
TAG_NEXT_PAGE         = "[NEXT_PAGE]"
TAG_NEXT_LAYOUT       = "[NEXT_LAYOUT]"
TAG_SERVER_ACTION     = "[SERVER_ACTION]"
TAG_FIREBASE_CALLABLE = "[FIREBASE_CALLABLE]"
TAG_FIRESTORE_TRIGGER = "[FIRESTORE_TRIGGER]"

# Firebase client SDK entry-point functions.
_FIREBASE_CLIENT_FUNS: frozenset[str] = frozenset({
    "getFirestore", "getAuth", "getStorage", "getDatabase",
    "getFunctions", "getAnalytics", "getMessaging", "getRemoteConfig",
    "getPerformance", "getAppCheck",
})

# ---------------------------------------------------------------------------
# Tree-sitter queries (TS/JS shared)
# ---------------------------------------------------------------------------

_IMPORT_QUERY = "(import_statement source: (string (string_fragment) @path))"

_CALL_QUERY = """
(call_expression
  function: [
    (identifier) @name
    (member_expression property: (property_identifier) @name)
  ])
"""

_DIRECTIVE_QUERY = "(expression_statement (string (string_fragment) @dir))"

_JSX_QUERY = """
(jsx_element) @jsx
(jsx_self_closing_element) @jsx
"""

_IDENT_CALL_QUERY      = "(call_expression function: (identifier) @fn)"
_MEMBER_CALL_OBJ_QUERY = """
(call_expression
    function: (member_expression
        object: (identifier) @obj))
"""

# ---------------------------------------------------------------------------
# Regex patterns for tag detection
# ---------------------------------------------------------------------------

_CONTEXT_PROVIDE_RE   = re.compile(r'<([A-Za-z_][A-Za-z0-9_.]*\.Provider)\b')
_CONTEXT_CONSUME_RE   = re.compile(r'\buseContext\s*\(')
_FIREBASE_CALLABLE_RE = re.compile(r'\bonCall\s*\(')
_FIRESTORE_TRIGGER_RE = re.compile(r'\bon(?:Write|Create|Update|Delete)\s*\(')
_DB_WRITE_RE = re.compile(
    r'\b(?:setDoc|addDoc|updateDoc|deleteDoc|writeBatch|runTransaction)\b'
    r'|\.(set|add|update|delete)\s*\('
)
_EXPORT_RE = re.compile(r'\bexport\b')
_ASYNC_RE  = re.compile(r'(?:^|\s)async\s')
_GEN_RE    = re.compile(r'function\s*\*|async\s*\*')

# ---------------------------------------------------------------------------
# Internal node-type sets
# ---------------------------------------------------------------------------

_TS_NAME_TYPES = frozenset(("identifier", "type_identifier", "property_identifier"))
_TS_STUB_TYPES: set[str] = {"method_definition", "function_declaration", "arrow_function"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ts_decl_name(node: Node, src: bytes) -> Optional[str]:
    """Return the first identifier/type_identifier/property_identifier child."""
    for child in node.children:
        if child.type in _TS_NAME_TYPES:
            return node_text(child, src)
    return None


def _extract_ts_type(node: Node, src: bytes, fqn: str) -> Optional[SymbolType]:
    """Extract type annotations from a TS function/method/arrow-function node."""
    text_prefix = src[node.start_byte:min(node.start_byte + 150, node.end_byte)].decode("utf-8", errors="replace")
    is_async     = bool(_ASYNC_RE.search(text_prefix[:80]))
    is_generator = bool(_GEN_RE.search(text_prefix[:80]))

    return_type: Optional[str] = None
    type_params: Optional[str] = None
    params: list[dict] = []

    children   = list(node.children)
    params_idx: Optional[int] = None
    for i, child in enumerate(children):
        if child.type == "type_parameters":
            type_params = node_text(child, src)
        if child.type == "formal_parameters":
            params_idx = i
            for param in child.children:
                if param.type in ("required_parameter", "optional_parameter"):
                    pname = next((c for c in param.children if c.type == "identifier"), None)
                    ptype = next((c for c in param.children if c.type == "type_annotation"), None)
                    if pname:
                        p: dict = {"name": node_text(pname, src)}
                        if ptype:
                            p["type"] = node_text(ptype, src).lstrip(": ").strip()
                        params.append(p)

    if params_idx is not None:
        for child in children[params_idx + 1:]:
            if child.type == "type_annotation":
                return_type = node_text(child, src).lstrip(": ").strip()
                break
            if child.type in ("statement_block", "=>"):
                break

    if return_type or params or type_params or is_async or is_generator:
        return SymbolType(
            fqn          = fqn,
            return_type  = return_type,
            params       = params,
            type_params  = type_params,
            is_async     = is_async,
            is_generator = is_generator,
        )
    return None


def _extract_context_edges(node: Node, src: bytes, enclosing_fqn: str) -> list[Edge]:
    """Emit PROVIDES_CONTEXT / CONSUMES_CONTEXT edges from a symbol body."""
    text  = node_text(node, src)
    edges: list[Edge] = []
    for m in _CONTEXT_CONSUME_RE.finditer(text):
        rest  = text[m.end():]
        arg_m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)', rest)
        if arg_m:
            edges.append(Edge(
                source_fqn = enclosing_fqn,
                target     = arg_m.group(1),
                kind       = "consumes_context",
            ))
    for m in _CONTEXT_PROVIDE_RE.finditer(text):
        edges.append(Edge(
            source_fqn = enclosing_fqn,
            target     = m.group(1),
            kind       = "provides_context",
        ))
    return edges


def _extract_calls(node: Node, src: bytes, lang: Language) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for n, _ in run_query(lang, _CALL_QUERY, node):
        name = node_text(n, src)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


# ---------------------------------------------------------------------------
# Shared TS/JS adapter base
# ---------------------------------------------------------------------------

class _WebAdapter:
    """Shared parse + tag-analysis logic for TypeScript and JavaScript adapters."""

    _grammars: dict[str, Language]  # populated by subclasses

    def parse(self, path: str, src: bytes) -> ParseResult:
        ext  = os.path.splitext(path)[1].lower()
        lang = self._grammars[ext]
        root = Parser(lang).parse(src).root_node

        import_edges = [
            Edge(source_fqn=path, target=node_text(n, src), kind="import")
            for n, _ in run_query(lang, _IMPORT_QUERY, root)
        ]

        symbols, call_edges, symbol_types = self._extract_symbols(root, src, path, lang, ext)
        references = self._extract_references(root, src, lang, symbols)

        return ParseResult(
            symbols      = symbols,
            edges        = import_edges + call_edges,
            references   = references,
            symbol_types = symbol_types,
        )

    def analyze_tags(
        self,
        path: str,
        src: bytes,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        ext  = os.path.splitext(path)[1].lower()
        lang = self._grammars[ext]
        root = Parser(lang).parse(src).root_node
        return self._analyze_tags_impl(root, src, path, lang, ext, symbols)

    # ------------------------------------------------------------------
    # Internal: symbol extraction (mirrors _extract_ts_symbols exactly)
    # ------------------------------------------------------------------

    def _extract_symbols(
        self,
        root: Node,
        src: bytes,
        file_path: str,
        lang: Language,
        ext: str,
    ) -> tuple[list[Symbol], list[Edge], list[SymbolType]]:
        symbols:      list[Symbol]     = []
        edges:        list[Edge]       = []
        symbol_types: list[SymbolType] = []

        def emit(
            node: Node,
            kind: str,
            name: str,
            class_ctx: Optional[str],
            call_scope: Optional[Node] = None,
            type_node: Optional[Node]  = None,
        ) -> Symbol:
            fqn = build_fqn(file_path, class_ctx, name)
            sym = Symbol(
                fqn           = fqn,
                kind          = kind,
                name          = name,
                class_context = class_ctx,
                start_line    = node.start_point[0] + 1,
                end_line      = node.end_point[0] + 1,
                text          = node_text(node, src),
            )
            symbols.append(sym)
            scope_node = call_scope or node
            for call_name in _extract_calls(scope_node, src, lang):
                edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
            edges.extend(_extract_context_edges(scope_node, src, fqn))
            if class_ctx:
                class_fqn = build_fqn(file_path, None, class_ctx)
                edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
            st = _extract_ts_type(type_node or node, src, fqn)
            if st:
                symbol_types.append(st)
            return sym

        def walk(node: Node, class_ctx: Optional[str]) -> None:
            t = node.type

            if t == "export_statement":
                for child in node.children:
                    walk(child, class_ctx)
                return

            if t == "class_declaration":
                name = _ts_decl_name(node, src)
                if name:
                    fqn = build_fqn(file_path, None, name)
                    sym = Symbol(
                        fqn           = fqn,
                        kind          = "class",
                        name          = name,
                        class_context = None,
                        start_line    = node.start_point[0] + 1,
                        end_line      = node.end_point[0] + 1,
                        text          = skeletonize(node, src, _TS_STUB_TYPES),
                    )
                    symbols.append(sym)
                    for child in node.children:
                        if child.type == "class_heritage":
                            for sub in child.children:
                                if sub.type == "extends_clause":
                                    for id_node in sub.children:
                                        if id_node.type in _TS_NAME_TYPES:
                                            edges.append(Edge(
                                                source_fqn = fqn,
                                                target     = node_text(id_node, src),
                                                kind       = "extends",
                                            ))
                                            break
                                elif sub.type == "implements_clause":
                                    for id_node in sub.children:
                                        if id_node.type in _TS_NAME_TYPES:
                                            edges.append(Edge(
                                                source_fqn = fqn,
                                                target     = node_text(id_node, src),
                                                kind       = "implements",
                                            ))
                    class_body = next(
                        (c for c in node.children if c.type == "class_body"), None
                    )
                    if class_body:
                        for child in class_body.children:
                            walk(child, name)
                return

            if t == "interface_declaration":
                name = _ts_decl_name(node, src)
                if name:
                    sym = emit(node, "interface", name, class_ctx)
                    for child in node.children:
                        if child.type in ("extends_clause", "extends_type_clause"):
                            for id_node in child.children:
                                if id_node.type in _TS_NAME_TYPES:
                                    edges.append(Edge(
                                        source_fqn = sym.fqn,
                                        target     = node_text(id_node, src),
                                        kind       = "extends",
                                    ))
                return

            if t == "type_alias_declaration":
                name = _ts_decl_name(node, src)
                if name:
                    emit(node, "type_alias", name, class_ctx)
                return

            if t == "function_declaration":
                name = _ts_decl_name(node, src)
                if name:
                    emit(node, "function", name, class_ctx, type_node=node)
                return

            if t == "method_definition":
                name = _ts_decl_name(node, src)
                if name:
                    emit(node, "method", name, class_ctx, type_node=node)
                return

            if t == "lexical_declaration":
                for decl in node.children:
                    if decl.type != "variable_declarator":
                        continue
                    name_node = next(
                        (c for c in decl.children if c.type == "identifier"), None
                    )
                    value_node = next(
                        (c for c in decl.children
                         if c.type in ("arrow_function", "function_expression")),
                        None,
                    )
                    if name_node and value_node:
                        name = node_text(name_node, src)
                        fqn  = build_fqn(file_path, class_ctx, name)
                        sym  = Symbol(
                            fqn           = fqn,
                            kind          = "arrow_function",
                            name          = name,
                            class_context = class_ctx,
                            start_line    = node.start_point[0] + 1,
                            end_line      = node.end_point[0] + 1,
                            text          = node_text(node, src),
                        )
                        symbols.append(sym)
                        for call_name in _extract_calls(value_node, src, lang):
                            edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
                        edges.extend(_extract_context_edges(value_node, src, fqn))
                        if class_ctx:
                            class_fqn = build_fqn(file_path, None, class_ctx)
                            edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
                        st = _extract_ts_type(value_node, src, fqn)
                        if st:
                            symbol_types.append(st)
                return

            for child in node.children:
                walk(child, class_ctx)

        walk(root, None)
        return symbols, edges, symbol_types

    def _extract_references(
        self,
        root: Node,
        src: bytes,
        lang: Language,
        symbols: list[Symbol],
    ) -> list[Reference]:
        sorted_syms = sorted(symbols, key=lambda s: s.start_line)

        def find_context_fqn(line: int) -> Optional[str]:
            for sym in sorted_syms:
                if sym.start_line <= line <= sym.end_line:
                    return sym.fqn
            return None

        refs: list[Reference] = []
        for n, _ in run_query(lang, _CALL_QUERY, root):
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

    # ------------------------------------------------------------------
    # Internal: framework tag analysis (mirrors _analyze_tags exactly)
    # ------------------------------------------------------------------

    def _analyze_tags_impl(
        self,
        root: Node,
        src: bytes,
        file_path: str,
        lang: Language,
        ext: str,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        file_tags: list[str]             = []
        fqn_tags:  dict[str, list[str]] = {}

        # ── Framework tags (JS/TS only) ───────────────────────────────────────

        # 1. Environment boundary directive
        for n, _ in run_query(lang, _DIRECTIVE_QUERY, root):
            if n.start_point[0] >= 10:
                continue
            text = node_text(n, src)
            if text == "use client":
                file_tags.append(TAG_USE_CLIENT)
                break
            if text == "use server":
                file_tags.append(TAG_USE_SERVER)
                break

        # 2. Next.js page/layout (file-level)
        norm_path = file_path.replace("\\", "/")
        if re.search(r'/app/.*/page\.tsx?$', norm_path):
            file_tags.append(TAG_NEXT_PAGE)
        elif re.search(r'/app/.*/layout\.tsx?$', norm_path):
            file_tags.append(TAG_NEXT_LAYOUT)

        # 3. Zustand store (file-level)
        imports = [
            node_text(n, src)
            for n, _ in run_query(lang, _IMPORT_QUERY, root)
        ]
        if any("zustand" in imp for imp in imports):
            all_ident_calls = run_query(lang, _IDENT_CALL_QUERY, root)
            if any(node_text(n, src) == "create" for n, _ in all_ident_calls):
                file_tags.append(TAG_ZUSTAND)

        # 4. Per-symbol framework tags
        jsx_matches = (
            run_query(lang, _JSX_QUERY, root) if ext in (".tsx", ".jsx", ".js") else []
        )
        ident_matches  = run_query(lang, _IDENT_CALL_QUERY,      root)
        member_matches = run_query(lang, _MEMBER_CALL_OBJ_QUERY, root)

        is_use_server = TAG_USE_SERVER in file_tags
        raw_lines: list[str] = (
            src.decode("utf-8", errors="replace").splitlines() if is_use_server else []
        )

        for sym in symbols:
            lo, hi = sym.start_line, sym.end_line
            tags:   list[str] = []
            text = sym.text

            if any(lo <= n.start_point[0] + 1 <= hi for n, _ in jsx_matches):
                tags.append(TAG_REACT_COMP)

            if any(
                lo <= n.start_point[0] + 1 <= hi and node_text(n, src) in _FIREBASE_CLIENT_FUNS
                for n, _ in ident_matches
            ):
                tags.append(TAG_USER_PRIV)

            if any(
                lo <= n.start_point[0] + 1 <= hi and node_text(n, src) == "admin"
                for n, _ in member_matches
            ):
                tags.append(TAG_ELEVATED_PRIV)

            if sym.kind in ("function", "arrow_function") and re.match(r'use[A-Z]', sym.name):
                tags.append(TAG_HOOK)

            if _CONTEXT_PROVIDE_RE.search(text):
                tags.append(TAG_CONTEXT_PROVIDER)

            if _CONTEXT_CONSUME_RE.search(text):
                tags.append(TAG_CONTEXT_CONSUMER)

            if _DB_WRITE_RE.search(text):
                tags.append(TAG_MUTATION)

            if is_use_server and sym.kind in ("function", "arrow_function", "method"):
                has_export = bool(_EXPORT_RE.search(text[:200]))
                if not has_export and raw_lines and sym.start_line >= 2:
                    prev_line  = raw_lines[sym.start_line - 2]
                    has_export = bool(_EXPORT_RE.search(prev_line))
                if has_export:
                    tags.append(TAG_SERVER_ACTION)

            if _FIREBASE_CALLABLE_RE.search(text):
                tags.append(TAG_FIREBASE_CALLABLE)

            if _FIRESTORE_TRIGGER_RE.search(text):
                tags.append(TAG_FIRESTORE_TRIGGER)

            if tags:
                fqn_tags[sym.fqn] = tags

        # ── Semantic category tags (all languages) ────────────────────────────
        for sym in symbols:
            cat_tags = tag_symbol(sym.name, sym.text)
            if cat_tags:
                fqn_tags[sym.fqn] = fqn_tags.get(sym.fqn, []) + cat_tags

        return file_tags, fqn_tags


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------

class TypeScriptAdapter(_WebAdapter):
    language_id = "typescript"
    extensions  = frozenset({".ts", ".tsx"})
    _grammars   = {
        ".ts":  Language(tstypescript.language_typescript()),
        ".tsx": Language(tstypescript.language_tsx()),
    }

    def test_conventions(self):
        return TestConventions(
            file_suffixes=[".test.ts", ".test.tsx", ".spec.ts"],
            in_file_markers=["describe(", "it(", "test(", "expect("],
        )

    def project_resolver(self):
        return None


class JavaScriptAdapter(_WebAdapter):
    language_id = "javascript"
    extensions  = frozenset({".js", ".jsx"})
    _grammars   = {
        ".js":  Language(tsjavascript.language()),
        ".jsx": Language(tsjavascript.language()),
    }

    def test_conventions(self):
        return TestConventions(
            file_suffixes=[".test.js", ".test.jsx", ".spec.js"],
            in_file_markers=["describe(", "it(", "test(", "expect("],
        )

    def project_resolver(self):
        return None
