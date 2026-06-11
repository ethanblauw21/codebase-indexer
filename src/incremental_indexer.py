#!/usr/bin/env python3
"""
incremental_indexer.py — Incremental rebuild of FAISS + SQLite indexes.

Only files that are new, modified, or deleted since the last run are touched.
The embedding step (sentence-transformers on GPU/CPU) is skipped for unchanged
files, making repeated runs over a stable codebase close to instant.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGE DETECTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For each file on disk we compute an MD5 digest of its raw bytes and compare it
against the `content_hash` column in the SQLite `files` table.  MD5 is chosen
over SHA-256 because it is ~3× faster and collision resistance is irrelevant
for change detection (we are not using it for security).  Reading the file in
64 KiB blocks keeps memory usage constant regardless of file size.

Three categories result from the comparison:

  NEW      – path exists on disk; no row in the `files` table.
  MODIFIED – path exists in both; hashes differ.
  DELETED  – row exists in `files`; path is absent from disk.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAISS numpy DTYPE CONTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAISS is a C++ library with strict type requirements surfaced through its
SWIG Python bindings.  Passing the wrong dtype produces either a TypeError,
a silent wrong result, or a segfault depending on the FAISS build:

  ┌──────────────────────┬─────────────────────────────────────────────────┐
  │ API call             │ Required numpy dtype / shape                    │
  ├──────────────────────┼─────────────────────────────────────────────────┤
  │ add_with_ids vectors │ np.float32, shape (n, d), C-contiguous          │
  │ add_with_ids ids     │ np.int64,   shape (n,)                          │
  │ remove_ids           │ np.int64,   shape (n,)                          │
  │ normalize_L2         │ np.float32, shape (n, d), mutates in-place      │
  │ search               │ np.float32, shape (1, d) or (n, d)              │
  └──────────────────────┴─────────────────────────────────────────────────┘

  VECTORS — float32
    FAISS internally uses C `float` (32-bit IEEE 754) for every vector
    operation.  sentence-transformers returns float32 by default, but
    np.vstack() can silently promote to float64 in some numpy versions.
    Always use np.ascontiguousarray(arr, dtype=np.float32) before passing
    to any FAISS call.  Float64 vectors are accepted by the Python binding
    but the C++ layer reads them as float32 — every other float is skipped,
    producing completely wrong embeddings.

  IDS — int64
    FAISS idx_t is int64_t on all supported platforms (32-bit and 64-bit).
    Critical Windows pitfall: numpy's default integer type is int32 on
    Windows (the default C `int`).  np.array([1, 2, 3]) on Windows gives
    int32; FAISS raises a SWIG TypeError.  ALWAYS specify dtype=np.int64
    — never rely on the numpy default.

  MEMORY LAYOUT — C-contiguous
    FAISS C++ code assumes row-major (C-order) layout.  A Fortran-order
    (column-major) array of shape (n, d) has the same bytes but in the
    wrong order; FAISS will silently process transposed data.  np.ascontiguous
    array(…) guarantees C-order in one call.

  normalize_L2
    Operates IN-PLACE.  Returns None.  The function converts an inner-product
    (dot-product) index (IndexFlatIP) into a cosine-similarity index by
    pre-normalising every stored vector to unit length.  Pass a copy if you
    need the original magnitudes downstream.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STABLE ID SPACE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAISS IDs are deterministic integers derived from the compound key:
    int(md5(f"{tier_name}::{file_path}::{scope}")[:15], 16)

  15 hex chars  → 60-bit unsigned integer.
  int64_t range → 63 usable bits (signed), max ≈ 9.2 × 10^18.
  60-bit max    → ≈ 1.15 × 10^18 — safely below the signed ceiling.

  Using 16 hex chars (64 bits) would occasionally produce values > 2^63,
  which FAISS interprets as negative idx_t values and either rejects or
  silently mis-routes in the IDMap.  15 chars is the safe upper bound.

  IDs are NOT stored in SQLite.  They are recomputed on demand from the
  (scope, tier, path) columns in the `chunks` table, making remove_ids
  fully reproducible without any schema changes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
remove_ids MECHANICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IndexIDMap wraps an IndexFlatIP.  When remove_ids(int64_array) is called:

  1. IDMap translates each external int64 ID to its internal position in
     the flat array using the stored (external_id → internal_position) table.
  2. The underlying IndexFlatIP rebuilds its vector store by copying all
     surviving vectors into a new contiguous block (compaction).
  3. The IDMap table is updated to reflect the new internal positions.

Cost: O(n_total_vectors) — proportional to the TOTAL index size, not the
number of removed IDs.  This is fine for small incremental removals
(a few files = a few hundred vectors).  For bulk deletions (thousands of
files), a full rebuild is faster.

DEDUPLICATION: Passing the same ID twice to remove_ids is undefined behaviour
in some FAISS builds; the IDMap entry may be double-freed or leave a dangling
pointer.  Always deduplicate the id array first (np.unique preserves int64).
"""
from __future__ import annotations

import hashlib
import os
import traceback
from pathlib import Path
from typing import NamedTuple, Optional

import faiss
import numpy as np

from ast_chunker import chunk_file_ast, fallback_token_chunker, parse_file
from core import embed, MultiIndexManager, DocumentStore
from db import CodeDB
from import_resolver import ImportResolver

# ---------------------------------------------------------------------------
# Configuration — mirrors indexer.py so both scripts can coexist
# ---------------------------------------------------------------------------

REPO_PATH = os.getcwd()
INDEX_DIR = ".code-index"
DB_PATH   = f"{INDEX_DIR}/graph.db"

# Set to False to skip LLM summarization (e.g. for fast CI rebuilds).
# Model: Qwen2.5-Coder-1.5B-Instruct by default (fast CPU); upgrade to
# "Qwen/Qwen2.5-Coder-7B-Instruct" in summarizer.py for higher quality on GPU.
ENABLE_SUMMARIZATION: bool = True

IGNORE_DIRS: frozenset[str] = frozenset({
    ".next", "node_modules", "dist", ".git", "build",
    ".code-index", ".continue", ".claude", ".vs", ".vscode",
    "Modelfiles", "playwright-report", "test-results",
    ".github", ".firebase", ".idx", "genkit", "indexer", "public", "mocks"
})
IGNORE_ROOT_DIRS: frozenset[str] = frozenset({"functions"})

INDEXABLE_EXTS: frozenset[str] = frozenset({".py", ".ts", ".tsx", ".js", ".jsx"})

# Each tuple: (faiss_index_name, max_tokens_per_chunk, overlap_tokens)
#   Tier 1 — surgical: one chunk per AST symbol (function/class/interface)
#   Tier 2 — component: 1 500-token windows over the whole file
#   Tier 3 — architectural: 4 000-token windows, one or two per file
TIER_CONFIGS: list[tuple[str, int, int]] = [
    ("tier1_surgical",       500,   50),
    ("tier2_component",     1500,  100),
    ("tier3_architectural", 4000,  200),
]

# Bidirectional mappings between the FAISS tier name and the SQLite tier integer.
# SQLite stores an integer (1/2/3) to avoid repeating long strings in every row.
TIER_NUM:  dict[str, int] = {name: idx + 1 for idx, (name, _, _) in enumerate(TIER_CONFIGS)}
TIER_NAME: dict[int, str] = {v: k for k, v in TIER_NUM.items()}

# ---------------------------------------------------------------------------
# ID utilities
# ---------------------------------------------------------------------------

def md5_file(path: str) -> str:
    """
    MD5 digest of a file's raw bytes, read in 64 KiB blocks.

    Block-reading keeps memory usage constant for large generated files
    (e.g. bundled JS).  MD5 is fast and sufficient for change detection
    — we are not using it for authentication or integrity guarantees.
    """
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def stable_id(tier_name: str, file_path: str, scope: str) -> int:
    """
    Deterministic 60-bit FAISS vector ID.

    Formula: int(md5(f"{tier_name}::{file_path}::{scope}")[:15], 16)

    Why 15 hex chars?
      16 hex chars = 64 bits.  int64_t is SIGNED, so values ≥ 2^63 are
      negative in FAISS — the IDMap either rejects them or stores them at
      the wrong slot.  15 hex chars = 60 bits, comfortably below 2^63.
    """
    raw = f"{tier_name}::{file_path}::{scope}".encode()
    return int(hashlib.md5(raw).hexdigest()[:15], 16)


# ---------------------------------------------------------------------------
# FAISS array factories — the single place where dtypes are enforced
# ---------------------------------------------------------------------------

def to_faiss_ids(ids: list[int]) -> np.ndarray:
    """
    Convert a list of Python ints to a FAISS-safe int64 array.

    FAISS dtype contract: idx_t = int64_t on ALL platforms.

    Why not rely on numpy's default?
      • Linux/macOS 64-bit:  default int is int64  → happens to work.
      • Windows 64-bit:      default int is int32  → SWIG TypeError at runtime.
      • To be safe on every platform, ALWAYS pass dtype=np.int64 explicitly.

    np.unique() called on the result preserves the int64 dtype while also
    deduplicating, which is required before remove_ids (see module docstring).
    """
    return np.array(ids, dtype=np.int64)


def to_faiss_matrix(vecs: list[np.ndarray]) -> np.ndarray:
    """
    Stack 1-D embedding vectors into a 2-D float32 C-contiguous matrix.

    FAISS dtype contract for vectors: float (32-bit), shape (n, d), C-order.

    np.ascontiguousarray simultaneously:
      1. Casts to float32   — guards against np.vstack promoting to float64.
      2. Ensures C-order    — guards against Fortran-order arrays from some
                              scipy / sklearn utilities.
    Both are silent failures if not corrected: float64 is read as half-width
    float32s; column-major layout produces transposed embeddings.
    """
    return np.ascontiguousarray(np.vstack(vecs), dtype=np.float32)


# ---------------------------------------------------------------------------
# Disk scan
# ---------------------------------------------------------------------------

def scan_disk(repo_path: str) -> dict[str, str]:
    """
    Walk `repo_path` and return {relative_path: md5_hash} for every indexable file.

    Directory exclusions mirror indexer.py:
      • Global ignores (IGNORE_DIRS) are applied at every depth.
      • IGNORE_ROOT_DIRS ("functions") is skipped only at the repository root
        because the same name might legitimately appear in nested packages.
    """
    result: dict[str, str] = {}

    for root, dirs, files in os.walk(repo_path):
        rel_root = os.path.relpath(root, repo_path).replace("\\", "/")

        if rel_root == ".":
            dirs[:] = [
                d for d in dirs
                if d not in IGNORE_DIRS and d not in IGNORE_ROOT_DIRS
            ]
        else:
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        for fname in files:
            if Path(fname).suffix.lower() in INDEXABLE_EXTS:
                full_path = os.path.join(root, fname)
                rel_path  = os.path.relpath(full_path, repo_path).replace("\\", "/")
                try:
                    result[rel_path] = md5_file(full_path)
                except OSError:
                    # Race: file disappeared between os.walk listing and open()
                    pass

    return result


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

class DiffResult(NamedTuple):
    new:      list[str]    # paths present on disk, absent from SQLite
    modified: list[str]    # paths present in both, but MD5 hash differs
    deleted:  list[str]    # paths present in SQLite, absent from disk


def compute_diff(db: CodeDB, disk: dict[str, str]) -> DiffResult:
    """
    Three-way comparison between disk state and the SQLite `files` table.

    SQLite is the sole authoritative record of "what was indexed last run".
    The comparison is done entirely in Python (no SQL set operations) so we
    get clean Python lists to pass to the rest of the pipeline.
    """
    db_state: dict[str, str] = {
        row[0]: row[1]
        for row in db._conn.execute("SELECT path, content_hash FROM files").fetchall()
    }

    disk_paths: set[str] = set(disk)
    db_paths:   set[str] = set(db_state)

    new      = sorted(disk_paths - db_paths)
    deleted  = sorted(db_paths  - disk_paths)
    modified = sorted(p for p in disk_paths & db_paths if disk[p] != db_state[p])

    return DiffResult(new=new, modified=modified, deleted=deleted)


# ---------------------------------------------------------------------------
# Stale-vector identification (SQLite → FAISS IDs)
# ---------------------------------------------------------------------------

def get_stale_ids(db: CodeDB, stale_paths: list[str]) -> np.ndarray:
    """
    Return the FAISS int64 IDs of every chunk that belongs to `stale_paths`.

    HOW IT WORKS
    The chunks table stores (scope, tier, file_path).  The FAISS ID is
    deterministic from these three values via stable_id(), so we can
    reconstruct the exact int64 IDs that were passed to add_with_ids at index
    time — without ever persisting the raw IDs in the schema.

    QUERYING SQLITE
    We JOIN chunks → files to get all three fields, then call stable_id() for
    each row.  The idx_chunks_file_tier index makes this JOIN fast even for
    large repos.

    RETURN VALUE
    Always returns np.int64 (even when empty).  This lets the caller pass the
    result directly to remove_ids without a dtype guard.
    """
    if not stale_paths:
        # Return a correctly-typed empty array rather than a generic np.array([])
        # whose dtype would be float64 — an easy footgun.
        return np.empty(0, dtype=np.int64)

    rows = db.get_chunk_metadata_for_files(stale_paths)
    # rows: [(scope: str, tier_num: int, file_path: str), ...]

    raw_ids = [
        stable_id(TIER_NAME[tier_num], file_path, scope)
        for scope, tier_num, file_path in rows
    ]

    return to_faiss_ids(raw_ids)


# ---------------------------------------------------------------------------
# Stale-vector removal from FAISS + DocumentStore
# ---------------------------------------------------------------------------

def purge_stale_vectors(
    faiss_indexes:  dict[str, faiss.Index],
    doc_store:      DocumentStore,
    stale_ids:      np.ndarray,
) -> int:
    """
    Remove stale vectors from every FAISS tier and mirror the removal in
    the DocumentStore (the MCP server's JSON read-cache).

    remove_ids DTYPE REQUIREMENTS
    ─────────────────────────────
    stale_ids MUST be np.int64.  The assertion below fires early with a clear
    error message rather than letting FAISS produce a silent wrong result:

      • In Python FAISS builds: wrong dtype → SWIG TypeError (visible).
      • In C-extension-only builds: wrong dtype → reads adjacent memory,
        silently removing the wrong vectors (invisible, data corruption).

    DEDUPLICATION BEFORE remove_ids
    ─────────────────────────────────
    np.unique() deduplicates AND sorts the id array.  Calling remove_ids with
    a duplicated ID is undefined behaviour in some FAISS versions: the IDMap
    may attempt to free the same internal slot twice, corrupting the mapping
    table for subsequent queries.  np.unique() preserves the int64 dtype.

    IndexIDMap → IndexFlatIP COMPACTION
    ─────────────────────────────────────
    Under the hood, IndexIDMap translates each external ID to its internal
    position, then calls IndexFlatIP.remove_ids which REBUILDS the vector
    store by compacting all surviving vectors into a new contiguous block.
    Cost: O(n_total_vectors) — not O(n_removed).
    For a local repo index (< 500k vectors), this is < 1 second.

    DOCUMENT STORE SYNC
    ─────────────────────
    DocumentStore.docs is a plain dict {str(faiss_id): metadata}.  It has no
    delete API, so we rebuild the dict with a comprehension that excludes the
    stale keys.  The change is persisted when doc_store.save() is called later.

    Returns the number of unique IDs submitted for removal.
    """
    if len(stale_ids) == 0:
        return 0

    # ── dtype guard: catch the common Windows int32 mistake before FAISS does ──
    assert stale_ids.dtype == np.int64, (
        f"remove_ids requires np.int64 — got {stale_ids.dtype}.  "
        "Use to_faiss_ids() to construct the array."
    )

    # ── deduplicate: double-free is undefined behaviour in some FAISS builds ──
    # np.unique returns sorted, dtype-preserved array
    unique_ids: np.ndarray = np.unique(stale_ids)   # still int64

    for tier_name, idx in faiss_indexes.items():
        try:
            # remove_ids signature: faiss.Index.remove_ids(ids: np.ndarray[int64])
            # IndexIDMap translates IDs → internal positions, then compacts the
            # underlying IndexFlatIP.  IDs not present in the index are silently
            # ignored (safe to call with a superset of valid IDs).
            idx.remove_ids(unique_ids)
        except Exception as exc:
            # Non-fatal: log and continue.  A stale vector that survives in
            # FAISS will never match anything in doc_store after the next step,
            # so search results remain correct — just slightly bloated.
            print(f"  [WARN] {tier_name}: remove_ids raised {type(exc).__name__}: {exc}")

    # ── mirror removal in the DocumentStore ──
    stale_str_keys: set[str] = {str(sid) for sid in unique_ids}
    doc_store.docs = {
        k: v
        for k, v in doc_store.docs.items()
        if k not in stale_str_keys
    }

    return len(unique_ids)


# ---------------------------------------------------------------------------
# Single-file ingest: parse → chunk → embed → add_with_ids → SQLite upsert
# ---------------------------------------------------------------------------

def ingest_file(
    rel_path:      str,
    content:       str,
    content_hash:  str,
    faiss_indexes: dict[str, faiss.Index],
    doc_store:     DocumentStore,
    db:            CodeDB,
    resolver:      Optional[ImportResolver] = None,
    summarizer:    Optional[object] = None,
) -> dict[str, int]:
    """
    Full pipeline for one file: AST parse → three-tier chunking → embedding
    → FAISS add_with_ids → DocumentStore add → SQLite upsert.

    add_with_ids DTYPE REQUIREMENTS
    ─────────────────────────────────
    vectors
        np.float32, shape (n, d), C-contiguous.
        sentence-transformers returns float32 from embed(), but np.vstack()
        can silently promote to float64.  to_faiss_matrix() calls
        np.ascontiguousarray(…, dtype=np.float32) as the single authoritative
        cast point.

    ids
        np.int64, shape (n,).
        to_faiss_ids() enforces the dtype regardless of platform.

    normalize_L2 CONTRACT
    ──────────────────────
        • Input:   float32, shape (n, d), C-contiguous.
        • Mutates: in-place, computes L2 norm per row and divides.
        • Returns: None — do not use the return value.
        • Purpose: converts IndexFlatIP (dot product) into cosine similarity.
                   All stored vectors must be normalised; query vectors are
                   normalised in the search path (MCPServer.py).

    Returns {tier_name: chunk_count} for progress logging.
    """

    print(f"  [ingest:{rel_path}] chunking...", flush=True)
    # ── Tier-1 chunks via AST parser; Tier-2/3 via token windows ──
    tier_chunks: dict[str, list] = {}
    for tier_name, max_tokens, overlap in TIER_CONFIGS:
        if tier_name == "tier1_surgical":
            tier_chunks[tier_name] = chunk_file_ast(rel_path, content, max_tokens, overlap)
        else:
            tier_chunks[tier_name] = fallback_token_chunker(
                content, rel_path, max_tokens, overlap, parent_scope="Full File"
            )
    print(f"  [ingest:{rel_path}] chunks: " +
          " | ".join(f"{n}={len(c)}" for n, c in tier_chunks.items()), flush=True)

    print(f"  [ingest:{rel_path}] parsing AST...", flush=True)
    # ── AST symbol + edge graph for SQLite ──
    parse_result = parse_file(rel_path, content)
    symbols      = parse_result.symbols
    references   = parse_result.references
    symbol_types = parse_result.symbol_types
    edges        = parse_result.edges
    print(f"  [ingest:{rel_path}] AST done: {len(symbols)} symbols, {len(edges)} edges", flush=True)

    # Resolve IMPORTS edges to canonical repo-relative paths
    if resolver is not None:
        for edge in edges:
            if edge.kind == "import":
                resolved = resolver.resolve(edge.target, rel_path)
                if resolved:
                    edge.resolved_target = resolved

    # ── Per-tier embedding loop ──
    for tier_name, chunks in tier_chunks.items():
        faiss_idx = faiss_indexes[tier_name]

        texts_to_embed: list[str] = []
        all_ids: list[int] = []

        for chunk in chunks:
            fid = stable_id(tier_name, rel_path, chunk.scope)
            texts_to_embed.append(chunk.text)
            all_ids.append(fid)

            # Keep the DocumentStore in sync for MCP server reads
            doc_store.add(fid, {
                "tier":  tier_name,
                "file":  rel_path,
                "scope": chunk.scope,
                "text":  chunk.text,
                "tags":  chunk.tags,
            })

        if not texts_to_embed:
            print(f"  [ingest:{rel_path}] {tier_name}: no chunks, skipping", flush=True)
            continue

        # ── OPTIONAL SUMMARY AUGMENTATION ──
        embed_texts = texts_to_embed
        if summarizer is not None:
            print(f"  [ingest:{rel_path}] {tier_name}: summarizing {len(texts_to_embed)} chunks...", flush=True)
            text_hashes = [
                hashlib.md5(t.encode()).hexdigest() for t in texts_to_embed
            ]
            cached = db.get_cached_summaries(text_hashes)

            uncached_idx = [i for i, h in enumerate(text_hashes) if h not in cached]
            print(f"  [ingest:{rel_path}] {tier_name}: {len(uncached_idx)} cache misses → LLM", flush=True)
            if uncached_idx:
                new_summaries = summarizer.summarize_batch(
                    [texts_to_embed[i] for i in uncached_idx]
                )
                new_pairs = [
                    (text_hashes[i], s)
                    for i, s in zip(uncached_idx, new_summaries)
                    if s
                ]
                db.cache_summaries(new_pairs)
                for i, s in zip(uncached_idx, new_summaries):
                    if s:
                        cached[text_hashes[i]] = s
            print(f"  [ingest:{rel_path}] {tier_name}: summarization done", flush=True)

            embed_texts = []
            for fid, original, h in zip(all_ids, texts_to_embed, text_hashes):
                summary = cached.get(h, "")
                if summary:
                    embed_texts.append(f"{original}\n\n# Summary\n{summary}")
                    entry = doc_store.get(fid)
                    if entry is not None:
                        entry["summary"] = summary
                else:
                    embed_texts.append(original)

        # ── BATCH EMBEDDING ──
        print(f"  [ingest:{rel_path}] {tier_name}: embedding {len(embed_texts)} texts...", flush=True)
        from core import embed_batch
        vec_matrix: np.ndarray = embed_batch(embed_texts)
        print(f"  [ingest:{rel_path}] {tier_name}: embedding done, shape={vec_matrix.shape}", flush=True)

        # ── normalize_L2: in-place, converts dot-product → cosine similarity ──
        faiss.normalize_L2(vec_matrix)

        # ── add_with_ids: vectors float32 (n, d), ids int64 (n,) ──
        id_array: np.ndarray = to_faiss_ids(all_ids)
        faiss_idx.add_with_ids(vec_matrix, id_array)
        print(f"  [ingest:{rel_path}] {tier_name}: FAISS add done", flush=True)

    print(f"  [ingest:{rel_path}] writing SQLite...", flush=True)
    # ── Persist symbols, edges, chunks, references, and types to SQLite ──
    db.upsert_file(
        path=rel_path,
        content_hash=content_hash,
        symbols=symbols,
        edges=edges,
        chunks_by_tier={
            TIER_NUM[tier_name]: chunks
            for tier_name, chunks in tier_chunks.items()
        },
        references=references,
        symbol_types=symbol_types,
    )
    print(f"  [ingest:{rel_path}] SQLite done", flush=True)

    return {name: len(cks) for name, cks in tier_chunks.items()}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_incremental(repo_path: str = REPO_PATH) -> None:
    """
    Main entry point.  Execution order is chosen for crash safety:

      1. Compute diff  (read-only)
      2. Get stale IDs from SQLite BEFORE mutating anything
      3. Purge FAISS   (in-memory only until step 6)
      4. Delete from SQLite (committed per file — atomic transactions)
      5. Re-index new + modified files (each file is its own transaction)
      6. Persist FAISS indexes + DocumentStore to disk

    If the process crashes between steps 4 and 5, the deleted files are
    absent from the `files` table on the next run → they land in DiffResult.new
    and are re-indexed cleanly.  No manual recovery is needed.
    """
    print(f"━━ Incremental Indexer: {os.path.basename(repo_path)} ━━")

    # ── Open all persistent stores ──
    index_manager = MultiIndexManager(INDEX_DIR)
    doc_store     = DocumentStore(f"{INDEX_DIR}/doc_store.json")
    db            = CodeDB(DB_PATH)

    # ── Optional chunk summarizer (lazy-loads model on first file) ──
    summarizer = None
    if ENABLE_SUMMARIZATION:
        from summarizer import IsolatedChunkSummarizer
        summarizer = IsolatedChunkSummarizer()
        print("  Chunk summarizer enabled (worker process starts on first file processed)")

    faiss_indexes: dict[str, faiss.Index] = {
        name: index_manager.load_or_create(name)
        for name, _, _ in TIER_CONFIGS
    }

    # ── Step 1: Scan disk + compute diff ──
    print("Scanning files...")
    disk_hashes = scan_disk(repo_path)
    diff        = compute_diff(db, disk_hashes)

    n_changed = len(diff.new) + len(diff.modified) + len(diff.deleted)
    if n_changed == 0:
        print("Nothing changed — index is up to date.")
        db.close()
        return

    print(
        f"  Δ  {len(diff.new)} new  |  {len(diff.modified)} modified  "
        f"|  {len(diff.deleted)} deleted"
    )

    # ── Step 2: Collect stale FAISS IDs BEFORE any mutation ──
    # We query SQLite while the old chunk rows still exist.  After step 4
    # (delete_file), those rows are gone and we can no longer derive the IDs.
    stale_paths = diff.modified + diff.deleted
    stale_ids = get_stale_ids(db, stale_paths) if stale_paths else np.empty(0, dtype=np.int64)

    # ── Step 3: Purge stale vectors from FAISS (in-memory) ──
    if len(stale_ids) > 0:
        n_removed = purge_stale_vectors(faiss_indexes, doc_store, stale_ids)
        print(f"  Purged {n_removed} stale vector IDs ({len(stale_paths)} file(s)).")

    # ── Step 4: Remove stale records from SQLite ──
    # ON DELETE CASCADE removes symbols and chunks; edges are cleaned up by
    # CodeDB.delete_file().  Each call is its own committed transaction.
    for path in stale_paths:
        db.delete_file(path)

    # ── Step 5: Re-index new and modified files ──
    to_index = diff.new + diff.modified
    if to_index:
        print(f"Indexing {len(to_index)} file(s)...")
    resolver = ImportResolver(repo_path)

    errors = 0
    for rel_path in to_index:
        full_path = os.path.join(repo_path, rel_path)
        print(f"[loop] → {rel_path}", flush=True)
        try:
            print(f"[loop]   reading file...", flush=True)
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            print(f"[loop]   {len(content)} chars read, calling ingest_file...", flush=True)

            counts = ingest_file(
                rel_path=rel_path,
                content=content,
                content_hash=disk_hashes[rel_path],
                faiss_indexes=faiss_indexes,
                doc_store=doc_store,
                db=db,
                resolver=resolver,
                summarizer=summarizer,
            )
            t1 = counts.get("tier1_surgical",       0)
            t2 = counts.get("tier2_component",       0)
            t3 = counts.get("tier3_architectural",   0)
            print(f"[loop]   ingest_file returned", flush=True)
            print(f"  ✓  {rel_path}  (T1:{t1} | T2:{t2} | T3:{t3})")

        except Exception:
            errors += 1
            print(f"  ✗  {rel_path}")
            traceback.print_exc()

    # ── Step 6: Flush everything to disk ──
    print("Saving indexes...")
    index_manager.save_all()
    doc_store.save()
    db.close()

    # Reopen read-only to verify row counts
    with CodeDB(DB_PATH) as verify_db:
        s = verify_db.stats()

    status = "with errors" if errors else "successfully"
    print(
        f"Done {status}.  "
        f"files={s['files']}  symbols={s['symbols']}  "
        f"chunks={s['chunks']}  edges={s['edges']}"
    )
    if errors:
        print(f"  {errors} file(s) failed to index — check output above.")


if __name__ == "__main__":
    run_incremental()
