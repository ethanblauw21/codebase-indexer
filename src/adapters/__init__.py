"""
Language adapter registry.

REGISTRY maps file extensions to the adapter instance responsible for parsing
that language.  get_adapter(ext) is the primary entry point for ast_chunker.

Adding a new language:
  1. Implement LanguageAdapter in a new adapters/<lang>_adapter.py
  2. Instantiate it here and add its extensions to REGISTRY
  3. Add a conformance fixture suite under tests/fixtures/src/
  4. Capture before-snapshots via tools/capture_snapshots.py
  5. Verify byte-identical output after the adapter is wired in

The L5X entry is a stub that claims the extension from day one, preventing any
future implementer from accidentally baking tree-sitter into the interface.
"""
from __future__ import annotations

from adapters.base import LanguageAdapter
from adapters.python_adapter import PythonAdapter
from adapters.ts_adapter import JavaScriptAdapter, TypeScriptAdapter
from adapters.csharp_adapter import CSharpAdapter
from adapters.cpp_adapter import CppAdapter
from adapters.l5x_adapter import L5xAdapter

_python = PythonAdapter()
_ts     = TypeScriptAdapter()
_js     = JavaScriptAdapter()
_cs     = CSharpAdapter()
_cpp    = CppAdapter()
_l5x    = L5xAdapter()

REGISTRY: dict[str, LanguageAdapter] = {
    ".py":     _python,
    ".ts":     _ts,
    ".tsx":    _ts,
    ".js":     _js,
    ".jsx":    _js,
    ".cs":     _cs,
    ".csproj": _cs,
    ".sln":    _cs,
    ".cpp":    _cpp,
    ".cc":     _cpp,
    ".cxx":    _cpp,
    ".c":      _cpp,
    ".h":      _cpp,
    ".hpp":    _cpp,
    ".hxx":    _cpp,
    ".inl":    _cpp,
    ".L5X":    _l5x,
    ".l5x":    _l5x,
}


def get_adapter(ext: str) -> LanguageAdapter | None:
    """Return the adapter for the given file extension, or None if unsupported."""
    return REGISTRY.get(ext)
