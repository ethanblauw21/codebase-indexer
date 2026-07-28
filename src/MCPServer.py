import os
import re
import threading
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# H4 — watchdog reload guard
# ---------------------------------------------------------------------------
# Build new index objects before acquiring the lock (IO-bound, can be slow),
# then swap the globals atomically.  In-flight tool calls that already hold a
# reference to the old FAISS/DocumentStore objects complete against that
# generation; new calls see the freshly loaded generation.
_reload_lock = threading.Lock()
_index_generation = 0   # incremented on every swap; useful for logging

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
from core import jina_tokenizer, MultiIndexManager, DocumentStore
from hybrid_retriever import HybridRetriever, RetrievedChunk
from iterative_retriever import IterativeRetriever, RetrievalSession

# Initialize MCP Server
mcp = FastMCP("Local Codebase RAG")

# Lazy index state — loaded on first tool call so the MCP handshake
# completes instantly even when FAISS indexes / doc_store.json are large.
index_manager = None
doc_store = None
t1_index = None
t2_index = None
t3_index = None


def _ensure_indexes():
    global index_manager, doc_store, t1_index, t2_index, t3_index
    if doc_store is not None:
        return
    index_manager = MultiIndexManager()
    doc_store = DocumentStore()
    t1_index = index_manager.load_or_create("tier1_surgical")
    t2_index = index_manager.load_or_create("tier2_component")
    t3_index = index_manager.load_or_create("tier3_architectural")

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
@mcp.tool()
def semantic_code_search(query: str) -> str:
    """
    CRITICAL: Use this tool FIRST before using standard file reading or grep.
    Use this to understand system architecture, find where specific logic is implemented,
    or trace data flow across the codebase.
    This queries a local AI Vector Database that understands semantic concepts,
    React lifecycles, and cross-file dependencies far better than string matching.
    Prefer investigate_architecture when you need a complete picture of how a concept
    flows through the system — it wraps this same retrieval pipeline in a multi-round
    agentic loop with query enrichment. Use this tool for quick, single-pass lookups
    where that overhead is not needed.
    """
    print(f"\n[MCP] Tool invoked by LLM for query: '{query}'")
    _ensure_indexes()

    # Candidate generation via the shared RTR surface (ADR-023 §1): multi-tier RRF
    # plus resolved call-graph neighbours + import-corroboration, not FAISS-only.
    chunks = _search(query, top_n=15)

    # Context Packing (Protecting your 8GB Local Model's VRAM)
    # 8B models easily crash if you feed them more than 8k tokens.
    # We cap the returned context strictly at 4000 tokens to be safe.
    context = f"--- VECTOR DATABASE RESULTS FOR: '{query}' ---\n\n"
    current_tokens = 0
    MAX_TOKENS = 4000

    for c in chunks:
        chunk_text = f"--- FILE: {c.file} | SCOPE: {c.scope} ---\n{c.text}\n\n"
        tokens = jina_tokenizer.count_tokens(chunk_text)

        if current_tokens + tokens < MAX_TOKENS:
            context += chunk_text
            current_tokens += tokens
        else:
            context += "[Note: Further context truncated to protect token limits.]\n"
            break

    return context

@mcp.tool()
def find_similar_code(code_snippet: str) -> str:
    """
    Use this tool to find duplicate or mathematically similar code across the project.
    Pass in a raw snippet of code. It will stratify results into Origin, Callers, Parallels, and Weak matches.
    """
    print("\n[MCP] Searching for duplicates/callers of provided snippet...")
    _ensure_indexes()
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

@mcp.tool()
def analyze_blast_radius(anchor_file: str, target_symbol: str) -> str:
    """
    Use this tool when planning a refactor.
    It requires TWO inputs to ground the search:
    1. anchor_file (e.g., 'inventory-list.tsx') - The file where the change originates.
    2. target_symbol (e.g., 'activeView') - The specific concept, state, or interface being changed.
    """
    print(f"\n[MCP] Analyzing blast radius anchored at '{anchor_file}' for '{target_symbol}'")
    _ensure_indexes()
    norm_anchor = anchor_file.lower().replace('\\', '/').split('/')[-1]
    anchor_base = norm_anchor.replace('.tsx', '').replace('.ts', '').replace('.jsx', '').replace('.js', '')

    # 1. PRE-FETCH ANCHOR TEXT
    # We must know what the anchor imports to apply the "Primitive Directional Filter"
    anchor_text = ""
    for doc in doc_store.docs.values():
        if norm_anchor in doc['file'].lower() and doc['tier'] == 'tier2_component':
            anchor_text += doc['text'] + "\n"

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

        norm_path = file_path.lower().replace('\\', '/')
        file_base = norm_path.split('/')[-1].replace('.tsx', '').replace('.ts', '').replace('.jsx', '').replace('.js', '')
        doc_text = doc['text']
        # Aggregate all chunks for this file so import statements in part_1 are
        # visible when the FAISS hit landed on part_4.
        file_full_text = "".join(d['text'] + "\n" for d in doc_store.docs.values() if d['file'] == file_path)

        # --- STRUCTURAL & SEMANTIC CHECKS ---
        is_anchor = norm_anchor in norm_path
        has_symbol = target_symbol in doc_text

        # Structural: Does this file import the anchor? (downstream — true dependent)
        # re.DOTALL required — TS multi-line imports span `import {` to `} from "..."` across lines
        imports_anchor = bool(re.search(rf"(import|require).*?['\"].*?{re.escape(anchor_base)}.*?['\"]", file_full_text, re.IGNORECASE | re.DOTALL))

        # Structural: Does the anchor import this file? (upstream — primitive/dependency)
        is_imported_by_anchor = bool(re.search(rf"(import|require).*?['\"].*?{re.escape(file_base)}.*?['\"]", anchor_text, re.IGNORECASE | re.DOTALL)) if file_base and anchor_text else False

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
    all_file_paths = {doc['file'] for doc in doc_store.docs.values()}
    for file_path in all_file_paths:
        if file_path in seen_files: continue
        file_full_text = "".join(d['text'] + "\n" for d in doc_store.docs.values() if d['file'] == file_path)
        if not re.search(rf"(import|require).*?['\"].*?{re.escape(anchor_base)}.*?['\"]", file_full_text, re.IGNORECASE | re.DOTALL):
            continue
        seen_files.add(file_path)
        rep_doc = next(d for d in doc_store.docs.values() if d['file'] == file_path)
        has_symbol = target_symbol in file_full_text
        snippet = rep_doc['text'][:150].replace('\n', ' ').strip() + "..."
        evidence = [f"Imports `{anchor_base}`"]
        if has_symbol: evidence.append(f"Contains symbol `{target_symbol}`")
        dependents_data.append(f"- {file_path}\n  [Evidence]: {' + '.join(evidence)}\n  [Snippet]: {snippet}\n\n")

    # Fallback to force anchor if FAISS missed it
    if not anchor_data and anchor_text:
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

@mcp.tool()
def detect_pattern_violations(canonical_snippet: str, enforced_symbols_csv: str, ignore_regex: str = "") -> str:
    """
    Finds code that SHOULD follow a pattern but deviates.
    - enforced_symbols_csv: Comma-separated valid symbols (e.g., 'writeTransactionLogTx, writeTransactionLog').
    - ignore_regex: Regex to skip files (e.g., '^on[-A-Z]' for triggers).
    """
    print(f"\n[MCP] Scanning for violations missing '{enforced_symbols_csv}'...")
    _ensure_indexes()
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
        for _kw_doc in doc_store.docs.values():
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

@mcp.tool()
def trace_data_flow(target_symbol: str) -> str:
    """
    Traces data lifecycle. v11.0: Broad definition lookup + Dynamic Producer Tracing.
    """
    print(f"\n[MCP] Running v11.0 trace for '{target_symbol}'...")
    _ensure_indexes()
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
    for _d in doc_store.docs.values():
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
        for _, doc in doc_store.docs.items():
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

# Lazy singleton: built on first invocation. Reranking is off by default
# (see [reranker].enabled in indexer.toml); HybridRetriever() reads that config.
_hybrid_retriever: "HybridRetriever | None" = None
_iterative_retriever: "IterativeRetriever | None" = None


def _get_hybrid_retriever() -> HybridRetriever:
    global _hybrid_retriever
    if _hybrid_retriever is None:
        _hybrid_retriever = HybridRetriever()
    return _hybrid_retriever


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
    return _get_hybrid_retriever()._db


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


def _get_iterative_retriever() -> IterativeRetriever:
    global _iterative_retriever
    if _iterative_retriever is None:
        base = _get_hybrid_retriever()
        _iterative_retriever = IterativeRetriever(base, base._db)
    return _iterative_retriever


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


@mcp.tool()
def investigate_architecture(target_concept: str, deep: bool = False) -> str:
    """
    PREFERRED ENTRY POINT for all architectural investigations.
    Replaces raw `semantic_code_search` when you want a complete picture of how a
    concept, feature, data type, or function flows through the system.

    Internally runs the full Retrieve-Traverse-Rerank pipeline:
      1. Semantic Search  — FAISS tier-1 index, top-50 by cosine similarity.
      2. Graph Expansion  — one-hop call-graph traversal via SQLite for top-5 seeds.
      3. CrossEncoder Reranking — jina-reranker-v2-base-code scores every candidate.

    When deep=True, runs up to 3 iterative retrieval rounds with explored-node memory
    and query enrichment from prior evidence, stopping early when the score plateau
    indicates diminishing returns.

    Returns a Markdown report with:
      - <evidence> XML tags (source, fqn, type) for each retrieved chunk.
      - Programmatic Architectural Risk Analysis flagging layer/privilege mismatches.
    """
    print(f"\n[MCP] investigate_architecture: '{target_concept}' deep={deep}")
    _ensure_indexes()
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
    pipeline_label = (
        "Iterative Semantic Search → Graph Expansion → CrossEncoder Reranking"
        if deep else
        "Semantic Search → Graph Expansion → CrossEncoder Reranking"
    )
    header_lines: list[str] = [
        f"# Architectural Investigation: `{target_concept}`",
        "",
        f"> **Pipeline**: {pipeline_label}  ",
        f"> **Results**: {len(chunks)} candidates retrieved and reranked.",
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
            snippet = chunk.text[:300].replace("\n", "\n  ").strip()

            lines.append(
                f'<evidence source="{chunk.file}" fqn="{fqn}" '
                f'type="{rel_type}" layer="{layer}" '
                f'retrieval="{chunk.source}" score="{chunk.score:.4f}">'
            )
            lines.append("")
            lines.append(f"  {snippet}")
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


def _get_test_suffixes(source_file: str) -> list[str]:
    """Return test file suffixes for the language of source_file, or all languages."""
    import os as _os
    from adapters import REGISTRY, get_adapter
    ext = _os.path.splitext(source_file)[1].lower()
    adapter = get_adapter(ext)
    if adapter and hasattr(adapter, "test_conventions"):
        tc = adapter.test_conventions()
        if tc:
            return tc.file_suffixes
    # Fall back to all known test suffixes across all adapters
    seen: set[int] = set()
    suffixes: list[str] = []
    for a in REGISTRY.values():
        if id(a) not in seen:
            seen.add(id(a))
            tc = a.test_conventions() if hasattr(a, "test_conventions") else None
            if tc:
                suffixes.extend(tc.file_suffixes)
    return suffixes


@mcp.tool()
def find_test_coverage(source_file: str, target_symbol: str = "") -> str:
    """
    Finds unit tests that semantically cover a source file or symbol.

    Adapts to the language of the source file — TypeScript (.test.ts),
    C# (Tests.cs / Test.cs), Python (_test.py), etc.  Falls back to all
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
    _ensure_indexes()
    norm_source = source_file.lower().replace('\\', '/').split('/')[-1]
    source_base = re.sub(r'\.[^.]+$', '', norm_source)

    test_suffixes = [s.lower() for s in _get_test_suffixes(source_file)]

    def is_test_file(fp: str) -> bool:
        return any(fp.endswith(s) for s in test_suffixes)

    # Collect one representative doc per test file
    test_doc_by_file: dict[str, dict] = {}
    for doc in doc_store.docs.values():
        fp = doc['file'].replace('\\', '/').lower()
        if is_test_file(fp) and fp not in test_doc_by_file:
            test_doc_by_file[fp] = doc

    if not test_doc_by_file:
        suffixes_str = ", ".join(test_suffixes) if test_suffixes else "(none)"
        return (
            "--- TEST COVERAGE ANALYSIS ---\n\n"
            f"SOURCE: {source_file}\n\n"
            f"  [No test files found in the index (searched suffixes: {suffixes_str}) — run reindex first.]\n"
        )

    # --- Tier 1: Direct name match ---
    # Candidate direct test names: source_base + each test suffix
    # e.g. "auth" + ".test.ts" → "auth.test.ts";  "AuthService" + "Tests.cs" → "authservicetests.cs"
    direct_candidates = {f"{source_base}{s}" for s in test_suffixes}
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

    direct_label = " | ".join(f"{source_base}{s}" for s in test_suffixes[:2])

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

    context += f"\nNote: Searched for test files with suffixes: {', '.join(test_suffixes)}\n"
    return context


@mcp.tool()
def reindex(changed_files_only: bool = False) -> str:
    """
    Rebuilds the codebase index so all MCP tools reflect the current state of the files.

    The index goes stale whenever code changes — tools will otherwise return wrong answers
    on modified files. Call this tool after editing source files.

    changed_files_only=True  — incremental: only processes files added, modified, or
                               deleted since the last run. Fast for frequent refreshes.
    changed_files_only=False — full (default): clears all index state first, then
                               re-indexes every file from scratch. Use after major
                               refactors or when the incremental index appears corrupted.

    Returns a summary of chunks added/updated/removed, then reloads the in-memory indexes.
    """
    import sys
    import io
    import os
    import sqlite3
    import subprocess
    from incremental_indexer import run_incremental, INDEX_DIR, TIER_CONFIGS
    from db import CodeDB
    _ensure_indexes()

    global index_manager, doc_store, t1_index, t2_index, t3_index, _hybrid_retriever

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
                    ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
                ).strip()
                if _last_hash != _curr_hash:
                    _changed = subprocess.check_output(
                        ["git", "diff", "--name-only", _last_hash, "HEAD"],
                        text=True, stderr=subprocess.DEVNULL
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
    if not changed_files_only:
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
    finally:
        sys.stdout = _old_stdout

    # Reload in-memory state so subsequent MCP tool calls see the updated index
    index_manager = MultiIndexManager()
    doc_store = DocumentStore()
    t1_index = index_manager.load_or_create("tier1_surgical")
    t2_index = index_manager.load_or_create("tier2_component")
    t3_index = index_manager.load_or_create("tier3_architectural")
    _hybrid_retriever = None   # Reset lazy singleton; reloads on next investigate_architecture call

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


@mcp.tool()
def index_status(since: str = "1d") -> str:
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

    Reports: last_verified_at, last_indexed_commit vs current HEAD (with the list
    of diverged files when stale), files_total, and the recent-change list.
    """
    import os
    import subprocess
    from datetime import datetime, timedelta, timezone
    from incremental_indexer import INDEX_DIR
    from db import CodeDB

    db_path = os.path.join(INDEX_DIR, "graph.db")
    if not os.path.exists(db_path):
        return "No index found — run reindex first."

    def _parse_since(s: str) -> str:
        """Relative window → absolute ISO cutoff; pass an ISO string through as-is."""
        s = (s or "").strip()
        m = re.fullmatch(r"(\d+)\s*([dhm])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {
                "d": timedelta(days=n),
                "h": timedelta(hours=n),
                "m": timedelta(minutes=n),
            }[unit]
            return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        return s

    cutoff = _parse_since(since)

    with CodeDB(db_path) as db:
        last_verified = db.meta_get("last_verified_at") or "(never recorded)"
        last_commit   = db.meta_get("last_indexed_commit")
        files_total   = db.meta_get("files_total") or "?"
        rows = db._conn.execute(
            "SELECT path, content_changed_at FROM files "
            "WHERE content_changed_at IS NOT NULL AND content_changed_at > ? "
            "ORDER BY content_changed_at DESC",
            (cutoff,),
        ).fetchall()

    lines = [
        "--- INDEX STATUS ---",
        f"last_verified_at:    {last_verified}",
        f"files_total:         {files_total}",
    ]

    if last_commit:
        try:
            curr = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            if curr == last_commit:
                lines.append(f"last_indexed_commit: {last_commit[:8]} (== HEAD; index current)")
            else:
                lines.append(
                    f"last_indexed_commit: {last_commit[:8]}  HEAD: {curr[:8]}  ⚠️ STALE"
                )
                try:
                    diverged = subprocess.check_output(
                        ["git", "diff", "--name-only", last_commit, "HEAD"],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    for f in diverged.splitlines():
                        if f:
                            lines.append(f"    diverged: {f}")
                except subprocess.CalledProcessError:
                    pass
        except (FileNotFoundError, subprocess.CalledProcessError):
            lines.append(
                f"last_indexed_commit: {last_commit[:8]} (HEAD comparison unavailable — no git)"
            )
    else:
        lines.append("last_indexed_commit: (none recorded)")

    lines.append(f"\nfiles with content changed since {cutoff}  ({len(rows)}):")
    if rows:
        lines.extend(f"    {ts}  {path}" for path, ts in rows)
    else:
        lines.append("    (none)")

    return "\n".join(lines)


@mcp.tool()
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
    _ensure_indexes()
    norm_anchor = anchor_file.lower().replace('\\', '/').split('/')[-1]
    anchor_base = re.sub(r'\.[^.]+$', '', norm_anchor)

    # Pre-fetch anchor text for primitive detection
    anchor_text = ""
    for _d in doc_store.docs.values():
        if norm_anchor in _d['file'].lower() and _d['tier'] == 'tier2_component':
            anchor_text += _d['text'] + "\n"

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

        norm_path = file_path.lower().replace('\\', '/')
        if norm_anchor in norm_path: continue   # skip the defining file

        file_full = "".join(d['text'] + "\n" for d in doc_store.docs.values() if d['file'] == file_path)

        imports_anchor = bool(re.search(
            rf"(import|require).*?['\"].*?{re.escape(anchor_base)}.*?['\"]",
            file_full, re.IGNORECASE | re.DOTALL
        ))
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
    for file_path in {d['file'] for d in doc_store.docs.values()}:
        if file_path in seen_files: continue
        norm_path = file_path.lower().replace('\\', '/')
        if norm_anchor in norm_path: continue
        file_full = "".join(d['text'] + "\n" for d in doc_store.docs.values() if d['file'] == file_path)
        if not re.search(rf"(import|require).*?['\"].*?{re.escape(anchor_base)}.*?['\"]", file_full, re.IGNORECASE | re.DOTALL):
            continue
        seen_files.add(file_path)
        rep_doc = next(d for d in doc_store.docs.values() if d['file'] == file_path)
        snippet = rep_doc['text'][:120].replace('\n', ' ').strip() + "..."
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


@mcp.tool()
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
    _ensure_indexes()
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


@mcp.tool()
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
    _ensure_indexes()
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

        file_full = "".join(d['text'] + "\n" for d in doc_store.docs.values() if d['file'] == file_path)
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


@mcp.tool()
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

def _reload_indexes() -> None:
    """Hot-swap the in-memory FAISS + doc-store state after a reindex run.

    Build-then-swap pattern (H4): new objects are constructed outside the lock
    so the IO-bound work does not block in-flight tool calls.  The lock is
    acquired only for the brief reference-swap itself.
    """
    global index_manager, doc_store, t1_index, t2_index, t3_index
    global _hybrid_retriever, _iterative_retriever, _index_generation

    # Phase 1: build (slow — reads FAISS files + SQLite)
    new_im = MultiIndexManager()
    new_ds = DocumentStore()
    new_t1 = new_im.load_or_create("tier1_surgical")
    new_t2 = new_im.load_or_create("tier2_component")
    new_t3 = new_im.load_or_create("tier3_architectural")

    # Phase 2: atomic swap (fast — just reference assignments)
    with _reload_lock:
        index_manager        = new_im
        doc_store            = new_ds
        t1_index             = new_t1
        t2_index             = new_t2
        t3_index             = new_t3
        _index_generation   += 1
        _hybrid_retriever    = None
        _iterative_retriever = None


class _ReindexDebouncer:
    """
    Collapses a burst of rapid file-change events into a single reindex call.

    A formatter run, a git checkout, or a multi-file save can fire dozens of
    events in under a second.  Without debouncing each event would spawn a
    separate run_incremental() invocation that races the previous one for the
    SQLite lock.  Instead, every incoming event resets a timer; the reindex
    fires only after `delay` seconds of silence.
    """

    def __init__(self, delay: float = 3.0) -> None:
        self._delay  = delay
        self._timer: threading.Timer | None = None
        self._lock   = threading.Lock()

    def schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        print("\n[Watchdog] Change detected — running incremental reindex...")
        try:
            from incremental_indexer import run_incremental
            run_incremental(interactive=False)   # ADR-026 §5 — nobody is watching
            _reload_indexes()
            print("[Watchdog] Reindex complete — in-memory indexes reloaded.\n")
        except Exception as exc:
            print(f"[Watchdog] Reindex failed: {exc}\n")


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

    debouncer = _ReindexDebouncer(delay=debounce_seconds)
    handler   = _CodeChangeHandler(debouncer, repo_path)
    observer  = Observer()
    observer.schedule(handler, repo_path, recursive=True)
    observer.daemon = True
    observer.start()
    print(f"[Watchdog] Active — watching '{repo_path}' (debounce={debounce_seconds}s)")
    return observer


def main() -> None:
    start_watchdog()
    mcp.run()


if __name__ == "__main__":
    main()
