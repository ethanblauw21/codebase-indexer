"""
Tests for graph_report.py (ADR-006 §2) — the markdown report layer.

These are pure: graph_report imports only the standard library and the engine's
result types, so nothing here needs FAISS.  The engine (graph_analytics) is used
to produce realistic GraphAnalysis fixtures end-to-end, and a couple of analyses
are hand-built to exercise rendering edge cases deterministically.

Per ADR-006 the bar is MECHANICS + HONESTY, not finding-quality: we assert the
report runs, surfaces the raw numbers, carries the exploratory disclaimer + audit
note, respects min_community_size, and stamps every split with its caveat.
"""
import os

import pytest

import graph_analytics as ga
import graph_report as gr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _FakeDB:
    def __init__(self, edges):
        self._edges = edges

    def get_graph_edges(self):
        return list(self._edges)


def _god_object_graph_edges():
    """Same shape as the engine test: Cls owns m1/m2/m3 each coupling into a
    distinct cluster (span 3); a separate tight, non-god class."""
    G = ga.build_graph([
        ("Cls", "m1", "OWNS"), ("Cls", "m2", "OWNS"), ("Cls", "m3", "OWNS"),
        ("m1", "P1", "CALLS"), ("P1", "P2", "CALLS"), ("P2", "m1", "CALLS"),
        ("m2", "Q1", "CALLS"), ("Q1", "Q2", "CALLS"), ("Q2", "m2", "CALLS"),
        ("m3", "R1", "CALLS"), ("R1", "R2", "CALLS"), ("R2", "m3", "CALLS"),
        ("Tight", "d1", "OWNS"), ("Tight", "d2", "OWNS"),
        ("d1", "d2", "CALLS"), ("d2", "d1", "CALLS"),
    ])
    return [(u, v, next(iter(d["kinds"]))) for u, v, d in G.edges(data=True)]


# ---------------------------------------------------------------------------
# label_community
# ---------------------------------------------------------------------------
def test_module_of_strips_path_and_extension():
    assert gr._module_of("src/db.py::CodeDB.get_call_graph") == "db"
    assert gr._module_of("src\\core.py::embed") == "core"
    assert gr._module_of("bare_target") == "bare_target"


def test_label_dominant_module():
    members = ["src/db.py::CodeDB.a", "src/db.py::CodeDB.b", "src/db.py::CodeDB.c"]
    label = gr.label_community(members, set())
    assert "`db`" in label
    assert "module" not in label  # single module -> no "(+N modules)" suffix


def test_label_reports_extra_modules_and_central_god_object():
    members = [
        "src/db.py::CodeDB.a", "src/db.py::CodeDB.b",
        "src/core.py::embed", "src/MCPServer.py::reindex",
    ]
    god = {"src/db.py::CodeDB.a"}
    label = gr.label_community(members, god)
    assert "`db`" in label
    assert "module" in label          # reaches into other modules
    assert "central: `a`" in label    # the god-object member is named


def test_label_empty():
    assert gr.label_community([], set()) == "(empty)"


# ---------------------------------------------------------------------------
# render_report — structure / honesty
# ---------------------------------------------------------------------------
def test_render_empty_graph():
    analysis = ga.analyze(_FakeDB([]))
    report = gr.render_report(analysis)
    assert "empty" in report.lower()
    assert "reindex" in report.lower()
    # exploratory disclaimer is present even on the empty path
    assert "not an accuracy claim" in report


def test_render_has_exploratory_header_and_audit_note():
    analysis = ga.analyze(_FakeDB(_god_object_graph_edges()))
    report = gr.render_report(analysis)
    assert "not an accuracy claim" in report
    assert "ADR-008" in report                       # bar deferred
    assert "only EXTRACTED edges" in report          # provenance audit note


def test_render_summary_exposes_raw_numbers():
    analysis = ga.analyze(_FakeDB(_god_object_graph_edges()))
    report = gr.render_report(analysis)
    assert f"**Symbols (nodes):** {analysis.node_count}" in report
    assert f"**Edges:** {analysis.edge_count}" in report
    # modularity printed as a raw 4-dp number, never hidden
    assert f"{analysis.modularity:.4f}" in report


def test_render_flags_god_object_in_table():
    analysis = ga.analyze(_FakeDB(_god_object_graph_edges()))
    report = gr.render_report(analysis)
    assert "## God-Objects" in report
    assert "`Cls`" in report
    assert "| Symbol | Betweenness | Fan-in | Fan-out | Communities spanned | Score |" in report


def test_render_no_god_objects_explains_coupling_requirement():
    # OWNS-only graph: no coupling edges -> no god-objects (the stale-index case)
    analysis = ga.analyze(_FakeDB([
        ("Cls", "m1", "OWNS"), ("Cls", "m2", "OWNS"), ("Cls", "m3", "OWNS"),
    ]))
    report = gr.render_report(analysis)
    assert analysis.god_objects == []
    assert "None detected" in report
    assert "coupling" in report.lower()
    assert "reindex" in report.lower()


def test_render_min_community_size_filters_body_not_summary():
    # one 3-node community + isolated singletons via OWNS span
    edges = _god_object_graph_edges()
    analysis = ga.analyze(_FakeDB(edges))
    total = len(analysis.communities)
    # a huge floor hides every community from the body but the count stays in summary
    report = gr.render_report(analysis, min_community_size=999)
    assert f"**Communities:** {total}" in report
    assert "No community has ≥ 999 members" in report


def test_render_splits_carry_caveat_per_suggestion():
    # Cls with two same-community pairs across two communities -> one split
    edges_graph = ga.build_graph([
        ("Cls", "a1", "OWNS"), ("Cls", "a2", "OWNS"),
        ("Cls", "b1", "OWNS"), ("Cls", "b2", "OWNS"),
        ("a1", "a2", "CALLS"), ("a2", "a1", "CALLS"),
        ("b1", "b2", "CALLS"), ("b2", "b1", "CALLS"),
    ])
    edges = [(u, v, next(iter(d["kinds"]))) for u, v, d in edges_graph.edges(data=True)]

    # include_splits=True but GOD_MIN_COMMUNITIES gate is 3; this fixture spans 2,
    # so go through the engine pieces with min_communities=2 to force a split, then
    # build the analysis object directly for rendering.
    G = ga.build_graph(edges)
    cent = ga.compute_centrality(G)
    mem = ga.coupling_membership(G)
    gods = ga.find_god_objects(G, cent, mem, min_communities=2)
    splits = ga.suggest_splits(G, gods, mem)
    assert splits, "fixture should produce a split"
    analysis = ga.GraphAnalysis(
        communities=[ga.Community(0, "", ["a1", "a2", "b1", "b2"], 1.0)],
        god_objects=gods,
        splits=splits,
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
        modularity=0.0,
    )
    report = gr.render_report(analysis)
    assert "## Suggested Splits" in report
    assert "[HEURISTIC — unverified]" in report
    # the per-suggestion caveat is also present
    assert "Validate against actual call edges" in report
    assert "`Cls`" in report


def test_render_no_splits_section_when_absent():
    analysis = ga.analyze(_FakeDB(_god_object_graph_edges()), include_splits=False)
    report = gr.render_report(analysis)
    assert "## Suggested Splits" not in report


def test_render_scope_header_when_target_path_given():
    analysis = ga.analyze(_FakeDB(_god_object_graph_edges()))
    report = gr.render_report(analysis, target_path="src/db.py")
    assert "scoped to `src/db.py`" in report


# ---------------------------------------------------------------------------
# Integration — full engine -> report against the live index, if present
# ---------------------------------------------------------------------------
def test_render_against_live_index_if_present():
    """Smoke check: run the whole pipeline against the real graph.db when it
    exists. Importing db pulls FAISS, so this needs the VectorEnv interpreter;
    it skips cleanly when the index or stack is unavailable."""
    index_db = os.path.join(".code-index", "graph.db")
    if not os.path.exists(index_db):
        pytest.skip("no live .code-index/graph.db to analyze")
    try:
        from db import CodeDB
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"db import unavailable: {exc}")

    with CodeDB(index_db) as db:
        analysis = ga.analyze(db)
    report = gr.render_report(analysis)
    assert "# Module Community Map" in report
    assert "## Summary" in report
    assert "## Audit" in report
