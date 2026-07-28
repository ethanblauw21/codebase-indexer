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
See stable_id.py — to_faiss_ids() and to_faiss_matrix() are the single
authoritative dtype-enforcement points for all FAISS array construction.

  add_with_ids vectors : np.float32, shape (n, d), C-contiguous
  add_with_ids ids     : np.int64,   shape (n,)
  remove_ids           : np.int64,   shape (n,)
  normalize_L2         : np.float32, shape (n, d), mutates in-place
  search               : np.float32, shape (1, d) or (n, d)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STABLE ID SPACE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
See stable_id.stable_id() — the formula lives there and is imported here.
IDs are NOT stored in SQLite; they are recomputed on demand from the
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
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import faiss
import numpy as np

from ast_chunker import chunk_file_ast, fallback_token_chunker, parse_file
from call_resolver import resolve_call_edges
from config import summarization_enabled, summarizer_model_id
from core import MultiIndexManager, DocumentStore
from db import CodeDB
from import_resolver import ImportResolver
from stable_id import stable_id, to_faiss_ids, TIER_CONFIGS, TIER_NUM, TIER_NAME

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_PATH = os.getcwd()
INDEX_DIR = ".code-index"
DB_PATH   = f"{INDEX_DIR}/graph.db"

# Summarization is config-driven (ADR-026): the gate is [summarization].enabled in
# indexer.toml, resolved by config.summarization_enabled(). The module constant that
# used to live here (ENABLE_SUMMARIZATION) was the real gate while the documented
# config key did nothing, so turning summarization off — the difference between a CPU
# index that completes and one that does not — required editing source. The default
# now lives beside its accessor in config.py, once.

IGNORE_DIRS: frozenset[str] = frozenset({
    ".next", "node_modules", "dist", ".git", "build",
    ".code-index", ".continue", ".claude", ".vs", ".vscode",
    "Modelfiles", "playwright-report", "test-results",
    ".github", ".firebase", ".idx", "genkit", "indexer", "public", "mocks"
})
IGNORE_ROOT_DIRS: frozenset[str] = frozenset({"functions"})

# Source extensions the disk scan chunks + embeds. C#/C++ have full Tier-A adapters
# (ADR-003; adapters/__init__.py registers .cs/.cpp/.cc/.cxx/.h/.hpp) and ADR-017 §1
# lists them as Tier A, but the scan gate historically omitted them — so their source
# was never chunked, only their .csproj/.sln descriptors (edges only). Wiring the
# extensions here makes the shipping indexer honor the Tier-A claim for C#/C++.
INDEXABLE_EXTS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx",   # Tier-A: JS/TS/Python
    ".cs",                                   # Tier-A: C#
    ".cpp", ".cc", ".cxx", ".h", ".hpp",     # Tier-A: C++ (.h routed to the C++ adapter)
})

# Project descriptor files: edges only, no chunking or embedding.
PROJECT_EXTS: frozenset[str] = frozenset({".csproj", ".sln"})

# Specific filenames (regardless of extension) that are project descriptors.
PROJECT_FILES: frozenset[str] = frozenset({"compile_commands.json"})

# Combined set for disk scan
_ALL_SCAN_EXTS: frozenset[str] = INDEXABLE_EXTS | PROJECT_EXTS

# ---------------------------------------------------------------------------
# MD5 file hash (change detection only — not the stable ID formula)
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


# ---------------------------------------------------------------------------
# Disk scan
# ---------------------------------------------------------------------------

def scan_disk(repo_path: str) -> dict[str, str]:
    """
    Walk `repo_path` and return {relative_path: md5_hash} for every indexable file.

    Directory exclusions:
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
            if Path(fname).suffix.lower() in _ALL_SCAN_EXTS or fname in PROJECT_FILES:
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
# Git-derived content timestamps (ADR-025 §2)
#
# Every helper degrades to an empty/None result on ANY git failure (not a repo,
# git binary absent, no commits) and NEVER raises — a git problem must not break
# indexing. All paths are forward-slash relative to repo root, matching
# scan_disk()'s normalization, so lookups line up without extra munging.
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Current UTC time in the same ISO-8601 shape the DDL default uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_change_times(repo_path: str) -> dict[str, tuple[str, str]]:
    """One git pass → {path: (committer_iso, author_iso)} for the most recent
    commit that touched each tracked path (first occurrence wins, as `git log`
    is newest-first). Returns {} on any git failure. ~85 ms over this repo's
    history; a single walk replaces per-file `git log -1` invocations.

    A sentinel-prefixed --format lets header lines be told apart from name-only
    path lines unambiguously (a path cannot begin with the sentinel)."""
    try:
        out = subprocess.check_output(
            ["git", "log", "--format=@@@%cI|%aI", "--name-only", "--no-merges"],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    times: dict[str, tuple[str, str]] = {}
    committer: Optional[str] = None
    author: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("@@@"):
            parts = line[3:].split("|", 1)
            if len(parts) == 2:
                committer, author = parts[0], parts[1]
            continue
        path = line.strip()
        if path and committer and path not in times:
            times[path] = (committer, author or "")
    return times


def git_dirty_paths(repo_path: str) -> set[str]:
    """Tracked files with uncommitted working-tree changes vs HEAD (ADR-025 §2
    rule 2). Untracked files are NOT included — they have no git history and fall
    through to NULL, which is correct. Empty set on any git failure."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def git_head_commit(repo_path: str) -> Optional[str]:
    """HEAD commit hash, or None on any git failure (ADR-025 §4)."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _backfill_null_stamps(db: CodeDB, git_times: dict[str, tuple[str, str]]) -> int:
    """ADR-025 §1 one-time backfill. Rows indexed before this feature have a NULL
    content_changed_at and would be invisible to "changed since T" forever. Fill
    them from the git pass — only NULLs, only tracked paths, never overwriting a
    real stamp. Idempotent: once filled they no longer match the WHERE, so it is a
    no-op on every later run. (Legacy dirty files get committer time here rather
    than now(); a one-time reconciliation, and their next real change restamps.)"""
    n = 0
    for path, (committer, author) in git_times.items():
        cur = db._conn.execute(
            "UPDATE files SET content_changed_at = ?, "
            "authored_at = COALESCE(authored_at, ?) "
            "WHERE path = ? AND content_changed_at IS NULL",
            (committer, author or None, path),
        )
        n += cur.rowcount
    return n


def _write_index_meta(db: CodeDB, repo_path: str) -> None:
    """ADR-025 §4/§5: record run-level freshness facts from the shared chokepoint
    so CLI and MCP agree about what is indexed. Written on EVERY completed run,
    including no-ops — "at commit X, at time T, we verified the index matches the
    code" is true and strictly more informative than recording only on real work."""
    head = git_head_commit(repo_path)
    if head:
        db.meta_set("last_indexed_commit", head)
    db.meta_set("last_verified_at", _now_iso())
    try:
        db.meta_set("files_total", str(db.stats().get("files", 0)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stale-vector identification (SQLite → FAISS IDs)
# ---------------------------------------------------------------------------

def get_stale_ids(db: CodeDB, stale_paths: list[str]) -> np.ndarray:
    """
    Return the FAISS int64 IDs of every chunk that belongs to `stale_paths`.

    The chunks table stores (scope, tier, file_path).  The FAISS ID is
    deterministic from these three values via stable_id(), so we can
    reconstruct the exact int64 IDs that were passed to add_with_ids at index
    time — without ever persisting the raw IDs in the schema.

    Always returns np.int64 (even when empty) so the caller can pass the
    result directly to remove_ids without a dtype guard.
    """
    if not stale_paths:
        return np.empty(0, dtype=np.int64)

    rows = db.get_chunk_metadata_for_files(stale_paths)
    # rows: [(scope: str, tier_num: int, file_path: str), ...]

    raw_ids = [
        stable_id(TIER_NAME[tier_num], file_path, scope)
        for scope, tier_num, file_path in rows
    ]

    return to_faiss_ids(raw_ids)


# ---------------------------------------------------------------------------
# Stale-vector removal from FAISS + DocumentStore cache
# ---------------------------------------------------------------------------

def purge_stale_vectors(
    faiss_indexes:  dict[str, faiss.Index],
    doc_store:      DocumentStore,
    stale_ids:      np.ndarray,
) -> int:
    """
    Remove stale vectors from every FAISS tier and mirror the removal in
    the DocumentStore in-memory cache.

    remove_ids DTYPE REQUIREMENTS
    ─────────────────────────────
    stale_ids MUST be np.int64.  The assertion below fires early with a clear
    error message rather than letting FAISS produce a silent wrong result.

    DEDUPLICATION BEFORE remove_ids
    ─────────────────────────────────
    np.unique() deduplicates AND sorts the id array.  Calling remove_ids with
    a duplicated ID is undefined behaviour in some FAISS versions.

    Returns the number of unique IDs submitted for removal.
    """
    if len(stale_ids) == 0:
        return 0

    assert stale_ids.dtype == np.int64, (
        f"remove_ids requires np.int64 — got {stale_ids.dtype}.  "
        "Use to_faiss_ids() to construct the array."
    )

    unique_ids: np.ndarray = np.unique(stale_ids)   # preserves int64

    for tier_name, idx in faiss_indexes.items():
        try:
            idx.remove_ids(unique_ids)
        except Exception as exc:
            print(f"  [WARN] {tier_name}: remove_ids raised {type(exc).__name__}: {exc}")

    stale_str_keys: set[str] = {str(sid) for sid in unique_ids}
    doc_store.docs = {
        k: v
        for k, v in doc_store.docs.items()
        if k not in stale_str_keys
    }

    return len(unique_ids)


# ---------------------------------------------------------------------------
# Project-descriptor ingest: parse edges only — no chunking or embedding
# ---------------------------------------------------------------------------

def ingest_project_file(
    rel_path:     str,
    content:      str,
    content_hash: str,
    db:           CodeDB,
    content_changed_at: Optional[str] = None,
    authored_at:        Optional[str] = None,
) -> None:
    """
    Process a project descriptor (.csproj, .sln, or compile_commands.json):
    extract dependency edges and store them without creating any chunks or
    FAISS vectors.

    Dispatch:
      .csproj / .sln         → CSharpAdapter (via ast_chunker.parse_file)
      compile_commands.json  → CppProjectResolver.parse() directly
        (ast_chunker cannot route by filename, only by extension)
    """
    fname = Path(rel_path).name
    if fname == "compile_commands.json":
        from adapters.cpp_adapter import _CPP_PROJECT_RESOLVER
        parse_result = _CPP_PROJECT_RESOLVER.parse(rel_path, content.encode("utf-8"))
    else:
        from ast_chunker import parse_file
        parse_result = parse_file(rel_path, content)

    db.upsert_file(
        path               = rel_path,
        content_hash       = content_hash,
        symbols            = [],
        edges              = parse_result.edges,
        chunks_by_tier     = {},
        content_changed_at = content_changed_at,
        authored_at        = authored_at,
    )
    print(f"  [project:{rel_path}] {len(parse_result.edges)} dependency edges stored", flush=True)


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
    content_changed_at: Optional[str] = None,
    authored_at:        Optional[str] = None,
) -> dict[str, int]:
    """
    Full pipeline for one file: AST parse → three-tier chunking → embedding
    → FAISS add_with_ids → DocumentStore cache update → SQLite upsert.

    Returns {tier_name: chunk_count} for progress logging.
    """

    print(f"  [ingest:{rel_path}] chunking...", flush=True)
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
    parse_result = parse_file(rel_path, content)
    symbols      = parse_result.symbols
    references   = parse_result.references
    symbol_types = parse_result.symbol_types
    edges        = parse_result.edges
    print(f"  [ingest:{rel_path}] AST done: {len(symbols)} symbols, {len(edges)} edges", flush=True)

    if resolver is not None:
        for edge in edges:
            if edge.kind == "import":
                resolved = resolver.resolve(edge.target, rel_path)
                if resolved:
                    edge.resolved_target = resolved

    for tier_name, chunks in tier_chunks.items():
        faiss_idx = faiss_indexes[tier_name]

        texts_to_embed: list[str] = []
        all_ids: list[int] = []

        for chunk in chunks:
            fid = stable_id(tier_name, rel_path, chunk.scope)
            texts_to_embed.append(chunk.text)
            all_ids.append(fid)

            # Keep the DocumentStore cache in sync for MCP server reads
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

        print(f"  [ingest:{rel_path}] {tier_name}: embedding {len(embed_texts)} texts...", flush=True)
        from core import embed_batch
        vec_matrix: np.ndarray = embed_batch(embed_texts)
        print(f"  [ingest:{rel_path}] {tier_name}: embedding done, shape={vec_matrix.shape}", flush=True)

        faiss.normalize_L2(vec_matrix)

        id_array: np.ndarray = to_faiss_ids(all_ids)
        faiss_idx.add_with_ids(vec_matrix, id_array)
        del vec_matrix, id_array  # FAISS copied the data; free embedding matrix per tier
        print(f"  [ingest:{rel_path}] {tier_name}: FAISS add done", flush=True)

    print(f"  [ingest:{rel_path}] writing SQLite...", flush=True)
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
        content_changed_at=content_changed_at,
        authored_at=authored_at,
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
      6. Persist FAISS indexes to disk

    If the process crashes between steps 4 and 5, the deleted files are
    absent from the `files` table on the next run → they land in DiffResult.new
    and are re-indexed cleanly.  No manual recovery is needed.

    Chunk payloads (formerly doc_store.json) are now served from SQLite.
    DocumentStore is an in-memory cache loaded from SQLite on startup;
    add() keeps it in sync during the run; no save() call is needed.
    """
    print(f"━━ Incremental Indexer: {os.path.basename(repo_path)} ━━")

    index_manager = MultiIndexManager(INDEX_DIR)
    doc_store     = DocumentStore(DB_PATH)
    db            = CodeDB(DB_PATH)

    summarizer = None
    if summarization_enabled():
        from summarizer import IsolatedChunkSummarizer
        summarizer = IsolatedChunkSummarizer()
        print(f"  Chunk summarizer enabled: {summarizer_model_id()} "
              f"(worker process starts on first file processed)")
    else:
        print("  Chunk summarizer disabled ([summarization].enabled = false)")

    faiss_indexes: dict[str, faiss.Index] = {
        name: index_manager.load_or_create(name)
        for name, _, _ in TIER_CONFIGS
    }

    print("Scanning files...")
    disk_hashes = scan_disk(repo_path)
    diff        = compute_diff(db, disk_hashes)

    # ADR-025 §2: one git pass up front. Reused for back-dating new files, dirty
    # detection, and the §1 one-time backfill of legacy NULL stamps. Done before the
    # no-op check so a quiet repo with legacy rows still gets its stamps backfilled.
    _git_times = git_change_times(repo_path)
    _dirty     = git_dirty_paths(repo_path)
    _run_now   = _now_iso()
    _n_backfilled = _backfill_null_stamps(db, _git_times)
    if _n_backfilled:
        print(f"  Backfilled content_changed_at for {_n_backfilled} legacy file(s).")

    n_changed = len(diff.new) + len(diff.modified) + len(diff.deleted)
    if n_changed == 0:
        print("Nothing changed — index is up to date.")
        # ADR-025 §4: still record that we verified the index against this HEAD at
        # this time. The early return fires only AFTER scan_disk hashed every file
        # and found nothing changed, so "verified, nothing stale" is a true fact —
        # and recording it means a docs-only commit still advances last_indexed_commit
        # instead of leaving the staleness check firing on README.md forever.
        _write_index_meta(db, repo_path)
        db.close()
        return

    print(
        f"  Δ  {len(diff.new)} new  |  {len(diff.modified)} modified  "
        f"|  {len(diff.deleted)} deleted"
    )

    stale_paths = diff.modified + diff.deleted
    stale_ids = get_stale_ids(db, stale_paths) if stale_paths else np.empty(0, dtype=np.int64)

    if len(stale_ids) > 0:
        n_removed = purge_stale_vectors(faiss_indexes, doc_store, stale_ids)
        print(f"  Purged {n_removed} stale vector IDs ({len(stale_paths)} file(s)).")

    for path in stale_paths:
        db.delete_file(path)

    to_index = diff.new + diff.modified
    if to_index:
        print(f"Indexing {len(to_index)} file(s)...")
    resolver = ImportResolver(repo_path)

    # ADR-025 §2: resolve each file's content_changed_at / authored_at from the git
    # pass computed above. New files back-date to real change history; modified files
    # (content just changed since last index) stamp now(); dirty and history-less
    # files never claim a time they didn't have.
    _new_set = set(diff.new)

    def _content_stamp(rel: str) -> tuple[Optional[str], Optional[str]]:
        committer, author = _git_times.get(rel, (None, None))
        if rel in _new_set:
            # First index of this path → back-date. Rules, in order:
            if rel in _dirty:
                changed = _run_now          # rule 2: indexed the dirty version, not the committed one
            elif committer:
                changed = committer         # rule 1: tracked + clean → git committer time
            else:
                changed = None              # rule 3: no git history → NULL (segmem ignores it)
        else:
            # Modified since last index → the content changed now, as far as we saw.
            changed = _run_now
        return changed, (author or None)

    errors = 0
    for rel_path in to_index:
        full_path = os.path.join(repo_path, rel_path)
        ext = Path(rel_path).suffix.lower()
        print(f"[loop] → {rel_path}", flush=True)
        try:
            print("[loop]   reading file...", flush=True)
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            print(f"[loop]   {len(content)} chars read", flush=True)

            _cc_at, _auth_at = _content_stamp(rel_path)

            if ext in PROJECT_EXTS or Path(rel_path).name in PROJECT_FILES:
                ingest_project_file(
                    rel_path, content, disk_hashes[rel_path], db,
                    content_changed_at=_cc_at, authored_at=_auth_at,
                )
                continue

            print("[loop]   calling ingest_file...", flush=True)

            counts = ingest_file(
                rel_path=rel_path,
                content=content,
                content_hash=disk_hashes[rel_path],
                faiss_indexes=faiss_indexes,
                doc_store=doc_store,
                db=db,
                resolver=resolver,
                summarizer=summarizer,
                content_changed_at=_cc_at,
                authored_at=_auth_at,
            )
            t1 = counts.get("tier1_surgical",       0)
            t2 = counts.get("tier2_component",       0)
            t3 = counts.get("tier3_architectural",   0)
            print("[loop]   ingest_file returned", flush=True)
            print(f"  ✓  {rel_path}  (T1:{t1} | T2:{t2} | T3:{t3})")

        except Exception:
            errors += 1
            print(f"  ✗  {rel_path}")
            traceback.print_exc()

    # ADR-021: resolve CALLS-edge bare callee names to in-repo FQNs so the graph
    # Traverse step has real neighbours to walk. Runs once here, over the now-complete
    # symbols table; precision-first (only provably-unique targets), recomputes every
    # run so a name that became ambiguous is demoted back to unresolved.
    res = resolve_call_edges(db)
    print(f"  Call resolution: {res['resolved']} resolved | "
          f"{res['typed']} typed | {res['ambiguous']} ambiguous | {res['external']} external")

    # Flush FAISS indexes to disk.
    # Chunk payloads are already in SQLite (committed per-file by upsert_file).
    print("Saving indexes...")
    index_manager.save_all()

    # ADR-025 §4/§5: record run-level freshness facts from this one chokepoint that
    # all four triggers (CLI, MCP reindex, watchdog, startup) share, so they agree.
    _write_index_meta(db, repo_path)
    db.close()

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


def main() -> None:
    run_incremental()


if __name__ == "__main__":
    main()
