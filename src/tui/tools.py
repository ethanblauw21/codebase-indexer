from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Param:
    name: str
    label: str
    placeholder: str = ""
    multiline: bool = False


@dataclass
class ToolDef:
    id: str
    label: str
    description: str
    shortcut: str
    params: list[Param]


TOOLS: list[ToolDef] = [
    ToolDef(
        id="semantic_code_search",
        label="Semantic Search",
        shortcut="s",
        description=(
            "Find code by semantic intent rather than keywords. Searches all three "
            "index tiers with RRF fusion — better than grep for questions like "
            "'where is auth validated?' or 'how is rate limiting implemented?'"
        ),
        params=[Param("query", "Query", "e.g. where is auth validated?")],
    ),
    ToolDef(
        id="investigate_architecture",
        label="Investigate Architecture",
        shortcut="i",
        description=(
            "Full RTR pipeline: Semantic Search → Call-graph Expansion → CrossEncoder "
            "Reranking. The preferred entry point for open-ended architectural questions. "
            "Use deep=y for 3-round iterative retrieval with confidence-based early stopping."
        ),
        params=[
            Param("target_concept", "Concept", "e.g. authentication flow"),
            Param("deep", "Deep mode", "y = 3-round iterative, n = single pass (default)"),
        ],
    ),
    ToolDef(
        id="trace_data_flow",
        label="Trace Data Flow",
        shortcut="t",
        description=(
            "Traces a symbol's full lifecycle: where it's defined, produced, transformed, "
            "and consumed. Shows architectural layer labels (DATABASE, CLOUD_FUNCTION, "
            "CLIENT_COMPONENT) and flags client-side Firestore mutations as violations."
        ),
        params=[Param("target_symbol", "Symbol", "e.g. aggregatedInventory")],
    ),
    ToolDef(
        id="analyze_blast_radius",
        label="Blast Radius",
        shortcut="b",
        description=(
            "Before refactoring, see everything that depends on a symbol. Validates "
            "relationships via import graph — not just semantic similarity. Returns: "
            "Origin, Direct Dependents (import-validated), Parallel Implementations, Primitives."
        ),
        params=[
            Param("anchor_file", "Anchor File", "e.g. inventory-list.tsx"),
            Param("target_symbol", "Target Symbol", "e.g. activeView"),
        ],
    ),
    ToolDef(
        id="find_similar_code",
        label="Find Similar Code",
        shortcut="f",
        description=(
            "Paste a code snippet to find duplicates, callers, and structural parallels. "
            "Uses composite scoring with read/write operation profile analysis. Returns: "
            "Origin (exact match), Direct Callers, High Confidence Matches, Weak Matches."
        ),
        params=[Param("code_snippet", "Code Snippet", "Paste a function or block", multiline=True)],
    ),
    ToolDef(
        id="detect_pattern_violations",
        label="Pattern Violations",
        shortcut="p",
        description=(
            "Finds code that should follow a pattern but deviates from it. Pass a canonical "
            "implementation and the symbols it must call. Automatically skips pure readers, "
            "type-only files, and inert helpers. Optional regex exempts trigger files."
        ),
        params=[
            Param("canonical_snippet", "Canonical Snippet", "Paste the reference implementation", multiline=True),
            Param("enforced_symbols_csv", "Enforced Symbols", "e.g. writeTransactionLog, writeTransactionLogTx"),
            Param("ignore_regex", "Ignore Regex (optional)", "e.g. ^on[-A-Z] to skip Cloud Function triggers"),
        ],
    ),
    ToolDef(
        id="find_test_coverage",
        label="Test Coverage",
        shortcut="c",
        description=(
            "Finds Vitest .test.ts files that cover a source file or specific symbol. "
            "Reports direct coverage (spec file named after source) and semantic coverage "
            "(tests describing the same behavior). Excludes Playwright .spec.ts E2E tests."
        ),
        params=[
            Param("source_file", "Source File", "e.g. edit-ticket-items.ts"),
            Param("target_symbol", "Symbol (optional)", "e.g. updateTicketItems"),
        ],
    ),
    ToolDef(
        id="find_dead_code",
        label="Dead Code",
        shortcut="d",
        description=(
            "Given a symbol and its defining file, determines whether anything in the "
            "codebase actually uses it. Returns a clear DEAD / ALIVE verdict with evidence "
            "categories: Callers (import + reference), Consumers (reference only), Parallels."
        ),
        params=[
            Param("symbol", "Symbol", "e.g. useInventorySync"),
            Param("anchor_file", "Anchor File", "e.g. inventory-sync.ts"),
        ],
    ),
    ToolDef(
        id="find_unabstracted_collection_reads",
        label="Unabstracted Reads",
        shortcut="u",
        description=(
            "Enforces 'all reads of collection X must go through abstraction Y'. More "
            "precise than chaining trace_data_flow + detect_pattern_violations: focuses "
            "on reads only, skips write-only producers, cross-references approved entry points."
        ),
        params=[
            Param("collection_name", "Collection", "e.g. aggregatedInventory"),
            Param("canonical_symbols_csv", "Canonical Symbols", "e.g. useAggregatedInventory, getAggregatedInventory"),
        ],
    ),
    ToolDef(
        id="reindex",
        label="Reindex",
        shortcut="r",
        description=(
            "Rebuilds the codebase index so all tools reflect the current state of files. "
            "Incremental mode processes only changed files — fast for post-edit refreshes. "
            "Full mode clears all index state first — use after major refactors or corruption."
        ),
        params=[
            Param("changed_files_only", "Mode", "inc = incremental (default)   full = complete rebuild"),
        ],
    ),
]

TOOL_BY_ID: dict[str, ToolDef] = {t.id: t for t in TOOLS}
