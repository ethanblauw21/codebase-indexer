"""
L5X adapter stub — Rockwell Automation Logix Designer XML format.

L5X has no tree-sitter grammar. The design (rung chunking, tag edges, embedding
strategy) is deferred pending an example corpus and gold queries.

This stub claims the L5X extensions in the adapter registry from day one so
that a future implementer cannot accidentally reach for tree-sitter out of
convention — proving the interface contains no tree-sitter assumption.

See ADR-003 §D4 for the deferral rationale.
"""
from __future__ import annotations

from adapters.base import ParseResult, Symbol


class L5xAdapter:
    language_id = "l5x"
    extensions  = frozenset({".L5X", ".l5x"})

    def parse(self, path: str, src: bytes) -> ParseResult:
        raise NotImplementedError(
            "L5X adapter is not implemented. "
            "See docs/adr/ADR-003 §D4 for deferral rationale. "
            "Design (rung chunking, tag edges, embedding strategy) is deferred "
            "pending an example corpus and gold queries."
        )

    def analyze_tags(
        self,
        path: str,
        src: bytes,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        raise NotImplementedError(
            "L5X adapter is not implemented. See docs/adr/ADR-003 §D4."
        )

    def test_conventions(self):
        return None

    def project_resolver(self):
        return None
