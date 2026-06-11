"""Shared tree-sitter helpers used by all adapters that parse with tree-sitter grammars."""
from __future__ import annotations

import warnings

from tree_sitter import Language, Node, Query, QueryCursor


def node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def run_query(lang: Language, pattern: str, node: Node) -> list[tuple[Node, str]]:
    """
    Run a tree-sitter query and return (node, capture_name) pairs.

    Uses the tree-sitter 0.25+ QueryCursor API.  The older lang.query().captures()
    path is kept as a fallback but is deprecated upstream.

    Returns [] on any query compilation or execution failure so callers degrade gracefully.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            q = Query(lang, pattern)
        cursor = QueryCursor(q)
        raw = cursor.captures(node)      # dict: {capture_name: [Node, ...]}
        return [(n, cap) for cap, nodes in raw.items() for n in nodes]
    except Exception:
        return []


def skeletonize(node: Node, src: bytes, stub_node_types: set[str]) -> str:
    """Return source of a class/struct with method bodies replaced by ' ...' stubs."""
    class_body = next(
        (c for c in node.children if c.type in ("class_body", "block", "statement_block")),
        None,
    )
    if class_body is None:
        return node_text(node, src)

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
