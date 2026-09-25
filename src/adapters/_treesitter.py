"""Shared tree-sitter helpers used by all adapters that parse with tree-sitter grammars."""
from __future__ import annotations

import warnings
from typing import Optional

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


def leading_doc(node: Node, src: bytes) -> Optional[Node]:
    """The `/** ... */` comment that leads a class member, or None.

    Leading means the member's previous named sibling in the class body, skipping the member's
    own decorators, with at most one blank line between the comment and what follows it.
    """
    nxt, prev = node, node.prev_named_sibling
    while prev is not None and prev.type == "decorator":
        nxt, prev = prev, prev.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    if not node_text(prev, src).startswith("/**"):
        return None
    if nxt.start_point[0] - prev.end_point[0] > 2:
        return None
    return prev


def _whole_lines(src: bytes, start: int, end: int) -> tuple[int, int]:
    """Widen [start, end) to whole lines when only whitespace shares them."""
    line_start = src.rfind(b"\n", 0, start) + 1
    if src[line_start:start].strip():
        line_start = start
    line_end = src.find(b"\n", end)
    line_end = len(src) if line_end == -1 else line_end + 1
    if src[end:line_end].strip():
        line_end = end
    return line_start, line_end


def skeletonize(
    node: Node,
    src: bytes,
    stub_node_types: set[str],
    drop: Optional[set[int]] = None,
) -> str:
    """Return source of a class/struct with method bodies replaced by ' ...' stubs.

    `drop` holds the start bytes of class-body children to leave out, such as doc comments that
    were moved into their member's own chunk.
    """
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
        if drop and child.start_byte in drop:
            cut_start, cut_end = _whole_lines(src, child.start_byte, child.end_byte)
            parts.append(src[last:cut_start].decode("utf-8", errors="replace"))
            last = cut_end
            continue
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
