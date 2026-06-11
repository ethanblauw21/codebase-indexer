"""
stable_id.py — Deterministic FAISS vector IDs and FAISS dtype utilities.

Single source of truth for the 60-bit compound-key hash that drives every
add_with_ids / remove_ids call.  Changing this formula invalidates every
existing FAISS index.  The golden fixture tests in tests/test_stable_id.py
are the migration guard — they will fail if the formula changes.
"""
from __future__ import annotations

import hashlib

import numpy as np

# ---------------------------------------------------------------------------
# Tier configuration — fundamental system constants
# ---------------------------------------------------------------------------
#
# Each tuple: (faiss_index_name, max_tokens_per_chunk, overlap_tokens)
#   Tier 1 — surgical:      one chunk per AST symbol (function/class/interface)
#   Tier 2 — component:     1 500-token sliding windows over the whole file
#   Tier 3 — architectural: 4 000-token sliding windows, one or two per file
TIER_CONFIGS: list[tuple[str, int, int]] = [
    ("tier1_surgical",       500,   50),
    ("tier2_component",     1500,  100),
    ("tier3_architectural", 4000,  200),
]

# Bidirectional tier name ↔ integer.  SQLite stores integers (1/2/3) to avoid
# repeating long strings in every row; we map back to names when materialising
# FAISS IDs from SQLite chunk rows.
TIER_NUM:  dict[str, int] = {name: idx + 1 for idx, (name, _, _) in enumerate(TIER_CONFIGS)}
TIER_NAME: dict[int, str] = {v: k for k, v in TIER_NUM.items()}


# ---------------------------------------------------------------------------
# Stable ID — single authoritative implementation
# ---------------------------------------------------------------------------

def stable_id(tier_name: str, file_path: str, scope: str) -> int:
    """
    Deterministic 60-bit FAISS vector ID.

    Formula: int(md5(f"{tier_name}::{file_path}::{scope}")[:15], 16)

    Why 15 hex chars?
      16 hex chars = 64 bits.  FAISS idx_t is int64_t (signed), so values
      >= 2^63 are negative — the IDMap either rejects them or mis-routes them.
      15 hex chars = 60 bits, comfortably below 2^63 (9.2 × 10^18).

    This formula is pinned by tests/test_stable_id.py.  A change here
    requires a planned full-index rebuild.
    """
    raw = f"{tier_name}::{file_path}::{scope}".encode()
    return int(hashlib.md5(raw).hexdigest()[:15], 16)


# ---------------------------------------------------------------------------
# FAISS dtype factories — enforce correct array types in one place
# ---------------------------------------------------------------------------

def to_faiss_ids(ids: list[int]) -> np.ndarray:
    """
    Convert a list of Python ints to a FAISS-safe int64 array.

    FAISS dtype contract: idx_t = int64_t on ALL platforms.

    Critical Windows pitfall: numpy's default integer type on Windows is int32.
    np.array([1, 2, 3]) on Windows → int32 → SWIG TypeError at runtime.
    Always use this factory rather than np.array(...) directly.
    """
    return np.array(ids, dtype=np.int64)


def to_faiss_matrix(vecs: list[np.ndarray]) -> np.ndarray:
    """
    Stack 1-D embedding vectors into a 2-D float32 C-contiguous matrix.

    FAISS dtype contract for add_with_ids / search: float32, shape (n, d),
    C-contiguous (row-major).

    np.ascontiguousarray simultaneously:
      1. Casts to float32 — guards against np.vstack silently promoting to float64.
      2. Ensures C-order  — guards against Fortran-order arrays from some
                            scipy / sklearn utilities.
    Both are silent failures if not corrected (float64 reads as half-width
    float32; column-major layout produces transposed embeddings).
    """
    return np.ascontiguousarray(np.vstack(vecs), dtype=np.float32)
