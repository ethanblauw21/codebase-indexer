#!/usr/bin/env python3
"""ADR-019 — graph-only fixture scout.

A fixture only measures the graph Traverse step's value when the query is
semantically DISTANT from the gold, yet the gold is structurally reachable (a
caller/callee) from a symbol the query DOES retrieve. Hand-finding such pairs is
slow; this scouts them.

For a prepared+resolved index it lists **resolved intra-repo call pairs
(caller → callee) ranked by ASCENDING embedding similarity** — the most
semantically-dissimilar structurally-connected pairs first. Those are the natural
graph-only candidates: a query that matches one endpoint will not semantically
retrieve the other, so only the graph edge connects them. The author then writes a
query matching one endpoint with the other as gold.

Similarity uses the SAME embedder the retriever uses (core.embed_batch over each
symbol's tier-1 chunk text), so "distant" here means distant to the actual pipeline.

Usage:
    python tools/graph_only_scout.py --repo click --top 25
    python tools/graph_only_scout.py --repo spdlog --max-sim 0.5 --top 40
"""
import argparse
import os
import sys

import numpy as np

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_INDEX = os.path.join(_ROOT, "benchmarks", "real_repo", "index")

sys.path.insert(0, os.path.join(_ROOT, "src"))
from core import embed_batch  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _short(fqn):
    return fqn.split("::")[-1]


def scout(repo, top, max_sim, min_chars):
    import sqlite3

    db_path = os.path.join(_INDEX, repo, "graph.db")
    if not os.path.exists(db_path):
        raise SystemExit(f"{repo} not prepared: {db_path}")
    conn = sqlite3.connect(db_path)

    # tier-1 chunk text per symbol fqn (scope == fqn for AST chunks; tier 1 = surgical)
    text_by_fqn = {}
    for scope, text in conn.execute("SELECT scope, text FROM chunks WHERE tier = 1"):
        if "::" in scope and len(text) >= min_chars:
            text_by_fqn.setdefault(scope, text)

    # resolved intra-repo call pairs
    pairs = []
    for src, tgt in conn.execute(
        "SELECT source_fqn, resolved_target FROM edges "
        "WHERE kind = 'CALLS' AND resolved_target IS NOT NULL"
    ):
        if src in text_by_fqn and tgt in text_by_fqn and src != tgt:
            pairs.append((src, tgt))
    conn.close()

    if not pairs:
        print(f"{repo}: no resolved call pairs with tier-1 text — nothing to scout")
        return

    # unique endpoints → one embedding each (same embedder as the retriever)
    fqns = sorted({f for p in pairs for f in p})
    vecs = embed_batch([text_by_fqn[f] for f in fqns])
    vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    idx = {f: i for i, f in enumerate(fqns)}

    scored = []
    seen = set()
    for src, tgt in pairs:
        key = tuple(sorted((src, tgt)))
        if key in seen:
            continue
        seen.add(key)
        sim = float(vecs[idx[src]] @ vecs[idx[tgt]])
        if sim <= max_sim:
            scored.append((sim, src, tgt))
    scored.sort(key=lambda x: x[0])

    print(f"\n{repo}: {len(scored)} distant call pairs (sim ≤ {max_sim}); showing top {top}")
    print(f"{'sim':>6}  caller → callee")
    print("-" * 72)
    for sim, src, tgt in scored[:top]:
        print(f"{sim:6.3f}  {_short(src)}  →  {_short(tgt)}")
        print(f"         {src}")
        print(f"         {tgt}")


def main():
    ap = argparse.ArgumentParser(description="Scout graph-only fixture candidates (ADR-019).")
    ap.add_argument("--repo", required=True, help="prepared repo name")
    ap.add_argument("--top", type=int, default=25, help="how many pairs to show")
    ap.add_argument("--max-sim", type=float, default=0.6,
                    help="only pairs with cosine similarity ≤ this (lower = more distant)")
    ap.add_argument("--min-chars", type=int, default=80,
                    help="skip symbols whose chunk text is shorter than this (trivial stubs)")
    args = ap.parse_args()
    scout(args.repo, args.top, args.max_sim, args.min_chars)


if __name__ == "__main__":
    main()
