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


def _body_of(node: Node) -> Optional[Node]:
    return next((c for c in node.children if c.type in ("block", "statement_block")), None)


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
            body = _body_of(child)
            if body is None:
                # A wrapper such as a field whose value is a function, or a decorated
                # definition: the body is one level down (ADR-034 §2, §4).
                inner = next((c for c in child.children if c.type in stub_node_types), None)
                body = _body_of(inner) if inner is not None else None
            if body:
                parts.append(src[last:body.start_byte].decode("utf-8", errors="replace"))
                parts.append(" ...\n")
                last = body.end_byte

    parts.append(src[last:class_body.end_byte].decode("utf-8", errors="replace"))
    parts.append(src[class_body.end_byte:node.end_byte].decode("utf-8", errors="replace"))
    return "".join(parts)


def merge_adjacent_same_fqn(symbols: list, impl_rank, merge_top_level: bool = False) -> list[tuple]:
    """Merge consecutive class members that share an FQN into one symbol (ADR-034 §3).

    Accessor pairs, `@property` with its setter and `@overload` sets are one concept declared
    more than once; the database keeps one row per FQN, so all but the last used to be lost.
    Only runs of consecutive symbols merge, and only class members unless `merge_top_level`
    is set. TypeScript leaves it off: helpers redeclared in separate test callbacks are
    consecutive top-level symbols that must stay apart. Python sets it: it never extracts
    nested functions, so a top-level run is a module-level `@overload` set or redefinition.

    `impl_rank(sym)` orders a run: lowest first, so the implementation leads the text and stays
    inside the embedder's window. Returns [(symbol, implementation)] in source order, where
    `symbol` is the merged one (or the original when it stands alone).
    """
    from dataclasses import replace

    out: list[tuple] = []
    i = 0
    while i < len(symbols):
        j = i + 1
        mergeable = symbols[i].class_context is not None or merge_top_level
        while (mergeable and j < len(symbols) and symbols[j].fqn == symbols[i].fqn
               and symbols[j].class_context == symbols[i].class_context):
            j += 1
        run = symbols[i:j]
        if len(run) == 1:
            out.append((run[0], run[0]))
        else:
            ordered = sorted(run, key=impl_rank)
            impl = ordered[0]
            out.append((replace(
                impl,
                start_line = min(s.start_line for s in run),
                end_line   = max(s.end_line for s in run),
                text       = "\n\n".join(s.text for s in ordered),
            ), impl))
        i = j
    return out
