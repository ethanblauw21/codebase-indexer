#!/usr/bin/env python3
"""
H3 Retrieval Eval — before/after comparison for three-tier RRF fusion.

Runs a fixed query set against two modes:
  before  — tier-1 FAISS only (the old behaviour)
  after   — tier-1 + tier-2 + tier-3 fused via RRF (the H3 implementation)

Metrics reported: MRR@5 and Hit@{1,3,5} per query, plus averages.

Requires a live .code-index/ directory.  Run 'code-indexer' first.

Usage:
    python tools/eval_retrieval.py [--index-dir .code-index] [--verbose]

Exit codes:
    0 — three-tier MRR@5 >= tier-1 baseline  (PASS)
    1 — three-tier MRR@5 <  tier-1 baseline  (FAIL — surface, do not revert silently)
    2 — index not found
"""
import argparse
import os
import sys
import tomllib

# Resolve src/ relative to this file's location so the script works regardless
# of where it is invoked from.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

import numpy as np

# ---------------------------------------------------------------------------
# Query set — tuned to the indexer codebase itself.
#
# "relevant" is a set of file paths (as stored in the index, forward-slash,
# relative to the repo root).  A result counts as a hit if any returned file
# appears in the relevant set.
# ---------------------------------------------------------------------------

QUERIES = [
    {
        "id": "stable-id-formula",
        "query": "stable FAISS vector ID formula md5 hash 60-bit deterministic",
        "relevant": {"src/stable_id.py"},
    },
    {
        "id": "rrf-fusion",
        "query": "reciprocal rank fusion multi-tier semantic search candidates",
        "relevant": {"src/hybrid_retriever.py"},
    },
    {
        "id": "sqlite-docstore",
        "query": "SQLite chunk payload document store in-memory cache",
        "relevant": {"src/core.py", "src/db.py"},
    },
    {
        "id": "reload-guard",
        "query": "watchdog reload concurrency guard index generation atomic swap",
        "relevant": {"src/MCPServer.py"},
    },
    {
        "id": "import-corroboration",
        "query": "import graph edge corroboration blast radius verdict unverified",
        "relevant": {"src/hybrid_retriever.py"},
    },
    {
        "id": "ast-chunking",
        "query": "tree-sitter AST chunking symbol scope extraction",
        "relevant": {"src/ast_chunker.py"},
    },
    {
        "id": "embedding-budget",
        "query": "embedding model sequence length token budget truncation OOM",
        "relevant": {"src/core.py"},
    },
    {
        "id": "incremental-diff",
        "query": "incremental indexing file change detection hash diff modified deleted",
        "relevant": {"src/incremental_indexer.py"},
    },
    {
        "id": "import-resolver",
        "query": "tsconfig path aliases barrel files module boundary resolution",
        "relevant": {"src/import_resolver.py"},
    },
    {
        "id": "risk-rules",
        "query": "risk rules YAML pattern severity analyze violations",
        "relevant": {"src/MCPServer.py"},
    },
]

TOP_K  = 5
RRF_K  = 60


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def _load_index(path):
    import faiss
    if os.path.exists(path):
        return faiss.read_index(path)
    return None


def _faiss_hits(index, vec: np.ndarray, k: int) -> list[tuple[float, int]]:
    if index is None:
        return []
    D, I = index.search(vec, k)
    return [(float(d), int(i)) for d, i in zip(D[0], I[0]) if i != -1]


# ---------------------------------------------------------------------------
# Search modes
# ---------------------------------------------------------------------------

def search_tier1_only(vec, t1, doc_store, k=TOP_K) -> list[str]:
    """Baseline: search only tier-1."""
    files: list[str] = []
    for _score, fid in _faiss_hits(t1, vec, k):
        doc = doc_store.get(fid)
        if doc and doc["file"] not in files:
            files.append(doc["file"])
        if len(files) >= k:
            break
    return files


def search_three_tier_rrf(vec, t1, t2, t3, doc_store, k=TOP_K) -> list[str]:
    """After: tier-1 + tier-2 + tier-3 fused via RRF."""
    accum: dict[str, float] = {}
    for index in (t1, t2, t3):
        if index is None:
            continue
        for rank, (_score, fid) in enumerate(_faiss_hits(index, vec, k * 4)):
            doc = doc_store.get(fid)
            if doc:
                key = doc["file"]
                accum[key] = accum.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(accum, key=lambda f: -accum[f])
    return ranked[:k]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def mrr_at_k(results: list[str], relevant: set[str], k: int = TOP_K) -> float:
    for i, r in enumerate(results[:k]):
        if r in relevant:
            return 1.0 / (i + 1)
    return 0.0


def hit_at_k(results: list[str], relevant: set[str], k: int) -> int:
    return int(any(r in relevant for r in results[:k]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(index_dir: str, verbose: bool) -> None:
    import faiss
    from core import DocumentStore

    t1_path = os.path.join(index_dir, "tier1_surgical.faiss")
    if not os.path.exists(t1_path):
        print(f"ERROR: index not found at {t1_path}")
        print("Run 'code-indexer' first to build the index.")
        sys.exit(2)

    t1 = faiss.read_index(t1_path)
    t2 = _load_index(os.path.join(index_dir, "tier2_component.faiss"))
    t3 = _load_index(os.path.join(index_dir, "tier3_architectural.faiss"))
    db_path = os.path.join(index_dir, "graph.db")
    doc_store = DocumentStore(db_path)

    # Read model ID from indexer.toml if present; fall back to hardcoded default.
    model_id = "jinaai/jina-embeddings-v2-base-code"
    toml_candidate = os.path.join(os.path.dirname(__file__), "..", "indexer.toml")
    if os.path.exists(toml_candidate):
        with open(toml_candidate, "rb") as fh:
            cfg = tomllib.load(fh)
        model_id = cfg.get("embeddings", {}).get("model_id", model_id)

    from sentence_transformers import SentenceTransformer
    print(f"Loading embedding model: {model_id}", flush=True)
    model = SentenceTransformer(model_id, trust_remote_code=True)
    model.max_seq_length = 512

    rows = []
    for q in QUERIES:
        vec = model.encode([q["query"]], normalize_embeddings=True).astype(np.float32)
        relevant = q["relevant"]

        before = search_tier1_only(vec, t1, doc_store)
        after  = search_three_tier_rrf(vec, t1, t2, t3, doc_store)

        row = {
            "id":         q["id"],
            "b_mrr":      mrr_at_k(before, relevant),
            "a_mrr":      mrr_at_k(after,  relevant),
            "b_hit1":     hit_at_k(before, relevant, 1),
            "a_hit1":     hit_at_k(after,  relevant, 1),
            "b_hit3":     hit_at_k(before, relevant, 3),
            "a_hit3":     hit_at_k(after,  relevant, 3),
            "b_hit5":     hit_at_k(before, relevant, 5),
            "a_hit5":     hit_at_k(after,  relevant, 5),
        }
        rows.append(row)

        if verbose:
            print(f"\n[{q['id']}]")
            print(f"  query    : {q['query']}")
            print(f"  relevant : {relevant}")
            print(f"  before   : {before}")
            print(f"  after    : {after}")
            print(f"  MRR@5  {row['b_mrr']:.3f} → {row['a_mrr']:.3f}", end="")
            delta = row["a_mrr"] - row["b_mrr"]
            print(f"  ({'↑' if delta > 0 else '↓' if delta < 0 else '='})")

    n = len(rows)
    numeric_keys = [k for k in rows[0] if k != "id"]
    avg = {k: sum(r[k] for r in rows) / n for k in numeric_keys}

    # Print table
    W = 24
    sep = "-" * 74
    print(f"\n{'=' * 74}")
    print(f"  H3 Retrieval Eval — tier-1 baseline vs. three-tier RRF (k={RRF_K})")
    print(f"{'=' * 74}")
    print(f"  {'Query':<{W}}  {'MRR@5':>9}  {'→':>2}  {'MRR@5':>9}  {'H@1':>4} {'H@3':>4} {'H@5':>4}")
    print(f"  {'':>{W}}  {'before':>9}  {'':>2}  {'after':>9}  {'B A':>4} {'B A':>4} {'B A':>4}")
    print(f"  {sep}")
    for r in rows:
        delta = r["a_mrr"] - r["b_mrr"]
        arrow = "↑" if delta > 0.001 else ("↓" if delta < -0.001 else "=")
        h1 = f"{'✓' if r['b_hit1'] else '✗'} {'✓' if r['a_hit1'] else '✗'}"
        h3 = f"{'✓' if r['b_hit3'] else '✗'} {'✓' if r['a_hit3'] else '✗'}"
        h5 = f"{'✓' if r['b_hit5'] else '✗'} {'✓' if r['a_hit5'] else '✗'}"
        print(
            f"  {r['id']:<{W}}  {r['b_mrr']:>9.3f}  {arrow:>2}  {r['a_mrr']:>9.3f}"
            f"  {h1:>4} {h3:>4} {h5:>4}"
        )
    print(f"  {sep}")
    d_avg = avg["a_mrr"] - avg["b_mrr"]
    arrow = "↑" if d_avg > 0.001 else ("↓" if d_avg < -0.001 else "=")
    h1 = f"{avg['b_hit1']:>3.0%} {avg['a_hit1']:>3.0%}"
    h3 = f"{avg['b_hit3']:>3.0%} {avg['a_hit3']:>3.0%}"
    h5 = f"{avg['b_hit5']:>3.0%} {avg['a_hit5']:>3.0%}"
    print(
        f"  {'AVERAGE':<{W}}  {avg['b_mrr']:>9.3f}  {arrow:>2}  {avg['a_mrr']:>9.3f}"
        f"  {h1} {h3} {h5}"
    )
    print(f"{'=' * 74}")

    verdict = "PASS" if avg["a_mrr"] >= avg["b_mrr"] else "FAIL"
    print(f"\n  Verdict: {verdict}")
    print(f"  Three-tier MRR@5 {avg['a_mrr']:.3f}  vs  tier-1 baseline {avg['b_mrr']:.3f}")
    if verdict == "FAIL":
        print()
        print("  WARNING: three-tier RRF degraded retrieval quality.")
        print("  Per ADR-002 H3: surface this result and evaluate on its own merits.")
        print("  Do NOT revert the implementation silently.")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-dir", default=".code-index", metavar="DIR",
                    help="Path to FAISS index directory (default: .code-index)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print per-query result lists")
    args = ap.parse_args()
    run(args.index_dir, args.verbose)


if __name__ == "__main__":
    main()
