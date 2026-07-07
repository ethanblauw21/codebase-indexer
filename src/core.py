import os
import sqlite3
import faiss
import numpy as np
faiss.omp_set_num_threads(1)
# Silence ML backend noise
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "true"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import warnings
warnings.filterwarnings("ignore")

from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer

from config import load_indexer_config

# ---------------------------------------------------------------------------
# Embedder configuration (ADR-009 §P1) — config-driven via [embeddings].
#
# Read once and cached. Defaults preserve the historical jina-v2 stack exactly
# (model id, 512 max length, 768 dims), so an unconfigured repo behaves as before.
# Swapping the embedder is a model_id + dimension change in indexer.toml followed
# by a ONE-TIME reindex (vector dimensionality changes → FAISS rebuild; stable_ids
# are unchanged so it is recompute-vectors-only). MultiIndexManager guards against
# loading an index whose dimension no longer matches the configured embedder.
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_ID = "jinaai/jina-embeddings-v2-base-code"
_DEFAULT_MAX_SEQ_LENGTH = 512
_DEFAULT_DIMENSION = 768

_emb_cfg_cache = None


def _emb_cfg() -> dict:
    global _emb_cfg_cache
    if _emb_cfg_cache is None:
        _emb_cfg_cache = load_indexer_config().get("embeddings", {})
    return _emb_cfg_cache


def embed_model_id() -> str:
    return _emb_cfg().get("model_id", _DEFAULT_MODEL_ID)


def embed_max_seq_length() -> int:
    return int(_emb_cfg().get("max_seq_length", _DEFAULT_MAX_SEQ_LENGTH))


def embed_dimension() -> int:
    """Configured embedding dimension; must match the model and the FAISS indexes."""
    return int(_emb_cfg().get("dimension", _DEFAULT_DIMENSION))


# Lazy singleton — loaded on first call to embed() / embed_batch().
# Deferring the load prevents the Jina model from being loaded inside
# ProcessPoolExecutor worker processes (which import this module via the
# spawn import chain on Windows) and keeps import-time side effects minimal.
_embed_model = None

def _get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        model_id = embed_model_id()
        print(f"[core] Loading embedding model: {model_id} ...", flush=True)
        _embed_model = SentenceTransformer(model_id, trust_remote_code=True)
        # Cap sequence length to prevent native OOM on tier3 architectural chunks
        # (~4000 tokens). Self-attention memory scales as O(L²) — 4000-token inputs
        # require ~9 GB of intermediate tensors, killing the process with an
        # uncatchable Windows SEH exception. 512 tokens covers most semantic content
        # and keeps peak attention memory well under 1 GB even at batch_size=32.
        _embed_model.max_seq_length = embed_max_seq_length()
        print("[core] Embedding model ready.", flush=True)
    return _embed_model

class TokenizerManager:
    def __init__(self):
        self._tokenizer = None
        self.SAFE_LIMIT = 8192

    def _get(self):
        if self._tokenizer is None:
            self._tokenizer = AutoTokenizer.from_pretrained(
                embed_model_id(),
                trust_remote_code=True,
                model_max_length=100000,
            )
        return self._tokenizer

    @property
    def tokenizer(self):
        return self._get()

    def count_tokens(self, text: str) -> int:
        return len(self._get().encode(text, add_special_tokens=False))

    def decode_tokens(self, tokens: list) -> str:
        return self._get().decode(tokens)

# Export the singleton under a generic name so ast_chunker doesn't break
jina_tokenizer = TokenizerManager()

def embed_batch(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """
    Generates code-native embeddings in optimized batches.
    Provides a 5x-15x speedup during indexing by saturating the GPU/CPU matrix.
    """
    if not texts:
        return np.zeros((0, embed_dimension()), dtype=np.float32)
    print(f"[core] embed_batch: {len(texts)} texts...", flush=True)
    vectors = _get_embed_model().encode(texts, convert_to_numpy=True, batch_size=batch_size)
    print("[core] embed_batch done.", flush=True)
    return np.ascontiguousarray(vectors, dtype=np.float32)

def embed(text):
    """Generates a query embedding for the configured code embedder.

    This is the QUERY path (hybrid_retriever, MCPServer). Some embedders — e.g.
    bge-code-v1 (ADR-009 §P1) — require a task instruction on the query side only;
    documents get no prefix, so embed_batch() (the indexing path) is untouched. When
    [embeddings].query_instruct is empty (the jina default) no wrapping is applied.
    """
    if not text or not text.strip():
        return np.zeros(embed_dimension(), dtype="float32")

    instruct = _emb_cfg().get("query_instruct", "")
    if instruct:
        text = f"<instruct>{instruct}\n<query>{text}"
    vector = _get_embed_model().encode(text, convert_to_numpy=True)
    return np.array(vector, dtype="float32")

class MultiIndexManager:
    def __init__(self, base_dir=".code-index"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self.indexes = {}

    def load_or_create(self, tier_name: str, dimension=None):
        if dimension is None:
            dimension = embed_dimension()
        index_path = os.path.join(self.base_dir, f"{tier_name}.faiss")
        if os.path.exists(index_path):
            index = faiss.read_index(index_path)
            # Reindex guard (ADR-009 §P1): an existing index built with a different
            # embedder has the wrong vector dimension. Querying/adding against it would
            # raise deep in FAISS or silently corrupt results, so fail loud and early.
            if index.d != dimension:
                raise ValueError(
                    f"FAISS index '{tier_name}' has dimension {index.d}, but the "
                    f"configured embedder ([embeddings] in indexer.toml) expects "
                    f"{dimension}. The embedder changed — this is a one-time reindex: "
                    f"delete '{self.base_dir}' and rebuild (run code-indexer). "
                    f"stable_ids are unchanged, so it is recompute-vectors-only."
                )
        else:
            base_index = faiss.IndexFlatIP(dimension)
            index = faiss.IndexIDMap(base_index)
        self.indexes[tier_name] = index
        return index

    def save_all(self):
        for name, index in self.indexes.items():
            faiss.write_index(index, os.path.join(self.base_dir, f"{name}.faiss"))

class DocumentStore:
    """In-memory chunk-payload cache backed by the SQLite `chunks` table.

    On startup the cache is populated by reading all (scope, tier, text, tags,
    file_path) rows from SQLite and computing the stable FAISS ID for each.
    During an indexing run, `add()` keeps the cache in sync as new chunks are
    embedded.  `save()` is a no-op — chunk payloads are persisted atomically
    by `db.upsert_file()` in the SQLite write path.

    One-shot migration: if a legacy `doc_store.json` exists next to the
    SQLite file, it is deleted on first startup.  The JSON was redundant;
    SQLite is authoritative.
    """

    def __init__(self, sqlite_db_path: str = ".code-index/graph.db") -> None:
        self.docs: dict[str, dict] = {}
        self._load_from_sqlite(sqlite_db_path)
        self._retire_json(sqlite_db_path)

    def _load_from_sqlite(self, db_path: str) -> None:
        if not os.path.exists(db_path):
            return
        from stable_id import stable_id as _sid, TIER_NAME as _TIER_NAME
        conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            rows = conn.execute(
                """
                SELECT c.scope, c.tier, c.text, c.tags, f.path
                FROM   chunks c
                JOIN   files  f ON f.id = c.file_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()

        for scope, tier_num, text, tags, file_path in rows:
            tier_name = _TIER_NAME.get(tier_num)
            if tier_name is None:
                continue
            fid = _sid(tier_name, file_path, scope)
            self.docs[str(fid)] = {
                "tier":  tier_name,
                "file":  file_path,
                "scope": scope,
                "text":  text,
                "tags":  tags.split() if tags else [],
            }

    @staticmethod
    def _retire_json(db_path: str) -> None:
        json_path = os.path.join(os.path.dirname(db_path), "doc_store.json")
        if os.path.exists(json_path):
            try:
                os.remove(json_path)
                print(
                    f"[DocumentStore] Retired {json_path} — "
                    "chunk payloads now served from SQLite."
                )
            except OSError:
                pass

    def add(self, doc_id: int, metadata: dict) -> None:
        self.docs[str(doc_id)] = metadata

    def get(self, doc_id: int) -> dict | None:
        return self.docs.get(str(doc_id))

    def save(self) -> None:
        # No-op: chunk payloads are persisted to SQLite by upsert_file().
        pass

_TRUNCATION_WARNING = (
    "\n---\n"
    "[CONTEXT TRUNCATED] The retrieved context was cut at a chunk boundary to stay "
    "within the token budget. This response is based on partial information. "
    "Rephrase your query to be more specific, or request deeper retrieval, "
    "if the answer appears incomplete.\n"
)


def pack_context_safely(
    ranked_chunks: list[dict],
    max_tokens: int = 4000,
) -> str:
    """Pack reranked chunks into a strict token budget for 8B-parameter local models.

    Structural Expansion chunks (callers/callees) are promoted ahead of lower-ranked
    semantic chunks when their cross-encoder score is within 10 % of the top semantic
    score, ensuring architectural context is not crowded out by volume alone.

    A standardised warning is appended when chunks are dropped so the downstream LLM
    agent knows the context is incomplete.

    Parameters
    ----------
    ranked_chunks:
        Ordered list of chunk dicts.  Each dict must have at minimum:
          - ``"text"``   (str)  — the chunk body
          - ``"source"`` (str)  — ``"semantic"`` or ``"structural"``
          - ``"score"``  (float) — cross-encoder logit (or FAISS cosine fallback)
        Compatible with both raw dicts and ``RetrievedChunk`` instances converted via
        ``dataclasses.asdict()``.
    max_tokens:
        Hard token ceiling.  Defaults to 4 000 — a conservative limit for 8k-window
        8B models that leaves headroom for the system prompt and generation.

    Returns
    -------
    str
        Concatenated chunk texts, each separated by a blank line, with an optional
        truncation warning appended.
    """
    if not ranked_chunks:
        return ""

    # Highest cross-encoder score seen on a semantic chunk — used as the priority baseline.
    semantic_scores = [
        float(c.get("score", 0.0))
        for c in ranked_chunks
        if c.get("source") == "semantic"
    ]
    top_semantic_score = max(semantic_scores) if semantic_scores else 0.0
    structural_threshold = top_semantic_score * 0.9

    # Structural chunks whose score is within 10 % of the top semantic score are
    # promoted.  All other chunks retain their original reranked order.
    priority: list[dict] = []
    deferred: list[dict] = []
    for chunk in ranked_chunks:
        is_structural = chunk.get("source") == "structural"
        within_threshold = float(chunk.get("score", 0.0)) >= structural_threshold
        if is_structural and within_threshold:
            priority.append(chunk)
        else:
            deferred.append(chunk)

    packed_parts: list[str] = []
    used_tokens = 0
    truncated = False

    for chunk in priority + deferred:
        text = chunk.get("text", "")
        if not text:
            continue
        chunk_text = text + "\n\n"
        chunk_tokens = jina_tokenizer.count_tokens(chunk_text)
        if used_tokens + chunk_tokens > max_tokens:
            truncated = True
            break
        packed_parts.append(chunk_text)
        used_tokens += chunk_tokens

    result = "".join(packed_parts)
    if truncated:
        result += _TRUNCATION_WARNING
    return result
