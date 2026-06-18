"""
graph_analytics.py — structural analysis over the stored code graph (ADR-006).

Turns the SQLite symbol/edge graph (read via ``CodeDB.get_graph_edges()``) into
community structure, centrality, and god-object analysis.  Pure and in-process:
imports only ``networkx`` — never FAISS, the embedder, or the MCP server — so it
stays unit-testable in isolation and portable to the planned Rust engine.

Attribution
-----------
Inspired by Graphify (Safi Shamsi, https://github.com/safishamsi/graphify, MIT):
the idea of community-detecting a code graph and surfacing high-betweenness
"god-objects" as a decomposition map.  No Graphify source is used — this is an
independent NetworkX implementation over our own EXTRACTED edges.  The Louvain
refinement step (planned) is from the Codebase-Memory paper, not Graphify.

EXTRACTED-only: this layer analyses real parser-emitted edges.  When edge
confidence scores land (roadmap A3), community detection will gate on a
confidence floor; until then there are no inferred edges to gate.

Build order (ADR-006): the small foundation lives here now — graph construction,
community detection, cohesion, modularity, centrality.  The larger bespoke
functions (Louvain refinement, god-object span+scoring, split suggestions, the
``analyze`` orchestrator, report + DSM rendering) are stubbed below and land in a
later pass once this base is proven.
"""
from __future__ import annotations

from dataclasses import dataclass

import networkx as nx
from networkx.algorithms import community as nx_comm

# ---------------------------------------------------------------------------
# Tunable constants (pinned for determinism / reproducibility)
# ---------------------------------------------------------------------------
GRAPH_SEED = 20260618           # any fixed int; pins Louvain so reports are stable
GOD_MIN_COMMUNITIES = 3         # a god-object's owned methods must span >= this many communities
MIN_INTERNAL_DENSITY = 0.01     # A1 refinement: communities below this density are candidates to split
MAX_REFINE_ITERS = 5            # A1 refinement convergence cap
EJECT_RATIO = 0.5               # A1: members with internal/total weight below this are ejected
BETWEENNESS_EXACT_MAX = 5000    # above this node count, use sampled approximate betweenness
BETWEENNESS_SAMPLE_K = 600      # k sources for approximate betweenness
DSM_MAX_NODES = 1500            # above this, the DSM aggregates to community x community

# ---------------------------------------------------------------------------
# Edge vocabulary and weights.
#
# NOTE (ADR-006 deviation, 2026-06-18): the ADR draft assumed lowercase kinds
# {calls,imports,extends,implements,owns,contains}.  The real db.py vocabulary is
# uppercase and has no `contains`; it adds INSTANTIATES and *_CONTEXT.  Weights and
# the coupling set are mapped to the real kinds here.  `owns` (class->method) is
# the containment signal; *_CONTEXT edges are retrieval-context plumbing, weighted
# lowest and excluded from coupling.
# ---------------------------------------------------------------------------
EDGE_WEIGHTS: dict[str, float] = {
    "CALLS":            1.0,    # strongest coupling signal
    "EXTENDS":          0.8,
    "IMPLEMENTS":       0.8,
    "INSTANTIATES":     0.7,
    "IMPORTS":          0.6,
    "OWNS":             0.4,    # class -> method containment; structural, weak coupling
    "PROVIDES_CONTEXT": 0.2,
    "CONSUMES_CONTEXT": 0.2,
}
DEFAULT_EDGE_WEIGHT = 0.5       # any future/unknown kind still participates

# Edges that represent real coupling (used to compute god-object span on a
# separate view so an owns-clustered class cannot self-mask its span — ADR-006 §1.5).
COUPLING_KINDS: frozenset[str] = frozenset(
    {"CALLS", "EXTENDS", "IMPLEMENTS", "INSTANTIATES", "IMPORTS"}
)


def edge_weight(kind: str) -> float:
    """Weight for an edge kind; unknown kinds fall back to DEFAULT_EDGE_WEIGHT."""
    return EDGE_WEIGHTS.get(kind, DEFAULT_EDGE_WEIGHT)


# ---------------------------------------------------------------------------
# Result types (filled progressively; the report/scoring pass populates the rest)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Community:
    id: int
    label: str
    members: list[str]
    cohesion: float          # raw 0..1 internal-weight fraction (never hidden)


@dataclass(frozen=True)
class GodObject:
    fqn: str
    betweenness: float
    fan_in: int
    fan_out: int
    communities_spanned: int
    score: float


@dataclass(frozen=True)
class SplitSuggestion:
    fqn: str
    proposed_modules: list[tuple[str, list[str]]]
    rationale: str
    caveat: str = (
        "HEURISTIC — derived from EXTRACTED edges via Louvain; not verified. "
        "Validate against actual call edges before acting."
    )


@dataclass(frozen=True)
class GraphAnalysis:
    communities: list[Community]
    god_objects: list[GodObject]
    splits: list[SplitSuggestion]
    node_count: int
    edge_count: int
    modularity: float
    built_at_commit: str | None = None


# ===========================================================================
# Foundation — small, pure functions (this pass)
# ===========================================================================
def build_graph(edges: list[tuple[str, str, str]]) -> nx.DiGraph:
    """Build a weighted directed graph from ``(source, target, kind)`` triples.

    Multiple edges between the same ordered pair are aggregated: ``weight`` sums
    the per-kind weights, ``coupling_weight`` sums only the coupling kinds, and
    ``kinds`` records the set of kinds seen.  Self-loops are dropped (a symbol
    owning/calling itself adds no structural signal).
    """
    G = nx.DiGraph()
    for source, target, kind in edges:
        if not source or not target or source == target:
            continue
        w = edge_weight(kind)
        cw = w if kind in COUPLING_KINDS else 0.0
        if G.has_edge(source, target):
            data = G[source][target]
            data["weight"] += w
            data["coupling_weight"] += cw
            data["kinds"].add(kind)
        else:
            G.add_edge(source, target, weight=w, coupling_weight=cw, kinds={kind})
    return G


def coupling_subgraph(G: nx.DiGraph) -> nx.DiGraph:
    """Directed subgraph containing only edges with a coupling component.

    Used to compute community membership for god-object *span* independently of
    the full-graph clustering, so containment (OWNS) cannot pull a class's
    methods into one community and mask its true span (ADR-006 §1.5).
    """
    H = nx.DiGraph()
    H.add_nodes_from(G.nodes())
    for u, v, data in G.edges(data=True):
        cw = data.get("coupling_weight", 0.0)
        if cw > 0.0:
            H.add_edge(u, v, weight=cw, kinds=set(data.get("kinds", set())) & COUPLING_KINDS)
    return H


def to_weighted_undirected(G: nx.DiGraph, weight_attr: str = "weight") -> nx.Graph:
    """Undirected projection that *sums* reciprocal edge weights.

    ``DiGraph.to_undirected()`` lets one direction's attributes clobber the
    other; for modularity/Louvain we want the combined coupling strength.
    """
    U = nx.Graph()
    U.add_nodes_from(G.nodes())
    for u, v, data in G.edges(data=True):
        w = data.get(weight_attr, 1.0)
        if U.has_edge(u, v):
            U[u][v]["weight"] += w
        else:
            U.add_edge(u, v, weight=w)
    return U


def detect_communities(G: nx.DiGraph, seed: int = GRAPH_SEED) -> list[set[str]]:
    """Partition nodes into communities via Louvain on the weighted undirected
    projection.  Deterministic under a fixed ``seed``.

    Base implementation only — the A1 low-density refinement pass (ADR-006 §1.3)
    is applied by ``refine_communities`` in a later step.  Isolated nodes (no
    edges) each form their own singleton community, matching Louvain semantics.
    """
    if G.number_of_nodes() == 0:
        return []
    U = to_weighted_undirected(G)
    communities = nx_comm.louvain_communities(U, weight="weight", seed=seed)
    return [set(c) for c in communities]


def community_cohesion(G: nx.DiGraph, members: set[str], weight_attr: str = "weight") -> float:
    """Fraction of the community's incident edge weight that stays internal.

    0.0 = every edge leaves the community; 1.0 = fully self-contained.  Reported
    as a raw number (honesty rule — never collapsed to a symbol).  A community
    with no incident edges returns 0.0.
    """
    member_set = members if isinstance(members, set) else set(members)
    internal = 0.0
    incident = 0.0
    for u, v, data in G.edges(data=True):
        w = data.get(weight_attr, 1.0)
        u_in = u in member_set
        v_in = v in member_set
        if u_in or v_in:
            incident += w
            if u_in and v_in:
                internal += w
    return internal / incident if incident > 0.0 else 0.0


def graph_modularity(G: nx.DiGraph, communities: list[set[str]]) -> float:
    """Newman modularity of the partition on the weighted undirected projection."""
    if G.number_of_nodes() == 0 or not communities:
        return 0.0
    U = to_weighted_undirected(G)
    return nx_comm.modularity(U, communities, weight="weight")


def compute_centrality(G: nx.DiGraph) -> dict[str, dict]:
    """Per-node ``{betweenness, fan_in, fan_out}``.

    Betweenness is computed *unweighted* (structural bridge by hop count): our
    edge weights are similarities, not distances, so feeding them to a
    shortest-path betweenness would invert the meaning.  Distance-weighted
    betweenness is deferred to the scoring pass.  Above ``BETWEENNESS_EXACT_MAX``
    nodes, an approximate ``k``-sampled betweenness is used (A7 latency target).
    """
    n = G.number_of_nodes()
    if n == 0:
        return {}
    if n > BETWEENNESS_EXACT_MAX:
        bt = nx.betweenness_centrality(
            G, k=min(BETWEENNESS_SAMPLE_K, n), normalized=True, seed=GRAPH_SEED
        )
    else:
        bt = nx.betweenness_centrality(G, normalized=True)
    out = {}
    for node in G.nodes():
        out[node] = {
            "betweenness": bt.get(node, 0.0),
            "fan_in": G.in_degree(node),
            "fan_out": G.out_degree(node),
        }
    return out


# ===========================================================================
# Large bespoke functions
# ===========================================================================
def internal_density(U: nx.Graph, members: set[str]) -> float:
    """Edge-count density of a member set on an undirected graph U: actual
    internal edges / possible internal edges.  Singletons/empties return 1.0
    (a community too small to be 'sparse' is never a split candidate).
    """
    member_set = members if isinstance(members, set) else set(members)
    n = len(member_set)
    if n < 2:
        return 1.0
    internal = sum(1 for u, v in U.edges() if u in member_set and v in member_set)
    possible = n * (n - 1) / 2
    return internal / possible if possible > 0 else 1.0


def _internal_ratio(U: nx.Graph, node: str, members: set[str]) -> float:
    """Fraction of a node's incident weight (on U) that stays inside `members`."""
    total = 0.0
    inside = 0.0
    for nbr in U.neighbors(node):
        w = U[node][nbr].get("weight", 1.0)
        total += w
        if nbr in members:
            inside += w
    return inside / total if total > 0 else 0.0


def _best_community_index(U: nx.Graph, node: str, comms: list[set[str]]) -> int | None:
    """Index of the community `node` is most strongly attached to (by weight), or None."""
    best_idx, best_w = None, 0.0
    for i, c in enumerate(comms):
        w = sum(U[node][m].get("weight", 1.0) for m in U.neighbors(node) if m in c)
        if w > best_w:
            best_idx, best_w = i, w
    return best_idx


def refine_communities(G: nx.DiGraph, communities: list[set[str]]) -> list[set[str]]:
    """A1 — split low-density communities by ejecting weakly-connected members,
    then reassigning each ejected node to the neighbouring community it is most
    attached to (a single local-move step).  Converges within MAX_REFINE_ITERS.

    Source: Codebase-Memory paper §3.7.  In practice the MIN_INTERNAL_DENSITY=1%
    gate only fires on large or internally-disconnected communities — exactly the
    file-mirror / over-merged pathology the grill flagged; it is a deliberate
    no-op on small, cohesive partitions.  Every input node appears in exactly one
    output community.
    """
    if G.number_of_nodes() == 0:
        return [set(c) for c in communities]
    U = to_weighted_undirected(G)
    comms = [set(c) for c in communities]

    for _ in range(MAX_REFINE_ITERS):
        kept: list[set[str]] = []
        ejected: list[str] = []
        for c in comms:
            if len(c) >= 2 and internal_density(U, c) < MIN_INTERNAL_DENSITY:
                strong = {n for n in c if _internal_ratio(U, n, c) >= EJECT_RATIO}
                weak = c - strong
                if weak:
                    ejected.extend(weak)
                    if strong:
                        kept.append(strong)
                    # if nothing is strong, the community dissolves entirely
                else:
                    kept.append(c)
            else:
                kept.append(c)

        if not ejected:
            comms = [c for c in kept if c]
            break

        for node in ejected:
            idx = _best_community_index(U, node, kept)
            if idx is not None:
                kept[idx].add(node)
            else:
                kept.append({node})  # no external attachment -> singleton
        comms = [c for c in kept if c]

    return comms


def coupling_membership(G: nx.DiGraph, seed: int = GRAPH_SEED) -> dict[str, int]:
    """Map each *coupled* node to a community id derived from the coupling
    subgraph only (CALLS/EXTENDS/IMPLEMENTS/INSTANTIATES/IMPORTS).

    Nodes with no coupling edge are omitted — they contribute nothing to a
    god-object's span.  Clustering here is independent of OWNS containment, so a
    class's own methods cannot be forced together and self-mask the span
    (ADR-006 §1.5).
    """
    H = coupling_subgraph(G)
    core = [n for n in H.nodes() if H.degree(n) > 0]
    if not core:
        return {}
    Hc = H.subgraph(core)
    membership: dict[str, int] = {}
    for i, c in enumerate(detect_communities(Hc, seed=seed)):
        for n in c:
            membership[n] = i
    return membership


def _owned_methods(G: nx.DiGraph, fqn: str) -> list[str]:
    """Targets of OWNS edges out of `fqn` (its members)."""
    return [m for m in G.successors(fqn) if "OWNS" in G[fqn][m].get("kinds", set())]


def find_god_objects(
    G: nx.DiGraph,
    centrality: dict[str, dict],
    span_membership: dict[str, int],
    min_communities: int = GOD_MIN_COMMUNITIES,
) -> list[GodObject]:
    """Flag owners whose *coupled* members sprawl across many coupling communities.

    Gate (structural signature): a node's owned methods, restricted to those that
    have coupling edges, must land in >= `min_communities` distinct coupling
    communities.  Betweenness and fan-in feed the ranking score but are not the
    gate.  Span is read from `span_membership` (coupling-only partition), never
    from the all-edge clustering, so an OWNS-clustered class cannot self-mask.
    Returned highest-score first.
    """
    if not centrality:
        return []
    max_bt = max((c["betweenness"] for c in centrality.values()), default=0.0) or 0.0
    max_fan = max((c["fan_in"] for c in centrality.values()), default=0) or 0
    total_comms = len(set(span_membership.values())) or 1

    god_objects: list[GodObject] = []
    for node in G.nodes():
        owned = _owned_methods(G, node)
        if not owned:
            continue
        spanned = len({span_membership[m] for m in owned if m in span_membership})
        if spanned < min_communities:
            continue
        c = centrality.get(node, {"betweenness": 0.0, "fan_in": 0, "fan_out": 0})
        norm_bt = c["betweenness"] / max_bt if max_bt > 0 else 0.0
        norm_fi = c["fan_in"] / max_fan if max_fan > 0 else 0.0
        score = 0.5 * norm_bt + 0.3 * norm_fi + 0.2 * (spanned / total_comms)
        god_objects.append(
            GodObject(node, c["betweenness"], c["fan_in"], c["fan_out"], spanned, score)
        )
    god_objects.sort(key=lambda g: g.score, reverse=True)
    return god_objects


def _short_name(fqn: str) -> str:
    """Last path/scope segment of an FQN, for naming proposed modules."""
    tail = fqn.split("::")[-1]
    return tail.split(".")[-1] or tail


def suggest_splits(
    G: nx.DiGraph,
    god_objects: list[GodObject],
    span_membership: dict[str, int],
) -> list[SplitSuggestion]:
    """Opt-in: group each god-object's owned members by coupling community and
    propose one module per cohesive group (size >= 2).  Only god-objects that
    decompose into >= 2 such groups yield a suggestion.  Every suggestion carries
    the HEURISTIC caveat (ADR-006 §1.6).
    """
    suggestions: list[SplitSuggestion] = []
    for go in god_objects:
        owned = _owned_methods(G, go.fqn)
        groups: dict[int, list[str]] = {}
        for m in owned:
            cid = span_membership.get(m)
            if cid is None:
                continue
            groups.setdefault(cid, []).append(m)

        base = _short_name(go.fqn)
        modules = [
            (f"{base}_part{i + 1}", sorted(members))
            for i, (_cid, members) in enumerate(sorted(groups.items()))
            if len(members) >= 2
        ]
        if len(modules) >= 2:
            suggestions.append(
                SplitSuggestion(
                    fqn=go.fqn,
                    proposed_modules=modules,
                    rationale=(
                        f"{go.fqn} owns {len(owned)} members spanning "
                        f"{go.communities_spanned} coupling communities; grouped into "
                        f"{len(modules)} cohesive candidate modules."
                    ),
                )
            )
    return suggestions


def _edge_in_scope(edge: tuple[str, str, str], target_path: str) -> bool:
    """True if either endpoint's file segment is under `target_path`."""
    source, target, _ = edge
    sfile = source.split("::", 1)[0]
    tfile = target.split("::", 1)[0]
    return sfile.startswith(target_path) or tfile.startswith(target_path)


def analyze(db, target_path: str = "", include_splits: bool = False) -> GraphAnalysis:
    """Top-level orchestrator: edges -> graph -> (refined) communities + centrality
    + coupling-span -> god-objects -> optional splits -> GraphAnalysis.

    `db` only needs a ``get_graph_edges()`` method (duck-typed; no FAISS import).
    `include_splits` maps to the MCP tool's ``suggest_splits`` argument — split
    proposals are computed only when True.  Community ``label`` fields are left
    blank here; the report layer fills them heuristically.
    """
    edges = db.get_graph_edges()
    if target_path:
        edges = [e for e in edges if _edge_in_scope(e, target_path)]

    G = build_graph(edges)
    communities = refine_communities(G, detect_communities(G))
    centrality = compute_centrality(G)
    span_membership = coupling_membership(G)
    god_objects = find_god_objects(G, centrality, span_membership)
    splits = suggest_splits(G, god_objects, span_membership) if include_splits else []

    community_objs = [
        Community(id=i, label="", members=sorted(c), cohesion=community_cohesion(G, c))
        for i, c in enumerate(communities)
    ]
    return GraphAnalysis(
        communities=community_objs,
        god_objects=god_objects,
        splits=splits,
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
        modularity=graph_modularity(G, communities),
        built_at_commit=None,
    )
