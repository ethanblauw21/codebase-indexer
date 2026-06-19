# ADR-006: Graph Analytics — Community Detection, Centrality, and the Architecture Map Tool

**Status:** accepted (merged — `7dc7d74`)
**Date:** 2026-06-18
**Branch:** `feature/graph-analytics-community-detection`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — operates on the existing `graph.db` symbol/edge graph (`calls`, `imports`, `extends`, `implements`, `owns`, `contains`) produced by the adapter pipeline. No dependency on ADR-004/005.
- ADR-008 *(planned — docs/adr-backlog.md)* — defers this tool's **measured output-quality bar** to ADR-008's conformance harness, and will gate community detection on ADR-008's **edge `confidence` floor** (A3) once edges carry confidence. Until then this ADR is EXTRACTED-only and ships explicitly unmeasured. *(Soft forward dependency: ADR-006 ships first; ADR-008 sets the bar later.)*
**Depended on by:**
- ADR-010 *(planned — docs/adr-backlog.md)* — the drift layer's "vital" tier is auto-derived from this ADR's centrality / god-object scoring.
- ADR-015 *(planned)* — the local graph-explorer UI renders this ADR's community map.
- *Amendment (docs/adr-backlog.md):* make Leiden the preferred backend + add the refinement step (split <1% internal-density communities).

> **Pressure-tested 2026-06-18** via `/grill-plan`. Four open decisions were resolved against the
> depth-over-breadth thesis and the research roadmap in
> [design-research-informed-improvements.md](../design-research-informed-improvements.md). The locked
> outcomes are baked into the sections below and recorded in the Implementation Log notes.

## Context

The indexer already stores a ground-truth symbol/edge graph in `graph.db` (SQLite): `calls`, `imports`, `extends`, `implements`, `owns`, and `contains` edges, exposed via `db.get_adjacency_snapshot() -> dict[str, list[str]]`, `get_call_graph()`, `get_callees/callers()`, `get_references()`, and `get_owners()`. What it does **not** do is *analyze the shape of that graph*. We can answer "what calls X" and "what is the blast radius of X," but not "which modules form natural clusters," "which symbols are god-objects bridging unrelated subsystems," or "where are the seams along which an over-large class should be split."

This gap was identified empirically. On 2026-06-18 we trialed the third-party project **Graphify** ([github.com/safishamsi/graphify](https://github.com/safishamsi/graphify), MIT) against this repository. Graphify builds a knowledge graph and runs **Leiden community detection** plus **betweenness-centrality "god-node" analysis** over it. Running it (directed mode) surfaced a finding our own engine structurally could not produce: `db.py:CodeDB` is a **god-object** whose ~40 methods cluster into five latent services (schema migrations, symbol store, reference/ownership queries, call-graph store, write/cache+lifecycle), with near-total fan-in from retrieval/indexing/summarizer and fan-out only to value types. The community partition *was* a ready-made decomposition map.

The trial also clarified that Graphify is **mostly redundant** with this engine: both do tree-sitter AST extraction, call-graph construction, import resolution, and ship an MCP server. Graphify's retrieval (pure graph traversal) is strictly weaker than our hybrid dense+BM25 RTR pipeline, and it cannot be embedded in the planned Rust port (Python dependency). The only genuinely additive capability is the **clustering + centrality analytics layer** — and crucially, our graph runs on *real EXTRACTED edges*, not Graphify's LLM-INFERRED ones, so our community structure is higher-fidelity than what the trial itself produced.

Decision: **do not adopt Graphify as a dependency or sidecar. Reimplement the one missing algorithm natively, over the graph we already own, exposed as an MCP tool, with a deliberately distinct visualization.**

### Relationship to the rest of the roadmap

This ADR is the home of the engine's **graph-analytics layer** (`src/graph_analytics.py`), which several research-roadmap items extend:
- **A1 (Louvain refinement)** from the Codebase-Memory paper [1] §3.7 is folded into §1 of this ADR (not a separate ADR).
- **A2 (incremental community recompute)** is noted here as a deferred scaling follow-up (only relevant once results are cached).
- **A3 (edge confidence scores)** is a future coupling: when edges carry confidence, community detection will gate on a confidence floor. Until then, ADR-006 is **EXTRACTED-only** — consistent with the roadmap's "no fuzzy low-confidence edges silently feeding community detection."
- The **acceptance bar** for this tool's *output quality* is deferred to **ADR-008 (Measured Conformance)**; see Testing Additions.
- The DSM view (§3) is the static precursor to **S5** (the interactive web explorer in [suggestions-future-directions.md](../suggestions-future-directions.md)).

A second, smaller gap (treating docs/ADRs as first-class graph nodes linked to the symbols they govern) is acknowledged but deferred — it appears nowhere in the current roadmap, whose near-term priority is B1/B3 → ADR-008. See §5 / D2.

## Decision

Add a self-contained **graph-analytics layer** that reads the existing SQLite graph, computes community structure and centrality, scores god-objects, optionally proposes module splits, and renders the result two ways: a markdown report bound to a new MCP tool, and an interactive **Design Structure Matrix (DSM)** HTML view. No new graph is built; no LLM is invoked; the layer is pure, deterministic, and in-process.

### §1 — Analytics engine (`src/graph_analytics.py`, new)

A new module, dependency-light, that turns the stored graph into analysis. It must not import FAISS, the embedder, or any MCP symbol — it operates only on `CodeDB`.

```python
# src/graph_analytics.py
# Inspired by safishamsi/graphify (MIT) — community-detection + god-node analysis.
# Reimplemented natively over our EXTRACTED graph; no Graphify source used. See ADR-006.
from dataclasses import dataclass

# Edge-kind weights: structural certainty drives clustering.
# EXTRACTED edges only — we never ingest INFERRED edges into the analysis graph (A3 will
# later gate on a confidence floor). All six kinds participate in clustering (decision E),
# with the A1 refinement pass (below) ejecting weakly-connected file-mirror members.
EDGE_WEIGHTS = {
    "calls":      1.0,   # strongest coupling signal
    "imports":    0.6,
    "extends":    0.8,
    "implements": 0.8,
    "owns":       0.4,   # class→method containment; structural, weak coupling
    "contains":   0.2,   # file→symbol; weakest, mostly for completeness
}
COUPLING_KINDS = {"calls", "imports", "extends", "implements"}  # owns/contains excluded here

@dataclass(frozen=True)
class Community:
    id: int
    label: str               # filled by the report layer, not the engine
    members: list[str]       # symbol FQNs
    cohesion: float          # internal-edge density, raw 0..1 (never hidden behind symbols)

@dataclass(frozen=True)
class GodObject:
    fqn: str
    betweenness: float
    fan_in: int              # in-degree on the directed graph
    fan_out: int             # out-degree
    communities_spanned: int # distinct communities its OWNED methods land in (separate pass, see below)
    score: float             # composite, see scoring rubric below

@dataclass(frozen=True)
class SplitSuggestion:
    fqn: str                       # the over-large symbol/file
    proposed_modules: list[tuple[str, list[str]]]  # (suggested_name, member_fqns)
    rationale: str
    caveat: str = ("HEURISTIC — derived from EXTRACTED edges via Louvain; not verified. "
                   "Validate against actual call edges before acting.")

@dataclass(frozen=True)
class GraphAnalysis:
    communities: list[Community]
    god_objects: list[GodObject]
    splits: list[SplitSuggestion]   # empty unless the tool was called with suggest_splits=True
    node_count: int
    edge_count: int
    modularity: float
    built_at_commit: str | None
```

Engine responsibilities:

1. **Build a weighted `networkx.DiGraph`** from `CodeDB.get_graph_edges()` (a new typed-edge reader, §1.1). All six edge kinds participate, weighted by `EDGE_WEIGHTS`. Direction is preserved (source→target) so fan-in/fan-out are meaningful — the property the directed trial run proved matters.
2. **Community detection.** Default: `networkx.community.louvain_communities(G.to_undirected(as_view=True), weight="weight", seed=GRAPH_SEED)` — built into NetworkX, pure-Python, no native build step, deterministic under a pinned seed. Optional higher-quality path: if `leidenalg` + `python-igraph` are importable, use Leiden; otherwise fall straight through to Louvain. **No graspologic / no multiprocessing** (see Consequences → lessons from the trial).
3. **A1 — Louvain refinement** *(from Codebase-Memory paper [1] §3.7).* After the local-moving pass, any community with `< MIN_INTERNAL_DENSITY` (default 1%) internal edge density is **split by ejecting its weakly-connected members**, re-running local moving until convergence (≤ `MAX_REFINE_ITERS`, default 5). This is the mitigation for the triviality risk that `owns`/`contains` edges could otherwise produce communities that merely mirror the directory/class tree.
4. **Centrality.** `betweenness_centrality(G, weight="weight", normalized=True)` for bridge detection; in/out degree for fan-in/out. Scaling: above `BETWEENNESS_EXACT_MAX` nodes (default 5000) switch to sampled approximate betweenness (`k`-source sampling) — consistent with the A7 performance targets ([1] §4.3: ~1 ms structural query, ~6 s / 50K-node index).
5. **God-object scoring — span computed on a SEPARATE pass (decision S/E).** A class's `communities_spanned` is **not** read off the all-edge partition (where a monolithic class could collapse into one community and self-mask). Instead: take the symbol's *owned* methods (`owns` edges), look up which community each landed in **on the coupling subgraph** (`COUPLING_KINDS` only), and count distinct communities. A symbol is a god-object candidate when betweenness is high AND that span ≥ `GOD_MIN_COMMUNITIES` (default 3). Composite score:
   `score = 0.5 * norm(betweenness) + 0.3 * norm(fan_in) + 0.2 * (communities_spanned / total_communities)`.
6. **Split suggestions — opt-in only (decision Altitude).** Computed *only* when the tool is called with `suggest_splits=True`. For each god-object that is a class/file, group its owned members by the community they fall into and emit one proposed module per cluster of size ≥ 2. Every `SplitSuggestion` carries the `caveat` field verbatim. Default tool behavior emits none.
7. **Cohesion / modularity.** Report raw numeric cohesion per community and overall modularity. Never collapse to symbols or hide the number (honesty rule, §6).

#### §1.1 — `CodeDB.get_graph_edges()` (new method, `src/db.py`)

`get_adjacency_snapshot()` returns adjacency without edge kinds. Add a sibling that yields typed, directed edges so the engine can weight them and separate coupling from containment:

```python
def get_graph_edges(self) -> list[tuple[str, str, str]]:
    """(source_fqn, target_fqn, kind) for every edge. kind ∈ EDGE_WEIGHTS.
    Reuses the cached-graph invalidation already wired into invalidate_graph_cache()."""
```

This is additive and read-only; it rides the existing graph-cache lifecycle (`invalidate_graph_cache()` already clears traversal caches on any edge write).

### §2 — Report generator + MCP tool binding (`src/MCPServer.py`)

The report is **bound to a tool**, per requirement. Add one FastMCP tool (the engine itself stays UI-agnostic so a future Rust port or CLI can reuse it):

```python
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
    .code-index/architecture_matrix.html.

    NOTE: this is an EXPLORATORY structural view, not a verified accuracy claim.
    A measured quality bar for this output is deferred to ADR-008.
    """
```

- The tool calls `graph_analytics.analyze(db, suggest_splits=...)`, then `render_report(analysis)` for the markdown, then `graph_viz.render_dsm(analysis, db)` (§3) for the HTML side-effect.
- Community **labels** are generated heuristically from the most central member's file/symbol name (e.g., the community containing `CodeDB.get_call_graph` → "Call-Graph DB Layer"). No LLM call — labels are derived from existing symbol names, keeping the tool fast and offline like every other tool.
- `target_path` optionally scopes analysis to a subtree (mirrors the wing/room scoping idea); empty = whole graph.
- The report's **header line states it is exploratory and not an accuracy claim** (decision Acceptance-bar). Split suggestions, when present, repeat the per-suggestion caveat.
- This makes the engine the **eleventh** MCP tool; update `src/CLAUDE.md`'s "Ten AI-facing tools" count and the Discovery group.

Report sections (markdown, AI-facing docstring style): **Summary** (nodes/edges/modularity + the exploratory disclaimer), **Module Communities** (label, size, raw cohesion, key members), **God-Objects** (table: fqn, betweenness, fan-in, fan-out, communities spanned), **Suggested Splits** (only if `suggest_splits=True`; each with caveat), **Audit note** (states the analysis used only EXTRACTED edges — our provenance-honesty answer to Graphify's EXTRACTED/INFERRED tagging).

### §3 — Visualization: Design Structure Matrix (`src/graph_viz.py`, new)

Requirement: the same *kind* of interactive HTML deliverable as Graphify, but a **different format so we are not copying** their force-directed node-link graph. We render a **community-clustered Design Structure Matrix (DSM)** instead — an N×N adjacency heatmap with symbols ordered by community.

Why DSM is the right distinct choice (not just a cosmetic swap):
- It is a fundamentally different visual idiom (matrix, not node-link). No visual or code resemblance to Graphify's `graph.html`.
- It is **better suited to this engine's purpose**: god-objects appear as a dense full row/column; healthy communities appear as bright blocks on the diagonal; cross-community coupling appears as off-diagonal cells — exactly the `CodeDB` finding, made visible at a glance.
- It is self-contained static HTML (one file, no server, no external CDN at runtime — inline a tiny vanilla-JS canvas renderer), matching our "no web server / no build pipeline" constraint from `src/CLAUDE.md`.

`render_dsm(analysis, db) -> writes .code-index/architecture_matrix.html`:
- Rows/cols = symbols, grouped and color-banded by community; cells shaded by edge weight; diagonal community blocks outlined.
- Interactions (vanilla JS, no framework): hover a cell → show `source → target (kind)`; click a community band → collapse/expand it; a toggle to reorder by community vs. by file. God-objects flagged with a marker in the margin.
- Hard cap: if `node_count > DSM_MAX_NODES` (default 1500) render the **community-aggregated** matrix (community×community) instead of symbol×symbol, with a banner — analogous to Graphify's >5000-node aggregation guard, sized for our typical corpora.
- **Lineage:** this static DSM is the precursor to S5 (interactive web explorer). S5, if built, supersedes it; ADR-006 deliberately keeps the DSM single-file and dependency-free so S5 is an additive step, not a rewrite.

### §4 — Dependencies & packaging

- **Required:** `networkx` (pure-Python; add to `requirements.txt` and `pyproject.toml`). Nothing else is mandatory.
- **Optional:** `leidenalg`, `python-igraph` — only used if already importable; never required. Documented as an optional extra, not a default install (keeps Windows/CI installs clean — see lessons learned).
- **Explicitly excluded:** `graspologic` and any multiprocessing-based clustering. The trial hit a Python-3.14/Windows `multiprocessing` spawn crash and a graspologic ANSI-terminal bug; this layer stays single-process and dependency-light by design.

### §5 — Deferred: docs-as-graph-nodes (D2 seam)

The trial's second additive insight — ADRs/READMEs as graph nodes linked to the code they govern — is **out of scope here** and is not on the current roadmap (whose near-term priority is B1/B3 → ADR-008). It gets a one-line seam so it isn't re-litigated: `graph_analytics.analyze()` accepts an optional `extra_nodes` / `extra_edges` parameter (default empty). A future ADR can populate it from a doc-ingestion pass without touching the engine's core. No doc parsing ships in ADR-006.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Adopt Graphify as a dependency / run it as a sidecar | ~70% redundant with our engine (AST, call graph, import resolution, MCP server); its pure-traversal retrieval is weaker than our hybrid RTR; Python dep blocks the Rust port; its graph uses INFERRED edges where ours has EXTRACTED. Reimplementing the ~one algorithm we lack is more robust than carrying the whole package. |
| Vendor/copy Graphify's clustering + node-link `graph.html` | License permits it (MIT) but it would duplicate code we don't need and produce a visualization indistinguishable from theirs. We want attribution-with-independence, not a fork. |
| `leidenalg`/igraph as the **required** clustering backend | Native build dependency; fragile on Windows/CI. NetworkX Louvain is built-in, deterministic under a seed, and good enough at our corpus sizes. Leiden stays an optional upgrade. |
| Reuse Graphify's force-directed node-link viz format | Direct copy of their deliverable; also worse for the god-object/coupling question than a DSM matrix. |
| Cluster on coupling edges only (exclude owns/contains) | Considered for the triviality risk, but rejected in favor of keeping all edges + the A1 refinement pass, which ejects file-mirror members without discarding containment signal. Span is instead protected by computing it on a separate coupling-only view (§1.5). |
| Prescribe module splits by default | Rejected under "prefer unknown over a wrong edge": split suggestions ride on heuristic clustering. They are gated behind `suggest_splits=True` and stamped `[HEURISTIC — unverified]`. |
| Build clustering on a fresh re-extracted graph (Graphify-style) | We already store a ground-truth graph; re-extracting would duplicate `ast_chunker`/adapters and add cost for no fidelity gain. |

## Attribution & Prior Art

This feature is **inspired by Graphify** (Safi Shamsi, [github.com/safishamsi/graphify](https://github.com/safishamsi/graphify), MIT-licensed). Specifically, the trial of Graphify on this repository on 2026-06-18 demonstrated the value of **community detection + betweenness "god-node" analysis over a code graph**, and that demonstration motivated this ADR.

Scope of borrowing, stated plainly for license and intellectual honesty:
- **Borrowed:** the *idea* — clustering a code graph into communities, surfacing high-betweenness god-objects, and using the community partition as a refactor/split map; and the *provenance-honesty principle* (Graphify's EXTRACTED/INFERRED/AMBIGUOUS edge tagging), which we honor by analyzing only EXTRACTED edges and saying so in the report.
- **Not borrowed:** no Graphify source code is copied or vendored. The implementation uses independent, standard libraries (NetworkX; optionally leidenalg). The Louvain-refinement step is from the Codebase-Memory paper [1] §3.7, not Graphify. The visualization is a Design Structure Matrix, a deliberately different idiom from Graphify's force-directed node-link view.

Attribution must appear in three places: (1) this ADR; (2) a module docstring header in `src/graph_analytics.py` and `src/graph_viz.py` crediting Graphify as inspiration; (3) the README section that documents `map_module_communities`.

## Consequences

**Better:**
- The trial's best insight (god-object detection + community/decomposition mapping) becomes a native, offline, deterministic capability over our **higher-fidelity EXTRACTED graph** — no LLM, no new graph, no heavyweight dependency.
- Reuses infrastructure we already maintain (`CodeDB`, the edge graph, the graph-cache lifecycle, the FastMCP tool pattern), so the surface area is small.
- The DSM view is both legally/visually distinct from Graphify and better matched to coupling analysis than a node-link graph, and is a clean precursor to S5.
- Algorithm is portable to the planned Rust indexer (`petgraph` + a Louvain/Leiden crate) because it depends only on the stored graph, not on Python-specific machinery.
- Conservative by construction: descriptive by default, prescription opt-in and caveated, output labeled exploratory — consistent with the "prefer unknown over wrong" moat.

**Worse:**
- Adds `networkx` as a required dependency (pure-Python, low risk, but a new transitive surface).
- Betweenness centrality is O(V·E); the approximate-betweenness fallback above `BETWEENNESS_EXACT_MAX` trades exactness for the A7 latency targets — a documented scaling limit, not a fix.
- Community labels are heuristic (derived from member names), so they will occasionally be vague; they are navigation aids, not ground truth.
- The tool ships **unmeasured** until ADR-008 defines its quality bar — accepted, and mitigated by the explicit "not an accuracy claim" labeling.

**Neutral:**
- This is analysis-only: it reads the graph and writes a report + an HTML file. It never mutates the index, so it carries no re-index or migration risk (unlike ADR-003's schema change).
- The docs-as-nodes idea is deferred behind a no-op seam (§5); the engine signature anticipates it without committing to it.

## Testing Additions

> **Acceptance bar (decision):** ADR-006's *output quality* bar is **deferred to ADR-008 (Measured Conformance)**. The tool ships as an explicitly exploratory view. The tests below assert **mechanics and determinism only** — they prove it runs correctly, not that its findings are "accurate."

| Area | Type | Notes |
|------|------|-------|
| `get_graph_edges()` | Unit | Returns typed directed edges for a fixture graph; kinds ∈ EDGE_WEIGHTS; rides graph-cache invalidation |
| Community determinism | Unit | Same graph + pinned `GRAPH_SEED` → identical community assignment across runs (guards against non-determinism that would make reports untrustworthy) |
| A1 refinement | Unit | A synthetic low-density file-mirror community is split/ejected; refinement converges within `MAX_REFINE_ITERS` |
| God-object span (separate pass) | Unit | A monolithic class whose methods all cluster together on the all-edge partition still reports correct span via the coupling-only view (proves the self-mask guard works) |
| God-object scoring | Unit | Synthetic graph with one deliberate hub spanning 4 communities → flagged; a tight cohesive module → not flagged |
| Split suggestion gating | Unit | Default call → `splits == []`; `suggest_splits=True` → correct member partition + caveat present on every suggestion |
| Leiden optional path | Unit | With leidenalg absent, falls through to Louvain without error; with it present, returns a valid partition |
| `map_module_communities` tool | Integration | Default returns descriptive markdown with the exploratory header; writes `architecture_matrix.html`; raw cohesion numbers present (honesty rule) |
| DSM aggregation guard | Unit | `node_count > DSM_MAX_NODES` → community-aggregated matrix + banner, not symbol×symbol |
| Self-analysis smoke check | Integration | Running on this repo flags `CodeDB` as a god-object — a *smoke check that the pipeline works end-to-end*, **not** the acceptance bar (which is ADR-008's to set) |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 1 — Engine** *(base landed 2026-06-18; large functions next)*
- [x] Add `CodeDB.get_graph_edges() -> list[tuple[str,str,str]]` in `src/db.py`; returns all kinds, prefers `resolved_target` via `COALESCE`. Uncached (single full scan; correctness over micro-opt — adjust if it shows up in profiling). Unit-tested.
- [x] **Base** of `src/graph_analytics.py`: weighted DiGraph build (all kinds, weight + coupling_weight + kinds-set aggregation, self-loops dropped), `coupling_subgraph`, `to_weighted_undirected` (sums reciprocal weights), Louvain communities (seeded, deterministic), `community_cohesion` (raw internal-weight fraction), `graph_modularity`, `compute_centrality` (betweenness + fan-in/out, with k-sampled fallback above `BETWEENNESS_EXACT_MAX`). No FAISS/MCP imports. Graphify attribution + paper citation in docstring.
- [x] Pin constants: `GRAPH_SEED`, `GOD_MIN_COMMUNITIES`, `MIN_INTERNAL_DENSITY`, `MAX_REFINE_ITERS`, `BETWEENNESS_EXACT_MAX`, `BETWEENNESS_SAMPLE_K`, `DSM_MAX_NODES`.
- [x] Tests: `tests/test_graph_analytics.py` — 22 cases (build/aggregation, coupling filter, undirected sum, two-cluster detection + determinism, cohesion edge cases, modularity, centrality hub, deferred-stub contract, `get_graph_edges`). Full suite **60 passed** in VectorEnv, no regressions.
- [x] **A1 refinement pass** (`refine_communities` + `internal_density`, `_internal_ratio`, `_best_community_index`): density-gated ejection of weakly-connected members, reassigned to their strongest neighbouring community; converges within `MAX_REFINE_ITERS`; node-conserving. Fires on internally-disconnected/over-merged communities (the file-mirror pathology), no-op on cohesive small partitions.
- [x] **Separate coupling-only span pass** (`coupling_membership`) + god-object scoring (`find_god_objects`): span read from a coupling-only Louvain partition over the coupled core (OWNS-only nodes excluded), so an owns-clustered class cannot self-mask; score `0.5·bt + 0.3·fan_in + 0.2·span_frac`; span ≥ `GOD_MIN_COMMUNITIES` is the gate.
- [x] Split suggestions (`suggest_splits`): groups a god-object's owned members by coupling community, one module per group ≥ 2, ≥ 2 modules required; `SplitSuggestion.caveat` stamped on each. Gated by `analyze(include_splits=…)`.
- [x] `analyze()` orchestrator wires edges → graph → refined communities → centrality → coupling span → god-objects → optional splits → `GraphAnalysis`. Duck-typed `db` (only needs `get_graph_edges()`); `target_path` scoping by file segment.
- [x] Tests extended: 28 cases in the module (full suite **66 passed**). Includes refinement dissolve/no-op, coupling-span exclusion of OWNS-only nodes, god-object flag-spanning-only, split grouping + caveat, and `analyze` end-to-end via a fake db.
- [ ] Optional Leiden path guarded behind import-availability; Louvain default. *(deferred — base uses NetworkX Louvain.)*

**Smoke-check finding (2026-06-18):** running `analyze()` on the live `.code-index/graph.db` ran cleanly and correctly identified `src/db.py::CodeDB` as the **top owner (32 methods)**, but flagged **no god-objects** — because that index (built 2026-06-11, *before* the `_treesitter.run_query()` fix in `fe34609`) contains **only `OWNS` edges**, no `CALLS`/`IMPORTS`. The god-object gate depends on coupling edges, so it faithfully stayed silent. **The ADR's "self-analysis smoke check" therefore requires a freshly rebuilt index** (with the current adapter call/import extraction) to reproduce the trial's `CodeDB` finding — noted as a dependency, not a code defect.

**Phase 2 — Report + tool** *(landed 2026-06-18)*
- [x] `render_report(analysis) -> str` in **`src/graph_report.py`** (markdown; exploratory header; raw cohesion + raw modularity shown; audit note re: EXTRACTED-only; per-split `[HEURISTIC — unverified]` caveats when present; empty-graph and no-god-objects paths both explain the coupling/reindex requirement). Honest no-god-objects copy doubles as the stale-index hint.
- [x] Heuristic community labeling (`label_community`): dominant source module among members + "(+N modules)" reach + names the central god-object member when one is present. Derived from member file/symbol names only — no centrality threading, no LLM.
- [x] Add `@mcp.tool() map_module_communities(target_path="", min_community_size=3, suggest_splits=False)` to `src/MCPServer.py`; wires `CodeDB` → `graph_analytics.analyze` → `render_report`; DSM (§3) call guarded behind `try/except ImportError` so it lights up automatically when Phase 3 lands.
- [x] Update `src/CLAUDE.md` tool count (Ten → Eleven) and Discovery group + a paragraph on the tool. *(README section + Graphify attribution remains Phase 4.)*
- [x] Tests: `tests/test_graph_report.py` — 14 pure cases (labeling, exploratory header + audit note present, raw numbers exposed, god-object table, no-god-objects coupling note, `min_community_size` filters body-not-summary, split caveat per suggestion, scope header) + a live-index integration smoke. Full suite **80 passed** in VectorEnv.

> **Deviation (Phase 2):** `render_report` lives in a new pure module `src/graph_report.py`, **not** in `MCPServer.py` as the ADR §2 sketch implied. Reason: keeping it out of `MCPServer` (which imports FAISS via `core`) makes the renderer unit-testable with nothing but networkx + stdlib, consistent with the engine's UI-agnostic rule. Heuristic labeling is by dominant member *module* (+ god-object name) rather than re-deriving "most central member," to avoid threading per-node centrality through `GraphAnalysis`; labels stay deterministic/offline as designed. Live smoke on the stale OWNS-only index rendered cleanly: 141 nodes / 124 edges / 17 communities / modularity 0.8725, `db` (33), `app` (24), `hybrid_retriever` (11) correctly labeled, **0 god-objects** (no coupling edges — same stale-index condition noted in Phase 1).

**Phase 3 — DSM visualization** *(landed 2026-06-18)*
- [x] `src/graph_viz.py: render_dsm(analysis, db, out_path=None, max_nodes=DSM_MAX_NODES)` → writes `.code-index/architecture_matrix.html` and returns the path. Single self-contained file: inline CSS + a vanilla-JS **canvas** renderer, **no external CDN / no remote script** (asserted by test). Community-ordered (matches the report: communities by size desc, members sorted), diagonal community-block outlines, cells shaded by aggregated edge weight, **god-object margin markers** (triangles on the top + left margins). S5 lineage noted in the module docstring.
- [x] Edges read from `db.get_graph_edges()` and **filtered to the analysis node set**, so a path-scoped analysis yields a scoped matrix; emitted **sparsely** (`[row, col, weight, "KINDS"]`) rather than a dense N×N array. Hover tooltip shows `source → target (kind)` + weight; an **Order: community ↔ file** toggle re-sorts via two position arrays without moving stable edge indices.
- [x] **Aggregation guard**: above `max_nodes` symbols (default `DSM_MAX_NODES`=1500) the matrix collapses to **community × community** (diagonal = internal coupling) with a banner. Unit-tested by forcing `max_nodes=1`.
- [x] Wired into `map_module_communities` (the §2 tool) via the guarded `try/except ImportError`, now passing `out_path=INDEX_DIR/architecture_matrix.html`. Live run wrote a 33 KB self-contained file: symbol mode, 141 nodes / 124 cells, no remote refs.
- [x] Tests: `tests/test_graph_viz.py` — 5 cases (self-contained file written; community ordering + god flag; cells reference valid indices with kinds; aggregation guard → community matrix + banner + diagonal; empty graph). Full suite **85 passed** in VectorEnv.

> **Deviation (Phase 3):** the ADR §3 interaction list said "click a community band → **collapse/expand** it." Implemented instead as **click-to-focus/isolate** (clicking a community in the legend dims all cells not touching it; click again to clear) — a simpler, robust single-file canvas behavior that serves the same "interrogate one community" intent. Literal row/column collapsing (which needs live re-aggregation) is deferred to **S5**, the interactive explorer this static DSM is a precursor to. The order toggle (community ↔ file) ships as specified.

**Phase 4 — Dependencies & docs** *(landed 2026-06-18)*
- [x] Pinned `networkx>=3.0` as a **required** dep in both `requirements.txt` and `pyproject.toml` (`louvain_communities` needs ≥3.0). Added the three new modules (`graph_analytics`, `graph_report`, `graph_viz`) to `pyproject.toml`'s `py-modules` so they install.
- [x] Documented `leidenalg`/`python-igraph` as **optional, not default**: a commented block in `requirements.txt`, a `[project.optional-dependencies] leiden = [...]` extra in `pyproject.toml` (`pip install -e .[leiden]`), and a README note. Engine falls through to Louvain when absent — no functionality lost.
- [x] README: tool count 10 → 11, `map_module_communities` row in the tools table, a dedicated **Architecture mapping** section (community map + god-objects + DSM, the `target_path`/`suggest_splits` usage, the `[HEURISTIC — unverified]` + exploratory framing), the **Graphify attribution** blockquote, and the three new modules in the project-structure tree + manual `pip install` line.
- [x] Aligned in-code references to the conformance ADR after the user's renumber **ADR-007 → ADR-008** (the report disclaimer, `src/CLAUDE.md`, the MCP tool docstring, and the matching test assertion).

**Deferred (not this ADR):**
- [ ] A2 — incremental community recompute (only once `map_module_communities` results are cached).
- [ ] D2 — docs/ADRs as graph nodes linked to symbols (seam present via `extra_nodes`/`extra_edges`; design in a future ADR).
- [ ] ADR-008 — define the measured quality bar (precision/recall or human-judged) for this tool's god-object/community output.

**Notes:**
<!-- 2026-06-18: Motivated by a Graphify trial on this repo that flagged CodeDB as a god-object. Decision: reimplement the clustering/centrality gap natively over our EXTRACTED graph rather than adopt Graphify (mostly redundant, weaker retrieval, blocks Rust port). DSM chosen over node-link to avoid copying Graphify's deliverable and because it suits coupling analysis better. graspologic/multiprocessing explicitly excluded after the trial's Py3.14/Windows spawn crash. -->
<!-- 2026-06-18 grill-plan outcomes: (1) ACCEPTANCE BAR = defer to ADR-008 (was numbered ADR-007 at grill time; renumbered behind the harness per docs/adr-backlog.md); ship as exploratory, mechanical/determinism tests only, report labeled 'not an accuracy claim'. (2) SPLIT ALTITUDE = describe-only by default, prescription behind suggest_splits=True, each suggestion stamped '[HEURISTIC — unverified]'. (3) EDGE MIX = keep all 6 edge kinds in clustering + A1 refinement; compute god-object span on a SEPARATE coupling-only view so an owns-clustered class cannot self-mask its span. (4) DOCS-AS-NODES = confirmed deferred (not on roadmap). A1 (Louvain refinement, paper [1] §3.7) folded into §1. -->
<!-- 2026-06-18 implementation deviations (base layer): (a) EDGE VOCABULARY — the draft assumed lowercase {calls,imports,extends,implements,owns,contains}; the real db.py vocabulary is UPPERCASE {CALLS,IMPORTS,INSTANTIATES,OWNS,EXTENDS,IMPLEMENTS,PROVIDES_CONTEXT,CONSUMES_CONTEXT} with no `contains`. EDGE_WEIGHTS/COUPLING_KINDS remapped to real kinds: coupling = {CALLS,EXTENDS,IMPLEMENTS,INSTANTIATES,IMPORTS}; OWNS=containment(0.4); *_CONTEXT=retrieval plumbing(0.2, non-coupling). (b) BETWEENNESS computed UNWEIGHTED in the base — our edge weights are similarities, not distances, so feeding them to shortest-path betweenness would invert meaning; distance-weighted variant deferred to the scoring pass. (c) get_graph_edges left UNCACHED (single scan) rather than riding the adjacency cache. (d) networkx 3.6.1 already present in VectorEnv; requirements/pyproject pin still pending (Phase 4). (e) Tests run via VectorEnv python (has faiss+networkx+pytest); base interpreter lacks faiss. -->

---

### AMENDMENT: 2026-06-18 — Leiden preferred backend (Louvain fallback)

**Context.** §1 detects communities via NetworkX Louvain (`detect_communities`, `graph_analytics.py:183`,
seeded by `GRAPH_SEED` for reproducible reports), with the A1 density-gated refinement already folded into
§1. Louvain has a known defect the **Leiden** paper ([1] §3.7) exists to fix: it can produce
**badly-connected — even internally disconnected — communities**. For a tool selling *auditable* structure,
that is a math-reliability gap.

**Decision.**
- **Prefer Leiden** (`leidenalg` + `python-igraph`) as the community-detection backend **when available** —
  it guarantees well-connected communities and runs an internal refinement phase (modernization P5, "math
  reliability").
- **Keep NetworkX Louvain (+ the §1 density-gated refinement) as the zero-infra fallback.** `leidenalg` /
  `igraph` are native C-extension builds with real install friction (especially on Windows — the same class
  of problem that got `graspologic` excluded after its Py3.14/Windows spawn crash, per the base-layer notes).
  The engine must run with **no native build**, so Leiden is an optional acceleration, never a hard dependency.
- **Determinism + auditability preserved.** Leiden is seeded identically to the Louvain path (`GRAPH_SEED`),
  and **every report records which backend produced it** (`leiden` vs `louvain`). Partitions are only
  comparable within a backend, so the backend is stamped, not assumed.
- **Refinement relationship.** Under Leiden the internal refinement subsumes most of the §1 A1 density-gated
  ejection; the §1 post-step stays in force under the Louvain fallback (and as an extra guard). Both backends
  feed the same `Community` / `GraphAnalysis` output unchanged.

**Consequences.** *Better:* well-connected communities by construction when Leiden is present; seed + backend
stamp keep reports auditable. *Worse:* a second code path (Leiden vs Louvain) to test; the native-build
friction is exactly why it stays optional. *Neutral:* output schema unchanged — a backend swap, not a data
change.

**Status note.** ADR-006's header status corrected to `accepted` (merged `7dc7d74`; the doc still read
`proposed`).
