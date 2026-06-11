"""
hybrid_retriever.py — Retrieve-Traverse-Rerank pipeline for the Code Intelligence Engine.

Pipeline
--------
  1. Semantic Retrieval   — Multi-tier FAISS search (tier-1/2/3) fused via
                            Reciprocal Rank Fusion (RRF, k=60).
                            Tier-1 AST chunks carry FQNs and serve as graph
                            traversal seeds; tier-2/3 contribute component-
                            and architectural-level context to the rerank pool.
  2. Structural Expansion — Call-graph neighbours of the top-5 semantic hits via the
                            SQLite recursive CTE.  max_depth=1 gives immediate callers
                            and immediate callees, keeping latency bounded.
                            Edges are labelled `corroborated=True/False` based on
                            whether an IMPORTS edge corroborates the CALLS edge.
  3. Reranking            — jina-reranker-v2-base-code CrossEncoder scores every
                            candidate in the merged pool.  Returns the top-10.

Cross-Encoder fallback
-----------------------
Loading a reranker requires ~500 MB of model weights and CUDA/MPS alignment.  If the
model cannot be loaded (OOM, missing weights, no GPU), _load_reranker() sets
_reranker_failed=True and all subsequent calls degrade gracefully: the top-10
are chosen by RRF score instead.  The return type is identical in both paths.

Stable ID contract
-------------------
FAISS vector IDs are NOT stored anywhere; they are recomputed on demand from:
    stable_id(tier_name, file_path, scope)
See stable_id.py for the formula and its constraints.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import faiss
import numpy as np

from core import DocumentStore, MultiIndexManager, embed
from db import CodeDB
from category_tagger import classify_query
from stable_id import stable_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline constants
# ---------------------------------------------------------------------------

_SEMANTIC_K      = 50   # FAISS candidates retrieved in step 1 per tier
_EXPANSION_K     = 5    # top semantic hits whose call-graphs are expanded
_MAX_POOL_SIZE   = 35   # hard cap on candidates passed to the reranker
_RERANK_TOP_N    = 10   # results returned to the caller
_GRAPH_DEPTH     = 1    # one hop = immediate callers + immediate callees
_CATEGORY_BOOST  = 0.12 # additive score nudge for chunks whose category matches the query

_EXPANSION_BUDGET = 20  # max structural nodes added to the pool
_BEAM_WIDTH       = 5   # candidates kept per expansion step
_MIN_EDGE_SCORE   = 0.3 # prune expansion paths whose relative score falls below this

_RRF_K = 60             # RRF smoothing constant (standard value)

_TIER1_NAME = "tier1_surgical"
_TIER2_NAME = "tier2_component"
_TIER3_NAME = "tier3_architectural"

# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    faiss_id: int
    score: float       # reranker logit when available; RRF score otherwise
    file: str
    scope: str         # FQN for tier-1 AST chunks (contains "::"); positional label otherwise
    tier: str
    text: str
    source: str        # "semantic" | "structural"
    tags: list[str] = field(default_factory=list)
    corroborated: bool = True   # False when a CALLS edge lacks import-graph backing


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
        Directory that holds the ``.faiss`` files.
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
            print(c.score, c.file, c.scope, c.corroborated)
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
        self._tier2: faiss.IndexIDMap = self._index_manager.load_or_create(_TIER2_NAME)
        self._tier3: faiss.IndexIDMap = self._index_manager.load_or_create(_TIER3_NAME)
        self._doc_store = DocumentStore(db_path)
        self._db = CodeDB(db_path)
        self._reranker_model_id = reranker_model
        self._device = device

        self._reranker: Optional[object] = None
        self._reranker_failed: bool = False

        # Per-session import-corroboration cache: target_file → set of importer files
        self._import_cache: dict[str, set[str]] = {}

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
        self._import_cache.clear()
        semantic_hits = self._semantic_search(query, k=_SEMANTIC_K)
        pool = self._expand_structurally_budgeted(semantic_hits)
        return self._rerank(query, pool)

    # ------------------------------------------------------------------
    # Step 1 — Semantic retrieval (multi-tier RRF)
    # ------------------------------------------------------------------

    def _semantic_search(self, query: str, k: int) -> list[RetrievedChunk]:
        """Embed `query` and return the top-k chunks via three-tier RRF fusion.

        All three FAISS tiers are searched; their ranked lists are merged using
        Reciprocal Rank Fusion (k=60).  Tier-1 AST chunks carry FQNs and are
        preferred as graph traversal seeds; tier-2/3 chunks expand the rerank pool
        with component- and architectural-level context.
        """
        vec = embed(query)
        vec = np.array([vec], dtype=np.float32)
        faiss.normalize_L2(vec)  # in-place; aligns with IndexFlatIP (cosine similarity)

        fused: dict[int, float] = {}
        for idx in (self._tier1, self._tier2, self._tier3):
            actual_k = min(k, idx.ntotal)
            if actual_k == 0:
                continue
            _, ids = idx.search(vec, actual_k)
            for rank, fid in enumerate(ids[0]):
                if fid == -1:
                    continue
                fid = int(fid)
                fused[fid] = fused.get(fid, 0.0) + 1.0 / (_RRF_K + rank)

        if not fused:
            return []

        results: list[RetrievedChunk] = []
        for fid in sorted(fused, key=lambda x: fused[x], reverse=True)[:k]:
            meta = self._doc_store.get(fid)
            if meta is None:
                continue
            results.append(RetrievedChunk(
                faiss_id=fid,
                score=fused[fid],
                file=meta.get("file", ""),
                scope=meta.get("scope", ""),
                tier=meta.get("tier", _TIER1_NAME),
                text=meta.get("text", ""),
                source="semantic",
                tags=meta.get("tags") or [],
                corroborated=True,  # semantic hits are always considered corroborated
            ))
        return results

    # ------------------------------------------------------------------
    # Step 2 — Structural expansion
    # ------------------------------------------------------------------

    def _is_import_corroborated(self, source_file: str, target_file: str) -> bool:
        """
        Return True if `source_file` imports `target_file` (or they are the same file).

        Uses a per-session cache to avoid redundant SQLite queries when the same
        target file is a neighbour for multiple seeds.
        """
        if source_file == target_file:
            return True
        if target_file not in self._import_cache:
            self._import_cache[target_file] = set(
                self._db.get_importers_resolved(target_file)
            )
        return source_file in self._import_cache[target_file]

    def _expand_structurally_budgeted(
        self,
        semantic_hits: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Beam-search graph expansion with budget control and corroboration labels.

        Algorithm
        ---------
        1. Seed the frontier with the top-_EXPANSION_K semantic hits' FQNs,
           weighted by their RRF score.
        2. Each expansion step pops the highest-scoring frontier entry,
           queries its one-hop call-graph neighbours, and keeps only the top
           _BEAM_WIDTH by score (hop-decayed from the parent).
        3. Each structural chunk is labelled `corroborated=True/False` based
           on whether an IMPORTS edge from the calling file to the callee's file
           backs the CALLS edge.  Unverified edges still inform retrieval but
           are excluded from verdict tools (blast-radius, dead-code).
        4. Stops when the budget (_EXPANSION_BUDGET new nodes) is exhausted
           OR all frontier edges have decayed below _MIN_EDGE_SCORE.
        """
        pool: dict[int, RetrievedChunk] = {c.faiss_id: c for c in semantic_hits}

        top_score = semantic_hits[0].score if semantic_hits else 1.0
        abs_min_score = _MIN_EDGE_SCORE * max(top_score, 1e-9)

        seed_scores: dict[str, float] = {}
        for c in semantic_hits[:_EXPANSION_K]:
            if _is_fqn(c.scope):
                seed_scores[c.scope] = c.score

        explored: set[str] = set(seed_scores.keys())
        frontier: list[tuple[str, float]] = sorted(
            seed_scores.items(), key=lambda x: x[1], reverse=True
        )

        nodes_added = 0

        while frontier and nodes_added < _EXPANSION_BUDGET:
            fqn, parent_score = frontier.pop(0)
            hop_score = parent_score * 0.7  # decay per graph hop

            if hop_score < abs_min_score:
                break

            # Derive the calling file from the FQN ("file_path::symbol")
            source_file = fqn.split("::")[0] if "::" in fqn else None

            try:
                graph_nodes = self._db.get_call_graph(fqn, max_depth=1)
            except Exception as exc:
                logger.warning("get_call_graph(%s) failed: %s", fqn, exc)
                continue

            candidates: list[tuple[object, float]] = []
            for node in graph_nodes:
                if node.direction == "root" or node.fqn in explored or node.file_path is None:
                    continue
                candidates.append((node, hop_score))

            for node, score in candidates[:_BEAM_WIDTH]:
                if nodes_added >= _EXPANSION_BUDGET:
                    break
                explored.add(node.fqn)
                neighbor_id = stable_id(_TIER1_NAME, node.file_path, node.fqn)
                if neighbor_id in pool:
                    continue
                meta = self._doc_store.get(neighbor_id)
                if meta is None:
                    continue

                # Import-graph corroboration (H5)
                is_corroborated = bool(
                    source_file and self._is_import_corroborated(source_file, node.file_path)
                )

                pool[neighbor_id] = RetrievedChunk(
                    faiss_id=neighbor_id,
                    score=score,
                    file=meta.get("file", node.file_path),
                    scope=meta.get("scope", node.fqn),
                    tier=meta.get("tier", _TIER1_NAME),
                    text=meta.get("text", ""),
                    source="structural",
                    tags=meta.get("tags") or [],
                    corroborated=is_corroborated,
                )
                nodes_added += 1
                frontier.append((node.fqn, score))

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
                "Falling back to RRF score ranking.",
                type(exc).__name__,
                exc,
            )
            return None

    def _rerank(self, query: str, pool: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Score every candidate in `pool` against `query` via the cross-encoder.

        Falls back to RRF score ordering if the cross-encoder is unavailable.
        Returns at most _RERANK_TOP_N chunks, highest score first.
        """
        if not pool:
            return []

        query_categories = set(classify_query(query))

        reranker = self._load_reranker()

        if reranker is None:
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
                batch_size=min(32, len(pairs)),
            )
        except Exception as exc:
            logger.warning(
                "CrossEncoder.predict failed (%s: %s). Using RRF scores.",
                type(exc).__name__,
                exc,
            )
            pool.sort(key=lambda c: c.score, reverse=True)
            return pool[:_RERANK_TOP_N]

        # Composite scoring
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
