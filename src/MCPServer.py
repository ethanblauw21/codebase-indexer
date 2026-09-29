import contextvars
import functools
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field

import anyio
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Index state (ADR-047)
# ---------------------------------------------------------------------------
# The loaded index is ONE immutable IndexState, swapped as a single reference
# under _reload_lock. Every tool call binds the state current at its entry
# (_bind_index) and reads only that one for the rest of the call, so a watchdog
# or ref-poller swap mid-call can't mix generations (#52): the call finishes
# against the state it started with, and the next call sees the new one.
_reload_lock = threading.Lock()

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
from core import jina_tokenizer, DocumentStore
from hybrid_retriever import HybridRetriever, RetrievedChunk
from iterative_retriever import IterativeRetriever, RetrievalSession

def _server_instructions(repo_path: str | None = None) -> str:
    """What every connected agent is told about this index (ADR-038, B-053).

    Names the folder and the commit the index was built from, read from index_meta,
    so an agent working on another branch or worktree knows which hits to distrust.
    Kept short: every session pays for it.
    """
    from index_location import git_ref, index_dir
    repo_path = repo_path or os.getcwd()
    commit = verified = None
    try:
        ref = git_ref(repo_path)
        db_path = os.path.join(repo_path, index_dir(), "graph.db")
    except ValueError:
        ref, db_path = None, os.path.join(repo_path, ".code-index", "graph.db")
    if os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                meta = dict(con.execute(
                    "SELECT key, value FROM index_meta "
                    "WHERE key IN ('last_indexed_commit', 'last_verified_at')").fetchall())
            finally:
                con.close()
            commit = meta.get("last_indexed_commit")
            verified = meta.get("last_verified_at")
        except sqlite3.Error:
            pass
    built = (f"commit {commit[:10]}, last verified {verified}" if commit
             else "no finished build yet")
    if ref is not None:
        # ADR-042 §5: the index is a commit, read from git, not any folder.
        from index_location import ref_display
        return (
            f"This code index reflects {ref_display(repo_path, ref)} ({built}), read straight from git: not this "
            "folder's working tree, and not your branch. For any file your branch changed "
            f"(git diff --name-only {commit[:10] if commit else '<that commit>'}...HEAD, plus "
            "uncommitted edits), Read the file itself: index hits for it show the indexed "
            "version, and line numbers may be off. Use the index for how the rest of the "
            f"codebase works. It follows {ref} on its own when that ref moves; do not call "
            "`reindex` to pick up branch work. `index_status` reports freshness."
        )
    return (
        f"This code index reflects ONE checkout: {repo_path} ({built}). "
        "It does not see edits in other branches or worktrees. For any file your branch "
        "changed (git diff --name-only <that commit>...HEAD, plus uncommitted edits), Read "
        "the file itself: index hits for it show the indexed version, and line numbers may "
        "be off. Use the index for how the rest of the codebase works. "
        "Never call `reindex` from a git worktree made for separate or parallel work; it "
        "refuses there, and one server per index keeps it current on its own. "
        "`index_status` reports freshness."
    )


# Initialize MCP Server
mcp = FastMCP("Local Codebase RAG", instructions=_server_instructions())

@dataclass(frozen=True, eq=False)
class IndexState:
    """One loaded generation of the index: everything a tool call reads.

    The retriever owns the only in-memory copy of the chunks (its DocumentStore)
    and of the FAISS indexes. The server used to load a second copy of both for
    its own scans and counts (#63).
    """
    retriever: "HybridRetriever"
    stamp: tuple                 # _faiss_stamp() taken before this state was loaded
    generation: int
    _iterative: list = field(default_factory=list, repr=False)
    _iterative_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def doc_store(self) -> "DocumentStore":
        return self.retriever._doc_store

    @property
    def db(self):                # the retriever's CodeDB
        return self.retriever._db

    @property
    def tiers(self) -> tuple:
        r = self.retriever
        return (r._tier1, r._tier2, r._tier3)

    def iterative(self) -> "IterativeRetriever":
        """The iterative retriever over this generation, built on first use."""
        with self._iterative_lock:
            if not self._iterative:
                self._iterative.append(IterativeRetriever(self.retriever, self.retriever._db))
            return self._iterative[0]


# Lazy: loaded on the first tool call, so the MCP handshake completes at once
# even when the FAISS indexes are large.
_state: IndexState | None = None
_generation = 0
_load_lock = threading.Lock()        # one initial load, even if two calls race to it
# The state a tool call bound at entry (ADR-047). Unset outside a tool call.
_bound: contextvars.ContextVar["IndexState | None"] = contextvars.ContextVar(
    "index_state", default=None)


def _faiss_stamp(index_dir: str | None = None) -> tuple:
    """(name, mtime_ns, size) of every saved FAISS index: changes whenever any process saves."""
    if index_dir is None:
        from index_location import index_dir as _index_dir   # ADR-042 §3
        index_dir = _index_dir()
    try:
        names = sorted(n for n in os.listdir(index_dir) if n.endswith(".faiss"))
    except OSError:
        return ()
    stamp = []
    for n in names:
        try:
            st = os.stat(os.path.join(index_dir, n))
        except OSError:
            continue
        stamp.append((n, st.st_mtime_ns, st.st_size))
    return tuple(stamp)


def _load_state() -> IndexState:
    """Build a new generation from disk (slow: reads FAISS files and SQLite)."""
    global _generation
    stamp = _faiss_stamp()      # taken first: a save during the load triggers one more reload
    retriever = HybridRetriever()
    with _reload_lock:
        _generation += 1
        return IndexState(retriever=retriever, stamp=stamp, generation=_generation)


def _ensure_indexes() -> IndexState:
    """The current state, loading it on first use and reloading it when another
    process has saved since it was loaded."""
    global _state
    st = _state
    if st is None:
        with _load_lock:
            if _state is None:
                _state = _load_state()
            return _state
    # ADR-038 (B-033 fix 3): another process (the watching server, or a terminal
    # build) saved since this server loaded; serve the new vectors, not the old.
    if _faiss_stamp() != st.stamp:
        print("[MCP] The index was saved by another process; reloading.")
        _reload_indexes()
    return _state


def _index() -> IndexState:
    """The state this tool call is reading: the one bound at its entry, or, outside
    a tool call, the current one."""
    return _bound.get() or _ensure_indexes()


def _bind_index(fn):
    """Run ``fn`` against one IndexState from start to finish (ADR-047, #52).

    Binds the current state at entry unless the caller already bound one, so a
    tool that calls another tool reads the same generation.
    """
    @functools.wraps(fn)
    def bound(*args, **kwargs):
        if _bound.get() is not None:
            return fn(*args, **kwargs)
        token = _bound.set(_ensure_indexes())
        try:
            return fn(*args, **kwargs)
        finally:
            _bound.reset(token)
    return bound


# Read tools run one at a time, off the event loop (ADR-047, #53). One at a time,
# as they always have: they share one SQLite connection and a few lazily built
# caches. `reindex` is outside this limiter, so a long rebuild doesn't hold up
# searches, which keep reading the state they bound until the swap.
_read_limiter: "anyio.CapacityLimiter | None" = None


def _tool(*, reads_index: bool = True):
    """Register ``fn`` as an MCP tool that runs in a worker thread.

    FastMCP calls a plain ``def`` tool directly on the event loop, so one slow
    call stalled every other request on the server (#53). The registered tool is
    an ``async`` wrapper that runs the function in a thread. A tool that reads the
    index is bound to one IndexState and takes the read limiter; `reindex`
    (``reads_index=False``) does neither: it would only load the index it is
    about to rebuild, and a rebuild must not hold up searches. The module keeps
    the plain function, so the TUI and the tests still call it directly.
    """
    def register(fn):
        bound = _bind_index(fn) if reads_index else fn

        @functools.wraps(fn)
        async def run_in_thread(*args, **kwargs):
            global _read_limiter
            call = functools.partial(bound, *args, **kwargs)
            if not reads_index:
                return await anyio.to_thread.run_sync(call)
            if _read_limiter is None:
                _read_limiter = anyio.CapacityLimiter(1)
            return await anyio.to_thread.run_sync(call, limiter=_read_limiter)

        mcp.tool()(run_in_thread)
        return bound
    return register

def get_clean_scope(doc):
    """
    AST-Style Scope Recovery: converts anonymous_part_X to a navigable label.
    Tries three strategies in order:
      1. Named declaration (function/class/const/interface/type)
      2. Arrow-function or variable assignment
      3. Filename fallback — always navigable, never misleading
    """
    scope = doc.get('scope', 'Unknown')
    if "anonymous_part" in scope.lower():
        named_parent = re.search(
            r'(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class)\s+([a-zA-Z_$][\w$]*)'
            r'|(?:export\s+)?(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*(?:=|:)'
            r'|(?:interface|type)\s+([a-zA-Z_$][\w$]*)',
            doc['text']
        )
        if named_parent:
            name = named_parent.group(1) or named_parent.group(2) or named_parent.group(3)
            return f"Near: {name}"
        file_name = doc['file'].replace('\\', '/').split('/')[-1]
        file_base = re.sub(r'\.[^.]+$', '', file_name)
        return f"In: {file_base}"
    return scope

# --- THE SECRET SAUCE: The Docstring ---
# Claude Code and Continue.dev read this exact string.
# We must explicitly tell Claude WHY this is better than its native `grep`.
_CHUNK_LINES_RE = re.compile(r"^Lines: (\d+)-(\d+)$", re.MULTILINE)
_QUERY_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "does", "what", "where",
    "when", "which", "how", "are", "its", "not", "off", "out", "all", "any", "each",
})
_SNIPPET_LINES = 14          # lines of code shown per evidence block
_SNIPPET_LINE_CHARS = 200    # longer lines are cut, so one minified line cannot flood it


def _query_terms(query: str) -> set[str]:
    """Lower-case words of the query, identifiers split at camelCase and underscores."""
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query))
    return {w.lower() for w in words if len(w) >= 3 and w.lower() not in _QUERY_STOPWORDS}


def _line_score(line: str, terms: set[str]) -> int:
    low = line.lower()
    # A 5-letter stem catches "decrement" in "decremented", "writes" in "writer".
    return sum(1 for t in terms if t in low or (len(t) >= 6 and t[:5] in low))


def _focused_snippet(text: str, query: str) -> tuple[str, tuple[int, int] | None]:
    """The part of a chunk worth showing for this query, and its source line span.

    A preview of a chunk's first few hundred characters shows its header and signature,
    and the line that answers the question is usually further down. Instead: the first
    line of code (the signature), then the run of lines that mentions the most query
    terms. Tier-1 chunks record their source lines (``Lines: a-b``), so their lines are
    numbered as in the file; window chunks (tiers 2 and 3) have none, and neither does
    a class outline, whose elided bodies shift its lines; those are shown unnumbered.
    The span is the whole chunk's, for a targeted Read.
    """
    head, sep, code = text.partition("\nCode:\n")
    if not sep:
        head, code = "", text
    m = _CHUNK_LINES_RE.search(head)
    first = int(m.group(1)) if m and int(m.group(1)) > 0 else None
    span = (first, int(m.group(2))) if first else None

    src = code.strip("\n").split("\n")
    if span and len(src) != span[1] - span[0] + 1:
        first = None        # a class outline with elided bodies: the span holds, numbers drift
    if len(src) <= _SNIPPET_LINES:
        shown = list(range(len(src)))
    else:
        terms = _query_terms(query)
        scores = [_line_score(line, terms) for line in src]
        width = _SNIPPET_LINES - 1              # one line is kept for the signature
        best_start, best = 1, -1
        for start in range(1, len(src) - width + 1):
            total = sum(scores[start:start + width])
            if total > best:
                best_start, best = start, total
        if best <= 0:
            best_start = 1                      # no term found: show the opening lines
        shown = [0] + list(range(best_start, best_start + width))

    out: list[str] = []
    prev = -1
    for i in shown:
        if prev >= 0 and i != prev + 1:
            out.append("  ⋮")
        line = src[i].rstrip()
        if len(line) > _SNIPPET_LINE_CHARS:
            line = line[:_SNIPPET_LINE_CHARS] + " …"
        out.append(f"  {first + i:>5}  {line}" if first else f"  {line}")
        prev = i
    if shown and shown[-1] < len(src) - 1:
        out.append("  ⋮")
    return "\n".join(out), span


@_tool()
def semantic_code_search(query: str) -> str:
    """
    Find code by what it does: "where is X handled", "how does Y work", when you do not
    know the names to grep for. Returns whole ranked chunks with file and scope.
    For an exact identifier, string or config key, grep is faster and complete; this
    ranks, and can miss. Results show the indexed version of each file (index_status
    says which); Read a file before editing it or trusting a line of it.
    Prefer investigate_architecture for how a concept flows through the system; it
    runs this same retrieval and groups the results by role.
    """
    print(f"\n[MCP] Tool invoked by LLM for query: '{query}'")

    # Candidate generation via the shared RTR surface (ADR-023 §1): multi-tier RRF
    # plus resolved call-graph neighbours + import-corroboration, not FAISS-only.
    chunks = _search(query, top_n=15)

    # Context Packing (Protecting your 8GB Local Model's VRAM)
    # 8B models easily crash if you feed them more than 8k tokens.
    # We cap the returned context strictly at 4000 tokens to be safe.
    header = f"--- VECTOR DATABASE RESULTS FOR: '{query}' ---\n\n"
    # B-029: say so when the index predates the current chunker. A warning, not an
    # error: the results are still the best this index has.
    from incremental_indexer import chunker_version_warning
    version_warning = chunker_version_warning(_db())
    if version_warning:
        header = f"{version_warning}\n\n" + header
    return _pack_results(header, chunks, jina_tokenizer.count_tokens, max_tokens=4000)


def _pack_results(header: str, chunks, count_tokens, max_tokens: int) -> str:
    """Fill the token budget in rank order, skipping any chunk that does not fit.

    B-030: this used to stop at the first chunk that did not fit, so one large
    chunk hid every result ranked below it. Skipped chunks are listed by location
    so the caller can still open them.
    """
    context = header
    used = 0
    skipped: list[str] = []
    for c in chunks:
        chunk_text = f"--- FILE: {c.file} | SCOPE: {c.scope} ---\n{c.text}\n\n"
        tokens = count_tokens(chunk_text)
        if used + tokens < max_tokens:
            context += chunk_text
            used += tokens
        else:
            skipped.append(f"{c.file} | {c.scope}")
    if skipped:
        context += (f"[Note: {len(skipped)} result(s) did not fit the {max_tokens}-token "
                    "budget and were left out:]\n")
        context += "".join(f"  - {s}\n" for s in skipped)
    return context

@_tool()
def find_similar_code(code_snippet: str) -> str:
    """
    Use this tool to find duplicate or mathematically similar code across the project.
    Pass in a raw snippet of code. It will stratify results into Origin, Callers, Parallels, and Weak matches.
    """
    print("\n[MCP] Searching for duplicates/callers of provided snippet...")
    # Shared RTR surface (ADR-023 §1) instead of raw tier-1 FAISS; brings in
    # call-graph neighbours (real callers) the pure-similarity search missed.
    chunks = _search(code_snippet, top_n=15)

    seen_scopes = set()
    origin_data = []
    caller_data = []
    high_conf_data = []
    weak_conf_data = []

    norm_snippet = re.sub(r'\s+', '', code_snippet)

    symbol_match = re.search(r'(?:function|const|let|var|class)\s+([a-zA-Z_$][0-9a-zA-Z_$]*)', code_snippet)
    primary_symbol = symbol_match.group(1) if symbol_match else None

    GENERIC_API_TERMS = {
        'transaction', 'collection', 'doc', 'ref', 'update', 'set', 'get', 'delete',
        'void', 'entry', 'string', 'number', 'boolean', 'any', 'unknown', 'record',
        'promise', 'error', 'data', 'id', 'value', 'key', 'result'
    }
    STOPWORDS = {
        'const', 'let', 'var', 'function', 'return', 'import', 'export', 'async',
        'await', 'try', 'catch', 'if', 'else', 'console', 'true', 'false', 'null', 'undefined'
    }

    # --- 1. OPERATIONAL CONTEXT VERBS ---
    WRITE_VERBS = {'set', 'update', 'add', 'transaction', 'commit', 'write', 'delete', 'mutate'}
    READ_VERBS = {'get', 'fetch', 'query', 'where', 'onsnapshot', 'subscribe', 'use', 'read'}

    def get_domain_keywords(text):
        words = set(re.findall(r'[a-zA-Z_]\w{3,}', text))
        return {w for w in words if w.lower() not in GENERIC_API_TERMS and w.lower() not in STOPWORDS}

    def get_op_profile(text):
        text_lower = text.lower()
        has_write = any(v in text_lower for v in WRITE_VERBS)
        has_read = any(v in text_lower for v in READ_VERBS)
        return has_write, has_read

    snippet_keywords = get_domain_keywords(code_snippet)
    anchor_writes, anchor_reads = get_op_profile(code_snippet)

    top_score = chunks[0].score if chunks else 1.0

    for c in chunks:
        score = c.score
        clean_scope = get_clean_scope({'scope': c.scope, 'text': c.text, 'file': c.file})

        unique_key = c.file
        if unique_key in seen_scopes: continue
        seen_scopes.add(unique_key)

        doc_text = c.text
        norm_doc = re.sub(r'\s+', '', doc_text)

        is_origin = (norm_snippet in norm_doc) or (norm_doc in norm_snippet) or (score >= top_score * 0.99)

        is_caller = False
        if not is_origin and primary_symbol and (primary_symbol in doc_text):
            is_caller = True

        doc_keywords = get_domain_keywords(doc_text)

        shared_keywords = snippet_keywords.intersection(doc_keywords)
        strong_shared = [w for w in shared_keywords if re.search(r'[A-Z]', w) or '_' in w]

        # --- 2. OPERATIONAL MISMATCH DETECTION ---
        cand_writes, cand_reads = get_op_profile(doc_text)
        op_mismatch = False
        if anchor_writes and not anchor_reads and cand_reads and not cand_writes:
            op_mismatch = True  # Anchor writes, Candidate only reads
        elif anchor_reads and not anchor_writes and cand_writes and not cand_reads:
            op_mismatch = True  # Anchor reads, Candidate only writes

        if is_origin:
            evidence_str = "Origin File / Exact Snippet Match"
        elif is_caller:
            evidence_str = f"Direct Caller (Invokes `{primary_symbol}`)"
        elif op_mismatch:
            evidence_str = f"Op Mismatch (Reader vs Writer) + shared: {', '.join(strong_shared)}"
        elif strong_shared:
            evidence_str = f"Shared domain logic ({', '.join(strong_shared)})"
        else:
            evidence_str = "API/Structural similarity only"

        snippet_preview = doc_text[:120].replace('\n', ' ').strip() + "..."
        entry = f"- {c.file} ({clean_scope})\n  [Score]: {score:.3f}\n  [Evidence]: {evidence_str}\n  [Snippet]: {snippet_preview}\n\n"

        # --- 3. COMPOSITE SCORING (The Fix) ---
        # Calculate a fluid ratio instead of hard cut-offs
        raw_ratio = score / top_score

        # Every strong shared keyword adds a +0.05 bonus to the ratio
        semantic_bonus = len(strong_shared) * 0.05

        # Heavy penalty if the operation directions are opposites
        op_penalty = 0.20 if op_mismatch else 0.0

        composite_score = raw_ratio + semantic_bonus - op_penalty

        if is_origin:
            origin_data.append(entry)
        elif is_caller:
            caller_data.append(entry)
        elif composite_score >= 0.72:
            # 0.72 is the new "Golden Threshold" for the composite score.
            # A 7.7 score (0.68 ratio) + 1 keyword (0.05 bonus) = 0.73 (Passes!)
            high_conf_data.append(entry)
        else:
            if len(weak_conf_data) < 4:
                weak_conf_data.append(entry)

    context = "--- SIMILAR CODE ANALYSIS ---\n\n"

    context += "1. ORIGIN POINT (Anchor):\n"
    context += "".join(origin_data) if origin_data else "  [Snippet origin not found in index]\n\n"

    context += "2. DIRECT CALLERS (Files using this snippet):\n"
    context += "".join(caller_data) if caller_data else "  [No direct callers found]\n\n"

    context += "3. HIGH CONFIDENCE MATCHES (Strong Parallels):\n"
    context += "".join(high_conf_data) if high_conf_data else "  [No high confidence matches]\n\n"

    context += "4. WEAK MATCHES (Structural lookalikes / False Positives):\n"
    context += "".join(weak_conf_data) if weak_conf_data else "  [No weak matches]\n\n"

    context += """
    INSTRUCTIONS FOR AI AGENT:
    1. Do not suggest refactoring the Origin Point against itself.
    2. Note the Direct Callers so the user knows where this code is being executed.
    3. Focus heavily on High Confidence Matches to identify parallel logic that might need deduplication.
    4. Ignore Weak Matches as they are likely just generic API overlaps or mismatched operations (e.g. Read vs Write).
    """

    return context

@_tool()
def analyze_blast_radius(anchor_file: str, target_symbol: str) -> str:
    """
    Use this tool when planning a refactor.
    It requires TWO inputs to ground the search:
    1. anchor_file (e.g., 'inventory-list.tsx') - The file where the change originates.
    2. target_symbol (e.g., 'activeView') - The specific concept, state, or interface being changed.
    """
    print(f"\n[MCP] Analyzing blast radius anchored at '{anchor_file}' for '{target_symbol}'")
    anchor_base = _module_stem(anchor_file)

    # Who imports the anchor, and what the anchor imports (the "Primitive Directional
    # Filter"), computed once up front instead of re-scanning text per file (B-037).
    texts = _texts_by_file()
    importers, anchor_imports = _import_relations(anchor_file, texts)
    anchor_found = any(_is_anchor_path(anchor_file, f) for f in texts)

    query_text = f"Implementation, definition, or usage of {target_symbol}"
    # Shared RTR surface (ADR-023 §1) instead of raw t1+t2 FAISS: resolved
    # call-graph neighbours of the anchor join the candidate pool, so dependents
    # reachable only through the graph (not FAISS similarity) enter the radius.
    _blast_chunks = _search(query_text, top_n=30)

    seen_files = set()
    anchor_data = []
    primitives_data = []
    dependents_data = []
    parallel_data = []

    for c in _blast_chunks:
        doc = {'file': c.file, 'scope': c.scope, 'text': c.text}

        file_path = doc['file']
        if file_path in seen_files: continue
        seen_files.add(file_path)

        file_base = _module_stem(file_path)
        doc_text = doc['text']

        # --- STRUCTURAL & SEMANTIC CHECKS ---
        is_anchor = _is_anchor_path(anchor_file, file_path)
        has_symbol = target_symbol in doc_text

        # Structural: Does this file import the anchor? (downstream — true dependent)
        imports_anchor = file_path in importers

        # Structural: Does the anchor import this file? (upstream — primitive/dependency)
        is_imported_by_anchor = file_base in anchor_imports

        evidence = []
        if has_symbol: evidence.append(f"Contains symbol `{target_symbol}`")
        snippet = doc_text[:150].replace('\n', ' ').strip() + "..."

        # --- CATEGORY-SPECIFIC VALIDATION FUNNEL ---

        if is_anchor:
            evidence.append("Origin Anchor")
            evidence_str = " + ".join(evidence)
            anchor_data.append(f"- {file_path}\n  [Evidence]: {evidence_str}\n  [Snippet]: {snippet}\n\n")

        elif imports_anchor:
            # DIRECT DEPENDENTS: files that import the anchor (downstream callers)
            evidence.append(f"Imports `{anchor_base}`")
            evidence_str = " + ".join(evidence)
            dependents_data.append(f"- {file_path}\n  [Evidence]: {evidence_str}\n  [Snippet]: {snippet}\n\n")

        elif is_imported_by_anchor:
            # UNDERLYING PRIMITIVES: files the anchor imports from (upstream dependencies)
            evidence.append(f"Anchor imports `{file_base}`")
            evidence_str = " + ".join(evidence)
            primitives_data.append(f"- {file_path}\n  [Evidence]: {evidence_str}\n  [Snippet]: {snippet}\n\n")

        else:
            # PARALLEL IMPLEMENTATIONS: Semantic Filter (No imports required)
            if len(parallel_data) < 10:
                evidence.append("Semantic Pattern Match (No direct imports)")
                evidence_str = " + ".join(evidence)
                parallel_data.append(f"- {file_path}\n  [Evidence]: {evidence_str}\n  [Snippet]: {snippet}\n\n")

    # Exhaustive import sweep: catches callers that FAISS ranking missed
    for file_path in sorted(importers - seen_files):
        if file_path not in texts: continue     # an edge left by a file no longer indexed
        seen_files.add(file_path)
        file_full_text = texts[file_path]
        has_symbol = target_symbol in file_full_text
        snippet = file_full_text[:150].replace('\n', ' ').strip() + "..."
        evidence = [f"Imports `{anchor_base}`"]
        if has_symbol: evidence.append(f"Contains symbol `{target_symbol}`")
        dependents_data.append(f"- {file_path}\n  [Evidence]: {' + '.join(evidence)}\n  [Snippet]: {snippet}\n\n")

    # Fallback to force anchor if FAISS missed it
    if not anchor_data and anchor_found:
        anchor_data.append(f"- {anchor_file}\n  [Evidence]: Origin Anchor (Forced via Metadata)\n\n")

    # --- PAYLOAD GENERATION ---
    context = "--- BLAST RADIUS ANALYSIS ---\n"
    context += f"ANCHOR: {anchor_file} | SYMBOL: {target_symbol}\n\n"

    context += "1. ORIGIN POINT:\n"
    context += "".join(anchor_data) if anchor_data else "  [Anchor file not found]\n"

    context += "2. DIRECT DEPENDENTS (Import Graph Validated):\n"
    context += "".join(dependents_data) if dependents_data else "  [No dependents detected]\n"

    context += "3. PARALLEL IMPLEMENTATIONS (Semantic/Pattern Matches):\n"
    context += "".join(parallel_data) if parallel_data else "  [No parallel patterns detected]\n"

    context += "4. UNDERLYING PRIMITIVES (Directionally Validated):\n"
    context += "".join(primitives_data) if primitives_data else "  [No anchor-imported primitives detected]\n"

    # --- ADR-017 §7 edge-aware radius (safe direction: candidate neighbours EXPAND
    # the radius into a separate UNVERIFIED bucket, never merged into the verified
    # dependents count). ---
    verified_deps, candidate_deps = _caller_evidence(target_symbol, anchor_file)
    context += "\n5. CALL-GRAPH DEPENDENTS (resolved edges — verified):\n"
    context += ("".join(f"- {n.fqn}\n  [File]: {n.file_path}\n" for n in verified_deps)
                if verified_deps else "  [None detected]\n")
    if candidate_deps:
        context += "\n6. UNVERIFIED NEIGHBOURS (candidate edges — expand radius, review separately):\n"
        context += "".join(f"- {n.fqn}\n  [File]: {n.file_path}\n" for n in candidate_deps)

    context += """
    INSTRUCTIONS FOR AI AGENT:
    Review the categories and [Evidence] tags.
    1. For Direct Dependents, explain how a change to the anchor might break them.
    2. For Parallel Implementations, point out if they use the same pattern and should be refactored to match.
    3. For Primitives, explain if the core UI components need to be adjusted to support the change.
    """

    return context

@_tool()
def detect_pattern_violations(canonical_snippet: str, enforced_symbols_csv: str, ignore_regex: str = "") -> str:
    """
    Finds code that SHOULD follow a pattern but deviates.
    - enforced_symbols_csv: Comma-separated valid symbols (e.g., 'writeTransactionLogTx, writeTransactionLog').
    - ignore_regex: Regex to skip files (e.g., '^on[-A-Z]' for triggers).
    """
    print(f"\n[MCP] Scanning for violations missing '{enforced_symbols_csv}'...")
    enforced_symbols = [s.strip() for s in enforced_symbols_csv.split(',') if s.strip()]
    # Shared RTR surface (ADR-023 §1) instead of raw t1+t2 FAISS: structural
    # neighbours + import-corroboration enter the pattern-scan pool. Each candidate
    # keeps its RTR ranking score for the relative relevance gate below.
    _pv_chunks = _search(canonical_snippet, top_n=60)
    _pv_items: dict[str, dict] = {}
    for _c in _pv_chunks:
        _key = f"{_c.file}::{_c.scope}"
        if _key not in _pv_items:
            _pv_items[_key] = {'file': _c.file, 'scope': _c.scope, 'text': _c.text, 'score': _c.score}

    violations_data, possible_violations_data, compliant_data, exempt_data = [], [], [], []
    seen_scopes = set()
    seen_violation_files = set()
    compliant_files: set[str] = set()

    # --- 1. OPERATIONAL PROFILING (Reader vs Writer) ---
    READ_VERBS = {'get', 'fetch', 'query', 'where', 'onsnapshot', 'subscribe', 'use', 'read'}

    def get_op_profile(text):
        text_lower = text.lower()
        # Require a Firestore object before .set/.update/.add so useState/setState
        # don't register as writes. Keep bare verb checks for unambiguous write ops.
        has_firestore_write = bool(re.search(
            r"(\w*(?:transaction|batch|db|firestore|admin|ref|tx))\.(set|add|update)\(",
            text, re.IGNORECASE
        ))
        has_other_write = any(v in text_lower for v in {'commit', 'write', 'delete', 'mutate'})
        has_read = any(v in text_lower for v in READ_VERBS)
        return (has_firestore_write or has_other_write), has_read

    anchor_writes, anchor_reads = get_op_profile(canonical_snippet)

    stopwords = {'const', 'let', 'var', 'function', 'return', 'import', 'export', 'async', 'await'}
    words = set(re.findall(r'[a-zA-Z_]\w{3,}', canonical_snippet))
    strong_keywords = [w for w in words if (w not in stopwords) and (re.search(r'[A-Z]', w) or '_' in w)]

    top_score = max((d['score'] for d in _pv_items.values()), default=1.0)

    # --- KEYWORD SWEEP: Secondary retrieval for files missed by the RTR pool ---
    # Files may be semantically distant from the canonical snippet (low cosine score)
    # but still use the same Firestore collection or domain objects. Sweep every indexed
    # document for any that share 2+ domain-specific terms with the snippet and weren't
    # surfaced by cosine similarity. Floor score keeps them below the 0.65 threshold so
    # they only pass the existing len(shared_strong) >= 2 relevance gate.
    if strong_keywords:
        for _kw_doc in _index().doc_store.docs.values():
            _kw_key = f"{_kw_doc['file']}::{_kw_doc['scope']}"
            if _kw_key in _pv_items:
                continue
            _kw_words = set(re.findall(r'[a-zA-Z_]\w{3,}', _kw_doc['text']))
            if sum(1 for w in strong_keywords if w in _kw_words) >= 2:
                _pv_items[_kw_key] = {'file': _kw_doc['file'], 'scope': _kw_doc['scope'],
                                      'text': _kw_doc['text'], 'score': top_score * 0.45}
    _pv_sorted = sorted(_pv_items.values(), key=lambda d: d['score'], reverse=True)

    # ADR-017 §7 safe direction: a file reaching an enforced symbol only through a
    # candidate (unresolved) edge might actually comply — soften its accusation to
    # "possible violation" rather than assert a hard finding.
    _soft_files: set[str] = set()
    for _sym in enforced_symbols:
        _, _cand = _caller_evidence(_sym)
        for _n in _cand:
            if _n.file_path:
                _soft_files.add(_n.file_path.replace('\\', '/'))

    for doc in _pv_sorted:
        score = doc['score']

        unique_key = f"{doc['file']}::{doc['scope']}"
        if unique_key in seen_scopes: continue
        seen_scopes.add(unique_key)

        file_path, doc_text = doc['file'], doc['text']
        file_name = file_path.split('/')[-1].split('\\')[-1]

        # --- 2. ARCHITECTURAL EXEMPTIONS ---
        if ignore_regex and re.search(ignore_regex, file_name):
            exempt_data.append(f"- {file_path} ({doc['scope']}) [Regex Exemption]\n")
            continue

        is_compliant = any(sym in doc_text for sym in enforced_symbols)
        cand_writes, cand_reads = get_op_profile(doc_text)

        # --- 3. THE READER FILTER ---
        # If the anchor is a writer, but the candidate only reads, it is NOT a violation.
        is_pure_reader = anchor_writes and not anchor_reads and cand_reads and not cand_writes
        if is_pure_reader and not is_compliant:
            continue

        # --- 4. THE INERT-FILE FILTER ---
        # Files with no read or write verbs are type definitions, display components, or test
        # helpers — they are never expected to call the enforced symbol.
        is_inert = not cand_writes and not cand_reads
        if is_inert and not is_compliant:
            continue

        # --- 4. CONTEXT VALIDATION ---
        doc_words = set(re.findall(r'[a-zA-Z_]\w{3,}', doc_text))
        clean_scope = get_clean_scope(doc)
        shared_strong = [w for w in strong_keywords if w in doc_words]

        # Keep if compliant OR mathematically relevant OR semantically identical
        is_relevant = is_compliant or (score >= top_score * 0.65) or (len(shared_strong) >= 2)

        if not is_relevant:
            continue

        snippet_preview = doc_text[:150].replace('\n', ' ').strip() + "..."

        if is_compliant:
            matched = [s for s in enforced_symbols if s in doc_text]
            compliant_files.add(file_path)
            compliant_data.append(f"- {file_path} ({clean_scope}) [Uses: {', '.join(matched)}]\n")
        else:
            if file_path not in seen_violation_files:
                seen_violation_files.add(file_path)
                evidence = f"Shares context ({', '.join(shared_strong)}) but lacks compliant symbols."
                entry = f"- {file_path} ({clean_scope})\n  [Reason]: {evidence}\n  [Snippet]: {snippet_preview}\n\n"
                if file_path.replace('\\', '/') in _soft_files:
                    possible_violations_data.append(entry)
                else:
                    violations_data.append(entry)

    # --- PRE-OUTPUT: Remove violations for files that are also compliant in another chunk ---
    violations_data = [v for v in violations_data if not any(cp in v for cp in compliant_files)]
    possible_violations_data = [v for v in possible_violations_data if not any(cp in v for cp in compliant_files)]

    # --- OUTPUT ---
    context = "--- PATTERN VIOLATION ANALYSIS ---\n\n"
    context += f"RULES: Must use [{enforced_symbols_csv}]\n"
    context += f"IGNORE REGEX: {ignore_regex if ignore_regex else 'None'}\n\n"
    context += "1. DETECTED VIOLATIONS:\n" + ("".join(violations_data) if violations_data else "  [None!]\n") + "\n"
    if possible_violations_data:
        # ADR-017 §7: softened — reached via candidate (unresolved) edges, so
        # compliance can't be ruled out. Review, don't treat as a hard finding.
        context += ("1b. POSSIBLE VIOLATIONS (unverified — candidate edges, review):\n"
                    + "".join(possible_violations_data) + "\n")
    context += "2. COMPLIANT FILES:\n" + ("".join(compliant_data) if compliant_data else "  [None]\n") + "\n"
    if exempt_data:
        context += "3. EXEMPTED (Regex):\n" + "".join(exempt_data) + "\n"

    return context

@_tool()
def trace_data_flow(target_symbol: str) -> str:
    """
    Traces data lifecycle. v11.0: Broad definition lookup + Dynamic Producer Tracing.
    """
    print(f"\n[MCP] Running v11.0 trace for '{target_symbol}'...")
    query_text = f"Definition, usage, and fetching of {target_symbol} get{target_symbol} fetch{target_symbol}"
    # Shared RTR surface (ADR-023 §1) instead of raw t1+t2 FAISS: resolved
    # call-graph neighbours join the trace pool, so producers/consumers reached
    # only through the call graph are no longer invisible to the trace.
    _trace_chunks = _search(query_text, top_n=80)

    # PascalCase variant for type/interface definition matching (e.g. aggregatedInventory → AggregatedInventory)
    pascal_symbol = target_symbol[0].upper() + target_symbol[1:] if target_symbol else target_symbol

    # --- PRE-PASS: File-level producer detection ---
    # Per-chunk detection misses writes that land in a different chunk than the FAISS hit.
    # We aggregate full file text once and mark any file that writes to this collection.
    _db_write_re = re.compile(
        r"(\w*(?:transaction|batch|db|firestore|admin|ref|tx))\.(set|add|update)\(", re.IGNORECASE
    )
    _file_texts: dict[str, str] = {}
    for _d in _index().doc_store.docs.values():
        _fp = _d['file'].replace('\\', '/')
        _file_texts[_fp] = _file_texts.get(_fp, '') + _d['text'] + '\n'

    producer_files: set[str] = set()
    # Catches chained writes: db.collection('x').doc(id).set(data) where the write
    # lands on an unnamed result of a method chain, not a named Firestore variable.
    _chained_write_re = re.compile(r"\)\.(set|add|update)\(", re.IGNORECASE)
    for _fp, _full in _file_texts.items():
        if target_symbol not in _full:
            continue
        if not (_db_write_re.search(_full) or _chained_write_re.search(_full)):
            continue
        _fp_lower = _fp.lower()
        if any(x in _fp_lower for x in ["firebase/admin", "lib/admin"]):
            producer_files.add(_fp)
        elif "firebase/functions" in _fp_lower or "functions/src" in _fp_lower:
            producer_files.add(_fp)

    seen_scopes = set()
    # Tracks files that received any bucket entry from a tier-1 chunk.
    # Tier-2/3 chunks are skipped for files already covered, preventing duplicate
    # file entries caused by the component-level "Full File" scopes tier-2 produces.
    seen_files_any_bucket: set[str] = set()
    buckets = {"DEFINITIONS": [], "PRODUCERS": [], "TRANSFORMERS": [], "CONSUMERS": []}

    for c in _trace_chunks:
        if not (target_symbol in c.text or f"get{target_symbol}" in c.text):
            continue

        file_path = c.file.replace('\\', '/')

        # Tier-2/3 chunks provide component-level context; skip them for files that
        # tier-1 already covered so we don't add redundant "Full File" scope entries.
        if c.tier != 'tier1_surgical':
            if file_path in seen_files_any_bucket:
                continue

        unique_key = f"{file_path}::{c.scope}"
        if unique_key in seen_scopes: continue
        seen_scopes.add(unique_key)

        doc_text = c.text

        # --- LAYER DETECTION ---
        is_client = "'use client'" in doc_text or ".tsx" in file_path.lower()
        if any(x in file_path.lower() for x in ["firebase/admin", "lib/admin"]):
            layer = "DATABASE (Admin SDK)"
        elif "firebase/functions" in file_path.lower() or "functions/src" in file_path.lower():
            layer = "CLOUD FUNCTION"
        elif is_client:
            layer = "CLIENT COMPONENT (UI)"
            if "app/" in file_path.lower() and "page.tsx" in file_path.lower() and "'use client'" not in doc_text:
                layer = "SERVER COMPONENT"
        elif any(x in file_path.lower() for x in ["lib/", "types/", "models/"]):
            layer = "CORE LOGIC / LIB"
        else:
            layer = "UTILITY"

        # --- FIX 3: DYNAMIC PRODUCER DETECTION ---
        # We broaden the anchors to catch 'aggRef.set' or 'customBatch.update'
        has_db_write = bool(re.search(
            r"(\w*(?:transaction|batch|db|firestore|admin|ref|tx))\.(set|add|update)\(",
            doc_text,
            re.IGNORECASE
        ))

        is_def = bool(re.search(rf"export\s+(interface|type|class)\s+({re.escape(target_symbol)}|{re.escape(pascal_symbol)})", doc_text))
        is_producer = file_path in producer_files
        is_transformer = any(op in doc_text for op in [".filter(", ".map(", ".sort(", "useMemo("])

        violation_tag = ""
        if has_db_write and layer == "CLIENT COMPONENT (UI)" and target_symbol in doc_text:
            violation_tag = "  [⚠️ ARCHITECTURAL VIOLATION]: Client-side Firestore write detected.\n"

        # FIX 1 applied here (Scope Cleanup)
        clean_scope = get_clean_scope({'scope': c.scope, 'text': c.text, 'file': c.file})
        snippet = doc_text[:140].replace('\n', ' ').strip() + "..."
        entry = f"- [{layer}] {file_path}\n  [Scope]: {clean_scope}\n{violation_tag}  [Snippet]: {snippet}\n\n"

        seen_files_any_bucket.add(file_path)

        if is_def and layer == "CORE LOGIC / LIB":
            buckets["DEFINITIONS"].insert(0, entry)
        elif is_def:
            buckets["DEFINITIONS"].append(entry)
        elif is_producer:
            buckets["PRODUCERS"].append(entry)
        elif is_transformer:
            if len(buckets["TRANSFORMERS"]) < 12:
                buckets["TRANSFORMERS"].append(entry)
        else:
            if len(buckets["CONSUMERS"]) < 15:
                buckets["CONSUMERS"].append(entry)

    # --- FIX 2: GENERALIZED DEFINITION LOOKUP ---
    if not buckets["DEFINITIONS"]:
        for _, doc in _index().doc_store.docs.items():
            norm_path = doc['file'].replace('\\', '/')
            # Scan all lib, types, and models folders; match both camelCase and PascalCase type names
            if any(x in norm_path for x in ["lib/", "types/", "models/"]) and \
               re.search(rf"export\s+(interface|type)\s+({re.escape(target_symbol)}|{re.escape(pascal_symbol)})", doc['text']):
                clean_scope = get_clean_scope(doc)
                entry = f"- [CORE LOGIC / LIB] {doc['file']}\n  [Scope]: {clean_scope}\n  [Snippet]: {doc['text'][:140].strip()}...\n\n"
                buckets["DEFINITIONS"].append(entry)
                break

    context = f"--- DATA FLOW TRACE: {target_symbol} ---\n\n"
    for cat, items in buckets.items():
        context += f"### {cat}\n"
        context += "".join(items) if items else "  [No entries detected]\n"
        context += "\n"

    return context

# ---------------------------------------------------------------------------
# investigate_architecture — Agentic high-level investigation tool
# ---------------------------------------------------------------------------

# The retriever belongs to the IndexState. Reranking is off by default (see
# [reranker].enabled in indexer.toml); HybridRetriever() reads that config.
def _get_hybrid_retriever() -> HybridRetriever:
    return _index().retriever


def _search(query: str, top_n: int = 10) -> list[RetrievedChunk]:
    """Shared retrieval surface (ADR-023 §1).

    Every retrieval-backed MCP tool routes candidate generation through the
    Retrieve-Traverse-Rerank pipeline instead of raw multi-tier FAISS search, so
    the resolved call graph + import-corroboration (ADR-021) and the honest
    Wave-0 ranking (ADR-007) reach every tool — not just investigate_architecture.
    Returns RTR ``RetrievedChunk``s (``.file/.scope/.tier/.text/.source/.corroborated``);
    tools keep their own formatting and post-filters. ``top_n`` widens the pool for
    scan-style tools that need breadth.
    """
    return _get_hybrid_retriever().retrieve(query, top_n=top_n)


# ---------------------------------------------------------------------------
# ADR-023 §3 — edge-aware verdict support (ADR-017 §7 three-state rule)
# ---------------------------------------------------------------------------

def _db():
    """The shared ``CodeDB`` behind the RTR pipeline — the verdict tools' direct
    edge-graph read for the candidate/resolved split (ADR-017 §7)."""
    return _index().db


def _resolve_symbol_fqns(symbol: str, anchor_file: str = "") -> list[str]:
    """Resolve a bare symbol name to FQN(s), optionally scoped to a file basename.

    FQNs are ``path::symbol`` (see ``db.search_symbols``). When ``anchor_file`` is
    given we keep only FQNs defined in a file with that basename, so callers of a
    same-named symbol in a *different* file don't leak into the verdict.
    """
    matches = [s.fqn for s in _db().search_symbols(symbol) if s.name == symbol]
    if anchor_file:
        base = anchor_file.lower().replace("\\", "/").split("/")[-1]
        scoped = [
            f for f in matches
            if f.split("::", 1)[0].lower().replace("\\", "/").split("/")[-1] == base
        ]
        if scoped:
            return scoped
    return matches


def _caller_evidence(symbol: str, anchor_file: str = "") -> tuple[list, list]:
    """Return ``(verified, candidate)`` caller ``CallGraphNode``s for ``symbol``.

    Callers reaching the symbol only through low-confidence (name-based /
    unresolved) edges are firewalled into the second list — the safe-direction
    rule (ADR-017 §7) keys on this split. Deduped by caller FQN; a caller verified
    through any confident edge is never also counted as candidate.

    ADR-008 §5: the split gates on ``CallGraphNode.confidence`` (the ``MAX``
    effective confidence over reaching edges) against ``EDGE_CONFIDENCE_FLOOR``,
    not the coarse boolean. Under the derived mapping this is identical to the old
    ``node.candidate`` split, but a producer-graded candidate edge at/above the
    floor now correctly reads as verified.
    """
    from db import EDGE_CONFIDENCE_FLOOR
    db = _db()
    verified: dict[str, object] = {}
    candidate: dict[str, object] = {}
    for fqn in _resolve_symbol_fqns(symbol, anchor_file):
        for node in db.get_callers(fqn):
            below_floor = getattr(node, "confidence", 1.0) < EDGE_CONFIDENCE_FLOOR
            (candidate if below_floor else verified)[node.fqn] = node
    for f in list(candidate):        # a confident sighting wins over a candidate one
        if f in verified:
            del candidate[f]
    return list(verified.values()), list(candidate.values())


# ---------------------------------------------------------------------------
# Import relationships for analyze_blast_radius / find_dead_code (B-037)
# ---------------------------------------------------------------------------

# Extensions a module specifier may carry that are not part of the module's name.
_MODULE_EXTS = {
    ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".gs",
    ".py", ".pyi", ".cs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
}

# A quoted module specifier after `from`, `import`, `require(` or `import(`: the forms
# the JS/TS adapter does not turn into IMPORTS edges (it records `import ... from` only).
# Negated classes keep one match inside one string on one line. The pattern it replaces,
# `(import|require).*?['"].*?NAME.*?['"]` with DOTALL, backtracked across the whole file
# whenever a file did not import NAME: ~20 s for a 150K-char file, 10-20 min per call.
_SPECIFIER_RE = re.compile(r"""\b(?:from|import|require)\s*\(?\s*(['"])([^'"\r\n]+)\1""")


def _module_stem(spec: str) -> str:
    """The name a module specifier or file path refers to, for matching one to the other.

    './lib/foo.js' → 'foo', '@/lib/bar/index' → 'bar', 'pkg.mod' (Python) → 'mod',
    'src/scan_policy.py' → 'scan_policy'. Lower-cased. Matching whole names means
    './foobar' no longer counts as importing 'foo', as the old substring test did.
    """
    s = spec.strip().replace("\\", "/").rstrip("/").lower()
    last = s.rsplit("/", 1)[-1]
    root, ext = os.path.splitext(last)
    if ext in _MODULE_EXTS:
        last = root
    elif "/" not in s:
        last = last.rsplit(".", 1)[-1]      # dotted Python path: pkg.mod, .mod → mod
    if last == "index" and "/" in s:
        return _module_stem(s.rsplit("/", 1)[0])
    return last


def _texts_by_file() -> dict[str, str]:
    """Every indexed file's chunk text, joined, in one pass over the doc store."""
    parts: dict[str, list[str]] = {}
    for d in _index().doc_store.docs.values():
        parts.setdefault(d['file'], []).append(d['text'])
    return {f: "\n".join(p) for f, p in parts.items()}


def _specifier_stems(text: str) -> set[str]:
    return {_module_stem(m.group(2)) for m in _SPECIFIER_RE.finditer(text)}


def _is_anchor_path(anchor_file: str, path: str) -> bool:
    """Whether ``path`` is the anchor file: same file name, compared whole.

    A substring test counted 'tests/test_scan_policy.py' as the anchor 'scan_policy.py',
    so blast radius listed that importer as the origin and find_dead_code skipped it.
    """
    def name(p: str) -> str:
        return p.lower().replace("\\", "/").split("/")[-1]
    return name(path) == name(anchor_file)


def _import_relations(anchor_file: str, texts: dict[str, str]) -> tuple[set[str], set[str]]:
    """``(importers, imported)`` for ``anchor_file``.

    ``importers`` are the files that import the anchor; ``imported`` holds the module
    stems the anchor itself imports. Both come from the graph's IMPORTS edges (every
    Python import, and ES imports) plus the quoted specifiers in the file text
    (``require()``, ``import()``, re-exports), so neither source's gaps decide alone.
    """
    anchor_stem = _module_stem(anchor_file)
    anchor_paths = {f for f in texts if _is_anchor_path(anchor_file, f)}

    importers: set[str] = set()
    imported: set[str] = set()
    for source, target, resolved in _db().get_import_edges():
        if _module_stem(resolved or target) == anchor_stem:
            importers.add(source)
        if source in anchor_paths:
            imported.add(_module_stem(resolved or target))
    for path, text in texts.items():
        stems = _specifier_stems(text)
        if path in anchor_paths:
            imported |= stems
        elif anchor_stem in stems:
            importers.add(path)
    return importers - anchor_paths, imported


def _get_iterative_retriever() -> IterativeRetriever:
    return _index().iterative()


# --- Layer classification (mirrors trace_data_flow logic) ---

_DB_WRITE_RE = re.compile(
    r"(\w*(?:transaction|batch|db|firestore|admin|ref|tx))\.(set|add|update)\(",
    re.IGNORECASE,
)

_LAYER_RULES = [
    (lambda p, t: any(x in p for x in ["firebase/admin", "lib/admin"]),  "DATABASE"),
    (lambda p, t: "firebase/functions" in p or "functions/src" in p,     "CLOUD_FUNCTION"),
    (lambda p, t: "'use client'" in t or (p.endswith(".tsx") and "use client" in t), "CLIENT_COMPONENT"),
    (lambda p, t: "app/" in p and p.endswith("page.tsx") and "'use client'" not in t, "SERVER_COMPONENT"),
    (lambda p, t: any(x in p for x in ["lib/", "types/", "models/"]),    "CORE_LIB"),
]


def _detect_layer(file_path: str, text: str) -> str:
    norm = file_path.lower().replace("\\", "/")
    for test, label in _LAYER_RULES:
        if test(norm, text):
            return label
    return "UTILITY"


# --- Relationship-type classifier for <evidence type="..."> ---

def _classify_relationship(chunk: RetrievedChunk, concept: str) -> str:
    text, esc = chunk.text, re.escape(concept)
    if re.search(rf"export\s+(interface|type|class)\s+{esc}", text):
        return "definition"
    if re.search(rf"(:\s*{esc}\s*=\s*\{{|{esc}\.create\b)", text):
        return "definition"
    if _DB_WRITE_RE.search(text) and concept in text:
        layer = _detect_layer(chunk.file, text)
        if layer in ("CLOUD_FUNCTION", "DATABASE"):
            return "producer"
    if chunk.source == "structural":
        return "caller"
    if any(op in text for op in (".filter(", ".map(", ".sort(", "useMemo(")):
        return "transformer"
    if concept in text:
        return "consumer"
    return "semantic_match"


# --- Architectural risk detectors ---

# ---------------------------------------------------------------------------
# H6 — externalized risk rules
# ---------------------------------------------------------------------------

def _load_rules(rules_path: str | None = None) -> list[dict]:
    """Load risk rules from a rules.yaml file.

    Looks for rules.yaml in the current working directory if no path is given.
    Returns an empty list when no file is found (engine produces no violations).

    Ship examples/firebase-rules.yaml to your repo root as rules.yaml to
    re-enable the Firebase/Firestore rule set.
    """
    if rules_path is None:
        candidate = os.path.join(os.getcwd(), "rules.yaml")
        rules_path = candidate if os.path.exists(candidate) else None
    if rules_path is None:
        return []
    try:
        import yaml  # pyyaml — listed in pyproject.toml dependencies
        with open(rules_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data.get("rules", []) if isinstance(data, dict) else []
    except Exception as exc:
        print(f"[rules] Could not load {rules_path}: {exc}")
        return []


def _analyze_risks(chunks: list[RetrievedChunk], concept: str) -> list[str]:
    """
    Apply per-project risk rules to the retrieved evidence chunks.

    Rules are loaded from rules.yaml in the repo root (H6).  When no rules
    file is present, no violations are reported — install rules.yaml from
    examples/firebase-rules.yaml to enable risk analysis.

    Each rule specifies:
      layer           — layer classification where it applies (optional)
      pattern         — regex applied to chunk text (required)
      require_concept — if true, concept name must appear in text
      severity        — CRITICAL | HIGH | MEDIUM | LOW | HINT
      message         — finding description (supports {concept} placeholder)

    Returns a list of formatted Markdown risk blocks (### headings).
    """
    rules = _load_rules()
    if not rules:
        return []

    risks: list[str] = []
    for chunk in chunks:
        layer = _detect_layer(chunk.file, chunk.text)
        text  = chunk.text

        for rule in rules:
            rule_layer = rule.get("layer")
            if rule_layer and rule_layer != layer:
                continue

            pattern = rule.get("pattern", "")
            if pattern and not re.search(pattern, text, re.IGNORECASE):
                continue

            if rule.get("require_concept") and concept not in text:
                continue

            rule_id  = rule.get("id", "UNKNOWN").upper()
            severity = rule.get("severity", "MEDIUM")
            message  = rule.get("message", "").strip().format(concept=concept)

            risks.append(
                f"### ⚠️  {rule_id}\n"
                f"**Severity**: {severity}  \n"
                f"**File**: `{chunk.file}`  \n"
                f"**FQN**: `{chunk.scope}`  \n"
                f"**Finding**: {message}\n"
            )

    seen: set[str] = set()
    deduped: list[str] = []
    for risk in risks:
        key = risk.split("\n")[0]
        if key not in seen:
            seen.add(key)
            deduped.append(risk)
    return deduped


@_tool()
def investigate_architecture(target_concept: str, deep: bool = False) -> str:
    """
    PREFERRED ENTRY POINT for all architectural investigations.
    Replaces raw `semantic_code_search` when you want a complete picture of how a
    concept, feature, data type, or function flows through the system.

    Internally runs the full Retrieve-Traverse-Rerank pipeline:
      1. Semantic Search  — FAISS tier-1 index, top-50 by cosine similarity.
      2. Graph Expansion  — one-hop call-graph traversal via SQLite for top-5 seeds.
      3. Ranking — Reciprocal Rank Fusion; a reranker model only when enabled in indexer.toml.

    When deep=True, runs up to 3 iterative retrieval rounds with explored-node memory
    and query enrichment from prior evidence, stopping early when the score plateau
    indicates diminishing returns.

    Returns a Markdown report with:
      - <evidence> XML tags (source, lines, fqn, type) for each retrieved chunk, showing
        its signature and the lines that best match the concept. Read the `lines`
        span for the rest.
      - Programmatic Architectural Risk Analysis flagging layer/privilege mismatches.
    """
    print(f"\n[MCP] investigate_architecture: '{target_concept}' deep={deep}")
    session: "RetrievalSession | None" = None

    if deep:
        iter_retriever = _get_iterative_retriever()
        chunks, session = iter_retriever.retrieve(target_concept, max_iterations=3)
    else:
        retriever = _get_hybrid_retriever()
        chunks = retriever.retrieve(target_concept)

    if not chunks:
        return f"# Architectural Investigation: `{target_concept}`\n\n> No evidence found in the index. Run the indexer first.\n"

    # --- Classify chunks into report sections ---
    sections: dict[str, list[RetrievedChunk]] = {
        "Definitions":             [],
        "Producers (Writers)":     [],
        "Callers (Structural)":    [],
        "Transformers":            [],
        "Consumers & Readers":     [],
        "Semantic Matches":        [],
    }
    rel_map = {
        "definition":    "Definitions",
        "producer":      "Producers (Writers)",
        "caller":        "Callers (Structural)",
        "transformer":   "Transformers",
        "consumer":      "Consumers & Readers",
        "semantic_match": "Semantic Matches",
    }

    for chunk in chunks:
        rel = _classify_relationship(chunk, target_concept)
        sections[rel_map[rel]].append(chunk)

    # --- Build Markdown report ---
    # Say what actually ranked the results: reranking is off by default (ADR-007),
    # and an enabled reranker that fails to load falls back to RRF.
    retriever = _get_hybrid_retriever()
    reranked = retriever._reranker_enabled and not retriever._reranker_failed
    rank_step = f"Reranking ({retriever._reranker_model_id})" if reranked else "RRF Fusion"
    pipeline_label = (
        f"{'Iterative ' if deep else ''}Semantic Search → Graph Expansion → {rank_step}"
    )
    header_lines: list[str] = [
        f"# Architectural Investigation: `{target_concept}`",
        "",
        f"> **Pipeline**: {pipeline_label}  ",
        f"> **Results**: {len(chunks)} candidates retrieved and {'reranked' if reranked else 'ranked'}.",
    ]
    if session is not None:
        header_lines.append(
            f"> **Iterations**: {session.iteration} | **Confidence**: {session.confidence:.2f}"
        )
    header_lines += ["", "---"]
    lines: list[str] = header_lines + [
        "",
        "## Evidence Corpus",
        "",
    ]

    section_order = [
        "Definitions",
        "Producers (Writers)",
        "Callers (Structural)",
        "Transformers",
        "Consumers & Readers",
        "Semantic Matches",
    ]

    for section_name in section_order:
        section_chunks = sections[section_name]
        if not section_chunks:
            continue

        rel_type = next(k for k, v in rel_map.items() if v == section_name)
        lines.append(f"### {section_name}")
        lines.append("")

        for chunk in section_chunks:
            layer = _detect_layer(chunk.file, chunk.text)
            # Derive a clean FQN: prefer scope when it's a real FQN (contains ::)
            fqn = chunk.scope if "::" in chunk.scope else get_clean_scope(
                {"scope": chunk.scope, "text": chunk.text, "file": chunk.file}
            )
            snippet, span = _focused_snippet(chunk.text, target_concept)
            where = f' lines="{span[0]}-{span[1]}"' if span else ""

            lines.append(
                f'<evidence source="{chunk.file}"{where} fqn="{fqn}" '
                f'type="{rel_type}" layer="{layer}" '
                f'retrieval="{chunk.source}" score="{chunk.score:.4f}">'
            )
            lines.append("")
            lines.append(snippet)
            lines.append("")
            lines.append("</evidence>")
            lines.append("")

    # --- Architectural Risk Analysis ---
    lines.append("---")
    lines.append("")
    lines.append("## Architectural Risk Analysis")
    lines.append("")

    risks = _analyze_risks(chunks, target_concept)
    if risks:
        for risk in risks:
            lines.append(risk)
            lines.append("")
    else:
        lines.append("✅ **No architectural violations detected** in the retrieved evidence corpus.")
        lines.append("")
        lines.append(
            "> _Note: This analysis is scoped to the top-10 reranked chunks. "
            "Run `detect_pattern_violations` for an exhaustive audit._"
        )
        lines.append("")

    return "\n".join(lines)


def _get_test_patterns(source_file: str) -> tuple[list[str], list[str]]:
    """Return ``(suffixes, globs)`` naming test files for the language of source_file,
    or for all languages when it has none."""
    import os as _os
    from adapters import REGISTRY, get_adapter
    ext = _os.path.splitext(source_file)[1].lower()
    adapter = get_adapter(ext)
    if adapter and hasattr(adapter, "test_conventions"):
        tc = adapter.test_conventions()
        if tc:
            return list(tc.file_suffixes), list(tc.file_globs)
    # Fall back to all known test patterns across all adapters
    seen: set[int] = set()
    suffixes: list[str] = []
    globs: list[str] = []
    for a in REGISTRY.values():
        if id(a) not in seen:
            seen.add(id(a))
            tc = a.test_conventions() if hasattr(a, "test_conventions") else None
            if tc:
                suffixes.extend(tc.file_suffixes)
                globs.extend(tc.file_globs)
    return suffixes, globs


@_tool()
def find_test_coverage(source_file: str, target_symbol: str = "") -> str:
    """
    Finds unit tests that semantically cover a source file or symbol.

    Adapts to the language of the source file — TypeScript (.test.ts),
    C# (Tests.cs / Test.cs), Python (test_*.py / _test.py), etc.  Falls back to all
    known test file patterns when the language cannot be determined.

    Inputs:
      source_file   — filename of the source being tested (e.g. 'auth.ts', 'AuthService.cs')
      target_symbol — optional function/method name to narrow the search

    Output tiers:
      Direct   — test file named after the source (e.g. auth.test.ts, AuthServiceTests.cs)
      Semantic — tests describing the same behavior via FAISS + RRF search
      None     — explicit signal that no coverage was found
    """
    print(f"\n[MCP] find_test_coverage: '{source_file}' symbol='{target_symbol}'")
    norm_source = source_file.lower().replace('\\', '/').split('/')[-1]
    source_base = re.sub(r'\.[^.]+$', '', norm_source)

    from fnmatch import fnmatchcase
    suffixes, globs = _get_test_patterns(source_file)
    test_suffixes = [s.lower() for s in suffixes]
    test_globs = [g.lower() for g in globs]
    test_patterns = test_suffixes + test_globs     # for messages

    def is_test_file(fp: str) -> bool:
        name = fp.split('/')[-1]
        return (any(fp.endswith(s) for s in test_suffixes)
                or any(fnmatchcase(name, g) for g in test_globs))

    # Collect one representative doc per test file
    test_doc_by_file: dict[str, dict] = {}
    for doc in _index().doc_store.docs.values():
        fp = doc['file'].replace('\\', '/').lower()
        if is_test_file(fp) and fp not in test_doc_by_file:
            test_doc_by_file[fp] = doc

    if not test_doc_by_file:
        patterns_str = ", ".join(test_patterns) if test_patterns else "(none)"
        return (
            "--- TEST COVERAGE ANALYSIS ---\n\n"
            f"SOURCE: {source_file}\n\n"
            f"  [No test files found in the index (searched: {patterns_str}) — run reindex first.]\n"
        )

    # --- Tier 1: Direct name match ---
    # Candidate direct test names: source_base + each test suffix, or each glob with `*`
    # as source_base. "auth" + ".test.ts" → "auth.test.ts"; "test_*.py" → "test_auth.py"
    direct_names = ([f"{source_base}{s}" for s in test_suffixes]
                    + [g.replace("*", source_base) for g in test_globs])
    direct_candidates = set(direct_names)
    direct_data: list[str] = []
    direct_fps:  set[str]  = set()
    for fp, doc in test_doc_by_file.items():
        if fp.split('/')[-1] in direct_candidates:
            direct_fps.add(fp)
            snippet = doc['text'][:120].replace('\n', ' ').strip() + "..."
            direct_data.append(
                f"- {doc['file']} ({get_clean_scope(doc)})\n  [Snippet]: {snippet}\n\n"
            )

    # --- Tier 2: Semantic match via the shared RTR surface (ADR-023 §1) ---
    query = f"tests for {source_file} {target_symbol}".strip()

    semantic_data: list[str] = []
    seen_semantic: set[str] = set()

    for c in _search(query, top_n=30):
        fp = c.file.replace('\\', '/').lower()
        if not is_test_file(fp): continue
        if fp in seen_semantic or fp in direct_fps: continue
        seen_semantic.add(fp)

        symbol_hit = bool(target_symbol and target_symbol in c.text)
        evidence = "Semantic match" + (f" + mentions `{target_symbol}`" if symbol_hit else "")
        snippet = c.text[:120].replace('\n', ' ').strip() + "..."
        semantic_data.append(
            f"- {c.file} ({get_clean_scope({'scope': c.scope, 'text': c.text, 'file': c.file})})\n"
            f"  [Evidence]: {evidence}\n"
            f"  [Snippet]: {snippet}\n\n"
        )
        if len(semantic_data) >= 5:
            break

    # --- Build output ---
    header = f"SOURCE: {source_file}"
    if target_symbol:
        header += f" | SYMBOL: {target_symbol}"

    direct_label = " | ".join(direct_names[:2])

    context = f"--- TEST COVERAGE ANALYSIS ---\n\n{header}\n\n"
    context += "1. DIRECT COVERAGE (test file named after source):\n"
    context += "".join(direct_data) if direct_data else f"  [No direct test file — expected: {direct_label}]\n\n"
    context += "2. SEMANTIC COVERAGE (tests describing the same behavior):\n"
    context += "".join(semantic_data) if semantic_data else "  [No semantically related tests found]\n\n"

    if not direct_data and not semantic_data:
        context += (
            f"\n⚠️  COVERAGE VERDICT: NO TESTS FOUND\n"
            f"  Neither a direct test file ({direct_label}) nor any semantically related\n"
            f"  test files were found for '{source_file}'.\n"
        )

    context += f"\nNote: Searched for test files named: {', '.join(test_patterns)}\n"
    return context


@_tool(reads_index=False)
def reindex(changed_files_only: bool = False) -> str:
    """
    Rebuilds the index from its source: this folder's files, or, when indexer.toml sets
    `[indexer] source = "git:<ref>"`, that ref's commit, never your branch or your edits.

    Rarely needed: the server's watchdog keeps the index current on its own
    (index_status shows whether it is). Never call it to pick up branch work.

    changed_files_only=True  — incremental: only processes files added, modified, or
                               deleted since the last run. Fast for frequent refreshes.
    changed_files_only=False — full (default): clears all index state first, then
                               re-indexes every file from scratch. Use after major
                               refactors or when the incremental index appears corrupted.

    Returns a summary of chunks added/updated/removed, then reloads the in-memory indexes.
    """
    from incremental_indexer import INDEX_DIR
    from index_lock import WRITE_LOCK, IndexBusy, acquire, worktree_refusal
    # ADR-038 (B-053): parallel-work worktrees never drive a rebuild. Raised, not
    # returned, so the client sees isError rather than a successful refusal.
    refusal = worktree_refusal(os.getcwd())
    if refusal:
        raise RuntimeError(f"reindex refused: {refusal}")
    # ADR-036: a watchdog reindex in flight in this process finishes first; this one
    # then runs alone. ADR-038: a run in ANOTHER process is not waited on (it can take
    # an hour); the call fails at once and names it.
    with _reindex_lock:
        try:
            with acquire(INDEX_DIR, WRITE_LOCK, "reindex tool"):
                return _reindex(changed_files_only)
        except IndexBusy as exc:
            raise RuntimeError(
                f"reindex refused: another process is writing this index ({exc}). "
                f"Its run picks up the current files; call index_status after it finishes."
            ) from exc


def _reindex(changed_files_only: bool) -> str:
    """The body of `reindex`. Call it holding `_reindex_lock`."""
    import sys
    import io
    import os
    import sqlite3
    import subprocess
    from incremental_indexer import run_incremental, INDEX_DIR, TIER_CONFIGS
    from db import CodeDB

    print(f"\n[MCP] reindex: changed_files_only={changed_files_only}")

    # Git-aware staleness report for incremental mode (ADR-025 §5).
    # The last-indexed commit is now read from the index_meta table in graph.db —
    # not the retired last_indexed_commit.txt, which segmem's DB-only connector could
    # never see. Exceptions are handled narrowly: "git absent" (FileNotFoundError) is
    # distinguished from "git ran and failed" (CalledProcessError), instead of a blanket
    # swallow that hid non-git-repo, no-commits, and git-missing alike.
    _stale_warning = ""
    db_path = os.path.join(INDEX_DIR, "graph.db")
    if changed_files_only:
        _last_hash = None
        if os.path.exists(db_path):
            try:
                with CodeDB(db_path) as _meta_db:
                    _last_hash = _meta_db.meta_get("last_indexed_commit")
            except sqlite3.Error:
                _last_hash = None
        if _last_hash:
            try:
                _curr_hash = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
                ).strip()
                if _last_hash != _curr_hash:
                    _changed = subprocess.check_output(
                        ["git", "diff", "--name-only", _last_hash, "HEAD"],
                        text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
                    ).strip()
                    if _changed:
                        _stale_warning = (
                            f"⚠️  INDEX STALENESS DETECTED\n"
                            f"   Last indexed at commit: {_last_hash[:8]}\n"
                            f"   Current HEAD:           {_curr_hash[:8]}\n"
                            f"   Files changed since the index was built:\n"
                            + "\n".join(f"     - {f}" for f in _changed.splitlines() if f)
                            + "\n   Consider reindex(changed_files_only=False) for a clean rebuild.\n\n"
                        )
            except FileNotFoundError:
                pass   # git binary not installed — no staleness signal available
            except subprocess.CalledProcessError:
                pass   # not a git repo, or HEAD/ref invalid — nothing to compare

    _preserved: dict[str, tuple] = {}
    _backup_dir = None
    if not changed_files_only:
        # #50: keep a copy of the index to restore if the rebuild raises (embedder OOM,
        # a failed model download), rather than leaving it empty until the next rerun.
        _backup_dir = _snapshot_index(INDEX_DIR, db_path)
        # Wipe all index state so run_incremental treats everything as new
        if os.path.exists(db_path):
            with CodeDB(db_path) as _db:
                # ADR-025 §3: capture content stamps BEFORE the wipe. A full rebuild
                # makes every file look "new", so §2 back-dating alone would reset
                # dirty and history-less files to now()/NULL. Restored by hash match
                # after re-ingest (below), leaving genuinely-changed files re-stamped.
                try:
                    for _row in _db._conn.execute(
                        "SELECT path, content_hash, content_changed_at, authored_at FROM files"
                    ).fetchall():
                        _preserved[_row[0]] = (_row[1], _row[2], _row[3])
                except sqlite3.Error:
                    _preserved = {}
                with _db._tx() as _cur:
                    _cur.execute("DELETE FROM edges")
                    _cur.execute("DELETE FROM files")   # CASCADE removes symbols + chunks
        for _tier_name, _, _ in TIER_CONFIGS:
            _fp = os.path.join(INDEX_DIR, f"{_tier_name}.faiss")
            if os.path.exists(_fp):
                os.remove(_fp)
        # doc_store.json was retired in H2; chunk payloads live in graph.db

    # Run the indexer, capturing its console output to return as the tool result
    _captured = io.StringIO()
    _old_stdout = sys.stdout
    sys.stdout = _captured
    try:
        # interactive=False (ADR-026 §5): stdout is captured and there is no human on
        # the other end, so a bulk deletion is reported in the tool result and skipped
        # rather than blocking the server on a prompt nobody can see.
        run_incremental(interactive=False)
    except BaseException:
        if _backup_dir is not None:
            sys.stdout = _old_stdout
            _restore_index(_backup_dir, INDEX_DIR, db_path)
            _reload_indexes()
            print("[MCP] reindex: full rebuild failed; the previous index was restored")
        raise
    finally:
        sys.stdout = _old_stdout
    if _backup_dir is not None:
        shutil.rmtree(_backup_dir, ignore_errors=True)

    # Reload in-memory state so subsequent MCP tool calls see the updated index. The
    # same swap the watchdog uses, so both retrievers are reset (#51).
    _reload_indexes()

    # ADR-025 §3: restore preserved stamps for files whose content survived the full
    # rebuild unchanged (hash match). Genuinely-changed files keep the git-backdated
    # stamp run_incremental just wrote. HEAD itself is recorded into index_meta by
    # run_incremental() from the shared chokepoint — the last_indexed_commit.txt write
    # is retired, which is what makes the CLI and MCP entry points finally agree.
    if not changed_files_only and _preserved:
        try:
            with CodeDB(db_path) as _db:
                with _db._tx() as _cur:
                    for _p, (_h, _cc, _au) in _preserved.items():
                        _cur.execute(
                            "UPDATE files SET content_changed_at = ?, authored_at = ? "
                            "WHERE path = ? AND content_hash = ?",
                            (_cc, _au, _p, _h),
                        )
        except sqlite3.Error:
            pass

    mode = "Incremental" if changed_files_only else "Full"
    output = _captured.getvalue().strip()
    return (
        f"--- REINDEX ({mode}) COMPLETE ---\n\n"
        f"{_stale_warning}"
        f"{output}\n\n"
        "In-memory indexes reloaded. All MCP tools now reflect the updated index."
    )


@_tool()
def index_status(since: str = "1d", limit: int = 20) -> str:
    """Report index freshness and which files changed recently (ADR-025 §6).

    Use this to answer "what changed in this codebase lately?" and "is the index
    current with the code?" — e.g. before trusting the other tools on recently
    edited files, or to feed a downstream multi-project context hub. Output is
    agent-parseable: absolute ISO-8601 UTC timestamps and full repo-relative
    paths, never prose you have to re-parse.

    `since` accepts a relative window ("1d", "12h", "30m", "7d") or an absolute
    ISO-8601 timestamp ("2026-07-15T00:00:00Z"). Files whose CONTENT changed after
    that point are listed — content_changed_at, not index-write time — so a file
    re-indexed today but unchanged in a week does not show up. Files with no git
    history (untracked / vendored) carry a NULL stamp and are correctly excluded.
    `limit` caps the list at its newest entries (default 20; 0 lists all); the
    count shown is always the full one.

    Reports: last_verified_at, last_indexed_commit vs current HEAD (with the list
    of diverged files when stale), files_total, and the recent-change list.
    """
    import os
    import subprocess
    from datetime import datetime, timedelta, timezone
    from incremental_indexer import INDEX_DIR, CHUNKER_VERSION, chunker_version_warning
    from db import CodeDB

    db_path = os.path.join(INDEX_DIR, "graph.db")
    if not os.path.exists(db_path):
        return "No index found — run reindex first."

    def _utc(ts: str) -> datetime | None:
        """An ISO-8601 timestamp as an aware UTC datetime; naive means UTC."""
        try:
            dt = datetime.fromisoformat(ts.strip())
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    def _parse_since(s: str) -> datetime:
        """A relative window ("7d", "12h", "30m") or an ISO-8601 timestamp → UTC cutoff.

        Anything else is an error. It used to pass through as a text cutoff, so
        since="garbage" reported "0 files changed" as if that were an answer.
        """
        s = (s or "").strip()
        m = re.fullmatch(r"(\d+)\s*([dhm])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {
                "d": timedelta(days=n),
                "h": timedelta(hours=n),
                "m": timedelta(minutes=n),
            }[unit]
            return datetime.now(timezone.utc) - delta
        dt = _utc(s)
        if dt is None:
            raise ValueError(
                f"since={s!r} is not a window like '7d', '12h', '30m' or an "
                "ISO-8601 timestamp like '2026-07-15T00:00:00Z'"
            )
        return dt

    cutoff_dt = _parse_since(since)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    with CodeDB(db_path) as db:
        last_verified = db.meta_get("last_verified_at") or "(never recorded)"
        last_commit   = db.meta_get("last_indexed_commit")
        files_total   = db.meta_get("files_total") or "?"
        version_warning = chunker_version_warning(db)
        stamped = db._conn.execute(
            "SELECT path, content_changed_at FROM files WHERE content_changed_at IS NOT NULL"
        ).fetchall()
    # Compared as instants, not strings: stamps carry the committer's offset
    # ("…T16:30:47-05:00") and a text comparison against a UTC cutoff was off by it.
    recent = [(path, ts, _utc(ts)) for path, ts in stamped]
    rows = [(path, ts) for path, ts, dt in sorted(
        (r for r in recent if r[2] is not None and r[2] > cutoff_dt),
        key=lambda r: r[2], reverse=True)]

    lines = [
        "--- INDEX STATUS ---",
        f"last_verified_at:    {last_verified}",
        f"files_total:         {files_total}",
        version_warning or f"chunker_version:     {CHUNKER_VERSION} (== current)",
    ]

    # ADR-042 §5: a git-mode index is current when it matches its ref, not HEAD.
    from index_location import git_ref
    try:
        ref = git_ref(os.getcwd())
    except ValueError:
        ref = None
    target = ref or "HEAD"
    if last_commit:
        try:
            curr = subprocess.check_output(
                ["git", "rev-parse", target], text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            ).strip()
            if ref is not None:
                from index_location import ref_display
                lines.append(f"source:              git:{ref_display(os.getcwd(), ref)} "
                             "(a commit, not this folder)")
            if curr == last_commit:
                lines.append(f"last_indexed_commit: {last_commit[:8]} (== {target}; index current)")
            else:
                lines.append(
                    f"last_indexed_commit: {last_commit[:8]}  {target}: {curr[:8]}  ⚠️ STALE"
                    + ("  (the watching server follows it)" if ref else "")
                )
                try:
                    diverged = subprocess.check_output(
                        ["git", "diff", "--name-only", last_commit, target],
                        text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    ).strip()
                    for f in diverged.splitlines():
                        if f:
                            lines.append(f"    diverged: {f}")
                except subprocess.CalledProcessError:
                    pass
            if ref is not None:
                # What the caller's own checkout changed against the indexed commit:
                # the files to Read rather than trust the index for.
                try:
                    mine = subprocess.check_output(
                        ["git", "diff", "--name-only", f"{last_commit}...HEAD"],
                        text=True, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    ).split()
                    lines.append(
                        f"your HEAD vs index:  {len(mine)} file(s) differ — list them with "
                        f"`git diff --name-only {last_commit[:10]}...HEAD`, plus uncommitted "
                        f"edits; Read those instead of trusting index hits"
                    )
                except subprocess.CalledProcessError:
                    pass
        except (FileNotFoundError, subprocess.CalledProcessError):
            lines.append(
                f"last_indexed_commit: {last_commit[:8]} ({target} comparison unavailable)"
            )
    else:
        lines.append("last_indexed_commit: (none recorded)")

    # B-028: each FAISS index must hold exactly one vector per chunk row. Since
    # ADR-037 every reindex repairs a difference (a run killed before its save), so a
    # mismatch that survives a reindex is a bug.
    with CodeDB(db_path) as db:
        row_counts = dict(db._conn.execute(
            "SELECT tier, COUNT(*) FROM chunks GROUP BY tier"
        ).fetchall())
    for tier_num, idx in enumerate(_index().tiers, start=1):
        rows_n = row_counts.get(tier_num, 0)
        if idx.ntotal == rows_n:
            lines.append(f"tier{tier_num}_vectors:       {idx.ntotal} (== chunk rows)")
        else:
            lines.append(
                f"tier{tier_num}_vectors:       {idx.ntotal}  chunk rows: {rows_n}  "
                "⚠️ MISMATCH — the next reindex repairs it (ADR-037)"
            )

    lines.append(f"\nfiles with content changed since {cutoff}  ({len(rows)}):")
    # Every connected session reads this output, and a bulk change (a merge, a
    # source switch) listed hundreds of files. The newest `limit` are shown.
    shown = rows if limit <= 0 else rows[:limit]
    if shown:
        lines.extend(f"    {ts}  {path}" for path, ts in shown)
    else:
        lines.append("    (none)")
    if len(shown) < len(rows):
        lines.append(f"    … {len(rows) - len(shown)} more; pass limit=0 to list all, "
                     "or a shorter `since`")

    return "\n".join(lines)


@_tool()
def find_dead_code(symbol: str, anchor_file: str) -> str:
    """
    Given a symbol and its defining file, determines whether anything in the codebase
    depends on it. A symbol with no consumers, callers, or parallel implementations
    is a strong candidate for removal.

    This inverts analyze_blast_radius: instead of cataloguing the blast radius, it
    explicitly reports when the blast radius is empty — with a clear verdict.

    Inputs:
      symbol      — the function, hook, type, or constant being investigated
      anchor_file — the file where the symbol is defined (e.g. 'edit-ticket-items.ts')

    Output: Either a summary of what references the symbol (proving it's alive) or an
    explicit "dead code candidate" verdict with the empty category list as evidence.
    """
    print(f"\n[MCP] find_dead_code: symbol='{symbol}' anchor='{anchor_file}'")
    # Files importing the anchor, computed once (B-037).
    texts = _texts_by_file()
    importers, _ = _import_relations(anchor_file, texts)

    # Shared RTR surface (ADR-023 §1) instead of raw t1+t2 FAISS: resolved
    # call-graph callers now reach the dead-code scan, so references visible only
    # through the graph are no longer mistaken for absence of a reference.
    _dc_chunks = _search(f"Usage and consumption of {symbol}", top_n=30)

    seen_files: set[str] = set()
    callers: list[str] = []      # imports anchor + references symbol
    consumers: list[str] = []    # references symbol without direct import
    parallels: list[str] = []    # imports anchor but doesn't reference symbol

    for c in _dc_chunks:
        file_path = c.file
        if file_path in seen_files: continue
        seen_files.add(file_path)

        if _is_anchor_path(anchor_file, file_path): continue   # skip the defining file

        file_full = texts.get(file_path, c.text)

        imports_anchor = file_path in importers
        has_symbol = symbol in file_full

        snippet = c.text[:120].replace('\n', ' ').strip() + "..."
        entry = f"- {file_path} ({get_clean_scope({'scope': c.scope, 'text': c.text, 'file': c.file})})\n  [Snippet]: {snippet}\n\n"

        if imports_anchor and has_symbol:
            callers.append(entry)
        elif has_symbol:
            consumers.append(entry)
        elif imports_anchor:
            parallels.append(entry)

    # Exhaustive import sweep to catch callers FAISS ranking missed
    for file_path in sorted(importers - seen_files):
        if file_path not in texts: continue     # an edge left by a file no longer indexed
        if _is_anchor_path(anchor_file, file_path): continue
        seen_files.add(file_path)
        file_full = texts[file_path]
        snippet = file_full[:120].replace('\n', ' ').strip() + "..."
        entry = f"- {file_path}\n  [Snippet]: {snippet}\n\n"
        if symbol in file_full:
            callers.append(entry)
        else:
            parallels.append(entry)

    # --- ADR-017 §7 edge-aware three-state verdict ---
    # Safe direction: deletion is the one verdict whose wrong answer destroys data,
    # so ANY candidate (unresolved) reference BLOCKS "dead" — it downgrades to
    # INSUFFICIENT ("not provably dead"), never a green light.
    verified_callers, candidate_callers = _caller_evidence(symbol, anchor_file)
    text_alive = bool(callers or consumers)
    edge_alive = bool(verified_callers)

    context = f"--- DEAD CODE ANALYSIS ---\n\nSYMBOL: {symbol} | ANCHOR: {anchor_file}\n\n"

    if text_alive or edge_alive:
        context += "✅ VERDICT: SYMBOL IS REFERENCED [VERIFIED]\n"
        context += (f"  `{symbol}` has {len(callers)} textual caller(s), "
                    f"{len(consumers)} consumer(s), and {len(verified_callers)} "
                    f"resolved call-graph reference(s).\n\n")
    elif candidate_callers:
        context += "🟡 VERDICT: NOT PROVABLY DEAD [INSUFFICIENT]\n"
        context += (f"  No resolved reference to `{symbol}` was found, but "
                    f"{len(candidate_callers)} unverified (candidate) reference(s) "
                    f"exist — deletion is unsafe until they are checked. Run "
                    f"verify_candidate_edges('{symbol}', '{anchor_file}').\n\n")
    else:
        context += "🔴 VERDICT: DEAD CODE CANDIDATE [VERIFIED]\n"
        context += f"  No callers, consumers, or call-graph references of `{symbol}` were found outside `{anchor_file}`.\n"
        if parallels:
            context += f"  ({len(parallels)} file(s) import the anchor but do not reference `{symbol}`.)\n"
        context += "\n"

    context += f"1. CALLERS (import anchor + reference `{symbol}`):\n"
    context += "".join(callers) if callers else "  [None found]\n\n"

    context += f"2. CONSUMERS (reference `{symbol}` without direct anchor import):\n"
    context += "".join(consumers) if consumers else "  [None found]\n\n"

    context += "3. PARALLEL (import anchor, no symbol reference):\n"
    context += "".join(parallels) if parallels else "  [None found]\n\n"

    context += "4. RESOLVED CALL-GRAPH REFERENCES (verified edges):\n"
    if verified_callers:
        context += "".join(f"- {n.fqn}\n  [File]: {n.file_path}\n" for n in verified_callers) + "\n"
    else:
        context += "  [None found]\n\n"

    if candidate_callers:
        context += "5. UNVERIFIED REFERENCES (candidate edges — block deletion):\n"
        context += "".join(f"- {n.fqn}\n  [File]: {n.file_path}\n" for n in candidate_callers) + "\n"

    return context


@_tool()
def verify_candidate_edges(symbol: str, anchor_file: str = "") -> str:
    """
    The second pass behind an ADVISORY / INSUFFICIENT verdict (ADR-017 §7.1).

    When analyze_blast_radius or find_dead_code reports *unverified (candidate)*
    references — name-based edges the resolver could not confirm — call this to get
    the actual code behind each one, so YOU can confirm or dismiss it before any
    irreversible action (e.g. deleting a symbol find_dead_code could not clear).

    This tool does ZERO resolution of its own: it fetches each candidate caller's
    source snippet and hands it to you, the verifier. Advisory evidence is enough
    for estimation; this is the opt-in confirmation step.

    Inputs:
      symbol      — the symbol whose candidate references you want to inspect
      anchor_file — (optional) the defining file, to disambiguate same-named symbols

    Output: one entry per candidate edge — (caller FQN, file:line) + code snippet.
    """
    print(f"\n[MCP] verify_candidate_edges: symbol='{symbol}' anchor='{anchor_file}'")
    db = _db()
    verified_callers, candidate_callers = _caller_evidence(symbol, anchor_file)

    context = f"--- CANDIDATE EDGE VERIFICATION: {symbol} ---\n\n"
    if not candidate_callers:
        context += ("✅ No candidate (unresolved) references to verify — "
                    f"`{symbol}` has {len(verified_callers)} resolved reference(s).\n")
        return context

    context += (f"{len(candidate_callers)} unverified reference(s). Inspect each snippet "
                f"and decide whether it is a real use of `{symbol}`:\n\n")
    for node in candidate_callers:
        sym = db.get_symbol(node.fqn)
        snippet = (sym.text if sym and sym.text else "").strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + " …"
        loc = f"{node.file_path}:{node.start_line}" if node.start_line else str(node.file_path)
        context += f"- {node.fqn}\n  [Location]: {loc}\n  [Snippet]:\n{snippet or '  <source unavailable>'}\n\n"

    return context


@_tool()
def what_writes(tag: str, include_readers: bool = False) -> str:
    """
    Given a PLC tag, reports EXACTLY which routines write it and at which rungs.

    This is the question a controls engineer asks first on a support call: an alarm
    will not clear, a valve will not open, a sequence will not advance — so what
    sets that bit? Answer it one hop at a time and let the human prune: the next
    question is `what_writes` on whichever condition looks wrong.

    **This is a lookup, not a search.** It reads resolved `writes` edges straight
    out of the graph — no embedding, no ranking, no reranker. Every result is a
    real write position the extractor resolved, and the list is complete. Do not
    reach for semantic_code_search or trace_data_flow for this question; both rank,
    and a ranked answer to "what writes X" can silently omit a writer.

    Inputs:
      tag             — the tag as an engineer types it (e.g. 'Valve_Open'), or a
                        scoped FQN ('Filler.Step') for a program-scoped tag
      include_readers — also list where the tag is READ. Off by default because
                        widely-read tags produce long lists; turn it on when you
                        are asking "what uses this" rather than "what sets this"

    Output: each writing routine with its rung numbers, plus alias/module-I/O
    mapping when the tag is an alias.

    What this CANNOT tell you: which of several conditions is *currently* true.
    That needs a live connection to the controller. This enumerates the candidates
    — which is the half you can do from the phone without going online.
    """
    print(f"\n[MCP] what_writes: tag='{tag}' readers={include_readers}")
    db = _db()

    writers = db.get_edges_to(tag, "writes")
    readers = db.get_edges_to(tag, "reads")
    # Outbound: the alias edge runs tag -> module I/O, so this tag's own hardware
    # endpoint is what leaves it. Asking which tags alias ONTO it is a different
    # question that would answer just as plausibly and be wrong.
    aliases = db.get_edges_from(tag, "alias_of")

    if not writers and not readers and not aliases:
        known = [s.fqn for s in db.search_symbols(tag)][:8]
        out = f"No read or write edge targets '{tag}'.\n\n"
        if known:
            out += "Symbols matching that name:\n"
            out += "".join(f"- {f}\n" for f in known)
            out += "\nIf one of these is the tag you meant, re-run with its full FQN.\n"
        else:
            out += (
                "Nothing in the index carries that name. Either the controller is "
                "not indexed, or the tag is spelled differently in the program than "
                "on the HMI — try the abbreviated form.\n"
            )
        return out

    def _sites(routine_fqns: list[str], kind: str) -> dict[str, list[int]]:
        """routine -> sorted rung numbers, from the reference table."""
        by_routine: dict[str, list[int]] = {f: [] for f in routine_fqns}
        for r in db.get_references_to(tag, kind):
            ctx = r["context_fqn"]
            if ctx in by_routine:
                by_routine[ctx].append(r["line"])
        return {f: sorted(v) for f, v in by_routine.items()}

    out = f"WRITES TO '{tag}'\n{'=' * (len(tag) + 12)}\n\n"

    if aliases:
        out += (
            "This tag is an ALIAS. It is a name for a hardware endpoint, so the\n"
            "value is driven by the I/O module, not only by ladder logic:\n"
        )
        out += "".join(f"  {tag} -> {a}\n" for a in aliases) + "\n"

    if not writers:
        out += (
            "No routine writes it.\n\n"
            "For an alias that is expected — an input alias is written by the\n"
            "module. Otherwise the tag is read-only in this controller, and\n"
            "whatever sets it is outside this program: an HMI write, a message\n"
            "from another controller, or a produced/consumed tag.\n"
        )
    else:
        write_sites = _sites(writers, "WRITE")
        n_rungs = sum(len(v) for v in write_sites.values())
        out += (
            f"{len(writers)} routine(s) write it"
            + (f", at {n_rungs} rung(s):\n\n" if n_rungs else ":\n\n")
        )
        for fqn in writers:
            rungs = write_sites.get(fqn) or []
            if rungs:
                where = "rung " + ", ".join(str(r) for r in rungs)
            else:
                where = "rung unknown (no rung-level reference recorded)"
            out += f"- {fqn}\n  [{where}]\n"
        out += "\n"

        if len(writers) > 1:
            out += (
                "More than one routine writes this tag. On a scanning controller\n"
                "the LAST write in scan order wins, so program execution order\n"
                "decides the value — check the task and program scheduling before\n"
                "assuming the rung you found is the one that took effect.\n\n"
            )

    if include_readers:
        read_sites = _sites(readers, "READ")
        n_rungs = sum(len(v) for v in read_sites.values())
        out += f"READ BY {len(readers)} routine(s), at {n_rungs} rung(s):\n\n"
        for fqn in readers:
            rungs = read_sites.get(fqn) or []
            where = ", ".join(str(r) for r in rungs) if rungs else "?"
            out += f"- {fqn}\n  [rung {where}]\n"
        out += "\n"
    elif readers:
        out += f"Read by {len(readers)} routine(s) — pass include_readers=True.\n\n"

    out += (
        "NEXT HOP: pick the condition that looks wrong in one of those rungs and\n"
        "run what_writes on IT. Go one hop at a time — an automatic backward\n"
        "trace fans out fast (the survey corpus has a tag written 177 times).\n"
    )
    return out


@_tool()
def find_unabstracted_collection_reads(collection_name: str, canonical_symbols_csv: str) -> str:
    """
    Given a Firestore collection name, finds every place it is READ without going through
    the canonical abstraction layer.

    Use this to enforce "all reads of X must go through Y" rules. It is more precise than
    manually chaining trace_data_flow + detect_pattern_violations: it focuses on reads only
    and cross-references the approved abstraction entry points automatically.

    Inputs:
      collection_name       — Firestore collection (e.g. 'aggregatedInventory')
      canonical_symbols_csv — comma-separated approved abstraction entry points
                              (e.g. 'useAggregatedInventory, getAggregatedInventory')

    Output tiers:
      Compliant  — reads through a canonical symbol
      Violation  — direct Firestore reads bypassing abstraction (flagged with layer label)
      Ambiguous  — reads through an intermediate variable that can't be statically resolved
    """
    print(f"\n[MCP] find_unabstracted_collection_reads: collection='{collection_name}'")
    canonical_symbols = [s.strip() for s in canonical_symbols_csv.split(',') if s.strip()]

    # Warn if caller supplied write-path symbols instead of read-path abstractions.
    # This tool enforces "reads must go through Y" — canonical symbols should be hooks or
    # getters (e.g. useAggregatedInventory, getAggregatedInventory), not writers.
    _WRITE_VERB_PREFIXES = ('write', 'set', 'update', 'delete', 'add', 'save', 'commit', 'put')
    _write_canon = [s for s in canonical_symbols if s.lower().startswith(_WRITE_VERB_PREFIXES)]
    _canon_warning = ""
    if _write_canon:
        _canon_warning = (
            f"⚠️  INPUT WARNING: canonical symbol(s) [{', '.join(_write_canon)}] appear to be "
            f"write-path functions, not read-path abstractions.\n"
            f"   This tool enforces 'reads must go through Y'. Canonical symbols should be hooks "
            f"or getters (e.g. 'useAggregatedInventory', 'getAggregatedInventory').\n"
            f"   Results below may be misleading — cloud functions that write to the collection "
            f"will appear as violations even though they are the canonical write path.\n\n"
        )

    # Direct Firestore read patterns referencing this specific collection
    _DIRECT_READ_RE = re.compile(
        rf"collection\s*\([^)]*['\"]{{0,1}}{re.escape(collection_name)}['\"]{{0,1}}"
        rf"|getDocs\s*\([^)]*{re.escape(collection_name)}"
        rf"|getDoc\s*\([^)]*{re.escape(collection_name)}"
        rf"|query\s*\([^)]*{re.escape(collection_name)}"
        rf"|onSnapshot\s*\([^)]*{re.escape(collection_name)}",
        re.IGNORECASE,
    )
    # Indirect: variable assigned a collection() call (static resolution not possible)
    _INDIRECT_READ_RE = re.compile(
        r"(?:const|let|var)\s+\w+\s*=\s*collection\s*\(",
        re.IGNORECASE,
    )
    # Write detection to filter out write-only files
    _WRITE_RE = re.compile(
        r"(\w*(?:transaction|batch|db|firestore|admin|ref|tx))\.(set|add|update)\(",
        re.IGNORECASE,
    )

    # Shared RTR surface (ADR-023 §1) instead of raw t1+t2 FAISS.
    _ur_chunks = _search(
        f"reading from {collection_name} collection Firestore query get", top_n=40
    )

    seen_files: set[str] = set()
    compliant_data: list[str] = []
    violation_data: list[str] = []
    ambiguous_data: list[str] = []

    for c in _ur_chunks:
        if collection_name not in c.text: continue

        file_path = c.file
        if file_path in seen_files: continue
        seen_files.add(file_path)

        file_full = "".join(d['text'] + "\n" for d in _index().doc_store.docs.values() if d['file'] == file_path)
        if collection_name not in file_full: continue

        layer = _detect_layer(file_path, file_full)
        has_canonical = any(sym in file_full for sym in canonical_symbols)
        used_canonical = [sym for sym in canonical_symbols if sym in file_full]
        has_direct_read = bool(_DIRECT_READ_RE.search(file_full))
        has_indirect = bool(_INDIRECT_READ_RE.search(file_full))
        # Skip write-only producers — they're not reads
        is_write_only = bool(_WRITE_RE.search(file_full)) and not has_direct_read and not has_canonical and not has_indirect

        if is_write_only:
            continue

        snippet = c.text[:120].replace('\n', ' ').strip() + "..."
        clean_scope = get_clean_scope({'scope': c.scope, 'text': c.text, 'file': c.file})

        if has_canonical and not has_direct_read:
            compliant_data.append(
                f"- [{layer}] {file_path} ({clean_scope})\n"
                f"  [Via]: {', '.join(used_canonical)}\n"
                f"  [Snippet]: {snippet}\n\n"
            )
        elif has_direct_read and has_canonical:
            # Both present — partial compliance, flag it
            violation_data.append(
                f"- [{layer}] {file_path} ({clean_scope})\n"
                f"  [Reason]: Direct read AND canonical symbol both present — partial compliance\n"
                f"  [Via canonical]: {', '.join(used_canonical)}\n"
                f"  [Snippet]: {snippet}\n\n"
            )
        elif has_direct_read:
            violation_data.append(
                f"- [{layer}] {file_path} ({clean_scope})\n"
                f"  [Reason]: Direct Firestore read of `{collection_name}` without canonical symbol\n"
                f"  [Snippet]: {snippet}\n\n"
            )
        elif has_indirect and not has_canonical:
            ambiguous_data.append(
                f"- [{layer}] {file_path} ({clean_scope})\n"
                f"  [Reason]: Reads via intermediate variable — abstraction compliance unverifiable\n"
                f"  [Snippet]: {snippet}\n\n"
            )

    context = (
        f"--- UNABSTRACTED COLLECTION READ ANALYSIS ---\n\n"
        f"COLLECTION: {collection_name}\n"
        f"CANONICAL:  {canonical_symbols_csv}\n\n"
        f"{_canon_warning}"
    )
    context += "1. COMPLIANT (reads through canonical abstraction):\n"
    context += "".join(compliant_data) if compliant_data else "  [None found]\n\n"
    context += "2. VIOLATIONS (direct reads bypassing abstraction):\n"
    context += "".join(violation_data) if violation_data else "  [None — no violations detected]\n\n"
    context += "3. AMBIGUOUS (indirect reads — compliance unverifiable):\n"
    context += "".join(ambiguous_data) if ambiguous_data else "  [None]\n\n"

    return context


@_tool()
def map_module_communities(target_path: str = "", min_community_size: int = 3,
                           suggest_splits: bool = False) -> str:
    """Map the codebase into natural module communities and flag god-objects.

    WHEN TO CALL: the user asks how the codebase is *structured*, which classes
    have grown too large, where to split a module, or what the high-coupling
    chokepoints are. Complements analyze_blast_radius (single-symbol impact) and
    investigate_architecture (narrative) with a whole-graph structural view.

    By default returns a DESCRIPTIVE report: community map (labeled, raw cohesion)
    and god-objects (betweenness + fan-in/out + communities spanned). Pass
    suggest_splits=True to ALSO emit proposed module decompositions — each stamped
    '[HEURISTIC — unverified]'. Also writes the DSM view to
    .code-index/architecture_matrix.html when the visualization layer is available.

    Inputs:
      target_path        — optional subtree to scope analysis to (e.g. 'src/'); empty = whole graph.
      min_community_size — communities smaller than this are omitted from the body (still counted).
      suggest_splits     — when True, also emit heuristic module-split proposals.

    NOTE: this is an EXPLORATORY structural view, not a verified accuracy claim.
    A measured quality bar for this output is deferred to ADR-008.
    """
    print(f"\n[MCP] map_module_communities: path='{target_path}' "
          f"min_size={min_community_size} splits={suggest_splits}")
    import os
    from incremental_indexer import INDEX_DIR
    from db import CodeDB
    import graph_analytics
    from graph_report import render_report

    db_path = os.path.join(INDEX_DIR, "graph.db")
    if not os.path.exists(db_path):
        return (
            "--- MODULE COMMUNITY MAP ---\n\n"
            "No index found — `graph.db` does not exist. Run `reindex` first, then "
            "call this tool again.\n"
        )

    with CodeDB(db_path) as db:
        analysis = graph_analytics.analyze(
            db, target_path=target_path, include_splits=suggest_splits
        )
        report = render_report(
            analysis, target_path=target_path, min_community_size=min_community_size
        )
        # DSM visualization is a side-effect (ADR-006 §3, Phase 3). Guard the import
        # so this tool is fully functional before the viz layer lands, and lights up
        # automatically once src/graph_viz.py exists.
        try:
            from graph_viz import render_dsm
            dsm_path = render_dsm(
                analysis, db,
                out_path=os.path.join(INDEX_DIR, "architecture_matrix.html"),
            )
            report += (
                "\n---\n\n**Design Structure Matrix** written to "
                f"`{dsm_path}` — open in a browser for the interactive coupling matrix.\n"
            )
        except ImportError:
            report += "\n---\n\n_DSM visualization pending (ADR-006 Phase 3)._\n"

    return report


# ---------------------------------------------------------------------------
# File watchdog — auto-reindex on source changes
# ---------------------------------------------------------------------------

# ADR-036: one run_incremental at a time in this process. The watchdog and the
# reindex tool both take it, so a save during a running reindex waits for that
# run to finish instead of starting a second one beside it (B-032). Other
# processes on the same index are B-033.
_reindex_lock = threading.Lock()


def _snapshot_index(index_dir: str, db_path: str) -> str:
    """Copy graph.db and every .faiss file in `index_dir` to a backup directory.

    The database is copied with SQLite's backup API, which reads a consistent
    snapshot through the WAL. Returns the backup directory.
    """
    backup_dir = os.path.join(index_dir, ".pre-full-reindex")
    shutil.rmtree(backup_dir, ignore_errors=True)   # left by a killed earlier run
    os.makedirs(backup_dir)
    if os.path.exists(db_path):
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(os.path.join(backup_dir, "graph.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for name in os.listdir(index_dir):
        if name.endswith(".faiss"):
            shutil.copy2(os.path.join(index_dir, name), os.path.join(backup_dir, name))
    return backup_dir


def _restore_index(backup_dir: str, index_dir: str, db_path: str) -> None:
    """Put back what `_snapshot_index` saved, then delete the backup."""
    saved_db = os.path.join(backup_dir, "graph.db")
    if os.path.exists(saved_db):
        src = sqlite3.connect(saved_db)
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    saved_faiss = {n for n in os.listdir(backup_dir) if n.endswith(".faiss")}
    for name in os.listdir(index_dir):
        if name.endswith(".faiss") and name not in saved_faiss:
            os.remove(os.path.join(index_dir, name))
    for name in saved_faiss:
        os.replace(os.path.join(backup_dir, name), os.path.join(index_dir, name))
    shutil.rmtree(backup_dir, ignore_errors=True)


def _reload_indexes() -> None:
    """Hot-swap the index after a reindex run, or a save by another process.

    The new IndexState is built outside the lock, so the slow load doesn't block
    anything, then swapped in as one reference. A call already running keeps the
    state it bound at entry (ADR-047); the old state is freed when the last such
    call returns.
    """
    global _state
    new = _load_state()
    with _reload_lock:
        _state = new


class _ReindexDebouncer:
    """
    Collapses a burst of rapid file-change events into a single reindex call.

    A formatter run, a git checkout, or a multi-file save can fire dozens of
    events in under a second.  Without debouncing each event would spawn a
    separate run_incremental() invocation that races the previous one for the
    SQLite lock.  Instead, every incoming event resets a timer; the reindex
    fires only after `delay` seconds of silence.

    The timer only collapses a burst. An event that arrives while a reindex is
    running starts a new timer, so the run itself takes `_reindex_lock`: the
    follow-up waits for the one in flight (ADR-036). At most one run waits;
    later events fold into it, since it reads the disk only once it starts.
    """

    def __init__(self, delay: float = 3.0) -> None:
        self._delay  = delay
        self._timer: threading.Timer | None = None
        self._lock   = threading.Lock()
        self._queued = False    # a fired run is waiting for _reindex_lock

    def schedule(self, delay: float | None = None) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay if delay is None else delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if self._queued:
                return          # the queued run scans the disk when it starts, so it sees this change too
            self._queued = True
        try:
            from datetime import datetime  # B-049: stamp the watchdog's start/complete lines
            from incremental_indexer import INDEX_DIR, run_incremental
            from index_lock import WRITE_LOCK, describe_holder, holder, try_acquire
            with _reindex_lock:
                with self._lock:
                    self._queued = False    # from here on, a new change needs a new run
                # ADR-038 (B-033): a first build is an hour of GPU work nobody sees
                # from inside a server. Only a finished build is kept current here.
                if not _index_built(INDEX_DIR):
                    print("[Watchdog] No finished index here yet; skipped. Build it once "
                          "with `code-indexer` from a terminal.")
                    return
                lock = try_acquire(INDEX_DIR, WRITE_LOCK, "watchdog")
                if lock is None:
                    print(f"[Watchdog] Another process is writing this index "
                          f"({describe_holder(holder(INDEX_DIR, WRITE_LOCK) or {})}); "
                          f"retrying in {_BUSY_RETRY_S:.0f} s.")
                    self.schedule(_BUSY_RETRY_S)
                    return
                with lock:
                    print(f"\n[{datetime.now():%H:%M:%S}] [Watchdog] Change detected — "
                          f"running incremental reindex...")
                    run_incremental(interactive=False)   # ADR-026 §5 — nobody is watching
                    _reload_indexes()
            print(f"[{datetime.now():%H:%M:%S}] [Watchdog] Reindex complete — "
                  f"in-memory indexes reloaded.\n")
        except Exception as exc:
            print(f"[Watchdog] Reindex failed: {exc}\n")


# ADR-038: how long a watchdog waits before retrying when another process is writing.
_BUSY_RETRY_S = 60.0
# ADR-038: how often a server without the watch lock checks whether it has freed up.
_WATCH_RETRY_S = 60.0


def _index_built(index_dir: str) -> bool:
    """True once a build has finished here: index_meta has last_verified_at (ADR-025 §4)."""
    db_path = os.path.abspath(os.path.join(index_dir, "graph.db"))
    if not os.path.exists(db_path):
        return False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT value FROM index_meta WHERE key = 'last_verified_at'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


if _WATCHDOG_AVAILABLE:
    class _CodeChangeHandler(FileSystemEventHandler):
        """
        Filters OS filesystem events down to indexable source files, then
        schedules a debounced reindex.

        Shares the inclusion/exclusion decision with
        `incremental_indexer.scan_disk()` by calling the same `scan_policy` export
        (ADR-026 §2). It used to re-implement that logic by hand from the raw
        constants, which is a rule that can drift — and had.

        Handles created, modified, deleted, and moved (rename) events.
        """

        def __init__(self, debouncer: _ReindexDebouncer, repo_root: str) -> None:
            self._debouncer  = debouncer
            self._repo_root  = repo_root.replace("\\", "/")

        def _is_relevant(self, path: str) -> bool:
            from scan_policy import scan_policy
            try:
                rel = os.path.relpath(path, self._repo_root).replace("\\", "/")
            except ValueError:
                return False  # different drive — can't be under repo root
            if rel.startswith("../"):
                return False  # outside the repo root entirely
            return scan_policy(self._repo_root).is_scannable(rel)

        def _maybe_schedule(self, path: str) -> None:
            if self._is_relevant(path):
                self._debouncer.schedule()

        def on_created(self, event):
            if not event.is_directory:
                self._maybe_schedule(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                self._maybe_schedule(event.src_path)

        def on_deleted(self, event):
            if not event.is_directory:
                self._maybe_schedule(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                # Fire if either endpoint is relevant (rename into/out-of scope)
                if self._is_relevant(event.src_path) or self._is_relevant(event.dest_path):
                    self._debouncer.schedule()


def start_watchdog(repo_path: str | None = None, debounce_seconds: float = 3.0):
    """
    Start a background file watcher that triggers an incremental reindex
    whenever an indexable source file changes.

    Uses ReadDirectoryChangesW on Windows (zero CPU overhead — kernel pushes
    events; the process does not poll).

    Returns the running Observer, or None if watchdog is not installed.
    """
    if not _WATCHDOG_AVAILABLE:
        print("[Watchdog] 'watchdog' package not found — auto-reindex disabled.")
        print("           pip install watchdog")
        return None

    if repo_path is None:
        repo_path = os.getcwd()

    # ADR-038 (B-053): no watchdog in a worktree made for parallel work.
    from index_lock import WATCH_LOCK, describe_holder, holder, try_acquire, worktree_refusal
    refusal = worktree_refusal(repo_path)
    if refusal:
        print(f"[Watchdog] Off: {refusal}")
        return None

    # ADR-038: one watchdog per index. The others serve the read tools, and one of
    # them takes over when the watching server exits (the OS frees its lock).
    from index_location import index_dir as _index_dir
    index_dir = os.path.join(repo_path, _index_dir())    # absolute in git mode (ADR-042 §3)
    watch_lock = try_acquire(index_dir, WATCH_LOCK, "watchdog")
    if watch_lock is None:
        print(f"[Watchdog] Standby — another server watches this index "
              f"({describe_holder(holder(index_dir, WATCH_LOCK) or {})}); "
              f"checking again every {_WATCH_RETRY_S:.0f} s.")
        _retry_watch(repo_path, debounce_seconds, index_dir)
        return None

    return _start_observer(repo_path, debounce_seconds, watch_lock)


def _retry_watch(repo_path: str, debounce_seconds: float, index_dir: str) -> None:
    """Try for the watch lock again in _WATCH_RETRY_S, until this server gets it."""
    from index_lock import WATCH_LOCK, try_acquire

    def _attempt() -> None:
        lock = try_acquire(index_dir, WATCH_LOCK, "watchdog")
        if lock is None:
            _retry_watch(repo_path, debounce_seconds, index_dir)
            return
        print("[Watchdog] The watching server exited; this one takes over.")
        _start_observer(repo_path, debounce_seconds, lock)

    timer = threading.Timer(_WATCH_RETRY_S, _attempt)
    timer.daemon = True
    timer.start()


_observers: list = []   # observers started after startup, kept alive with their locks


def _start_observer(repo_path: str, debounce_seconds: float, watch_lock):
    from index_location import git_ref
    ref = git_ref(repo_path)
    if ref is not None:
        return _start_ref_poller(repo_path, ref, watch_lock)
    debouncer = _ReindexDebouncer(delay=debounce_seconds)
    handler   = _CodeChangeHandler(debouncer, repo_path)
    observer  = Observer()
    observer.schedule(handler, repo_path, recursive=True)
    observer.daemon = True
    observer.start()
    _observers.append((observer, watch_lock))   # the lock is held for the life of the process
    print(f"[Watchdog] Active — watching '{repo_path}' (debounce={debounce_seconds}s)")
    return observer


class _RefPoller:
    """Git mode's watchdog (ADR-042 §4): follow a ref instead of watching files.

    Every ``interval`` seconds, resolve the ref (a local ``git rev-parse``, about
    10 ms) and compare it with the commit the index was built from. When it moved,
    the debouncer runs the usual incremental reindex, under the same locks and the
    same "only a finished index" rule as the file watcher. Nothing touches the
    network: the index follows the ref once anything on the machine fetches.
    """

    def __init__(self, repo_path: str, ref: str, interval: float,
                 debouncer: "_ReindexDebouncer") -> None:
        self.repo_path = repo_path
        self.ref = ref
        self.interval = interval
        self.debouncer = debouncer
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="ref-poller", daemon=True)

    def indexed_commit(self) -> str | None:
        from incremental_indexer import INDEX_DIR
        db_path = os.path.abspath(os.path.join(INDEX_DIR, "graph.db"))
        if not os.path.exists(db_path):
            return None
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT value FROM index_meta "
                                  "WHERE key = 'last_indexed_commit'").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def check(self) -> bool:
        """Schedule a reindex if the ref moved. True if one was scheduled."""
        from source import GitError, resolve_ref
        try:
            current = resolve_ref(self.repo_path, self.ref)
        except GitError:
            return False            # the ref is gone for now (mid-fetch); look again later
        if current == self.indexed_commit():
            return False
        print(f"[Watchdog] {self.ref} moved to {current[:10]}; reindexing.")
        self.debouncer.schedule(0.0)
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception as exc:        # never let the poller die quietly
                print(f"[Watchdog] ref check failed: {exc}")

    def start(self) -> "_RefPoller":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def _start_ref_poller(repo_path: str, ref: str, watch_lock):
    from config import index_ref_poll_s
    poller = _RefPoller(repo_path, ref, index_ref_poll_s(), _ReindexDebouncer(delay=0.0))
    poller.start()
    _observers.append((poller, watch_lock))     # the lock is held for the life of the process
    from index_location import ref_display
    print(f"[Watchdog] Active — following {ref_display(repo_path, ref)} "
          f"(checked every {poller.interval:.0f} s)")
    return poller


def _utf8_stdio() -> None:
    """Make print() safe for the indexer's own output under an MCP client (ADR-036).

    A client that launches this server over stdio on Windows gives it pipes, and a
    pipe's text encoding is the ANSI code page (cp1252), not UTF-8. The indexer's
    first line is a "━━" banner, so every watchdog reindex died on its first print
    with UnicodeEncodeError. The protocol is unaffected: it has its own UTF-8
    writer on a private copy of the pipe (see _claim_stdout).
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass    # not a TextIOWrapper (already replaced by a harness); leave it


def _detach_stdin() -> None:
    """Keep the MCP stdin pipe away from every child process (ADR-036).

    On Windows a process started without handle inheritance still gets its
    parent's standard input handle. The MCP transport keeps a read pending on
    that pipe, and a spawned child that touches its stdin at startup waits
    behind that read forever. multiprocessing's bootstrap closes stdin, so the
    summarizer's worker (IsolatedChunkSummarizer) hung at startup: every
    watchdog reindex with summaries on stalled at "[summarize]", with the
    worker at 11 MB and no CPU. ADR-031's git calls hung the same way.

    The transport gets a private, non-inheritable copy of the pipe, and fd 0
    (and with it the process's standard input handle) becomes NUL. Children
    then inherit NUL, and nothing else in this process reads stdin.
    """
    import io
    import sys
    if os.name != "nt":
        return
    try:
        fd = os.dup(0)
    except OSError:
        return          # no stdin at all; nothing to protect
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)     # the CRT also points STD_INPUT_HANDLE at NUL
    os.close(nul)
    sys.stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(fd, "rb")),
                                 encoding="utf-8", errors="replace")


def _claim_stdout():
    """Keep stdout for the protocol alone; everything else goes to stderr (B-039).

    The MCP spec says a stdio server must not write anything to stdout that is not
    a protocol message. The tools print progress, a watchdog reindex prints its
    whole log, and a child process (the summarizer's worker, git) inherits stdout.
    All of it reached the client as lines that are not JSON-RPC, and the Python
    client logged a validation error for each one.

    The protocol gets a private, non-inheritable copy of the pipe, and fd 1 (and
    with it the process's standard output handle) becomes stderr, the channel the
    spec gives servers for logging. print() and children then write there with no
    change to them. Returns the protocol's writer, or None to leave stdout alone.
    """
    import io
    import sys
    try:
        sys.stdout.flush()
        fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        return None     # no usable stdout/stderr; let the transport use sys.stdout
    return io.TextIOWrapper(io.BufferedWriter(io.FileIO(fd, "wb")), encoding="utf-8")


async def _serve_stdio(protocol_out) -> None:
    """FastMCP.run_stdio_async, with the protocol written to ``protocol_out``."""
    import anyio
    from mcp.server.stdio import stdio_server
    stdout = anyio.wrap_file(protocol_out) if protocol_out is not None else None
    async with stdio_server(stdout=stdout) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream, write_stream, mcp._mcp_server.create_initialization_options()
        )


def main() -> None:
    import anyio
    _utf8_stdio()
    _detach_stdin()
    protocol_out = _claim_stdout()
    start_watchdog()
    anyio.run(_serve_stdio, protocol_out)


if __name__ == "__main__":
    main()
