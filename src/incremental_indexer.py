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

import argparse
import os
import subprocess
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import faiss
import numpy as np

from ast_chunker import chunk_file_ast, fallback_token_chunker, parse_file
from call_resolver import resolve_call_edges
from config import embed_overlap, summarization_enabled, summarizer_model_id, summarizer_tiers
from core import MultiIndexManager, DocumentStore
from db import CodeDB, summary_cache_key
from import_resolver import ImportResolver, resolve_python_imports
import index_location as _index_location
from source import WorkingTreeSource, check_anchor, make_source, md5_file  # noqa: F401 (md5_file re-exported)
from scan_policy import PROJECT_EXTS, PROJECT_FILES
from stable_id import stable_id, to_faiss_ids, TIER_CONFIGS, TIER_NUM, TIER_NAME

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_PATH = os.getcwd()
# ADR-042 §3: ".code-index" in worktree mode (relative, as always); the repository's
# <git common dir>/code-index in git mode.
INDEX_DIR = _index_location.index_dir()

# ADR-030: summaries have their own FAISS index, keyed by their chunk's id, and the
# code vectors hold code only. index_meta records the layout so an index built
# before it (summaries appended to the code) can be told apart.
SUMMARY_INDEX = "summary"
EMBED_LAYOUT = "code+summary-index"
DB_PATH   = f"{INDEX_DIR}/graph.db"

# Summarization is config-driven (ADR-026): the gate is [summarization].enabled in
# indexer.toml, resolved by config.summarization_enabled(). The module constant that
# used to live here (ENABLE_SUMMARIZATION) was the real gate while the documented
# config key did nothing, so turning summarization off — the difference between a CPU
# index that completes and one that does not — required editing source. The default
# now lives beside its accessor in config.py, once.

# The scan gate lives in scan_policy (ADR-026 §2), which resolves it from the
# [ignore] block of indexer.toml over built-in defaults. It used to be three module
# constants right here, re-implemented by hand in MCPServer's watchdog filter — the
# drift that let a JavaScript-seeded exclusion list survive on a Python tool.
#
# `from incremental_indexer import IGNORE_DIRS` now raises, deliberately: a stale
# import must fail loudly rather than resolve to an unconfigured value.

# ---------------------------------------------------------------------------
# MD5 file hash (change detection only — not the stable ID formula)
# ---------------------------------------------------------------------------

def scan_disk(repo_path: str, *, quiet: bool = False) -> dict[str, str]:
    """{relative_path: md5} for every scannable file in the folder at ``repo_path``.

    The folder's `Source.list()` (ADR-042 §1); kept under its old name because the
    tests and tools call it.
    """
    return WorkingTreeSource(repo_path).list(quiet=quiet)


_check_anchor = check_anchor


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

class DiffResult(NamedTuple):
    new:      list[str]    # paths present on disk, absent from SQLite
    modified: list[str]    # paths present in both, but MD5 hash differs
    deleted:  list[str]    # paths present in SQLite, absent from disk


# ---------------------------------------------------------------------------
# Bulk-deletion guard (ADR-026 §5)
# ---------------------------------------------------------------------------

def _deletion_threshold(n_indexed: int) -> int:
    """Above this many deletions in one run, ask before purging.

    `max(50, 20%)` rather than a flat number: 50 deletions out of 60 files is a
    catastrophe and 50 out of 20,000 is a refactor.
    """
    return max(50, int(0.20 * n_indexed))


def bulk_deletion_verdict(
    diff: DiffResult,
    n_indexed: int,
    *,
    prune: bool = False,
    interactive: bool = True,
) -> tuple[DiffResult, str | None]:
    """Gate an unusually large deletion set. Returns (diff_to_apply, message).

    **Why this exists.** `DiffResult.deleted` is "in SQLite, absent from disk", and it
    drives an irreversible purge — FAISS `remove_ids` physically compacts the survivors
    and `db.delete_file` cascades to symbols, chunks and locations. That mechanism is
    also what makes this ADR's migration free: newly-ignored files land in `deleted`
    and clean themselves up with no reindex. Both halves are the same mechanism, so
    the change that fixes an over-broad index by pulling 84% of it out is
    indistinguishable, at this line, from a misconfiguration that destroys one.

    The guard does not try to tell those apart. It shows which top-level directories
    are responsible and makes a human say yes — once, with the numbers in front of
    them.

    Deletions are *skipped*, not deferred: the entries stay in the index, stale, and
    the next run asks again. `--prune` answers yes in advance.
    """
    if prune or len(diff.deleted) <= _deletion_threshold(n_indexed):
        return diff, None

    share = (len(diff.deleted) / n_indexed * 100) if n_indexed else 100.0
    tops = Counter(p.split("/")[0] for p in diff.deleted).most_common(10)
    report = "\n".join(f"     {count:6d}  {name}/" for name, count in tops)
    banner = (
        f"\n  This run would delete {len(diff.deleted)} of {n_indexed} indexed "
        f"file(s) ({share:.0f}%) from the index.\n"
        f"  Top-level directories responsible:\n{report}\n"
        f"  Deleting from the index is irreversible (vectors are compacted, rows "
        f"cascade).\n"
    )

    if not interactive or not sys.stdin or not sys.stdin.isatty():
        return diff._replace(deleted=[]), (
            banner
            + "  No terminal to ask - deletions SKIPPED, everything else indexed.\n"
            + "  Re-run `code-indexer --prune` from a shell to apply them."
        )

    print(banner)
    try:
        answer = input("  Apply these deletions? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in {"y", "yes"}:
        return diff, "  Deletions confirmed."
    return diff._replace(deleted=[]), "  Deletions SKIPPED - everything else indexed."


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


def git_change_times(repo_path: str, rev: Optional[str] = None) -> dict[str, tuple[str, str]]:
    """One git pass → {path: (committer_iso, author_iso)} for the most recent
    commit that touched each tracked path (first occurrence wins, as `git log`
    is newest-first). Returns {} on any git failure. ~85 ms over this repo's
    history; a single walk replaces per-file `git log -1` invocations.

    A sentinel-prefixed --format lets header lines be told apart from name-only
    path lines unambiguously (a path cannot begin with the sentinel)."""
    try:
        out = subprocess.check_output(
            ["git", "log", "--format=@@@%cI|%aI", "--name-only", "--no-merges",
             *([rev] if rev else [])],   # ADR-042 §5: the indexed commit's history
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
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
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def git_head_commit(repo_path: str) -> Optional[str]:
    """HEAD commit hash, or None on any git failure (ADR-025 §4)."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
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


# B-029: the generation of the parser and chunker that built an index. Bump it in the
# same commit as any change to what chunks a file produces (their scope, text, count or
# ids), and add a line here. Incremental runs key on file content only, so without this
# an index silently mixes chunks from old and new code.
#   (no marker)  built before ADR-033
#   2            ADR-031: one chunk per (file, scope, tier), so one vector per row
#   3            ADR-034: member docs moved to members, # members and function fields,
#                same-FQN siblings merged, split skeleton parts carry header and lines
CHUNKER_VERSION = 3
CHUNKER_VERSION_KEY = "chunker_version"


def chunker_version_warning(db: CodeDB) -> Optional[str]:
    """A one-line warning when the index was built by another chunker, else None.

    Never triggers a rebuild: at MCP startup that could block the first search for
    minutes (hours with summaries on), and a run cut off midway would leave a mixed
    index. The user runs a full re-index when they choose.
    """
    recorded = db.meta_get(CHUNKER_VERSION_KEY)
    if recorded == str(CHUNKER_VERSION):
        return None
    built = f"chunker v{recorded}" if recorded else "a chunker older than v2"
    return (f"⚠️ This index was built by {built}; the current chunker is v{CHUNKER_VERSION}. "
            "Results may mix old and new chunks. Run a full re-index "
            "(reindex(changed_files_only=False) or a fresh code-indexer build).")


def _write_index_meta(db: CodeDB, repo_path: str, source=None) -> None:
    """ADR-025 §4/§5: record run-level freshness facts from the shared chokepoint
    so CLI and MCP agree about what is indexed. Written on EVERY completed run,
    including no-ops — "at commit X, at time T, we verified the index matches the
    code" is true and strictly more informative than recording only on real work."""
    # ADR-042 §5: a git source indexed its own commit, not HEAD.
    head = getattr(source, "commit", None) or git_head_commit(repo_path)
    if head:
        db.meta_set("last_indexed_commit", head)
    db.meta_set("source", getattr(source, "label", "worktree"))
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
# Row/vector reconciliation (ADR-037)
# ---------------------------------------------------------------------------

def _index_ids(index: faiss.Index) -> Optional[set[int]]:
    """The ids an IndexIDMap holds, or None for an index type without an id map."""
    id_map = getattr(index, "id_map", None)
    if id_map is None:
        return None
    return set(faiss.vector_to_array(id_map).tolist())


def reconcile_vectors(
    db:            CodeDB,
    faiss_indexes: dict[str, faiss.Index],
    doc_store:     DocumentStore,
) -> tuple[list[str], int]:
    """
    Compare every tier index with the chunk rows, and repair what a killed run left.

    SQLite rows are committed per file during a run, but FAISS is saved only at the
    end, so a kill in between leaves rows with no vectors. MD5 diffing then skips
    those files forever (B-035). The reverse also happens: rows deleted and committed,
    their vectors removed only in memory, and the old vectors still on disk.

    Returns (files with a chunk that has no tier vector, number of surplus ids removed).
    The caller re-indexes the files. Only tier indexes are checked for missing ids:
    each chunk row has exactly one tier vector (ADR-031), while a summary vector exists
    only if summarization was on when the chunk was indexed.
    """
    rows = db._conn.execute(
        "SELECT c.scope, c.tier, f.path FROM chunks c JOIN files f ON f.id = c.file_id"
    ).fetchall()
    expected: dict[str, dict[int, str]] = {name: {} for name, _, _ in TIER_CONFIGS}
    for scope, tier_num, path in rows:
        name = TIER_NAME[tier_num]
        expected[name][stable_id(name, path, scope)] = path

    missing_files: set[str] = set()
    surplus: set[int] = set()
    for name, _, _ in TIER_CONFIGS:
        index = faiss_indexes.get(name)
        actual = _index_ids(index) if index is not None else None
        if actual is None:
            continue
        missing_files.update(p for sid, p in expected[name].items() if sid not in actual)
        surplus.update(actual - expected[name].keys())

    summary_index = faiss_indexes.get(SUMMARY_INDEX)
    summary_ids = _index_ids(summary_index) if summary_index is not None else None
    if summary_ids:
        all_expected = set().union(*(e.keys() for e in expected.values()))
        surplus.update(summary_ids - all_expected)

    n_removed = 0
    if surplus:
        n_removed = purge_stale_vectors(faiss_indexes, doc_store, to_faiss_ids(sorted(surplus)))
    return sorted(missing_files), n_removed


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

# ─────────────────────────────────────────────────────────────────────────────
# Chunking — single source of truth for both passes
# ─────────────────────────────────────────────────────────────────────────────
def chunk_all_tiers(rel_path: str, content: str) -> dict[str, list]:
    """Produce the three-tier chunk sets for one file.

    Both ingest_file() and run_summarization_pass() call this, and that is a
    correctness requirement rather than a convenience. The summary cache is
    keyed by an md5 of the chunk text, so if the two passes chunked even
    slightly differently, every lookup in the embedding pass would miss, the
    LLM would reload, and both models would be resident at once — precisely the
    failure the two-pass split exists to prevent.
    """
    tier_chunks: dict[str, list] = {}
    for tier_name, max_tokens, overlap in TIER_CONFIGS:
        if tier_name == "tier1_surgical":
            tier_chunks[tier_name] = chunk_file_ast(rel_path, content, max_tokens, overlap)
        else:
            tier_chunks[tier_name] = fallback_token_chunker(
                content, rel_path, max_tokens, overlap, parent_scope="Full File"
            )
    # B-028: two symbols can share a scope (a getter/setter pair, an overload set, a
    # redeclared test helper). They share a stable id, and SQLite keeps only the last
    # row per (file, scope, tier), so embedding both would leave a FAISS vector whose
    # text belongs to the other symbol.
    for tier_name, chunks in tier_chunks.items():
        kept = dedupe_chunks_by_scope(chunks)
        if len(kept) != len(chunks):
            print(f"  [chunk:{rel_path}] {tier_name}: dropped "
                  f"{len(chunks) - len(kept)} chunk(s) with a duplicate scope", flush=True)
            tier_chunks[tier_name] = kept
    return tier_chunks


def chunk_text_hash(text: str) -> str:
    """Cache key for one chunk's summary. Must agree across both passes. Line numbers
    are not part of it (ADR-045): see db.summary_cache_key."""
    return summary_cache_key(text)


def dedupe_chunks_by_scope(chunks: list) -> list:
    """Keep one chunk per scope: the last, which is the row `INSERT OR REPLACE` keeps.

    The kept chunk stays where the last occurrence was, so order otherwise follows
    the chunker's.
    """
    last = {chunk.scope: i for i, chunk in enumerate(chunks)}
    return [chunk for i, chunk in enumerate(chunks) if last[chunk.scope] == i]


class _FilePlan:
    """One file's parsed, chunked and (if applicable) summarized state, waiting on
    embedding vectors.

    B-050 (ADR-040): pass 2 used to call `embed_batch` once per (file, tier) — plus
    once per tier for summaries — so the GPU never saw a batch bigger than one
    file's smallest tier, mostly 1-4 texts. Splitting `ingest_file` into "prepare"
    (this), "embed" and "write" (`_write_plan`) lets `run_incremental`'s pass 2 embed
    a whole window of files in one call. `ingest_file` still runs all three steps
    back to back for exactly one file — unchanged for a single-file watchdog run and
    for the tests that call it directly.
    """

    __slots__ = (
        "rel_path", "content_hash", "content_changed_at", "authored_at",
        "symbols", "edges", "references", "symbol_types",
        "tier_chunks", "tier_ids", "tier_texts", "summary_items",
        "tier_vectors", "summary_vectors",
    )

    def __init__(
        self,
        rel_path: str,
        content_hash: str,
        content_changed_at: Optional[str],
        authored_at: Optional[str],
        symbols: list,
        edges: list,
        references: list,
        symbol_types: list,
        tier_chunks: dict[str, list],
        tier_ids: dict[str, list[int]],
        tier_texts: dict[str, list[str]],
        summary_items: dict[str, list[tuple[int, str]]],
    ) -> None:
        self.rel_path = rel_path
        self.content_hash = content_hash
        self.content_changed_at = content_changed_at
        self.authored_at = authored_at
        self.symbols = symbols
        self.edges = edges
        self.references = references
        self.symbol_types = symbol_types
        self.tier_chunks = tier_chunks
        self.tier_ids = tier_ids
        self.tier_texts = tier_texts
        self.summary_items = summary_items
        self.tier_vectors: dict[str, np.ndarray] = {}
        self.summary_vectors: dict[str, np.ndarray] = {}

    def pending_texts(self) -> list[str]:
        """Every text still needing a vector, in the fixed order `scatter` expects
        back: each tier's code texts (`TIER_CONFIGS` order), then each tier's
        summary texts (same order)."""
        texts: list[str] = []
        for tier_name, _, _ in TIER_CONFIGS:
            texts.extend(self.tier_texts.get(tier_name, ()))
        for tier_name, _, _ in TIER_CONFIGS:
            texts.extend(s for _fid, s in self.summary_items.get(tier_name, ()))
        return texts

    def scatter(self, vectors: np.ndarray) -> None:
        """Split `vectors` — already normalized and aligned to `pending_texts()` —
        into this file's per-tier code and summary vectors."""
        offset = 0
        for tier_name, _, _ in TIER_CONFIGS:
            n = len(self.tier_texts.get(tier_name, ()))
            if n:
                self.tier_vectors[tier_name] = vectors[offset:offset + n]
            offset += n
        for tier_name, _, _ in TIER_CONFIGS:
            items = self.summary_items.get(tier_name, ())
            n = len(items)
            if n:
                self.summary_vectors[tier_name] = vectors[offset:offset + n]
            offset += n


class _Done:
    """An already-finished result with a Future's ``result()``, for the no-overlap path."""

    __slots__ = ("_value",)

    def __init__(self, value) -> None:
        self._value = value

    def result(self):
        return self._value


def _embed_texts(texts: list[str]) -> np.ndarray:
    """Embed `texts` through the model host when enabled, else in-process
    (ADR-028) — the one import/call point `ingest_file` and B-050's cross-file
    batching in `run_incremental` share."""
    from model_client import embed_batch   # ADR-028: host when enabled, else core
    return embed_batch(texts)


def _prepare_file(
    rel_path:      str,
    content:       str,
    content_hash:  str,
    doc_store:     DocumentStore,
    db:            CodeDB,
    resolver:      Optional[ImportResolver] = None,
    summarizer:    Optional[object] = None,
    content_changed_at: Optional[str] = None,
    authored_at:        Optional[str] = None,
) -> _FilePlan:
    """Everything `ingest_file` did before it touched the embedder: AST parse,
    three-tier chunking, import-edge resolution, and summary-cache lookups
    (calling `summarizer` for any miss, exactly as before). No embedding, no
    FAISS, no SQLite — those happen once the returned plan has vectors
    (see `_write_plan`).

    The DocumentStore text/tag cache is populated here, same as it always was:
    it does not depend on the embedding and MCP reads should see it as soon as
    the chunk exists.
    """
    print(f"  [ingest:{rel_path}] chunking...", flush=True)
    tier_chunks: dict[str, list] = chunk_all_tiers(rel_path, content)
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
        # ADR-044 §2: TS/JS imports are classified here, Python's in resolve_python_imports.
        is_web = os.path.splitext(rel_path)[1].lower() in ImportResolver.EXTENSIONS
        for edge in edges:
            if edge.kind == "import":
                resolved = resolver.resolve(edge.target, rel_path)
                if resolved:
                    edge.resolved_target = resolved
                if is_web:
                    edge.external = resolver.classify(edge.target, resolved)

    tier_ids:      dict[str, list[int]] = {}
    tier_texts:    dict[str, list[str]] = {}
    summary_items: dict[str, list[tuple[int, str]]] = {}

    for tier_name, chunks in tier_chunks.items():
        ids: list[int] = []
        texts: list[str] = []

        for chunk in chunks:
            fid = stable_id(tier_name, rel_path, chunk.scope)
            ids.append(fid)
            texts.append(chunk.text)

            # Keep the DocumentStore cache in sync for MCP server reads
            doc_store.add(fid, {
                "tier":  tier_name,
                "file":  rel_path,
                "scope": chunk.scope,
                "text":  chunk.text,
                "tags":  chunk.tags,
            })

        tier_ids[tier_name] = ids
        tier_texts[tier_name] = texts

        if not texts:
            print(f"  [ingest:{rel_path}] {tier_name}: no chunks, skipping", flush=True)
            continue

        # ADR-030: code vectors are code only. Summaries get their own vectors in
        # the summary index, under the chunk's id, added by _write_plan.
        if summarizer is not None and TIER_NUM[tier_name] in summarizer_tiers():
            print(f"  [ingest:{rel_path}] {tier_name}: summarizing {len(texts)} chunks...", flush=True)
            text_hashes = [chunk_text_hash(t) for t in texts]
            cached = db.get_cached_summaries(text_hashes)

            uncached_idx = [i for i, h in enumerate(text_hashes) if h not in cached]
            print(f"  [ingest:{rel_path}] {tier_name}: {len(uncached_idx)} cache misses → LLM", flush=True)
            if uncached_idx:
                new_summaries = summarizer.summarize_batch(
                    [texts[i] for i in uncached_idx]
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

            pairs: list[tuple[int, str]] = []
            for fid, h in zip(ids, text_hashes):
                summary = cached.get(h, "")
                if summary:
                    pairs.append((fid, summary))
                    entry = doc_store.get(fid)
                    if entry is not None:
                        entry["summary"] = summary
            if pairs:
                summary_items[tier_name] = pairs

    return _FilePlan(
        rel_path=rel_path,
        content_hash=content_hash,
        content_changed_at=content_changed_at,
        authored_at=authored_at,
        symbols=symbols,
        edges=edges,
        references=references,
        symbol_types=symbol_types,
        tier_chunks=tier_chunks,
        tier_ids=tier_ids,
        tier_texts=tier_texts,
        summary_items=summary_items,
    )


def _write_plan(
    plan:          _FilePlan,
    faiss_indexes: dict[str, faiss.Index],
    doc_store:     DocumentStore,
    db:            CodeDB,
) -> dict[str, int]:
    """FAISS `add_with_ids` for every tier (and the summary index), then the
    SQLite upsert — the write half of `ingest_file`, run once `plan.scatter()` has
    given it vectors. This is the per-file commit point: a chunk row is written
    only after that file's vectors are in the in-memory FAISS index, exactly as
    `ingest_file` always did it, so a kill between two files' writes leaves the
    same partial state ADR-037's `reconcile_vectors` already heals on the next run.
    """
    for tier_name, _, _ in TIER_CONFIGS:
        ids = plan.tier_ids.get(tier_name)
        if not ids:
            continue

        id_array = to_faiss_ids(ids)
        faiss_indexes[tier_name].add_with_ids(plan.tier_vectors[tier_name], id_array)
        print(f"  [ingest:{plan.rel_path}] {tier_name}: FAISS add done", flush=True)

        items = plan.summary_items.get(tier_name)
        summary_idx = faiss_indexes.get(SUMMARY_INDEX)
        if items and summary_idx is not None:
            summary_idx.add_with_ids(
                plan.summary_vectors[tier_name], to_faiss_ids([fid for fid, _s in items])
            )
            print(f"  [ingest:{plan.rel_path}] {tier_name}: {len(items)} summary vectors added", flush=True)

    print(f"  [ingest:{plan.rel_path}] writing SQLite...", flush=True)
    db.upsert_file(
        path=plan.rel_path,
        content_hash=plan.content_hash,
        symbols=plan.symbols,
        edges=plan.edges,
        chunks_by_tier={
            TIER_NUM[tier_name]: chunks
            for tier_name, chunks in plan.tier_chunks.items()
        },
        references=plan.references,
        symbol_types=plan.symbol_types,
        content_changed_at=plan.content_changed_at,
        authored_at=plan.authored_at,
    )
    print(f"  [ingest:{plan.rel_path}] SQLite done", flush=True)

    return {name: len(cks) for name, cks in plan.tier_chunks.items()}


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

    This is `_prepare_file` → one `embed_batch` call for everything this file
    needs → `_write_plan`, for exactly one file, synchronously — the single-file
    reference path. B-050's cross-file batching in `run_incremental`'s pass 2 calls
    those same three pieces directly instead, so one `embed_batch` call can cover
    a whole window of files rather than one file's one tier; a single-file
    watchdog reindex still runs this function.
    """
    plan = _prepare_file(
        rel_path, content, content_hash, doc_store, db,
        resolver=resolver, summarizer=summarizer,
        content_changed_at=content_changed_at, authored_at=authored_at,
    )

    texts = plan.pending_texts()
    if texts:
        print(f"  [ingest:{rel_path}] embedding {len(texts)} text(s)...", flush=True)
        vectors = _embed_texts(texts)
        faiss.normalize_L2(vectors)
        print(f"  [ingest:{rel_path}] embedding done, shape={vectors.shape}", flush=True)
        plan.scatter(vectors)

    return _write_plan(plan, faiss_indexes, doc_store, db)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class _CacheOnlySummarizer:
    """Stand-in for the summarizer during the embedding pass.

    Presents the same duck-type but never starts a worker process, so the LLM
    cannot become resident while the embedding model is loaded. A chunk the
    pre-pass failed to cache yields an empty summary, which ingest_file already
    handles by embedding the raw chunk text. Misses are counted rather than
    ignored: a non-zero count means the two passes disagreed about chunking,
    which is the one way this design can silently degrade.
    """

    def __init__(self) -> None:
        self.misses = 0

    def summarize_batch(self, codes: list[str]) -> list[str]:
        self.misses += len(codes)
        return [""] * len(codes)


# Chunks per summarize_batch call in pass 1, and per cache write.
_SUMMARY_SLICE = 192

# B-050 (ADR-040): pass 2's embedding window, in pending texts across however many
# queued files it takes to reach it. 8x the embedder's own internal GPU batch size
# (32) — big enough to amortize the call/RPC overhead well past one small file,
# small enough that a huge repo never holds more than a window's worth of chunk
# text and vectors in memory at once. The window also flushes once at the end of
# `to_index`, whatever is left in it.
_EMBED_WINDOW_TEXTS = 256


def _hms(now: Optional[datetime] = None) -> str:
    """Local HH:MM:SS, used to stamp progress lines and phase banners (B-049)."""
    return (now or datetime.now()).strftime("%H:%M:%S")


def _format_summary_progress(
    done: int,
    total: int,
    slice_count: int,
    slice_seconds: float,
    now: datetime,
) -> str:
    """Render a ``[summarize N/M · HH:MM:SS · R/min last slice · ETA ~HH:MM]`` line (B-049).

    The rate comes from the slice that just finished, not the average since the run started:
    pass 1 processes texts longest-first, so throughput climbs roughly 3-11x over a run, and an
    average-since-start rate underestimates how close the run is to done. ``now`` and the elapsed
    slice time are passed in rather than read from the clock here, so the ETA math is testable
    without sleeping or a real clock.
    """
    rate_per_min = (slice_count / slice_seconds * 60.0) if slice_seconds > 0 else 0.0
    remaining = max(total - done, 0)
    if rate_per_min > 0:
        eta = (now + timedelta(minutes=remaining / rate_per_min)).strftime("%H:%M")
    else:
        eta = "?"
    return (f"  [summarize {done}/{total} · {now.strftime('%H:%M:%S')} · "
            f"{rate_per_min:.0f}/min last slice · ETA ~{eta}]")


def run_summarization_pass(
    to_index:   list[str],
    repo_path:  str,
    db:         CodeDB,
    summarizer: object,
    source:     Optional[object] = None,
) -> int:
    """Summarize every chunk of every file before any embedding begins.

    Returns how many distinct texts came back without a summary, so the run's last
    line can say so (B-044): a summarizer that runs out of GPU memory returns empty
    strings, and the run used to end "Done successfully" all the same.

    The summarizer (Qwen2.5-Coder-1.5B) and the embedder (bge-code-v1) do not
    fit on an 8 GB card together. The main loop interleaves them per file and
    per tier — roughly 350 alternations over a 118-file repository — so both end
    up resident. Windows does not raise an out-of-memory error in that state:
    the display driver pages GPU memory back through system RAM, and throughput
    collapses by roughly fifty times with nothing in the log to show for it.
    Measured on this repository, 77 chunks took 18 minutes instead of seconds,
    with 4260 MiB spilled to system RAM.

    Summaries persist in chunk_summaries keyed by content hash, so filling that
    cache up front lets the embedding pass run with the LLM unloaded entirely.
    Peak GPU memory becomes max(summarizer, embedder) instead of their sum.

    Unloading between every tier instead would mean reloading a 3 GB model on
    each alternation, which costs far more than the thrashing it avoids.
    """
    print(f"[{_hms()}] ━━ Pass 1 of 2: summarization (embedding model not loaded) ━━", flush=True)
    # ADR-027: collect every uncached chunk in the repository first, then
    # summarize them together, longest first. Calling the summarizer per file
    # and tier handed it ~4 chunks at a time, so the batch never grew past its
    # starting size and each batch mixed short and long chunks.
    total_chunks = 0
    pending: dict[str, str] = {}          # hash -> text; one entry per distinct text
    source = source or WorkingTreeSource(repo_path)
    for n, rel_path in enumerate(to_index, 1):
        ext = Path(rel_path).suffix.lower()
        if ext in PROJECT_EXTS or Path(rel_path).name in PROJECT_FILES:
            continue                      # descriptor files carry edges, never chunks
        try:
            content = source.read(rel_path)
        except OSError:
            continue                      # pass 2 reports the read failure properly
        for tier_name, chunks in chunk_all_tiers(rel_path, content).items():
            texts = [c.text for c in chunks]
            if not texts or TIER_NUM[tier_name] not in summarizer_tiers():
                continue
            total_chunks += len(texts)
            hashes = [chunk_text_hash(t) for t in texts]
            cached = db.get_cached_summaries(hashes)
            for h, t in zip(hashes, texts):
                if h not in cached:
                    pending.setdefault(h, t)
    print(f"  [summarize] {total_chunks} chunks in {len(to_index)} files, "
          f"{len(pending)} distinct texts to summarize", flush=True)

    # Written to the cache one slice at a time, so a crash keeps what was done.
    todo = sorted(pending.items(), key=lambda kv: len(kv[1]), reverse=True)
    total_new = 0
    for start in range(0, len(todo), _SUMMARY_SLICE):
        part = todo[start:start + _SUMMARY_SLICE]
        _slice_started = time.monotonic()
        summaries = summarizer.summarize_batch([t for _h, t in part])
        pairs = [(h, sm) for (h, _t), sm in zip(part, summaries) if sm]
        db.cache_summaries(pairs)
        total_new += len(pairs)
        _slice_elapsed = time.monotonic() - _slice_started
        print(_format_summary_progress(start + len(part), len(todo), len(part),
                                       _slice_elapsed, datetime.now()), flush=True)
    print(f"  Pass 1 done: {total_chunks} chunks seen, {total_new} newly summarized",
          flush=True)
    # ADR-027: empty summaries leave no cache row, so without this line a partly
    # failed pass is only visible by counting chunk_summaries afterward.
    if hasattr(summarizer, "stats_line"):
        print(f"  Pass 1 summarizer: {summarizer.stats_line()}", flush=True)
    missing = len(todo) - total_new
    if missing:
        print(f"  WARNING: {missing} of {len(todo)} texts got no summary.", flush=True)
    return missing


def run_incremental(
    repo_path: str = REPO_PATH,
    *,
    prune: bool = False,
    interactive: bool = True,
) -> None:
    """Run one indexing pass holding the index's write.lock (ADR-038, B-033).

    Raises `index_lock.IndexBusy` at once if another process is writing this index:
    two writers used to overwrite each other's FAISS saves. See `_run_incremental`.
    """
    from index_lock import WRITE_LOCK, acquire
    with acquire(INDEX_DIR, WRITE_LOCK, "build"):
        source = make_source(repo_path)     # ADR-042: the folder, or one commit's tree
        try:
            _run_incremental(repo_path, prune=prune, interactive=interactive, source=source)
        finally:
            source.close()


def _run_incremental(
    repo_path: str,
    *,
    prune: bool,
    interactive: bool,
    source=None,
) -> None:
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

    `prune` answers the ADR-026 §5 bulk-deletion prompt in advance; `interactive`
    is False for callers with nobody watching (the watchdog, the `reindex` MCP
    tool), which log and skip the bulk purge rather than block on a prompt.
    """
    source = source or WorkingTreeSource(repo_path)
    print(f"━━ Incremental Indexer: {os.path.basename(repo_path)} ━━")

    index_manager = MultiIndexManager(INDEX_DIR)
    doc_store     = DocumentStore(DB_PATH)
    db            = CodeDB(DB_PATH)

    summarizer = None
    if summarization_enabled():
        # ADR-028: the model host's summarizer when [model_host].enabled, else the worker.
        from model_client import make_summarizer
        summarizer = make_summarizer()
        print(f"  Chunk summarizer enabled: {summarizer_model_id()} "
              f"(worker process starts on first file processed)")
    else:
        print("  Chunk summarizer disabled ([summarization].enabled = false)")

    faiss_indexes: dict[str, faiss.Index] = {
        name: index_manager.load_or_create(name)
        for name, _, _ in TIER_CONFIGS
    }
    # ADR-030: summaries in their own index, under their chunk's id. Being in this
    # dict is what makes purge_stale_vectors and save_all cover it too.
    faiss_indexes[SUMMARY_INDEX] = index_manager.load_or_create(SUMMARY_INDEX)

    # ADR-030 §5: an index built before this layout has summaries inside its code
    # vectors. It still works, but only a full re-index moves it over. A run that
    # starts from an empty index writes the new layout.
    _fresh_index = db._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    if not _fresh_index and db.meta_get("embed_layout") != EMBED_LAYOUT:
        print(f"  [index] This index was built before ADR-030, with summaries appended to "
              f"the code vectors. Delete {INDEX_DIR} and re-index to get the separate "
              f"summary index. Cached summaries are reused, so it costs an embed pass, "
              f"not a summarizer pass.")
    elif _fresh_index:
        db.meta_set("embed_layout", EMBED_LAYOUT)

    # ADR-042 §2: say so when the configured source is not the one this index was
    # built from. Every content hash differs between the two, so this run re-embeds
    # every file; summaries come from the cache.
    _recorded_source = db.meta_get("source")
    if not _fresh_index and (_recorded_source or "worktree") != source.label:
        print(f"  [index] This index was built from {_recorded_source or 'worktree'}; "
              f"[indexer].source is now {source.label}. Every file is re-embedded once "
              f"(summaries are cached).")

    print("Scanning files...")
    disk_hashes = source.list()
    diff        = compute_diff(db, disk_hashes)

    # ADR-026 §5: an ordinary run can irreversibly purge most of an index — that is
    # the same mechanism that makes the ignore-set migration free. Checked before any
    # mutation, so a "no" leaves the index exactly as it was.
    _n_indexed = db._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    diff, _guard_msg = bulk_deletion_verdict(
        diff, _n_indexed, prune=prune, interactive=interactive
    )
    if _guard_msg:
        print(_guard_msg)

    # ADR-037: a run killed before its FAISS save leaves rows with no vectors, which the
    # MD5 diff would skip forever, and vectors whose rows it had already deleted. Files
    # missing a vector are re-indexed as modified; surplus vectors are removed now.
    _missing, _n_surplus = reconcile_vectors(db, faiss_indexes, doc_store)
    _in_diff = set(diff.new) | set(diff.modified) | set(diff.deleted)
    _heal = [p for p in _missing if p not in _in_diff and p in disk_hashes]
    if _heal:
        diff = diff._replace(modified=diff.modified + _heal)
    if _heal or _n_surplus:
        print(f"  [reconcile] {len(_heal)} file(s) had chunks with no vector and will be "
              f"re-indexed; {_n_surplus} vector(s) with no chunk row removed.")

    # B-029: a fresh build (nothing indexed yet) is the only run that makes every
    # chunk come from this chunker, so it is the only run that records the version,
    # and only once it has finished. A build killed midway leaves no marker.
    _fresh_build = _n_indexed == 0
    if not _fresh_build:
        _version_warning = chunker_version_warning(db)
        if _version_warning:
            print(f"  {_version_warning}")

    # ADR-025 §2: one git pass up front. Reused for back-dating new files, dirty
    # detection, and the §1 one-time backfill of legacy NULL stamps. Done before the
    # no-op check so a quiet repo with legacy rows still gets its stamps backfilled.
    # ADR-042 §5: a commit has its own history and no uncommitted edits.
    _git_times = git_change_times(repo_path, source.commit)
    _dirty     = git_dirty_paths(repo_path) if source.commit is None else set()
    _run_now   = _now_iso()
    _n_backfilled = _backfill_null_stamps(db, _git_times)
    if _n_backfilled:
        print(f"  Backfilled content_changed_at for {_n_backfilled} legacy file(s).")

    n_changed = len(diff.new) + len(diff.modified) + len(diff.deleted)
    if n_changed == 0:
        if _n_surplus:
            index_manager.save_all()
        print("Nothing changed — index is up to date.")
        # ADR-025 §4: still record that we verified the index against this HEAD at
        # this time. The early return fires only AFTER scan_disk hashed every file
        # and found nothing changed, so "verified, nothing stale" is a true fact —
        # and recording it means a docs-only commit still advances last_indexed_commit
        # instead of leaving the staleness check firing on README.md forever.
        _write_index_meta(db, repo_path, source)
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

    # ADR-037: a healed file's content did not change, so it keeps its ADR-025 stamps.
    _healed_stamps: dict[str, tuple[Optional[str], Optional[str]]] = {}
    if _heal:
        _ph = ",".join("?" * len(_heal))
        _healed_stamps = {
            p: (cc, au) for p, cc, au in db._conn.execute(
                f"SELECT path, content_changed_at, authored_at FROM files WHERE path IN ({_ph})",
                _heal,
            )
        }

    # ADR-042: switching [indexer].source switches the hash kind (an MD5 of the
    # folder's file vs git's blob id), so every file compares "modified" without its
    # content having changed. A different-kind hash says nothing about when the
    # content changed, so those files are stamped as on a first index (git history),
    # not now(): otherwise the switch reads as every file changing at once.
    _rehashed: set[str] = set()
    if diff.modified:
        _stored = dict(db._conn.execute("SELECT path, content_hash FROM files"))
        _rehashed = {p for p in diff.modified
                     if p in _stored and len(_stored[p]) != len(disk_hashes[p])}

    for path in stale_paths:
        db.delete_file(path)

    to_index = diff.new + diff.modified
    if to_index:
        print(f"Indexing {len(to_index)} file(s)...")
    # ADR-042 §7: a commit's imports resolve against that commit. The folder keeps
    # the resolver's own file access, unchanged.
    resolver = ImportResolver(repo_path, source=source if source.commit else None)

    # ADR-025 §2: resolve each file's content_changed_at / authored_at from the git
    # pass computed above. New files back-date to real change history; modified files
    # (content just changed since last index) stamp now(); dirty and history-less
    # files never claim a time they didn't have.
    _new_set = set(diff.new)

    def _content_stamp(rel: str) -> tuple[Optional[str], Optional[str]]:
        if rel in _healed_stamps:
            return _healed_stamps[rel]
        committer, author = _git_times.get(rel, (None, None))
        if rel in _new_set or rel in _rehashed:
            # First index of this path (or first under this hash kind) → back-date.
            # Rules, in order:
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

    # Two-pass split: summarize everything, release the LLM, then embed. See
    # run_summarization_pass() for why interleaving the two models does not fit.
    unsummarized = 0
    if summarizer is not None:
        unsummarized = run_summarization_pass(to_index, repo_path, db, summarizer, source)
        summarizer.shutdown()          # child process exits; its GPU memory returns
        summarizer = _CacheOnlySummarizer()
        print(f"[{_hms()}] ━━ Pass 2 of 2: embedding (summarizer unloaded) ━━", flush=True)

    # B-050 (ADR-040): collect a window of prepared files and embed their pending
    # texts in one call, instead of once per (file, tier). `pending_plans` and
    # `pending_counts` stay parallel: `pending_counts[i]` is how many of
    # `pending_texts`' entries — starting at the running offset — belong to
    # `pending_plans[i]`. `_flush_pending` embeds whatever is queued, slices the
    # result back to each plan, and writes each file in the order it was queued —
    # the same order and the same per-file commit point `ingest_file` always used,
    # so a kill between two files' writes is healed by reconcile_vectors exactly as
    # before (see ADR-040).
    #
    # B-055: with [indexer] embed_overlap on, a full window's embed call runs on one
    # background thread while this thread goes on chunking and parsing the next
    # window's files, so the GPU no longer idles through the CPU work. Only the model
    # call moves: every FAISS and SQLite write stays on this thread, in queue order,
    # and window k is written before window k+1, so kill-safety is unchanged.
    errors = 0
    pending_plans:  list[_FilePlan] = []
    pending_counts: list[int] = []
    pending_texts:  list[str] = []
    embedder = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
                if embed_overlap() else None)
    inflight: Optional[tuple] = None        # the window whose vectors are on their way
    timing = {"embed": 0.0, "wait": 0.0, "prep": 0.0, "write": 0.0, "calls": 0, "texts": 0}
    pass2_start = time.monotonic()

    def _embed_timed(texts: list[str]) -> tuple[np.ndarray, float]:
        t0 = time.monotonic()
        vectors = _embed_texts(texts)
        faiss.normalize_L2(vectors)
        return vectors, time.monotonic() - t0

    def _write_window(job: tuple) -> None:
        nonlocal errors
        future, plans, counts_by_plan = job
        vectors: Optional[np.ndarray] = None
        if future is not None:
            t0 = time.monotonic()
            vectors, took = future.result()
            timing["wait"] += time.monotonic() - t0
            timing["embed"] += took
            timing["calls"] += 1
            timing["texts"] += len(vectors)

        t0 = time.monotonic()
        offset = 0
        for plan, n in zip(plans, counts_by_plan):
            try:
                if n:
                    plan.scatter(vectors[offset:offset + n])
                counts = _write_plan(plan, faiss_indexes, doc_store, db)
                t1 = counts.get("tier1_surgical",       0)
                t2 = counts.get("tier2_component",       0)
                t3 = counts.get("tier3_architectural",   0)
                print(f"  ✓  {plan.rel_path}  (T1:{t1} | T2:{t2} | T3:{t3})")
            except Exception:
                errors += 1
                print(f"  ✗  {plan.rel_path}")
                traceback.print_exc()
            offset += n
        timing["write"] += time.monotonic() - t0

    def _flush_pending() -> None:
        nonlocal inflight
        if not pending_plans:
            return
        plans, counts_by_plan = list(pending_plans), list(pending_counts)
        texts = list(pending_texts)
        pending_plans.clear()
        pending_counts.clear()
        pending_texts.clear()
        future = None
        if texts:
            print(f"  [embed] batch: {len(texts)} text(s) across {len(plans)} file(s)",
                  flush=True)
            future = (embedder.submit(_embed_timed, texts) if embedder is not None
                      else _Done(_embed_timed(texts)))
        job = (future, plans, counts_by_plan)
        if embedder is None:
            _write_window(job)
            return
        # This window is queued behind the one in flight, so the GPU moves straight
        # on to it; the finished window is written while it runs.
        if inflight is not None:
            _write_window(inflight)
        inflight = job

    # The worker thread is shut down however the loop ends: a failed embed or write
    # must not leave a thread behind in a long-lived MCP server process.
    try:
        for rel_path in to_index:
            ext = Path(rel_path).suffix.lower()
            print(f"[loop] → {rel_path}", flush=True)
            try:
                print("[loop]   reading file...", flush=True)
                content = source.read(rel_path)
                print(f"[loop]   {len(content)} chars read", flush=True)

                _cc_at, _auth_at = _content_stamp(rel_path)

                if ext in PROJECT_EXTS or Path(rel_path).name in PROJECT_FILES:
                    ingest_project_file(
                        rel_path, content, disk_hashes[rel_path], db,
                        content_changed_at=_cc_at, authored_at=_auth_at,
                    )
                    continue

                print("[loop]   preparing (chunk/parse/summarize)...", flush=True)
                _t_prep = time.monotonic()
                plan = _prepare_file(
                    rel_path=rel_path,
                    content=content,
                    content_hash=disk_hashes[rel_path],
                    doc_store=doc_store,
                    db=db,
                    resolver=resolver,
                    summarizer=summarizer,
                    content_changed_at=_cc_at,
                    authored_at=_auth_at,
                )
                timing["prep"] += time.monotonic() - _t_prep
                texts = plan.pending_texts()
                pending_plans.append(plan)
                pending_counts.append(len(texts))
                pending_texts.extend(texts)
                print("[loop]   queued for batched embedding", flush=True)

            except Exception:
                errors += 1
                print(f"  ✗  {rel_path}")
                traceback.print_exc()
                continue

            if len(pending_texts) >= _EMBED_WINDOW_TEXTS:
                _flush_pending()

        _flush_pending()
        if inflight is not None:
            _write_window(inflight)
            inflight = None
    finally:
        if embedder is not None:
            embedder.shutdown(wait=True, cancel_futures=True)
    if timing["calls"]:
        # B-050/B-055 timing: embed = time inside the model calls; waited = time this
        # thread sat idle for vectors. Without overlap the two are equal; with it,
        # embed - waited is the embedding hidden behind chunking and parsing.
        print(f"[{_hms()}] Pass 2 timing: {timing['texts']} text(s) in {timing['calls']} "
              f"embed call(s): embed {timing['embed']:.1f}s, waited {timing['wait']:.1f}s, "
              f"prepare {timing['prep']:.1f}s, write {timing['write']:.1f}s, "
              f"wall {time.monotonic() - pass2_start:.1f}s "
              f"(overlap {'on' if embedder is not None else 'off'})", flush=True)

    # ADR-021: resolve CALLS-edge bare callee names to in-repo FQNs so the graph
    # Traverse step has real neighbours to walk. Runs once here, over the now-complete
    # symbols table; precision-first (only provably-unique targets), recomputes every
    # run so a name that became ambiguous is demoted back to unresolved.
    if isinstance(summarizer, _CacheOnlySummarizer) and summarizer.misses:
        print(f"  WARNING: {summarizer.misses} chunks missed the summary cache in pass 2 "
              "— the two passes disagree about chunking, and those chunks were "
              "embedded without a summary.")

    # ADR-044: Python imports resolve against the whole file list as it is now, so an
    # import un-resolves when its target is deleted. Before calls: the import-scoped
    # strategy reads these.
    imp = resolve_python_imports(db)
    print(f"  Python imports: {imp['resolved']} resolved | {imp['unresolved']} unresolved "
          f"({imp['external']} external)")
    res = resolve_call_edges(db)
    print(f"  Call resolution: {res['resolved']} resolved | "
          f"{res['typed']} typed | {res['bound_scoped']} import-bound | "
          f"{res['ambiguous']} ambiguous | {res['external']} external | "
          f"{res['bound_external']} into dependencies")

    # Flush FAISS indexes to disk.
    # Chunk payloads are already in SQLite (committed per-file by upsert_file).
    print(f"[{_hms()}] Saving indexes...")
    index_manager.save_all()

    # ADR-025 §4/§5: record run-level freshness facts from this one chokepoint that
    # all four triggers (CLI, MCP reindex, watchdog, startup) share, so they agree.
    _write_index_meta(db, repo_path, source)
    if _fresh_build:
        # Files that failed above are retried on the next run with this same chunker,
        # so the index is still one generation.
        db.meta_set(CHUNKER_VERSION_KEY, str(CHUNKER_VERSION))
    db.close()

    with CodeDB(DB_PATH) as verify_db:
        s = verify_db.stats()

    status = "with errors" if errors else "with warnings" if unsummarized else "successfully"
    print(
        f"[{_hms()}] Done {status}.  "
        f"files={s['files']}  symbols={s['symbols']}  "
        f"chunks={s['chunks']}  edges={s['edges']}"
    )
    if errors:
        print(f"  {errors} file(s) failed to index — check output above.")
    if unsummarized:
        # Pass 1 only visits changed files, so these stay unsummarized until their
        # file changes again or a full reindex runs.
        print(f"  {unsummarized} chunk text(s) have no summary — see 'Pass 1 summarizer' above "
              f"(oom = the GPU was full). Searchable, but without their summaries until the "
              f"file changes again or reindex(changed_files_only=False) runs.")


def main() -> None:
    # ADR-043 (B-008): first, before anything prints. A redirected or piped stdout on
    # Windows is cp1252, and the "━━" banner killed every captured run on its first line.
    from utf8_stdio import utf8_stdio
    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="code-indexer",
        description="Incrementally index this repository (see indexer.toml).",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        # ASCII only (ADR-026 §4). ADR-043 fixed B-008's cp1252 crash; ASCII help
        # text stays harmless either way.
        help="apply bulk deletions without asking. Above max(50, 20%%) of the index, "
             "deletions are confirmed first; this answers yes in advance.",
    )
    parser.add_argument(
        "--allow-worktree",
        action="store_true",
        help="index this linked git worktree anyway (ADR-038). Prefer "
             "[indexer] allow_linked_worktree = true in its indexer.toml.",
    )
    args = parser.parse_args()
    from index_lock import IndexBusy, worktree_refusal
    refusal = None if args.allow_worktree else worktree_refusal(REPO_PATH)
    if refusal:
        print(f"Refused: {refusal}", file=sys.stderr)
        sys.exit(2)
    try:
        run_incremental(prune=args.prune)
    except IndexBusy as exc:
        print(f"Refused: another process is writing this index ({exc}). "
              f"Wait for it to finish, then run again.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
