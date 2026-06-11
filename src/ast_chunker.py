"""
AST-based code chunker using tree-sitter.

Primary API
-----------
parse_file(file_path, content) -> ParseResult
    Full symbol + dependency graph extraction for any supported file.
    Returns ParseResult(symbols, edges, references, symbol_types).
    Backward-compatible: ParseResult[0]==symbols, ParseResult[1]==edges.

chunk_file_ast(file_path, content, max_tokens, overlap) -> list[Chunk]
    AST-guided chunker; each Symbol becomes one Chunk, oversized symbols
    are split via the token-based fallback.

fallback_token_chunker(text, file_path, max_tokens, overlap, parent_scope) -> list[Chunk]
    Line-oriented token chunker with a monster-line shredder for huge inlined
    blobs (Base64 strings, inline SVGs, etc.).

Chunk supports dict-style access (chunk['scope'], chunk['text'], ...) so
existing callers in indexer.py require no changes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
import tree_sitter_javascript as tsjavascript

from core import jina_tokenizer
from category_tagger import tag_symbol

# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

LANGUAGES: dict[str, Language] = {
    ".py":  Language(tspython.language()),
    ".ts":  Language(tstypescript.language_typescript()),
    ".tsx": Language(tstypescript.language_tsx()),
    ".js":  Language(tsjavascript.language()),
    ".jsx": Language(tsjavascript.language()),
}

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    """A named code entity extracted from a source file."""
    fqn: str              # e.g. "src/api/auth.ts::AuthService.login"
    kind: str             # "class" | "function" | "method" | "arrow_function"
                          # | "interface" | "type_alias"
    name: str
    class_context: Optional[str]
    start_line: int
    end_line: int
    text: str             # raw source text of the symbol


@dataclass
class Edge:
    """A directed dependency from a symbol (or file) to another identifier."""
    source_fqn: str               # FQN of the caller/importer; file path for imports
    target: str                   # module path for imports; function name for calls
    kind: str                     # "import" | "call" | "owns" | "extends" | "implements"
                                  # | "provides_context" | "consumes_context"
    resolved_target: Optional[str] = None  # canonical repo-relative path (IMPORTS only)


@dataclass
class Chunk:
    """
    Embeddable text segment with provenance and dependency metadata.
    Supports dict-style access for backward compatibility with indexer.py.
    """
    text: str
    file: str
    start_line: int
    end_line: int
    scope: str            # FQN when AST-derived; navigable label otherwise
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge]     = field(default_factory=list)
    tags: list[str]       = field(default_factory=list)

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


@dataclass
class Reference:
    """Exact usage location of a symbol within a file."""
    symbol_name: str
    symbol_fqn: Optional[str]   # best-effort resolved FQN (None until cross-file resolution)
    line: int
    ref_kind: str               # CALL | TYPE | EXTENDS | IMPLEMENTS | USE
    context_fqn: Optional[str]  # enclosing symbol FQN


@dataclass
class SymbolType:
    """TypeScript type annotation metadata for a symbol."""
    fqn: str
    return_type: Optional[str]
    params: list[dict]          # [{name: str, type?: str}, ...]
    type_params: Optional[str]  # generic type params string, e.g. "<T extends Base>"
    is_async: bool
    is_generator: bool


class ParseResult(NamedTuple):
    """
    Return type of parse_file(). NamedTuple so positional access still works:
    result[0] == symbols, result[1] == edges (backward-compatible).
    """
    symbols: list[Symbol]
    edges: list[Edge]
    references: list[Reference]
    symbol_types: list[SymbolType]


# ---------------------------------------------------------------------------
# Internal tree-sitter helpers
# ---------------------------------------------------------------------------

def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _run_query(lang: Language, pattern: str, node: Node) -> list[tuple[Node, str]]:
    """
    Run a tree-sitter query and return (node, capture_name) pairs.
    Handles both the legacy list API (< 0.23) and the new dict API (>= 0.23).
    Returns [] on any query compilation failure so callers degrade gracefully.
    """
    try:
        q = lang.query(pattern)
        raw = q.captures(node)
        if isinstance(raw, dict):
            # New API: {capture_name: [Node, ...]}
            return [(n, cap) for cap, nodes in raw.items() for n in nodes]
        return list(raw)  # Old API: [(Node, capture_name), ...]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

_TS_IMPORT_QUERY = "(import_statement source: (string (string_fragment) @path))"

_PY_IMPORT_QUERY = """
(import_statement name: (dotted_name) @path)
(import_from_statement module_name: (dotted_name) @path)
"""


def _extract_imports(root: Node, src: bytes, lang: Language, ext: str) -> list[str]:
    pattern = _PY_IMPORT_QUERY if ext == ".py" else _TS_IMPORT_QUERY
    return [_node_text(n, src) for n, _ in _run_query(lang, pattern, root)]


# ---------------------------------------------------------------------------
# Call extraction
# ---------------------------------------------------------------------------

_TS_CALL_QUERY = """
(call_expression
  function: [
    (identifier) @name
    (member_expression property: (property_identifier) @name)
  ])
"""

_PY_CALL_QUERY = """
(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ])
"""

# ---------------------------------------------------------------------------
# Framework architectural tag constants
# ---------------------------------------------------------------------------
#
# Tags are stored as a space-separated string in the SQLite `chunks.tags`
# column so callers can filter with:
#   WHERE tags LIKE '%[USER_PRIVILEGE]%'
#
# FILE-LEVEL tags propagate to every chunk in the file:
#   [USE_CLIENT]     — "use client" directive (Next.js App Router client boundary)
#   [USE_SERVER]     — "use server" directive (Next.js App Router server action file)
#   [ZUSTAND_STORE]  — file imports from zustand and calls create()
#   [NEXT_PAGE]      — app/**/page.tsx (Next.js App Router page)
#   [NEXT_LAYOUT]    — app/**/layout.tsx (Next.js App Router layout)
#
# SYMBOL-LEVEL tags apply only to the chunk containing the symbol:
#   [REACT_COMPONENT]    — function/arrow function whose body contains JSX
#   [USER_PRIVILEGE]     — calls a Firebase client SDK function (getFirestore, etc.)
#   [ELEVATED_PRIVILEGE] — calls a method on the `admin` object (Admin SDK)
#   [HOOK]               — function name starts with 'use' + uppercase letter
#   [CONTEXT_PROVIDER]   — renders a *.Provider JSX element
#   [CONTEXT_CONSUMER]   — calls useContext()
#   [FIRESTORE_MUTATION] — contains Firestore write operations
#   [SERVER_ACTION]      — exported function in a [USE_SERVER] file
#   [FIREBASE_CALLABLE]  — wraps onCall()
#   [FIRESTORE_TRIGGER]  — wraps onWrite/onCreate/onUpdate/onDelete

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

# Firebase client SDK entry-point functions — all follow the getXxx() naming pattern.
_FIREBASE_CLIENT_FUNS: frozenset[str] = frozenset({
    "getFirestore", "getAuth", "getStorage", "getDatabase",
    "getFunctions", "getAnalytics", "getMessaging", "getRemoteConfig",
    "getPerformance", "getAppCheck",
})

# Next.js directive: expression_statement whose string content is "use client"
# or "use server".  Must appear in the first ~10 lines.
_DIRECTIVE_QUERY = "(expression_statement (string (string_fragment) @dir))"

# JSX element presence — any jsx_element or self-closing element.
# Only run on .tsx / .jsx / .js files; .ts has no JSX grammar nodes.
_JSX_QUERY = """
(jsx_element) @jsx
(jsx_self_closing_element) @jsx
"""

# Bare function call, e.g. getFirestore() or create() — captures the callee identifier.
_IDENT_CALL_QUERY = "(call_expression function: (identifier) @fn)"

# Member call, e.g. admin.firestore() — captures the receiver object identifier.
_MEMBER_CALL_OBJ_QUERY = """
(call_expression
    function: (member_expression
        object: (identifier) @obj))
"""

# Patterns for advanced semantic tag detection
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


def _extract_calls(node: Node, src: bytes, lang: Language, ext: str) -> list[str]:
    pattern = _PY_CALL_QUERY if ext == ".py" else _TS_CALL_QUERY
    seen: set[str] = set()
    result: list[str] = []
    for n, _ in _run_query(lang, pattern, node):
        name = _node_text(n, src)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


# ---------------------------------------------------------------------------
# FQN builder
# ---------------------------------------------------------------------------

def _build_fqn(file_path: str, class_ctx: Optional[str], name: str) -> str:
    qualifier = f"{class_ctx}.{name}" if class_ctx else name
    return f"{file_path}::{qualifier}"


# ---------------------------------------------------------------------------
# Type annotation extractor (tree-sitter only, no ts-morph)
# ---------------------------------------------------------------------------

def _extract_ts_type(node: Node, src: bytes, fqn: str) -> Optional[SymbolType]:
    """Extract type annotations from a TS function/method/arrow-function node."""
    text_prefix = src[node.start_byte:min(node.start_byte + 150, node.end_byte)].decode("utf-8", errors="replace")
    is_async = bool(_ASYNC_RE.search(text_prefix[:80]))
    is_generator = bool(_GEN_RE.search(text_prefix[:80]))

    return_type: Optional[str] = None
    type_params: Optional[str] = None
    params: list[dict] = []

    children = list(node.children)
    params_idx: Optional[int] = None
    for i, child in enumerate(children):
        if child.type == "type_parameters":
            type_params = _node_text(child, src)
        if child.type == "formal_parameters":
            params_idx = i
            for param in child.children:
                if param.type in ("required_parameter", "optional_parameter"):
                    pname = next((c for c in param.children if c.type == "identifier"), None)
                    ptype = next((c for c in param.children if c.type == "type_annotation"), None)
                    if pname:
                        p: dict = {"name": _node_text(pname, src)}
                        if ptype:
                            p["type"] = _node_text(ptype, src).lstrip(": ").strip()
                        params.append(p)

    if params_idx is not None:
        for child in children[params_idx + 1:]:
            if child.type == "type_annotation":
                return_type = _node_text(child, src).lstrip(": ").strip()
                break
            if child.type in ("statement_block", "=>"):
                break

    if return_type or params or type_params or is_async or is_generator:
        return SymbolType(
            fqn=fqn,
            return_type=return_type,
            params=params,
            type_params=type_params,
            is_async=is_async,
            is_generator=is_generator,
        )
    return None


# ---------------------------------------------------------------------------
# Context edge extractor (PROVIDES_CONTEXT / CONSUMES_CONTEXT)
# ---------------------------------------------------------------------------

def _extract_context_edges(node: Node, src: bytes, enclosing_fqn: str) -> list[Edge]:
    """Emit PROVIDES_CONTEXT / CONSUMES_CONTEXT edges from a symbol body."""
    text = _node_text(node, src)
    edges: list[Edge] = []
    for m in _CONTEXT_CONSUME_RE.finditer(text):
        rest = text[m.end():]
        arg_m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)', rest)
        if arg_m:
            edges.append(Edge(
                source_fqn=enclosing_fqn,
                target=arg_m.group(1),
                kind="consumes_context",
            ))
    for m in _CONTEXT_PROVIDE_RE.finditer(text):
        edges.append(Edge(
            source_fqn=enclosing_fqn,
            target=m.group(1),
            kind="provides_context",
        ))
    return edges


# ---------------------------------------------------------------------------
# Reference extractor
# ---------------------------------------------------------------------------

def _extract_references(
    root: Node,
    src: bytes,
    lang: Language,
    ext: str,
    symbols: list[Symbol],
) -> list[Reference]:
    """Extract call-site reference locations for find-references and density scoring."""
    refs: list[Reference] = []
    sorted_syms = sorted(symbols, key=lambda s: s.start_line)

    def find_context_fqn(line: int) -> Optional[str]:
        for sym in sorted_syms:
            if sym.start_line <= line <= sym.end_line:
                return sym.fqn
        return None

    pattern = _PY_CALL_QUERY if ext == ".py" else _TS_CALL_QUERY
    for n, _ in _run_query(lang, pattern, root):
        name = _node_text(n, src)
        if not name or len(name) <= 1:
            continue
        line = n.start_point[0] + 1
        refs.append(Reference(
            symbol_name=name,
            symbol_fqn=None,
            line=line,
            ref_kind="CALL",
            context_fqn=find_context_fqn(line),
        ))

    return refs


# ---------------------------------------------------------------------------
# Framework tag analysis
# ---------------------------------------------------------------------------

def _detect_directive(root: Node, src: bytes, lang: Language) -> Optional[str]:
    """Return TAG_USE_CLIENT, TAG_USE_SERVER, or None based on first-10-line directive."""
    for n, _ in _run_query(lang, _DIRECTIVE_QUERY, root):
        if n.start_point[0] >= 10:
            continue
        text = _node_text(n, src)
        if text == "use client":
            return TAG_USE_CLIENT
        if text == "use server":
            return TAG_USE_SERVER
    return None


def _analyze_tags(
    root: Node,
    src: bytes,
    file_path: str,
    lang: Language,
    ext: str,
    symbols: list["Symbol"],
) -> tuple[list[str], dict[str, list[str]]]:
    """
    Detect architectural framework markers using the already-parsed tree.

    Returns
    -------
    file_tags  : Tags that propagate to every chunk in this file.
    fqn_tags   : {fqn: [tag, ...]} for per-symbol tags.
    """
    file_tags: list[str] = []
    fqn_tags: dict[str, list[str]] = {}

    if ext != ".py":
        # ── Framework tags (JS/TS only) ──────────────────────────────────────

        # ── 1. Environment boundary ──────────────────────────────────────────
        directive = _detect_directive(root, src, lang)
        if directive:
            file_tags.append(directive)

        # ── 2. Next.js page/layout (file-level) ──────────────────────────────
        norm_path = file_path.replace("\\", "/")
        if re.search(r'/app/.*/page\.tsx?$', norm_path):
            file_tags.append(TAG_NEXT_PAGE)
        elif re.search(r'/app/.*/layout\.tsx?$', norm_path):
            file_tags.append(TAG_NEXT_LAYOUT)

        # ── 3. Zustand store (file-level) ────────────────────────────────────
        imports = _extract_imports(root, src, lang, ext)
        if any("zustand" in imp for imp in imports):
            all_ident_calls = _run_query(lang, _IDENT_CALL_QUERY, root)
            if any(_node_text(n, src) == "create" for n, _ in all_ident_calls):
                file_tags.append(TAG_ZUSTAND)

        # ── 4. Per-symbol framework tags ─────────────────────────────────────
        jsx_matches = (
            _run_query(lang, _JSX_QUERY, root) if ext in (".tsx", ".jsx", ".js") else []
        )
        ident_matches  = _run_query(lang, _IDENT_CALL_QUERY,      root)
        member_matches = _run_query(lang, _MEMBER_CALL_OBJ_QUERY, root)

        is_use_server = TAG_USE_SERVER in file_tags
        raw_lines: list[str] = (
            src.decode("utf-8", errors="replace").splitlines() if is_use_server else []
        )

        for sym in symbols:
            lo, hi = sym.start_line, sym.end_line
            tags: list[str] = []
            text = sym.text

            if any(lo <= n.start_point[0] + 1 <= hi for n, _ in jsx_matches):
                tags.append(TAG_REACT_COMP)

            if any(
                lo <= n.start_point[0] + 1 <= hi and _node_text(n, src) in _FIREBASE_CLIENT_FUNS
                for n, _ in ident_matches
            ):
                tags.append(TAG_USER_PRIV)

            if any(
                lo <= n.start_point[0] + 1 <= hi and _node_text(n, src) == "admin"
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
                    prev_line = raw_lines[sym.start_line - 2]
                    has_export = bool(_EXPORT_RE.search(prev_line))
                if has_export:
                    tags.append(TAG_SERVER_ACTION)

            if _FIREBASE_CALLABLE_RE.search(text):
                tags.append(TAG_FIREBASE_CALLABLE)

            if _FIRESTORE_TRIGGER_RE.search(text):
                tags.append(TAG_FIRESTORE_TRIGGER)

            if tags:
                fqn_tags[sym.fqn] = tags

    # ── Semantic category tags (all languages) ────────────────────────────────
    for sym in symbols:
        cat_tags = tag_symbol(sym.name, sym.text)
        if cat_tags:
            fqn_tags[sym.fqn] = fqn_tags.get(sym.fqn, []) + cat_tags

    return file_tags, fqn_tags


def analyze_framework_tags(
    file_path: str,
    content: str,
) -> tuple[list[str], dict[str, list[str]]]:
    """
    Public API: analyse a source file for architectural framework markers.

    Returns ([], {}) for extensions not in LANGUAGES.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in LANGUAGES:
        return [], {}
    lang = LANGUAGES[ext]
    src  = content.encode("utf-8")
    root = Parser(lang).parse(src).root_node
    if ext == ".py":
        symbols, _, _ = _extract_py_symbols(root, src, file_path, lang)
    else:
        symbols, _, _ = _extract_ts_symbols(root, src, file_path, lang, ext)
    return _analyze_tags(root, src, file_path, lang, ext, symbols)


# ---------------------------------------------------------------------------
# Class skeleton — replaces method bodies with ' ...' for structural embedding
# ---------------------------------------------------------------------------

def _skeletonize(node: Node, src: bytes, stub_node_types: set[str]) -> str:
    """Return source of a class with method/function bodies replaced by stubs."""
    class_body = next(
        (c for c in node.children if c.type in ("class_body", "block", "statement_block")),
        None,
    )
    if class_body is None:
        return _node_text(node, src)

    parts: list[str] = [
        src[node.start_byte:class_body.start_byte].decode("utf-8", errors="replace")
    ]
    last = class_body.start_byte

    for child in class_body.children:
        if child.type in stub_node_types:
            body = next(
                (c for c in child.children if c.type in ("block", "statement_block")), None
            )
            if body:
                parts.append(src[last:body.start_byte].decode("utf-8", errors="replace"))
                parts.append(" ...\n")
                last = body.end_byte

    parts.append(src[last:class_body.end_byte].decode("utf-8", errors="replace"))
    parts.append(src[class_body.end_byte:node.end_byte].decode("utf-8", errors="replace"))
    return "".join(parts)


_TS_STUB_TYPES: set[str] = {"method_definition", "function_declaration", "arrow_function"}

# ---------------------------------------------------------------------------
# TypeScript / JavaScript symbol extractor
# ---------------------------------------------------------------------------

_TS_NAME_TYPES = frozenset(("identifier", "type_identifier", "property_identifier"))


def _ts_decl_name(node: Node, src: bytes) -> Optional[str]:
    """Return the first identifier/type_identifier/property_identifier child."""
    for child in node.children:
        if child.type in _TS_NAME_TYPES:
            return _node_text(child, src)
    return None


def _extract_ts_symbols(
    root: Node,
    src: bytes,
    file_path: str,
    lang: Language,
    ext: str,
) -> tuple[list[Symbol], list[Edge], list[SymbolType]]:
    symbols: list[Symbol] = []
    edges: list[Edge] = []
    symbol_types: list[SymbolType] = []

    def emit(
        node: Node,
        kind: str,
        name: str,
        class_ctx: Optional[str],
        call_scope: Optional[Node] = None,
        type_node: Optional[Node] = None,
    ) -> Symbol:
        fqn = _build_fqn(file_path, class_ctx, name)
        sym = Symbol(
            fqn=fqn,
            kind=kind,
            name=name,
            class_context=class_ctx,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            text=_node_text(node, src),
        )
        symbols.append(sym)
        scope_node = call_scope or node
        for call_name in _extract_calls(scope_node, src, lang, ext):
            edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
        edges.extend(_extract_context_edges(scope_node, src, fqn))
        if class_ctx:
            class_fqn = _build_fqn(file_path, None, class_ctx)
            edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
        st = _extract_ts_type(type_node or node, src, fqn)
        if st:
            symbol_types.append(st)
        return sym

    def walk(node: Node, class_ctx: Optional[str]) -> None:
        t = node.type

        # Unwrap export wrappers transparently
        if t == "export_statement":
            for child in node.children:
                walk(child, class_ctx)
            return

        # Class declaration
        if t == "class_declaration":
            name = _ts_decl_name(node, src)
            if name:
                fqn = _build_fqn(file_path, None, name)
                sym = Symbol(
                    fqn=fqn,
                    kind="class",
                    name=name,
                    class_context=None,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    text=_skeletonize(node, src, _TS_STUB_TYPES),
                )
                symbols.append(sym)
                # EXTENDS / IMPLEMENTS edges from class heritage
                for child in node.children:
                    if child.type == "class_heritage":
                        for sub in child.children:
                            if sub.type == "extends_clause":
                                for id_node in sub.children:
                                    if id_node.type in _TS_NAME_TYPES:
                                        edges.append(Edge(
                                            source_fqn=fqn,
                                            target=_node_text(id_node, src),
                                            kind="extends",
                                        ))
                                        break
                            elif sub.type == "implements_clause":
                                for id_node in sub.children:
                                    if id_node.type in _TS_NAME_TYPES:
                                        edges.append(Edge(
                                            source_fqn=fqn,
                                            target=_node_text(id_node, src),
                                            kind="implements",
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
                                    source_fqn=sym.fqn,
                                    target=_node_text(id_node, src),
                                    kind="extends",
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

        # Named arrow / function-expression:  const Foo = () => {}
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
                    name = _node_text(name_node, src)
                    fqn = _build_fqn(file_path, class_ctx, name)
                    sym = Symbol(
                        fqn=fqn,
                        kind="arrow_function",
                        name=name,
                        class_context=class_ctx,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        text=_node_text(node, src),
                    )
                    symbols.append(sym)
                    for call_name in _extract_calls(value_node, src, lang, ext):
                        edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
                    edges.extend(_extract_context_edges(value_node, src, fqn))
                    if class_ctx:
                        class_fqn = _build_fqn(file_path, None, class_ctx)
                        edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
                    st = _extract_ts_type(value_node, src, fqn)
                    if st:
                        symbol_types.append(st)
            return

        for child in node.children:
            walk(child, class_ctx)

    walk(root, None)
    return symbols, edges, symbol_types


# ---------------------------------------------------------------------------
# Python symbol extractor
# ---------------------------------------------------------------------------

def _extract_py_symbols(
    root: Node,
    src: bytes,
    file_path: str,
    lang: Language,
) -> tuple[list[Symbol], list[Edge], list[SymbolType]]:
    symbols: list[Symbol] = []
    edges: list[Edge] = []
    symbol_types: list[SymbolType] = []  # Python type extraction not implemented

    def walk(node: Node, class_ctx: Optional[str]) -> None:
        t = node.type

        if t == "class_definition":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node:
                name = _node_text(name_node, src)
                fqn = _build_fqn(file_path, None, name)
                sym = Symbol(
                    fqn=fqn,
                    kind="class",
                    name=name,
                    class_context=None,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    text=_skeletonize(node, src, {"function_definition"}),
                )
                symbols.append(sym)
                body = next((c for c in node.children if c.type == "block"), None)
                if body:
                    for child in body.children:
                        walk(child, name)
            return

        if t == "function_definition":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node:
                name = _node_text(name_node, src)
                fqn = _build_fqn(file_path, class_ctx, name)
                sym = Symbol(
                    fqn=fqn,
                    kind="method" if class_ctx else "function",
                    name=name,
                    class_context=class_ctx,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    text=_node_text(node, src),
                )
                symbols.append(sym)
                for call_name in _extract_calls(node, src, lang, ".py"):
                    edges.append(Edge(source_fqn=fqn, target=call_name, kind="call"))
                if class_ctx:
                    class_fqn = _build_fqn(file_path, None, class_ctx)
                    edges.append(Edge(source_fqn=class_fqn, target=fqn, kind="owns"))
            return

        for child in node.children:
            walk(child, class_ctx)

    walk(root, None)
    return symbols, edges, symbol_types


# ---------------------------------------------------------------------------
# Public: parse_file
# ---------------------------------------------------------------------------

def parse_file(file_path: str, content: str) -> ParseResult:
    """
    Parse a source file with tree-sitter and return all symbols, edges,
    references, and type annotations as a ParseResult.

    Backward-compatible: ParseResult is a NamedTuple — result[0]==symbols,
    result[1]==edges still work for callers using positional unpacking.

    Returns ParseResult([], [], [], []) for extensions not in LANGUAGES.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in LANGUAGES:
        return ParseResult([], [], [], [])

    lang = LANGUAGES[ext]
    src = content.encode("utf-8")
    tree = Parser(lang).parse(src)

    import_edges = [
        Edge(source_fqn=file_path, target=p, kind="import")
        for p in _extract_imports(tree.root_node, src, lang, ext)
    ]

    if ext == ".py":
        symbols, call_edges, symbol_types = _extract_py_symbols(
            tree.root_node, src, file_path, lang
        )
    else:
        symbols, call_edges, symbol_types = _extract_ts_symbols(
            tree.root_node, src, file_path, lang, ext
        )

    references = _extract_references(tree.root_node, src, lang, ext, symbols)

    return ParseResult(
        symbols=symbols,
        edges=import_edges + call_edges,
        references=references,
        symbol_types=symbol_types,
    )


# ---------------------------------------------------------------------------
# Chunk builders
# ---------------------------------------------------------------------------

def _symbol_rich_text(
    sym: Symbol,
    file_path: str,
    tags: Optional[list[str]] = None,
    sym_type: Optional[SymbolType] = None,
) -> str:
    """
    Build the header-prefixed text that is embedded for a tier-1 chunk.
    Tags and type annotations in the header let the embedding model attend to
    architectural context alongside the code itself.
    """
    tag_line  = f"Tags: {' '.join(tags)}\n" if tags else ""
    type_line = f"Type: {sym_type.return_type}\n" if (sym_type and sym_type.return_type) else ""
    return (
        f"File: {file_path}\n"
        f"Entity: {sym.fqn} ({sym.kind})\n"
        f"{tag_line}"
        f"{type_line}"
        f"Lines: {sym.start_line}-{sym.end_line}\n"
        f"Code:\n{sym.text}"
    )


# ---------------------------------------------------------------------------
# Public: chunk_file_ast
# ---------------------------------------------------------------------------

def chunk_file_ast(
    file_path: str,
    content: str,
    max_tokens: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """
    AST-guided chunker.  One Chunk per extracted Symbol; oversized symbols are
    split via fallback_token_chunker.  Falls back entirely for unsupported files
    or files with no detectable symbols.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in LANGUAGES:
        return fallback_token_chunker(content, file_path, max_tokens, overlap)

    lang = LANGUAGES[ext]
    src  = content.encode("utf-8")
    root = Parser(lang).parse(src).root_node

    # Extract symbols + edges (mirrors parse_file internals, reuses root)
    import_edges = [
        Edge(source_fqn=file_path, target=p, kind="import")
        for p in _extract_imports(root, src, lang, ext)
    ]
    if ext == ".py":
        symbols, call_edges, symbol_types = _extract_py_symbols(root, src, file_path, lang)
    else:
        symbols, call_edges, symbol_types = _extract_ts_symbols(root, src, file_path, lang, ext)
    edges = import_edges + call_edges

    type_by_fqn: dict[str, SymbolType] = {st.fqn: st for st in symbol_types}

    # Framework tag analysis — same tree, no second Parser.parse() call
    file_tags, fqn_tags = _analyze_tags(root, src, file_path, lang, ext, symbols)

    if not symbols:
        fallback = fallback_token_chunker(content, file_path, max_tokens, overlap)
        for c in fallback:
            c.tags = list(file_tags)
        return fallback

    edges_by_fqn: dict[str, list[Edge]] = {}
    for e in edges:
        edges_by_fqn.setdefault(e.source_fqn, []).append(e)

    chunks: list[Chunk] = []
    for sym in symbols:
        sym_tags  = list(dict.fromkeys(file_tags + fqn_tags.get(sym.fqn, [])))
        sym_type  = type_by_fqn.get(sym.fqn)
        rich_text = _symbol_rich_text(sym, file_path, sym_tags, sym_type)
        sym_edges = edges_by_fqn.get(sym.fqn, [])

        if jina_tokenizer.count_tokens(rich_text) > max_tokens:
            sub = fallback_token_chunker(
                sym.text, file_path, max_tokens, overlap, parent_scope=sym.fqn
            )
            if sub:
                sub[0].edges   = sym_edges
                sub[0].symbols = [sym]
                sub[0].tags    = sym_tags
            chunks.extend(sub)
        else:
            chunks.append(Chunk(
                text=rich_text,
                file=file_path,
                start_line=sym.start_line,
                end_line=sym.end_line,
                scope=sym.fqn,
                symbols=[sym],
                edges=sym_edges,
                tags=sym_tags,
            ))

    return chunks


# ---------------------------------------------------------------------------
# Public: fallback_token_chunker
# ---------------------------------------------------------------------------

def fallback_token_chunker(
    text: str,
    file_path: str,
    max_tokens: int = 1000,
    overlap: int = 100,
    parent_scope: str = "Global",
) -> list[Chunk]:
    """
    Token-based line chunker with a monster-line shredder for huge inlined blobs.
    """
    lines = text.split("\n")
    header_template = f"File: {file_path}\nScope: {parent_scope} (Part X)\nCode:\n"
    header_tokens = jina_tokenizer.count_tokens(header_template)
    safe_max = max(1, max_tokens - header_tokens)

    raw_chunks: list[str] = []
    current_lines: list[str] = []
    current_tokens = 0

    for line in lines:
        line_tokens = jina_tokenizer.count_tokens(line + "\n")

        # Monster-line shredder: single line exceeds the whole budget
        if line_tokens > safe_max:
            if current_lines:
                raw_chunks.append("\n".join(current_lines))
                current_lines, current_tokens = [], 0
            raw = jina_tokenizer.tokenizer.encode(line, add_special_tokens=False)
            for i in range(0, len(raw), safe_max - overlap):
                raw_chunks.append(jina_tokenizer.decode_tokens(raw[i : i + safe_max]))
            continue

        if current_tokens + line_tokens > safe_max and current_lines:
            raw_chunks.append("\n".join(current_lines))
            overlap_lines: list[str] = []
            overlap_tokens = 0
            for prev in reversed(current_lines):
                t = jina_tokenizer.count_tokens(prev + "\n")
                if overlap_tokens + t > overlap:
                    break
                overlap_lines.insert(0, prev)
                overlap_tokens += t
            current_lines = overlap_lines
            current_tokens = overlap_tokens

        current_lines.append(line)
        current_tokens += line_tokens

    if current_lines:
        raw_chunks.append("\n".join(current_lines))

    total = len(raw_chunks)
    result: list[Chunk] = []
    for idx, code in enumerate(raw_chunks):
        rich_text = (
            f"File: {file_path}\n"
            f"Scope: {parent_scope} (Part {idx + 1}/{total})\n"
            f"Code:\n{code}"
        )
        result.append(Chunk(
            text=rich_text,
            file=file_path,
            start_line=0,
            end_line=0,
            scope=f"{parent_scope}_part_{idx + 1}",
        ))

    return result
