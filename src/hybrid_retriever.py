"""
hybrid_retriever.py — Retrieve-Traverse-Rerank pipeline for the Code Intelligence Engine.

Pipeline
--------
  1. Semantic Retrieval   — FAISS tier-1 (surgical) index, top-50 by cosine similarity.
                            Tier-1 is chosen here because its chunks are FQN-aligned
                            (one chunk per AST symbol), which makes them ideal seeds for
                            the structural traversal step that follows.
  2. Structural Expansion — Call-graph neighbours of the top-5 semantic hits via the
                            SQLite recursive CTE.  max_depth=1 gives immediate callers
                            and immediate callees, keeping latency bounded.
  3. Reranking            — jina-reranker-v2-base-code CrossEncoder scores every
                            candidate in the merged pool.  Returns the top-10.

Cross-Encoder fallback
-----------------------
Loading a reranker requires ~500 MB of model weights and CUDA/MPS alignment.  If the
model cannot be loaded (OOM, missing weights, no GPU), _load_reranker() sets
_reranker_failed=True and all subsequent calls degrade gracefully: the top-10
are chosen by FAISS cosine-similarity score instead.  The return type is identical
in both paths so callers do not need special-casing.

Stable ID contract
-------------------
FAISS vector IDs are NOT stored anywhere; they are recomputed on demand from:
    int(md5(f"{tier_name}::{file_path}::{scope}")[:15], 16)
This formula is shared with indexer.py and incremental_indexer.py.  Changing it
requires a full index rebuild.  15 hex chars = 60 bits, safely below the signed
int64 ceiling (63 bits).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

import faiss
import numpy as np

from core import DocumentStore, MultiIndexManager, embed
from db import CodeDB
from category_tagger import classify_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline constants
# ---------------------------------------------------------------------------

_SEMANTIC_K      = 50   # FAISS candidates retrieved in step 1
_EXPANSION_K     = 5    # top semantic hits whose call-graphs are expanded
_MAX_POOL_SIZE   = 35   # hard cap on candidates passed to the reranker
_RERANK_TOP_N    = 10   # results returned to the caller
_GRAPH_DEPTH     = 1    # one hop = immediate callers + immediate callees
_CATEGORY_BOOST  = 0.12 # additive score nudge for chunks whose category matches the query

# Beam-search graph expansion (Section 4)
_EXPANSION_BUDGET = 20  # max structural nodes added to the pool
_BEAM_WIDTH       = 5   # candidates kept per expansion step
_MIN_EDGE_SCORE   = 0.3 # prune expansion paths whose relative score falls below this

_TIER1_NAME = "tier1_surgical"

# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    faiss_id: int
    score: float     # reranker logit when available; FAISS cosine score otherwise
    file: str
    scope: str       # FQN for tier-1 AST chunks (contains "::"); positional label otherwise
    tier: str
    text: str
    source: str      # "semantic" | "structural"
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stable_id(tier_name: str, file_path: str, scope: str) -> int:
    """Reproduce the FAISS vector ID from its three logical components.

    Must stay bit-for-bit identical to the formula in indexer.py and
    incremental_indexer.py.  15 hex chars = 60 bits < signed int64 max.
    """
    raw = f"{tier_name}::{file_path}::{scope}".encode()
    return int(hashlib.md5(raw).hexdigest()[:15], 16)


def _is_fqn(scope: str) -> bool:
    """True when a scope string looks like a Fully Qualified Name ("file::symbol")."""
    return "::" in scope


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Retrieve-Traverse-Rerank over the FAISS + SQLite code index.

    Parameters
    ----------
    index_dir :
        Directory that holds the ``.faiss`` files and ``doc_store.json``.
        Default: ``".code-index"``.
    db_path :
        SQLite database path.
        Default: ``".code-index/graph.db"``.
    reranker_model :
        HuggingFace model ID for the sentence-transformers CrossEncoder.
        Default: ``"jinaai/jina-reranker-v2-base-code"``.
    device :
        Torch device string passed to CrossEncoder: ``"cpu"``, ``"cuda"``, ``"mps"``.
        Default: ``"cpu"``.

    Usage
    -----
    with HybridRetriever() as r:
        chunks = r.retrieve("how does smart-pick deduplicate container slots?")
        for c in chunks:
            print(c.score, c.file, c.scope)
    """

    def __init__(
        self,
        index_dir: str = ".code-index",
        db_path: str = ".code-index/graph.db",
        reranker_model: str = "jinaai/jina-reranker-v2-base-code",
        device: str = "cpu",
    ) -> None:
        self._index_manager = MultiIndexManager(base_dir=index_dir)
        self._tier1: faiss.IndexIDMap = self._index_manager.load_or_create(_TIER1_NAME)
        self._doc_store = DocumentStore(db_path=f"{index_dir}/doc_store.json")
        self._db = CodeDB(db_path)
        self._reranker_model_id = reranker_model
        self._device = device

        # Lazy — loaded on first retrieve() call so __init__ never blocks
        self._reranker: Optional[object] = None
        self._reranker_failed: bool = False

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "HybridRetriever":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        """Run the full Retrieve-Traverse-Rerank pipeline.

        Returns up to ``_RERANK_TOP_N`` (10) chunks ordered by relevance,
        highest score first.
        """
        semantic_hits = self._semantic_search(query, k=_SEMANTIC_K)
        pool = self._expand_structurally_budgeted(semantic_hits)
        return self._rerank(query, pool)

    # ------------------------------------------------------------------
    # Step 1 — Semantic retrieval
    # ------------------------------------------------------------------

    def _semantic_search(self, query: str, k: int) -> list[RetrievedChunk]:
        """Embed `query` and return the top-k chunks from the tier-1 FAISS index."""
        vec = embed(query)
        vec = np.array([vec], dtype=np.float32)
        faiss.normalize_L2(vec)  # in-place; aligns with IndexFlatIP (cosine similarity)

        actual_k = min(k, self._tier1.ntotal)
        if actual_k == 0:
            return []

        scores, ids = self._tier1.search(vec, actual_k)

        results: list[RetrievedChunk] = []
        for score, fid in zip(scores[0], ids[0]):
            if fid == -1:
                # FAISS pads with -1 when the index has fewer than k vectors
                continue
            meta = self._doc_store.get(int(fid))
            if meta is None:
                continue
            results.append(RetrievedChunk(
                faiss_id=int(fid),
                score=float(score),
                file=meta.get("file", ""),
                scope=meta.get("scope", ""),
                tier=meta.get("tier", _TIER1_NAME),
                text=meta.get("text", ""),
                source="semantic",
                tags=meta.get("tags") or [],
            ))
        return results

    # ------------------------------------------------------------------
    # Step 2 — Structural expansion
    # ------------------------------------------------------------------

    def _expand_structurally(
        self,
        semantic_hits: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Walk one hop of the call graph for each of the top-_EXPANSION_K hits.

        Only tier-1 AST chunks carry FQNs ("::") in their scope field.  Token-window
        chunks (tier-2 / tier-3) are kept in the pool as semantic candidates but are
        not used as graph traversal seeds.

        Structural neighbours are looked up in DocumentStore by their recomputed
        stable FAISS ID.  Any neighbour whose file was never indexed (e.g. a call
        into node_modules) will be absent from DocumentStore and is silently skipped.

        Returns the merged, deduplicated pool capped at _MAX_POOL_SIZE.
        """
        # Keyed by faiss_id to enable O(1) deduplication
        pool: dict[int, RetrievedChunk] = {c.faiss_id: c for c in semantic_hits}

        expansion_seeds = [
            c for c in semantic_hits[:_EXPANSION_K] if _is_fqn(c.scope)
        ]

        for seed in expansion_seeds:
            try:
                graph_nodes = self._db.get_call_graph(seed.scope, max_depth=_GRAPH_DEPTH)
            except Exception as exc:
                logger.warning("get_call_graph(%s) failed: %s", seed.scope, exc)
                continue

            for node in graph_nodes:
                if node.direction == "root" or node.file_path is None:
                    continue

                neighbor_id = _stable_id(_TIER1_NAME, node.file_path, node.fqn)

                if neighbor_id in pool:
                    continue

                meta = self._doc_store.get(neighbor_id)
                if meta is None:
                    # Neighbour's file was never embedded (un-indexed dependency) — skip
                    continue

                pool[neighbor_id] = RetrievedChunk(
                    faiss_id=neighbor_id,
                    score=0.0,   # no FAISS score; the reranker assigns the final rank
                    file=meta.get("file", node.file_path),
                    scope=meta.get("scope", node.fqn),
                    tier=meta.get("tier", _TIER1_NAME),
                    text=meta.get("text", ""),
                    source="structural",
                    tags=meta.get("tags") or [],
                )

                if len(pool) >= _MAX_POOL_SIZE:
                    break

            if len(pool) >= _MAX_POOL_SIZE:
                break

        return list(pool.values())[:_MAX_POOL_SIZE]

    def _expand_structurally_budgeted(
        self,
        semantic_hits: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Beam-search graph expansion with budget control (Section 4).

        Replaces the flat max_depth=1 expansion to avoid graph explosion
        while preserving high recall for architecturally proximate code.

        Algorithm
        ---------
        1. Seed the frontier with the top-_EXPANSION_K semantic hits' FQNs,
           weighted by their FAISS cosine score.
        2. Each expansion step pops the highest-scoring frontier entry,
           queries its one-hop call-graph neighbours, and keeps only the top
           _BEAM_WIDTH by score (hop-decayed from the parent).
        3. Stops when the budget (_EXPANSION_BUDGET new nodes) is exhausted
           OR all frontier edges have decayed below _MIN_EDGE_SCORE relative
           to the top semantic score (confidence-based stopping).
        """
        pool: dict[int, RetrievedChunk] = {c.faiss_id: c for c in semantic_hits}

        top_score = semantic_hits[0].score if semantic_hits else 1.0
        abs_min_score = _MIN_EDGE_SCORE * max(top_score, 1e-9)

        seed_scores: dict[str, float] = {}
        for c in semantic_hits[:_EXPANSION_K]:
            if _is_fqn(c.scope):
                seed_scores[c.scope] = c.score

        explored: set[str] = set(seed_scores.keys())
        # frontier: list of (fqn, parent_score), highest first
        frontier: list[tuple[str, float]] = sorted(
            seed_scores.items(), key=lambda x: x[1], reverse=True
        )

        nodes_added = 0

        while frontier and nodes_added < _EXPANSION_BUDGET:
            fqn, parent_score = frontier.pop(0)
            hop_score = parent_score * 0.7  # decay per graph hop

            if hop_score < abs_min_score:
                break  # confidence-based stopping

            try:
                graph_nodes = self._db.get_call_graph(fqn, max_depth=1)
            except Exception as exc:
                logger.warning("get_call_graph(%s) failed: %s", fqn, exc)
                continue

            # Collect unseen neighbours and score them by hop_score
            candidates: list[tuple[object, float]] = []
            for node in graph_nodes:
                if node.direction == "root" or node.fqn in explored or node.file_path is None:
                    continue
                candidates.append((node, hop_score))

            # Beam: keep only the top _BEAM_WIDTH per expansion step
            for node, score in candidates[:_BEAM_WIDTH]:
                if nodes_added >= _EXPANSION_BUDGET:
                    break
                explored.add(node.fqn)
                neighbor_id = _stable_id(_TIER1_NAME, node.file_path, node.fqn)
                if neighbor_id in pool:
                    continue
                meta = self._doc_store.get(neighbor_id)
                if meta is None:
                    continue
                pool[neighbor_id] = RetrievedChunk(
                    faiss_id=neighbor_id,
                    score=score,
                    file=meta.get("file", node.file_path),
                    scope=meta.get("scope", node.fqn),
                    tier=meta.get("tier", _TIER1_NAME),
                    text=meta.get("text", ""),
                    source="structural",
                    tags=meta.get("tags") or [],
                )
                nodes_added += 1
                frontier.append((node.fqn, score))

            # Re-sort frontier so highest-scoring entry is always at index 0
            frontier.sort(key=lambda x: x[1], reverse=True)

        return list(pool.values())[:_MAX_POOL_SIZE]

    # ------------------------------------------------------------------
    # Step 3 — Reranking
    # ------------------------------------------------------------------

    def _load_reranker(self) -> Optional[object]:
        """Lazy-load the CrossEncoder.

        Sets ``_reranker_failed=True`` on any error so subsequent calls return
        immediately without re-attempting the expensive import.
        """
        if self._reranker is not None:
            return self._reranker
        if self._reranker_failed:
            return None
        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import]

            self._reranker = CrossEncoder(
                self._reranker_model_id,
                device=self._device,
                trust_remote_code=True,
            )
            logger.info(
                "Loaded cross-encoder %s on device=%s",
                self._reranker_model_id,
                self._device,
            )
            return self._reranker
        except Exception as exc:
            self._reranker_failed = True
            logger.warning(
                "CrossEncoder load failed (%s: %s). "
                "Falling back to FAISS cosine-score ranking.",
                type(exc).__name__,
                exc,
            )
            return None

    def _rerank(self, query: str, pool: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Score every candidate in `pool` against `query` via the cross-encoder.

        Falls back to FAISS score ordering if the cross-encoder is unavailable.
        Returns at most _RERANK_TOP_N chunks, highest score first.
        """
        if not pool:
            return []

        query_categories = set(classify_query(query))

        reranker = self._load_reranker()

        if reranker is None:
            # Graceful degradation: structural hits (score=0.0) rank below semantic ones
            if query_categories:
                for chunk in pool:
                    if query_categories & set(chunk.tags):
                        chunk.score += _CATEGORY_BOOST
            pool.sort(key=lambda c: c.score, reverse=True)
            return pool[:_RERANK_TOP_N]

        pairs = [(query, c.text) for c in pool]

        try:
            raw_scores: np.ndarray = reranker.predict(
                pairs,
                convert_to_numpy=True,
                # batch_size cap prevents OOM on large pools
                batch_size=min(32, len(pairs)),
            )
        except Exception as exc:
            logger.warning(
                "CrossEncoder.predict failed (%s: %s). Using FAISS scores.",
                type(exc).__name__,
                exc,
            )
            pool.sort(key=lambda c: c.score, reverse=True)
            return pool[:_RERANK_TOP_N]

        # ── Composite scoring (Section 5) ──────────────────────────────────
        # Signal 1: reference density — how many pool chunks mention this scope's
        # bare name? High density = architecturally important symbol.
        name_mentions: dict[str, int] = {}
        for chunk in pool:
            short_name = (
                chunk.scope.split("::")[-1].split(".")[-1]
                if "::" in chunk.scope else chunk.scope
            )
            if not short_name:
                continue
            for other in pool:
                if short_name in other.text:
                    name_mentions[chunk.scope] = name_mentions.get(chunk.scope, 0) + 1

        # Signal 2: import locality — files already in the top-5 semantic hits
        top5_files = {
            c.file
            for c in sorted(pool, key=lambda x: x.score, reverse=True)[:5]
            if c.source == "semantic"
        }

        for chunk, rs in zip(pool, raw_scores):
            ce_score = float(rs)
            density_bonus   = min(0.2, name_mentions.get(chunk.scope, 0) * 0.05)
            locality_bonus  = 0.08 if chunk.file in top5_files else 0.0
            category_bonus  = _CATEGORY_BOOST if (query_categories & set(chunk.tags)) else 0.0
            chunk.score     = ce_score + density_bonus + locality_bonus + category_bonus

        pool.sort(key=lambda c: c.score, reverse=True)
        return pool[:_RERANK_TOP_N]
