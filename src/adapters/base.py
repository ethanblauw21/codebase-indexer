"""
Language adapter base types and Protocol.

Symbol, Edge, Chunk, Reference, SymbolType, ParseResult — the shared data
model for all adapters.  ast_chunker re-exports these for backward compat.

LanguageAdapter — the Protocol all language adapters must satisfy.
build_fqn       — language-neutral FQN builder (adapters may override for their conventions).
TestConventions — file patterns + in-file markers identifying test code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Shared data types (moved from ast_chunker; re-exported there for compat)
# ---------------------------------------------------------------------------

@dataclass
class TestConventions:
    """File naming patterns and in-file markers that identify test code for a language."""
    file_suffixes: list[str]    # path suffixes: ["Tests.cs", ".test.ts", "_test.py"]
    in_file_markers: list[str]  # text substrings in test bodies: ["[Fact]", "describe("]


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
    shared: bool = False  # True for symbols that may span multiple files (C# partial classes)


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
    Supports dict-style access for backward compatibility.
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
    symbol_fqn: Optional[str]
    line: int
    ref_kind: str
    context_fqn: Optional[str]


@dataclass
class SymbolType:
    """TypeScript type annotation metadata for a symbol."""
    fqn: str
    return_type: Optional[str]
    params: list[dict]
    type_params: Optional[str]
    is_async: bool
    is_generator: bool


class ParseResult(NamedTuple):
    """
    Return type of LanguageAdapter.parse() and ast_chunker.parse_file().
    NamedTuple so positional access still works: result[0]==symbols, result[1]==edges.
    """
    symbols: list[Symbol]
    edges: list[Edge]
    references: list[Reference]
    symbol_types: list[SymbolType]


# ---------------------------------------------------------------------------
# FQN builder — language-neutral default; C# adapter overrides for CLR convention
# ---------------------------------------------------------------------------

def build_fqn(file_path: str, class_ctx: Optional[str], name: str) -> str:
    """Build a file-scoped FQN: `file_path::ClassName.member` or `file_path::name`."""
    qualifier = f"{class_ctx}.{name}" if class_ctx else name
    return f"{file_path}::{qualifier}"


# ---------------------------------------------------------------------------
# LanguageAdapter Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LanguageAdapter(Protocol):
    """
    Contract for all language-specific parsers.

    Shared infrastructure (stable IDs, three-tier FAISS index, embeddings,
    RTR pipeline, doc store) is never touched by language expansion — each
    new language adds only its own adapter.

    Each adapter ships with a conformance fixture suite in tests/fixtures/.
    A passing conformance suite is the definition of "supported language."
    """

    language_id: str
    extensions: frozenset[str]

    def parse(self, path: str, src: bytes) -> ParseResult:
        """
        Parse source bytes and return symbols, edges, references, symbol types.
        Must return ParseResult([], [], [], []) for unsupported extensions
        rather than raising — callers degrade gracefully.
        """
        ...

    def analyze_tags(
        self,
        path: str,
        src: bytes,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        """
        Return (file_tags, fqn_tags).
        file_tags propagate to every chunk in this file.
        fqn_tags maps symbol FQN → tag list for per-symbol chunks.
        """
        ...

    def test_conventions(self) -> Optional[TestConventions]:
        """Return test identification conventions (file suffixes + in-file markers)."""
        ...

    def project_resolver(self) -> Optional[object]:
        """Return a project-level dependency resolver, or None if not applicable."""
        ...
