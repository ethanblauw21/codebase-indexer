"""
iterative_retriever.py — Multi-round retrieval with confidence accumulation.

Wraps HybridRetriever to perform up to N rounds of retrieval, accumulating
evidence across rounds and stopping early when the score plateau indicates
diminishing returns.

Each round:
  1. Enriches the query with top-3 FQNs from prior evidence (context injection)
  2. Excludes already-explored FQNs so each round finds new code
  3. Accumulates the new chunks into the evidence pool
  4. Checks for a score plateau (confidence stopping condition)

After all rounds, the full accumulated pool is deduplicated and sorted by score.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hybrid_retriever import HybridRetriever, RetrievedChunk
from db import CodeDB


@dataclass
class RetrievalSession:
    """State accumulated across iteration rounds."""
    query: str
    explored_fqns: set[str] = field(default_factory=set)
    evidence_pool: list[RetrievedChunk] = field(default_factory=list)
    iteration: int = 0
    confidence: float = 0.0   # 0.0–1.0; ratio of 5th-best to top score


class IterativeRetriever:
    """
    Multi-round retrieval with explored-node memory and confidence-based stopping.

    Parameters
    ----------
    base_retriever : HybridRetriever
        The underlying Retrieve-Traverse-Rerank pipeline.
    db : CodeDB
        SQLite connection (currently unused but reserved for future graph queries).

    Usage
    -----
    retriever = IterativeRetriever(base_retriever, db)
    chunks, session = retriever.retrieve("smart-pick deduplication")
    print(f"Iterations: {session.iteration}, Confidence: {session.confidence:.2f}")
    """

    def __init__(self, base_retriever: HybridRetriever, db: CodeDB) -> None:
        self._retriever = base_retriever
        self._db = db

    def retrieve(
        self,
        query: str,
        max_iterations: int = 3,
        confidence_threshold: float = 0.85,
        top_n: int = 15,
    ) -> tuple[list[RetrievedChunk], RetrievalSession]:
        """
        Run up to `max_iterations` rounds of retrieval, accumulating evidence.

        Stopping conditions (whichever comes first):
          1. `max_iterations` rounds completed.
          2. `confidence` ≥ `confidence_threshold` (score plateau detected).
          3. No new chunks found in a round (exhausted relevant graph).

        Returns
        -------
        chunks : list[RetrievedChunk]
            Deduplicated pool sorted by score, capped at `top_n`.
        session : RetrievalSession
            Accumulated metadata for the report header.
        """
        session = RetrievalSession(query=query)

        for i in range(max_iterations):
            session.iteration = i + 1
            enriched_query = self._build_query(query, session)

            new_chunks = self._retriever.retrieve(enriched_query)
            new_chunks = [
                c for c in new_chunks
                if c.scope not in session.explored_fqns
            ]

            if not new_chunks:
                break

            session.explored_fqns.update(c.scope for c in new_chunks)
            session.evidence_pool.extend(new_chunks)

            # Confidence = ratio of 5th-best score to top score (plateau detection).
            # A high ratio means the top results cluster tightly — retrieval has
            # likely converged and further rounds will add diminishing evidence.
            sorted_pool = sorted(
                session.evidence_pool, key=lambda c: c.score, reverse=True
            )
            if len(sorted_pool) >= 5:
                top_score = sorted_pool[0].score
                fifth_score = sorted_pool[4].score
                session.confidence = (
                    fifth_score / top_score if top_score > 1e-9 else 0.0
                )

            if session.confidence >= confidence_threshold:
                break

        # Final deduplicated, sorted pool
        seen: set[int] = set()
        deduped: list[RetrievedChunk] = []
        for c in sorted(session.evidence_pool, key=lambda c: c.score, reverse=True):
            if c.faiss_id not in seen:
                seen.add(c.faiss_id)
                deduped.append(c)

        return deduped[:top_n], session

    def _build_query(self, original: str, session: RetrievalSession) -> str:
        """
        Enrich the original query with top-3 FQNs from prior evidence rounds.
        The injected context steers the embedding toward the neighbourhood of
        already-discovered evidence, encouraging the retriever to find adjacent
        code rather than repeating the same hits.
        """
        if not session.evidence_pool:
            return original

        top_fqns = [
            c.scope
            for c in sorted(
                session.evidence_pool, key=lambda c: c.score, reverse=True
            )[:3]
            if "::" in c.scope
        ]

        if top_fqns:
            context = ", ".join(top_fqns)
            return f"{original} [context: {context}]"
        return original
