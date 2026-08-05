"""
AST-based code chunker using tree-sitter.

Primary API
-----------
parse_file(file_path, content) -> ParseResult
    Full symbol + dependency graph extraction for any supported file.
    Dispatches to the registered LanguageAdapter for the file's extension.
    Returns ParseResult([], [], [], []) for unsupported extensions.

chunk_file_ast(file_path, content, max_tokens, overlap) -> list[Chunk]
    AST-guided chunker.  Calls adapter.parse() to get symbols, then runs the
    generic chunking loop.  Falls back to fallback_token_chunker for
    unsupported files or files with no detectable symbols.

fallback_token_chunker(text, file_path, max_tokens, overlap, parent_scope) -> list[Chunk]
    Line-oriented token chunker with a monster-line shredder for huge inlined
    blobs (Base64 strings, inline SVGs, etc.).

analyze_framework_tags(file_path, content) -> (file_tags, fqn_tags)
    Public API: delegate to the registered adapter.

Backward compatibility
----------------------
Symbol, Edge, Chunk, Reference, SymbolType, ParseResult are re-exported from
adapters.base so existing callers (db.py, incremental_indexer.py, etc.) need
no import changes.

Tag constants (TAG_USE_CLIENT, TAG_REACT_COMPONENT, …) are re-exported from
adapters.ts_adapter — they were always TS/JS-specific but were previously
defined here.
"""
from __future__ import annotations

import os
from typing import Optional

from core import jina_tokenizer

# ---------------------------------------------------------------------------
# Re-exports for backward compatibility
# ---------------------------------------------------------------------------

# Data types
from adapters.base import (          # noqa: F401
    Chunk, Edge, ParseResult, Reference, Symbol, SymbolType,
)

# Tag constants (JS/TS only; public API maintained for external callers)
from adapters.ts_adapter import (    # noqa: F401
    TAG_CONTEXT_CONSUMER, TAG_CONTEXT_PROVIDER, TAG_ELEVATED_PRIV,
    TAG_FIREBASE_CALLABLE, TAG_FIRESTORE_TRIGGER, TAG_HOOK, TAG_MUTATION,
    TAG_NEXT_LAYOUT, TAG_NEXT_PAGE, TAG_REACT_COMP, TAG_SERVER_ACTION,
    TAG_USE_CLIENT, TAG_USE_SERVER, TAG_USER_PRIV, TAG_ZUSTAND,
)

# Adapter registry
from adapters import get_adapter


# ---------------------------------------------------------------------------
# Chunk text builder (generic — language-neutral)
# ---------------------------------------------------------------------------

def _symbol_rich_text(
    sym: Symbol,
    file_path: str,
    tags: Optional[list[str]] = None,
    sym_type: Optional[SymbolType] = None,
) -> str:
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
# Public: parse_file
# ---------------------------------------------------------------------------

def parse_file(file_path: str, content: str) -> ParseResult:
    """
    Parse a source file and return all symbols, edges, references, and type
    annotations as a ParseResult.  Dispatches to the registered LanguageAdapter.

    Returns ParseResult([], [], [], []) for unsupported extensions.
    """
    ext     = os.path.splitext(file_path)[1].lower()
    adapter = get_adapter(ext)
    if adapter is None:
        return ParseResult([], [], [], [])
    return adapter.parse(file_path, content.encode("utf-8"))


# ---------------------------------------------------------------------------
# Public: analyze_framework_tags
# ---------------------------------------------------------------------------

def analyze_framework_tags(
    file_path: str,
    content: str,
) -> tuple[list[str], dict[str, list[str]]]:
    """
    Analyse a source file for architectural framework markers.
    Returns ([], {}) for unsupported extensions.
    """
    ext     = os.path.splitext(file_path)[1].lower()
    adapter = get_adapter(ext)
    if adapter is None:
        return [], {}
    src    = content.encode("utf-8")
    result = adapter.parse(file_path, src)
    return adapter.analyze_tags(file_path, src, result.symbols)


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
    ext     = os.path.splitext(file_path)[1].lower()
    adapter = get_adapter(ext)
    if adapter is None:
        return fallback_token_chunker(content, file_path, max_tokens, overlap)

    src    = content.encode("utf-8")
    result = adapter.parse(file_path, src)

    if not result.symbols:
        fallback = fallback_token_chunker(content, file_path, max_tokens, overlap)
        file_tags, _ = adapter.analyze_tags(file_path, src, [])
        for c in fallback:
            c.tags = list(file_tags)
        return fallback

    file_tags, fqn_tags = adapter.analyze_tags(file_path, src, result.symbols)

    type_by_fqn:  dict[str, SymbolType]     = {st.fqn: st for st in result.symbol_types}
    edges_by_fqn: dict[str, list[Edge]]     = {}
    for e in result.edges:
        edges_by_fqn.setdefault(e.source_fqn, []).append(e)

    # Adapter-supplied chunking policy. Absent on every adapter but L5X, in
    # which case every symbol is chunkable and behaviour is unchanged. Keeping
    # this as adapter policy rather than a special case in shared code means a
    # language's retrieval granularity is declared next to its extraction.
    chunkable = getattr(adapter, "chunkable_kinds", None)

    chunks: list[Chunk] = []
    for sym in result.symbols:
        if chunkable is not None and sym.kind not in chunkable:
            # Still a graph node with all its edges — see LanguageAdapter.
            continue
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
                text       = rich_text,
                file       = file_path,
                start_line = sym.start_line,
                end_line   = sym.end_line,
                scope      = sym.fqn,
                symbols    = [sym],
                edges      = sym_edges,
                tags       = sym_tags,
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
            current_lines  = overlap_lines
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
            text       = rich_text,
            file       = file_path,
            start_line = 0,
            end_line   = 0,
            scope      = f"{parent_scope}_part_{idx + 1}",
        ))

    return result
