"""
Tests for graph_viz.py (ADR-006 §3) — the Design Structure Matrix renderer.

Pure: graph_viz imports only stdlib + the engine + the report layer's labeling
helper, so nothing here needs FAISS.  Per ADR-006 the bar is MECHANICS, not visual
quality — we assert the file is written, is self-contained (no external CDN), embeds
the matrix payload, marks god-objects, and that the aggregation guard switches to a
community×community matrix above the node cap.
"""
import json
import re

import pytest

import graph_analytics as ga
import graph_viz as gv


class _FakeDB:
    def __init__(self, edges):
        self._edges = edges

    def get_graph_edges(self):
        return list(self._edges)


def _god_object_edges():
    G = ga.build_graph([
        ("Cls", "m1", "OWNS"), ("Cls", "m2", "OWNS"), ("Cls", "m3", "OWNS"),
        ("m1", "P1", "CALLS"), ("P1", "P2", "CALLS"), ("P2", "m1", "CALLS"),
        ("m2", "Q1", "CALLS"), ("Q1", "Q2", "CALLS"), ("Q2", "m2", "CALLS"),
        ("m3", "R1", "CALLS"), ("R1", "R2", "CALLS"), ("R2", "m3", "CALLS"),
        ("Tight", "d1", "OWNS"), ("Tight", "d2", "OWNS"),
        ("d1", "d2", "CALLS"), ("d2", "d1", "CALLS"),
    ])
    return [(u, v, next(iter(d["kinds"]))) for u, v, d in G.edges(data=True)]


def _extract_payload(html: str) -> dict:
    """Pull the inlined `const DATA = {...};` payload back out of the HTML."""
    m = re.search(r"const DATA = (\{.*?\});\nconst N", html, re.DOTALL)
    assert m, "DATA payload not found in HTML"
    return json.loads(m.group(1).replace("<\\/", "</"))


# ---------------------------------------------------------------------------
# File output / self-containment
# ---------------------------------------------------------------------------
def test_render_dsm_writes_self_contained_file(tmp_path):
    edges = _god_object_edges()
    analysis = ga.analyze(_FakeDB(edges))
    out = tmp_path / "nested" / "architecture_matrix.html"
    path = gv.render_dsm(analysis, _FakeDB(edges), out_path=str(out))

    assert path == str(out)
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "<canvas" in html
    assert "Design Structure Matrix" in html
    # no external CDN / remote script at runtime
    assert "<script src" not in html
    assert "http://" not in html.split("</style>")[1]  # no remote refs in body/JS
    assert "cdn" not in html.lower()


def test_payload_orders_by_community_and_marks_god(tmp_path):
    edges = _god_object_edges()
    analysis = ga.analyze(_FakeDB(edges))
    out = tmp_path / "m.html"
    gv.render_dsm(analysis, _FakeDB(edges), out_path=str(out))
    payload = _extract_payload(out.read_text(encoding="utf-8"))

    assert payload["mode"] == "symbol"
    assert payload["meta"]["aggregated"] is False
    assert len(payload["nodes"]) == analysis.node_count
    # Cls is a god-object -> at least one node flagged
    assert any(n["god"] for n in payload["nodes"])
    god_fqns = {n["fqn"] for n in payload["nodes"] if n["god"]}
    assert "Cls" in god_fqns
    # communities are contiguous ranges over the ordered node list
    assert payload["communities"][0]["start"] == 0


def test_cells_reference_valid_node_indices_with_kinds(tmp_path):
    edges = _god_object_edges()
    analysis = ga.analyze(_FakeDB(edges))
    out = tmp_path / "m.html"
    gv.render_dsm(analysis, _FakeDB(edges), out_path=str(out))
    payload = _extract_payload(out.read_text(encoding="utf-8"))

    n = len(payload["nodes"])
    assert payload["cells"], "expected at least one populated cell"
    for row, col, weight, kinds in payload["cells"]:
        assert 0 <= row < n and 0 <= col < n
        assert row != col            # self-loops dropped by build_graph
        assert weight > 0
        assert isinstance(kinds, str)
    # CALLS coupling should appear in at least one tooltip kind string
    assert any("CALLS" in c[3] for c in payload["cells"])


# ---------------------------------------------------------------------------
# Aggregation guard
# ---------------------------------------------------------------------------
def test_aggregation_guard_switches_to_community_matrix(tmp_path):
    edges = _god_object_edges()
    analysis = ga.analyze(_FakeDB(edges))
    out = tmp_path / "agg.html"
    # force aggregation by setting the cap below the node count
    gv.render_dsm(analysis, _FakeDB(edges), out_path=str(out), max_nodes=1)
    html = out.read_text(encoding="utf-8")
    payload = _extract_payload(html)

    assert payload["mode"] == "aggregated"
    assert payload["meta"]["aggregated"] is True
    # one matrix node per community, not per symbol
    assert len(payload["nodes"]) == len(analysis.communities)
    assert len(payload["nodes"]) < analysis.node_count
    # banner markup is present (JS reveals it when meta.aggregated)
    assert 'id="banner"' in html
    # aggregated cells index communities, and the diagonal (internal coupling) exists
    assert any(c[0] == c[1] for c in payload["cells"])


def test_empty_graph_renders_without_error(tmp_path):
    analysis = ga.analyze(_FakeDB([]))
    out = tmp_path / "empty.html"
    path = gv.render_dsm(analysis, _FakeDB([]), out_path=str(out))
    payload = _extract_payload(out.read_text(encoding="utf-8"))
    assert payload["nodes"] == []
    assert payload["cells"] == []
    assert path == str(out)
