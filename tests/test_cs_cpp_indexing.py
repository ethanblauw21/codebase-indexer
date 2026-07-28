"""C#/C++ source is chunked+embedded by the production indexer (ADR-017 §1 Tier A).

The csharp/cpp adapters (ADR-003) are registered in adapters/__init__.py and produce
real symbols, but the scan gate historically omitted their source extensions, so the
disk scan never fed them — C#/C++ code was never chunked (only .csproj/.sln
descriptors, edges-only). These tests lock the scan gate to the Tier-A claim so the
regression can't silently return.

The gate moved from `incremental_indexer`'s module constants to `scan_policy`
(ADR-026 §2); these assertions follow it there.
"""
import os

import scan_policy as sp
from ast_chunker import chunk_file_ast, parse_file

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "src")


def test_cs_cpp_source_extensions_are_scanned():
    for ext in (".cs", ".cpp", ".cc", ".cxx", ".h", ".hpp"):
        assert ext in sp.DEFAULT_INDEXABLE_EXTS, f"{ext} must be chunk/embed-scanned (Tier A)"


def test_project_descriptors_stay_edges_only():
    # .csproj/.sln remain descriptor-only (chunking would embed XML noise, not code).
    assert ".csproj" in sp.PROJECT_EXTS and ".csproj" not in sp.DEFAULT_INDEXABLE_EXTS
    assert ".sln" in sp.PROJECT_EXTS and ".sln" not in sp.DEFAULT_INDEXABLE_EXTS


def _chunk(path):
    text = open(path, encoding="utf-8", errors="ignore").read()
    return chunk_file_ast(path, text), parse_file(path, text)


def test_csharp_fixture_produces_symbol_chunks():
    chunks, pr = _chunk(os.path.join(_FIXTURES, "sample.cs"))
    assert len(chunks) > 0
    assert len(pr.symbols) > 0
    # C# FQNs are namespace-qualified (adapter contract), not path-prefixed.
    assert any("MyApp" in getattr(s, "fqn", "") for s in pr.symbols)


def test_cpp_fixture_produces_symbol_chunks():
    chunks, pr = _chunk(os.path.join(_FIXTURES, "sample.cpp"))
    assert len(chunks) > 0
    assert len(pr.symbols) > 0
    assert any("::" in getattr(s, "fqn", "") for s in pr.symbols)
