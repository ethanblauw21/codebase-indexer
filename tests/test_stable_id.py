"""
Tests for stable_id.py — the single source of truth for FAISS vector IDs
and dtype utilities.

GOLDEN FIXTURE CONTRACT
-----------------------
The stable_id formula is pinned by test_formula_matches_reference.  Any
change to the formula (algorithm, number of hex chars, input encoding) will
cause these tests to fail.  A failure here means ALL existing FAISS indexes
are orphaned — plan a full rebuild before merging.

The reference implementation in the test file uses the algorithm explicitly
(hashlib.md5, [:15], base-16 int) so that a change in stable_id.py's
implementation is caught even if it produces accidentally equivalent output
for one specific input.
"""
import hashlib
import sqlite3
import tempfile
import os

import numpy as np
import pytest

from stable_id import (
    stable_id,
    to_faiss_ids,
    to_faiss_matrix,
    TIER_CONFIGS,
    TIER_NUM,
    TIER_NAME,
)


# ---------------------------------------------------------------------------
# Reference implementation — kept intentionally separate from stable_id.py
# ---------------------------------------------------------------------------

def _reference_stable_id(tier_name: str, file_path: str, scope: str) -> int:
    """
    Reference formula for golden tests.  Must NOT be changed when stable_id.py
    is refactored — this is the pinned expected value.
    """
    raw = f"{tier_name}::{file_path}::{scope}".encode()
    return int(hashlib.md5(raw).hexdigest()[:15], 16)


# ---------------------------------------------------------------------------
# Golden fixture — formula match
# ---------------------------------------------------------------------------

_GOLDEN_CASES = [
    ("tier1_surgical",       "src/core.py",              "src/core.py::embed"),
    ("tier1_surgical",       "src/hybrid_retriever.py",  "src/hybrid_retriever.py::HybridRetriever.retrieve"),
    ("tier2_component",      "src/hybrid_retriever.py",  "Full File:0"),
    ("tier3_architectural",  "src/MCPServer.py",          "Full File:0"),
    ("tier1_surgical",       "functions/src/index.ts",   "functions/src/index.ts::writeTransactionLog"),
    # Path with Windows-style separators (normalised to forward slashes at ingest time)
    ("tier1_surgical",       "src/tui/backend.py",       "src/tui/backend.py::BackendThread.run"),
]


@pytest.mark.parametrize("tier,path,scope", _GOLDEN_CASES)
def test_formula_matches_reference(tier: str, path: str, scope: str) -> None:
    """stable_id() must produce the same output as the pinned reference formula."""
    assert stable_id(tier, path, scope) == _reference_stable_id(tier, path, scope)


# ---------------------------------------------------------------------------
# 60-bit range invariant
# ---------------------------------------------------------------------------

SIGNED_INT64_MAX = (1 << 63) - 1
SIXTY_BIT_MAX    = (1 << 60) - 1


@pytest.mark.parametrize("tier,path,scope", _GOLDEN_CASES)
def test_stable_id_fits_signed_int64(tier: str, path: str, scope: str) -> None:
    """All stable IDs must be non-negative and below the signed int64 ceiling."""
    fid = stable_id(tier, path, scope)
    assert 0 <= fid <= SIGNED_INT64_MAX, (
        f"stable_id({tier!r}, {path!r}, {scope!r}) = {fid} "
        f"exceeds signed int64 max ({SIGNED_INT64_MAX})"
    )


def test_stable_id_at_most_60_bits() -> None:
    """Formula uses 15 hex chars = 60 bits; result must fit."""
    for tier, path, scope in _GOLDEN_CASES:
        fid = stable_id(tier, path, scope)
        assert fid <= SIXTY_BIT_MAX, (
            f"stable_id({tier!r}, {path!r}, {scope!r}) = {fid} "
            f"exceeds 60-bit ceiling ({SIXTY_BIT_MAX})"
        )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_stable_id_is_deterministic() -> None:
    """Same inputs must always produce the same output."""
    for tier, path, scope in _GOLDEN_CASES:
        assert stable_id(tier, path, scope) == stable_id(tier, path, scope)


def test_stable_id_inputs_are_order_sensitive() -> None:
    """Swapping tier/path/scope must produce a different ID (field separator matters)."""
    a = stable_id("tier1_surgical", "src/a.py", "src/a.py::fn")
    b = stable_id("src/a.py", "tier1_surgical", "src/a.py::fn")
    assert a != b, "Input order must affect the ID"


# ---------------------------------------------------------------------------
# Tier configuration invariants
# ---------------------------------------------------------------------------

def test_tier_num_and_tier_name_are_inverses() -> None:
    """TIER_NUM and TIER_NAME must be exact inverses of each other."""
    for name, num in TIER_NUM.items():
        assert TIER_NAME[num] == name
    for num, name in TIER_NAME.items():
        assert TIER_NUM[name] == num


def test_tier_configs_count() -> None:
    assert len(TIER_CONFIGS) == 3


def test_tier_integers_are_1_2_3() -> None:
    assert set(TIER_NUM.values()) == {1, 2, 3}


# ---------------------------------------------------------------------------
# to_faiss_ids — dtype contract
# ---------------------------------------------------------------------------

def test_to_faiss_ids_dtype() -> None:
    ids = to_faiss_ids([1, 2, 3])
    assert ids.dtype == np.int64, f"Expected int64, got {ids.dtype}"


def test_to_faiss_ids_windows_int32_pitfall() -> None:
    """
    On Windows, numpy's default integer type is int32.
    np.array([1, 2, 3]) → int32 → FAISS SWIG TypeError.
    to_faiss_ids() must always produce int64.
    """
    # Simulate the Windows pitfall: create an int32 array and verify
    # that to_faiss_ids does NOT produce int32.
    native = np.array([1, 2, 3])   # may be int32 on Windows
    safe   = to_faiss_ids([1, 2, 3])
    assert safe.dtype == np.int64
    # Values must match regardless of platform default
    assert list(safe) == [1, 2, 3]


def test_to_faiss_ids_empty() -> None:
    ids = to_faiss_ids([])
    assert ids.dtype == np.int64
    assert len(ids) == 0


def test_to_faiss_ids_large_60bit_value() -> None:
    """A 60-bit stable ID must round-trip through to_faiss_ids without truncation."""
    large = SIXTY_BIT_MAX
    ids   = to_faiss_ids([large])
    assert ids[0] == large
    assert ids.dtype == np.int64


# ---------------------------------------------------------------------------
# to_faiss_matrix — dtype and layout contract
# ---------------------------------------------------------------------------

def test_to_faiss_matrix_dtype() -> None:
    vecs   = [np.ones(768, dtype=np.float64), np.zeros(768, dtype=np.float64)]
    matrix = to_faiss_matrix(vecs)
    assert matrix.dtype == np.float32, f"Expected float32, got {matrix.dtype}"


def test_to_faiss_matrix_shape() -> None:
    vecs   = [np.ones(768) for _ in range(5)]
    matrix = to_faiss_matrix(vecs)
    assert matrix.shape == (5, 768)


def test_to_faiss_matrix_c_contiguous() -> None:
    """FAISS C++ code requires C-order (row-major) memory layout."""
    vecs   = [np.ones(768) for _ in range(3)]
    matrix = to_faiss_matrix(vecs)
    assert matrix.flags["C_CONTIGUOUS"], "Matrix must be C-contiguous for FAISS"


def test_to_faiss_matrix_fortran_input_is_corrected() -> None:
    """Fortran-order input (from some scipy utilities) must be converted to C-order."""
    f_arr  = np.asfortranarray(np.ones((3, 768), dtype=np.float32))
    vecs   = list(f_arr)
    matrix = to_faiss_matrix(vecs)
    assert matrix.flags["C_CONTIGUOUS"]


# ---------------------------------------------------------------------------
# Diff categorization — tests for compute_diff
# ---------------------------------------------------------------------------

class _MockDB:
    """Minimal CodeDB stand-in for compute_diff tests."""
    def __init__(self, existing: dict[str, str]) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, content_hash TEXT)"
        )
        for path, hash_ in existing.items():
            self._conn.execute("INSERT INTO files VALUES (NULL, ?, ?)", (path, hash_))
        self._conn.commit()


def test_compute_diff_all_new() -> None:
    from incremental_indexer import compute_diff, DiffResult
    db   = _MockDB({})
    disk = {"a.py": "hash1", "b.py": "hash2"}
    diff = compute_diff(db, disk)
    assert set(diff.new) == {"a.py", "b.py"}
    assert diff.modified == []
    assert diff.deleted  == []


def test_compute_diff_all_unchanged() -> None:
    from incremental_indexer import compute_diff
    existing = {"a.py": "hash1", "b.py": "hash2"}
    db   = _MockDB(existing)
    disk = dict(existing)
    diff = compute_diff(db, disk)
    assert diff.new      == []
    assert diff.modified == []
    assert diff.deleted  == []


def test_compute_diff_modified() -> None:
    from incremental_indexer import compute_diff
    db   = _MockDB({"a.py": "old_hash"})
    disk = {"a.py": "new_hash"}
    diff = compute_diff(db, disk)
    assert diff.new      == []
    assert diff.modified == ["a.py"]
    assert diff.deleted  == []


def test_compute_diff_deleted() -> None:
    from incremental_indexer import compute_diff
    db   = _MockDB({"a.py": "hash1"})
    disk = {}
    diff = compute_diff(db, disk)
    assert diff.new      == []
    assert diff.modified == []
    assert diff.deleted  == ["a.py"]


def test_compute_diff_mixed() -> None:
    from incremental_indexer import compute_diff
    db = _MockDB({
        "unchanged.py": "same",
        "modified.py":  "old",
        "deleted.py":   "any",
    })
    disk = {
        "unchanged.py": "same",
        "modified.py":  "new",
        "new.py":       "fresh",
    }
    diff = compute_diff(db, disk)
    assert diff.new      == ["new.py"]
    assert diff.modified == ["modified.py"]
    assert diff.deleted  == ["deleted.py"]


def test_compute_diff_mtime_equal_content_changed() -> None:
    """Hash comparison must catch content changes even when mtime is identical."""
    from incremental_indexer import compute_diff
    db   = _MockDB({"tricky.py": "aaaa"})
    disk = {"tricky.py": "bbbb"}   # same path, different hash
    diff = compute_diff(db, disk)
    assert diff.modified == ["tricky.py"]
