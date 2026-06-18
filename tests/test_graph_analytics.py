"""
Tests for graph_analytics.py (ADR-006) base layer + CodeDB.get_graph_edges().

Scope: the FOUNDATION functions only — graph construction, community detection,
cohesion, modularity, centrality, and the typed-edge reader.  The large bespoke
functions (refinement, god-object scoring, splits, analyze) are deferred and are
asserted here only to be present-and-stubbed.

The pure-networkx tests need no FAISS.  The single db test imports `db` locally
(it transitively pulls FAISS via core) so the rest of the module collects in any
environment with networkx.
"""
import pytest

import graph_analytics as ga


# ---------------------------------------------------------------------------
# build_graph / edge_weight
# ---------------------------------------------------------------------------
def test_edge_weight_known_and_unknown():
    assert ga.edge_weight("CALLS") == 1.0
    assert ga.edge_weight("OWNS") == 0.4
    assert ga.edge_weight("TOTALLY_NEW_KIND") == ga.DEFAULT_EDGE_WEIGHT


def test_build_graph_aggregates_weight_kinds_and_coupling():
    edges = [
        ("A", "B", "CALLS"),     # coupling 1.0
        ("A", "B", "OWNS"),      # containment 0.4
        ("B", "C", "IMPORTS"),   # coupling 0.6
    ]
    G = ga.build_graph(edges)
    assert set(G.nodes()) == {"A", "B", "C"}
    ab = G["A"]["B"]
    assert ab["weight"] == pytest.approx(1.4)         # 1.0 + 0.4
    assert ab["coupling_weight"] == pytest.approx(1.0)  # only CALLS counts as coupling
    assert ab["kinds"] == {"CALLS", "OWNS"}
    assert G["B"]["C"]["coupling_weight"] == pytest.approx(0.6)


def test_build_graph_drops_selfloops_and_blanks():
    edges = [
        ("A", "A", "CALLS"),   # self-loop dropped
        ("", "B", "CALLS"),    # blank source dropped
        ("A", "", "CALLS"),    # blank target dropped
        ("A", "B", "CALLS"),   # kept
    ]
    G = ga.build_graph(edges)
    assert G.number_of_edges() == 1
    assert G.has_edge("A", "B")


def test_build_graph_empty():
    G = ga.build_graph([])
    assert G.number_of_nodes() == 0


# ---------------------------------------------------------------------------
# coupling_subgraph
# ---------------------------------------------------------------------------
def test_coupling_subgraph_excludes_containment():
    edges = [
        ("Cls", "Cls.method", "OWNS"),       # containment only -> excluded
        ("Cls.method", "Other", "CALLS"),    # coupling -> kept
        ("X", "Y", "PROVIDES_CONTEXT"),      # context only -> excluded
    ]
    G = ga.build_graph(edges)
    H = ga.coupling_subgraph(G)
    # all nodes preserved, only the coupling edge survives
    assert set(H.nodes()) == set(G.nodes())
    assert H.has_edge("Cls.method", "Other")
    assert not H.has_edge("Cls", "Cls.method")
    assert not H.has_edge("X", "Y")
    assert H.number_of_edges() == 1


# ---------------------------------------------------------------------------
# to_weighted_undirected
# ---------------------------------------------------------------------------
def test_to_weighted_undirected_sums_reciprocal():
    G = ga.build_graph([("A", "B", "CALLS"), ("B", "A", "IMPORTS")])  # 1.0 and 0.6
    U = ga.to_weighted_undirected(G)
    assert U.number_of_edges() == 1
    assert U["A"]["B"]["weight"] == pytest.approx(1.6)


# ---------------------------------------------------------------------------
# detect_communities
# ---------------------------------------------------------------------------
def _two_cluster_edges():
    """Two CALLS triangles joined by a single bridge edge C->X."""
    tri1 = [("A", "B"), ("B", "C"), ("C", "A")]
    tri2 = [("X", "Y"), ("Y", "Z"), ("Z", "X")]
    bridge = [("C", "X")]
    return [(u, v, "CALLS") for u, v in tri1 + tri2 + bridge]


def test_detect_communities_finds_two_clusters():
    G = ga.build_graph(_two_cluster_edges())
    comms = ga.detect_communities(G)
    assert len(comms) == 2
    # the two triangles must not be split across communities
    members = [frozenset(c) for c in comms]
    assert frozenset({"A", "B", "C"}) in members
    assert frozenset({"X", "Y", "Z"}) in members


def test_detect_communities_deterministic():
    G = ga.build_graph(_two_cluster_edges())
    first = {frozenset(c) for c in ga.detect_communities(G, seed=ga.GRAPH_SEED)}
    second = {frozenset(c) for c in ga.detect_communities(G, seed=ga.GRAPH_SEED)}
    assert first == second


def test_detect_communities_empty():
    assert ga.detect_communities(ga.build_graph([])) == []


# ---------------------------------------------------------------------------
# community_cohesion
# ---------------------------------------------------------------------------
def test_cohesion_fully_internal_is_one():
    G = ga.build_graph([("A", "B", "CALLS"), ("B", "C", "CALLS"), ("C", "A", "CALLS")])
    assert ga.community_cohesion(G, {"A", "B", "C"}) == pytest.approx(1.0)


def test_cohesion_all_boundary_is_zero():
    # the community is a single node whose only edge leaves it
    G = ga.build_graph([("A", "B", "CALLS")])
    assert ga.community_cohesion(G, {"A"}) == pytest.approx(0.0)


def test_cohesion_partial():
    # triangle internal (3 * 1.0) + one leaving edge C->D (1.0)
    edges = [("A", "B", "CALLS"), ("B", "C", "CALLS"), ("C", "A", "CALLS"), ("C", "D", "CALLS")]
    G = ga.build_graph(edges)
    # internal weight 3.0, incident weight 4.0
    assert ga.community_cohesion(G, {"A", "B", "C"}) == pytest.approx(3.0 / 4.0)


def test_cohesion_no_incident_edges():
    G = ga.build_graph([("A", "B", "CALLS")])
    assert ga.community_cohesion(G, {"Z"}) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# graph_modularity
# ---------------------------------------------------------------------------
def test_modularity_positive_for_clustered_graph():
    G = ga.build_graph(_two_cluster_edges())
    comms = ga.detect_communities(G)
    assert ga.graph_modularity(G, comms) > 0.2


def test_modularity_empty_graph_is_zero():
    assert ga.graph_modularity(ga.build_graph([]), []) == 0.0


# ---------------------------------------------------------------------------
# compute_centrality
# ---------------------------------------------------------------------------
def test_centrality_hub_has_max_betweenness_and_correct_fans():
    # 3 sources -> H -> 3 sinks; every cross path traverses H
    edges = (
        [(f"L{i}", "H", "CALLS") for i in range(3)]
        + [("H", f"R{i}", "CALLS") for i in range(3)]
    )
    G = ga.build_graph(edges)
    cent = ga.compute_centrality(G)
    bt = {n: cent[n]["betweenness"] for n in cent}
    assert bt["H"] == max(bt.values())
    assert bt["H"] > 0.0
    assert cent["H"]["fan_in"] == 3
    assert cent["H"]["fan_out"] == 3
    assert cent["L0"]["fan_in"] == 0
    assert cent["L0"]["fan_out"] == 1


def test_centrality_empty_graph():
    assert ga.compute_centrality(ga.build_graph([])) == {}


# ---------------------------------------------------------------------------
# internal_density
# ---------------------------------------------------------------------------
def test_internal_density_values():
    G = ga.build_graph([("A", "B", "CALLS"), ("B", "C", "CALLS"), ("C", "A", "CALLS")])
    U = ga.to_weighted_undirected(G)
    assert ga.internal_density(U, {"A", "B", "C"}) == pytest.approx(1.0)   # triangle, 3/3
    assert ga.internal_density(U, {"A", "B"}) == pytest.approx(1.0)        # 1/1
    assert ga.internal_density(U, {"A"}) == pytest.approx(1.0)            # singleton -> 1.0
    # a set with no internal edges among its members
    G2 = ga.build_graph([("X", "A", "CALLS"), ("Y", "A", "CALLS"), ("Z", "B", "CALLS")])
    U2 = ga.to_weighted_undirected(G2)
    assert ga.internal_density(U2, {"X", "Y", "Z"}) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# refine_communities (A1)
# ---------------------------------------------------------------------------
def test_refine_dissolves_zero_density_community():
    # {A,B} cohesive; {X,Y,Z} have NO internal edges (density 0) but each couples
    # into {A,B}. Refinement must dissolve {X,Y,Z} and reassign them to {A,B}.
    edges = [
        ("A", "B", "CALLS"), ("B", "A", "CALLS"),
        ("X", "A", "CALLS"), ("Y", "A", "CALLS"), ("Z", "B", "CALLS"),
    ]
    G = ga.build_graph(edges)
    refined = ga.refine_communities(G, [{"A", "B"}, {"X", "Y", "Z"}])
    members = [frozenset(c) for c in refined]
    assert frozenset({"X", "Y", "Z"}) not in members          # dissolved
    # every node still present exactly once
    all_nodes = [n for c in refined for n in c]
    assert sorted(all_nodes) == ["A", "B", "X", "Y", "Z"]
    assert len(all_nodes) == len(set(all_nodes))
    # X/Y/Z landed in the community containing A
    host = next(c for c in refined if "A" in c)
    assert {"X", "Y", "Z"} <= host


def test_refine_noop_on_cohesive_partition():
    G = ga.build_graph(_two_cluster_edges())
    comms = ga.detect_communities(G)
    before = {frozenset(c) for c in comms}
    after = {frozenset(c) for c in ga.refine_communities(G, comms)}
    assert before == after


# ---------------------------------------------------------------------------
# coupling_membership
# ---------------------------------------------------------------------------
def test_coupling_membership_ignores_owns_only_nodes():
    # method M is OWNED by Cls but has NO coupling edge -> excluded from membership
    edges = [
        ("Cls", "M", "OWNS"),
        ("A", "B", "CALLS"), ("B", "A", "CALLS"),
    ]
    G = ga.build_graph(edges)
    membership = ga.coupling_membership(G)
    assert "M" not in membership
    assert "A" in membership and "B" in membership


# ---------------------------------------------------------------------------
# find_god_objects
# ---------------------------------------------------------------------------
def _god_object_graph():
    """Cls owns m1,m2,m3; each method couples into a different cluster, so Cls's
    members span 3 coupling communities. Tight owns d1,d2 in the same cluster."""
    edges = [
        # god-object class
        ("Cls", "m1", "OWNS"), ("Cls", "m2", "OWNS"), ("Cls", "m3", "OWNS"),
        # three disjoint coupled clusters, one per method
        ("m1", "P1", "CALLS"), ("P1", "P2", "CALLS"), ("P2", "m1", "CALLS"),
        ("m2", "Q1", "CALLS"), ("Q1", "Q2", "CALLS"), ("Q2", "m2", "CALLS"),
        ("m3", "R1", "CALLS"), ("R1", "R2", "CALLS"), ("R2", "m3", "CALLS"),
        # a tight, non-god class
        ("Tight", "d1", "OWNS"), ("Tight", "d2", "OWNS"),
        ("d1", "d2", "CALLS"), ("d2", "d1", "CALLS"),
    ]
    return ga.build_graph(edges)


def test_find_god_objects_flags_spanning_class_only():
    G = _god_object_graph()
    centrality = ga.compute_centrality(G)
    membership = ga.coupling_membership(G)
    gods = ga.find_god_objects(G, centrality, membership)
    fqns = {g.fqn for g in gods}
    assert "Cls" in fqns
    assert "Tight" not in fqns
    cls = next(g for g in gods if g.fqn == "Cls")
    assert cls.communities_spanned >= 3
    assert cls.score > 0.0


def test_find_god_objects_empty_graph():
    assert ga.find_god_objects(ga.build_graph([]), {}, {}) == []


# ---------------------------------------------------------------------------
# suggest_splits
# ---------------------------------------------------------------------------
def test_suggest_splits_groups_members_with_caveat():
    G = _god_object_graph()
    centrality = ga.compute_centrality(G)
    membership = ga.coupling_membership(G)
    gods = ga.find_god_objects(G, centrality, membership)
    splits = ga.suggest_splits(G, gods, membership)
    # Cls has m1,m2,m3 each in its own community -> singleton groups (size 1),
    # so no module reaches size>=2 -> no split proposed. Verify that contract.
    assert splits == []

    # Now give Cls two members in the SAME community plus another community:
    edges = [
        ("Cls", "a1", "OWNS"), ("Cls", "a2", "OWNS"),
        ("Cls", "b1", "OWNS"), ("Cls", "b2", "OWNS"),
        ("a1", "a2", "CALLS"), ("a2", "a1", "CALLS"),   # community A
        ("b1", "b2", "CALLS"), ("b2", "b1", "CALLS"),   # community B (disjoint)
    ]
    G2 = ga.build_graph(edges)
    cent2 = ga.compute_centrality(G2)
    mem2 = ga.coupling_membership(G2)
    gods2 = ga.find_god_objects(G2, cent2, mem2, min_communities=2)
    splits2 = ga.suggest_splits(G2, gods2, mem2)
    assert len(splits2) == 1
    s = splits2[0]
    assert s.fqn == "Cls"
    assert len(s.proposed_modules) == 2
    assert "HEURISTIC" in s.caveat
    grouped = {m for _, members in s.proposed_modules for m in members}
    assert grouped == {"a1", "a2", "b1", "b2"}


# ---------------------------------------------------------------------------
# analyze (orchestrator) — duck-typed db, no FAISS
# ---------------------------------------------------------------------------
class _FakeDB:
    def __init__(self, edges):
        self._edges = edges

    def get_graph_edges(self):
        return list(self._edges)


def test_analyze_end_to_end():
    G = _god_object_graph()
    edges = [(u, v, next(iter(d["kinds"]))) for u, v, d in G.edges(data=True)]
    analysis = ga.analyze(_FakeDB(edges), include_splits=True)
    assert isinstance(analysis, ga.GraphAnalysis)
    assert analysis.node_count == G.number_of_nodes()
    assert analysis.edge_count == G.number_of_edges()
    assert any(go.fqn == "Cls" for go in analysis.god_objects)
    # communities carry raw cohesion numbers
    assert all(0.0 <= c.cohesion <= 1.0 for c in analysis.communities)
    assert isinstance(analysis.modularity, float)


def test_analyze_without_splits_returns_no_splits():
    G = _god_object_graph()
    edges = [(u, v, next(iter(d["kinds"]))) for u, v, d in G.edges(data=True)]
    analysis = ga.analyze(_FakeDB(edges), include_splits=False)
    assert analysis.splits == []


def test_analyze_target_path_scoping():
    edges = [
        ("src/db.py::CodeDB.a", "src/db.py::CodeDB.b", "CALLS"),
        ("src/tui/app.py::App.run", "src/tui/app.py::App.stop", "CALLS"),
    ]
    analysis = ga.analyze(_FakeDB(edges), target_path="src/db.py")
    nodes = {n for c in analysis.communities for n in c.members}
    assert any("db.py" in n for n in nodes)
    assert not any("app.py" in n for n in nodes)


# ---------------------------------------------------------------------------
# CodeDB.get_graph_edges() — needs FAISS via db import, so import locally
# ---------------------------------------------------------------------------
def test_get_graph_edges_all_kinds_and_resolved_target(tmp_path):
    from db import CodeDB

    db = CodeDB(tmp_path / "graph.db")
    try:
        db._conn.execute(
            "INSERT INTO edges(source_fqn, target, kind, resolved_target) VALUES (?,?,?,?)",
            ("mod.A", "mod.B", "CALLS", None),
        )
        db._conn.execute(
            "INSERT INTO edges(source_fqn, target, kind, resolved_target) VALUES (?,?,?,?)",
            ("mod.A", "raw.target", "IMPORTS", "canonical.target"),
        )
        db._conn.execute(
            "INSERT INTO edges(source_fqn, target, kind, resolved_target) VALUES (?,?,?,?)",
            ("mod.Cls", "mod.Cls.method", "OWNS", None),
        )
        edges = db.get_graph_edges()

        assert ("mod.A", "mod.B", "CALLS") in edges
        # resolved_target preferred over raw target
        assert ("mod.A", "canonical.target", "IMPORTS") in edges
        assert ("mod.A", "raw.target", "IMPORTS") not in edges
        # all kinds present, not just CALLS
        assert {k for _, _, k in edges} == {"CALLS", "IMPORTS", "OWNS"}
    finally:
        db.close()
