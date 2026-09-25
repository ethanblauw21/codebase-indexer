"""B-030 — one oversized result must not hide the results ranked below it."""
from __future__ import annotations

from types import SimpleNamespace

from MCPServer import _pack_results


def _chunk(name: str, size: int):
    return SimpleNamespace(file=f"src/{name}.ts", scope=name, text="x" * size)


def _count(text: str) -> int:
    return len(text)


def test_an_oversized_chunk_is_skipped_and_later_ones_still_show():
    chunks = [_chunk("first", 50), _chunk("huge", 5000), _chunk("third", 50)]
    out = _pack_results("HEAD\n", chunks, _count, max_tokens=1000)
    assert "SCOPE: first" in out
    assert "SCOPE: third" in out
    assert "SCOPE: huge" not in out
    assert "1 result(s) did not fit the 1000-token budget" in out
    assert "  - src/huge.ts | huge" in out


def test_everything_fits_so_there_is_no_note():
    out = _pack_results("HEAD\n", [_chunk("a", 10), _chunk("b", 10)], _count, max_tokens=1000)
    assert out.startswith("HEAD\n")
    assert "did not fit" not in out


def test_rank_order_is_kept():
    out = _pack_results("", [_chunk("a", 10), _chunk("b", 10), _chunk("c", 10)], _count, 1000)
    assert out.index("SCOPE: a") < out.index("SCOPE: b") < out.index("SCOPE: c")
