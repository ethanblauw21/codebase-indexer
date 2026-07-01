"""fusion.py — sparse tokenization + score-normalized convex fusion (ADR-009 §P3).

The retriever combines two signals: a **dense** score (multi-tier FAISS RRF in
production; cosine similarity in the eval harness) and a **sparse** BM25 score.
Raw RRF cannot weight the two; a score-normalized convex combination can, and
that weighting is the hook ADR-014 later learns.

This module is the single home for the pieces shared by the production retriever
(`src/hybrid_retriever.py`) and the eval harness (`tools/coir_eval.py`):
the tokenizer (so BM25 indexes both sides identically) and the normalize+combine
math (so "convex fusion" means the same thing in both).
"""
from __future__ import annotations

import re

import numpy as np

# v1 tokenizer: lowercase alphanumeric runs. Keeps identifiers whole (snake_case
# and camelCase each stay a single token), which is what lets BM25 catch the
# exact-identifier matches dense retrieval blurs. Splitting identifiers into
# subtokens (camelCase/snake_case → parts) is a deliberate future refinement.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """Tokenize text for BM25. Both corpus and query must use this same function."""
    return _TOKEN_RE.findall(text.lower())


def minmax_norm(values) -> np.ndarray:
    """Min-max normalize to [0, 1]. Returns zeros for an empty or constant input
    (a constant signal carries no ranking information, so it contributes nothing)."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    lo = float(arr.min())
    hi = float(arr.max())
    if hi <= lo:
        return np.zeros_like(arr)
    return (arr - lo) / (hi - lo)


def convex_fuse(dense_scores, sparse_scores, dense_weight: float,
                sparse_weight: float) -> np.ndarray:
    """Score-normalized convex combination of two aligned score arrays.

    Each signal is min-max normalized independently, then combined as a weighted
    sum with the weights renormalized to sum to 1 (so the output stays in [0, 1]
    and the weights express a *ratio*, not an absolute scale). The two input
    arrays must be aligned element-for-element (same candidate at each index).
    """
    d = minmax_norm(dense_scores)
    s = minmax_norm(sparse_scores)
    total = dense_weight + sparse_weight
    if total <= 0:
        return np.zeros(max(d.size, s.size))
    dw, sw = dense_weight / total, sparse_weight / total
    return dw * d + sw * s
