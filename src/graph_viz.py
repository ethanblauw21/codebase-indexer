"""
graph_viz.py — Design Structure Matrix (DSM) rendering for the graph-analytics
layer (ADR-006 §3).

Renders a ``graph_analytics.GraphAnalysis`` as a single self-contained HTML file:
an N×N adjacency **matrix** (Design Structure Matrix), symbols ordered and
color-banded by community.  God-objects appear as a dense full row/column; healthy
communities appear as bright blocks on the diagonal; cross-community coupling shows
up as off-diagonal cells — the same structural story the report tells, made visible
at a glance.

Why a matrix and not a node-link graph: the DSM is a deliberately different visual
idiom from Graphify's force-directed ``graph.html`` (no visual or code resemblance),
and it is *better* suited to coupling/god-object questions than a node-link diagram.
Inspired by Graphify (Safi Shamsi, MIT) only at the level of "visualize the community
structure"; no Graphify source is used. See ADR-006.

Constraints honored (src/CLAUDE.md): one static file, no web server, **no external
CDN at runtime** — a tiny vanilla-JS canvas renderer is inlined.  Pure aside from the
single ``open(out_path, "w")``: imports only stdlib + the engine result types + the
report layer's labeling helper, never FAISS or the MCP server.

Lineage: this static DSM is the precursor to **S5** (the interactive web explorer).
S5, if built, supersedes it; this file is kept single-file and dependency-free so S5
is an additive step, not a rewrite.
"""
from __future__ import annotations

import json
import os

import graph_analytics as ga
from graph_analytics import DSM_MAX_NODES, GraphAnalysis
from graph_report import _module_of, label_community

DEFAULT_OUT_PATH = os.path.join(".code-index", "architecture_matrix.html")


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------
def _ordered_nodes(analysis: GraphAnalysis) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Community-ordered node list (matching the report: communities by size desc,
    members already sorted), the per-community ranges, and an fqn→index map.

    Each node carries its stable matrix index, short label, community ordinal, and a
    ``god`` flag for the margin marker.  The fqn→index map lets the edge pass place a
    cell at the right (row, col) and drop edges whose endpoints fall outside the
    (possibly path-scoped) analysis.
    """
    god_fqns = {g.fqn for g in analysis.god_objects}
    ordered = sorted(analysis.communities, key=lambda c: len(c.members), reverse=True)

    nodes: list[dict] = []
    comm_ranges: list[dict] = []
    index: dict[str, int] = {}
    for ci, c in enumerate(ordered):
        start = len(nodes)
        for m in c.members:
            index[m] = len(nodes)
            nodes.append(
                {
                    "fqn": m,
                    "label": m.split("::", 1)[-1],
                    "module": _module_of(m),
                    "comm": ci,
                    "god": m in god_fqns,
                }
            )
        comm_ranges.append(
            {
                "id": ci,
                "label": label_community(c.members, god_fqns),
                "start": start,
                "size": len(c.members),
                "cohesion": round(c.cohesion, 3),
                "has_god": any(m in god_fqns for m in c.members),
            }
        )
    return nodes, comm_ranges, index


def _symbol_matrix(analysis: GraphAnalysis, edges: list[tuple[str, str, str]]) -> dict:
    """Sparse symbol×symbol matrix payload for the canvas renderer.

    Edges are aggregated per ordered pair (weight summed, kinds unioned) and emitted
    sparsely as ``[row, col, weight, "KIND,KIND"]`` — a dense N×N array would be
    wasteful at the DSM cap (≤1500²).  Two position arrays (community order, file
    order) let the UI re-sort without moving the stable edge indices.
    """
    nodes, comm_ranges, index = _ordered_nodes(analysis)
    G = ga.build_graph([e for e in edges if e[0] in index and e[1] in index])

    cells: list[list] = []
    for u, v, data in G.edges(data=True):
        cells.append(
            [
                index[u],
                index[v],
                round(float(data.get("weight", 0.0)), 3),
                ",".join(sorted(data.get("kinds", set()))),
            ]
        )

    # File ordering: a permutation over stable ids, grouped by source module.
    file_order = sorted(range(len(nodes)), key=lambda i: (nodes[i]["module"], nodes[i]["fqn"]))
    file_pos = [0] * len(nodes)
    for rank, sid in enumerate(file_order):
        file_pos[sid] = rank

    return {
        "mode": "symbol",
        "nodes": nodes,
        "communities": comm_ranges,
        "cells": cells,
        "file_pos": file_pos,
    }


def _aggregated_matrix(analysis: GraphAnalysis, edges: list[tuple[str, str, str]]) -> dict:
    """Community×community matrix payload, used above ``max_nodes``.

    Each node is a whole community; cell weight is the summed edge weight between two
    communities (the diagonal is internal coupling).  Analogous to Graphify's
    >5000-node aggregation guard, sized for our typical corpora.
    """
    _, comm_ranges, _ = _ordered_nodes(analysis)
    # Map fqn -> community ordinal directly from the ordered communities.
    ordered = sorted(analysis.communities, key=lambda c: len(c.members), reverse=True)
    comm_of: dict[str, int] = {}
    for ci, c in enumerate(ordered):
        for m in c.members:
            comm_of[m] = ci

    agg: dict[tuple[int, int], float] = {}
    for u, v, kind in edges:
        cu, cv = comm_of.get(u), comm_of.get(v)
        if cu is None or cv is None:
            continue
        agg[(cu, cv)] = agg.get((cu, cv), 0.0) + ga.edge_weight(kind)

    nodes = [
        {
            "fqn": r["label"],
            "label": r["label"],
            "module": r["label"],
            "comm": r["id"],
            "god": r["has_god"],
            "size": r["size"],
        }
        for r in comm_ranges
    ]
    # In aggregated mode each "node" is its own community band; keep the original
    # member count under "members" for the legend.
    comm_ranges_agg = [
        {**r, "start": i, "size": 1, "members": r["size"]}
        for i, r in enumerate(comm_ranges)
    ]
    cells = [[i, j, round(w, 3), ""] for (i, j), w in agg.items()]
    file_pos = list(range(len(nodes)))
    return {
        "mode": "aggregated",
        "nodes": nodes,
        "communities": comm_ranges_agg,
        "cells": cells,
        "file_pos": file_pos,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_dsm(
    analysis: GraphAnalysis,
    db,
    out_path: str | None = None,
    max_nodes: int = DSM_MAX_NODES,
) -> str:
    """Render ``analysis`` as a Design Structure Matrix HTML file; return its path.

    ``db`` supplies the edges (``get_graph_edges()``); only edges whose endpoints are
    inside the analysis are drawn, so a path-scoped analysis yields a scoped matrix.
    Above ``max_nodes`` symbols the matrix aggregates to community×community with a
    banner, keeping the file small and the canvas legible.
    """
    out_path = out_path or DEFAULT_OUT_PATH
    edges = db.get_graph_edges()

    aggregated = analysis.node_count > max_nodes
    payload = (
        _aggregated_matrix(analysis, edges)
        if aggregated
        else _symbol_matrix(analysis, edges)
    )
    payload["meta"] = {
        "node_count": analysis.node_count,
        "edge_count": analysis.edge_count,
        "community_count": len(analysis.communities),
        "modularity": round(analysis.modularity, 4),
        "god_count": len(analysis.god_objects),
        "aggregated": aggregated,
        "max_nodes": max_nodes,
    }

    html = _HTML_TEMPLATE.replace("__DATA__", _safe_json(payload))

    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


def _safe_json(obj) -> str:
    """JSON for inlining inside a <script> block — neutralize any </script>."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


# The page is intentionally one self-contained file: inline CSS + vanilla-JS canvas,
# no framework, no external CDN.  __DATA__ is replaced with the matrix payload.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Architecture Matrix — Design Structure Matrix</title>
<!-- Inspired by Graphify (safishamsi/graphify, MIT) at the idea level only; this is an
     independent Design Structure Matrix, a deliberately different idiom. ADR-006 §3. -->
<style>
  :root { --bg:#0f1117; --fg:#e6e6e6; --muted:#8a93a6; --band:#1b2030; --accent:#5da9ff; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 18px; border-bottom:1px solid #232838; }
  h1 { margin:0 0 4px; font-size:16px; }
  .meta { color:var(--muted); font-size:12px; }
  .banner { margin:10px 18px 0; padding:8px 12px; border-radius:6px;
            background:#3a2a12; color:#ffd9a0; border:1px solid #6b4a18; display:none; }
  .controls { padding:10px 18px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  button { background:var(--band); color:var(--fg); border:1px solid #2c3346;
           border-radius:5px; padding:5px 10px; cursor:pointer; font:inherit; }
  button:hover { border-color:var(--accent); }
  .wrap { display:flex; gap:16px; padding:0 18px 18px; align-items:flex-start; }
  #stage { position:relative; overflow:auto; max-height:78vh; border:1px solid #232838;
           border-radius:6px; }
  canvas { display:block; }
  #tip { position:fixed; pointer-events:none; background:#11151f; color:var(--fg);
         border:1px solid #2c3346; border-radius:5px; padding:6px 8px; font-size:12px;
         max-width:380px; display:none; z-index:10; white-space:nowrap; }
  aside { width:300px; flex:0 0 300px; max-height:78vh; overflow:auto; }
  .legend-item { display:flex; gap:8px; align-items:flex-start; padding:4px 6px;
                 border-radius:5px; cursor:pointer; }
  .legend-item:hover { background:var(--band); }
  .legend-item.dim { opacity:.4; }
  .swatch { width:12px; height:12px; border-radius:3px; margin-top:3px; flex:0 0 12px; }
  .legend-label { word-break:break-word; }
  .legend-sub { color:var(--muted); font-size:11px; }
  .god { color:#ff8f6b; }
</style>
</head>
<body>
<header>
  <h1>Architecture Matrix <span class="meta">— Design Structure Matrix (ADR-006)</span></h1>
  <div class="meta" id="summary"></div>
</header>
<div class="banner" id="banner"></div>
<div class="controls">
  <button id="toggleOrder">Order: by community</button>
  <button id="clearFocus">Clear focus</button>
  <span class="meta">Hover a cell for <code>source → target (kind)</code>. Click a community to focus it.</span>
</div>
<div class="wrap">
  <div id="stage"><canvas id="m"></canvas></div>
  <aside id="legend"></aside>
</div>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const N = DATA.nodes.length;
const M = DATA.meta;

// --- ordering: stable id -> drawn position ---
const commPos = new Array(N);          // community order = identity (nodes already in it)
for (let i=0;i<N;i++) commPos[i]=i;
const filePos = DATA.file_pos.slice();
let useFile = false;
let focusComm = null;

function pos(id){ return useFile ? filePos[id] : commPos[id]; }
function idAt(p){ // inverse of pos for current ordering
  const arr = useFile ? filePos : commPos;
  for (let i=0;i<N;i++) if (arr[i]===p) return i;
  return -1;
}

// community color via golden-angle hue
function commColor(ci, a){ const h=(ci*137.508)%360; return `hsla(${h},58%,58%,${a==null?1:a})`; }

const canvas = document.getElementById('m');
const ctx = canvas.getContext('2d');
const stage = document.getElementById('stage');
const tip = document.getElementById('tip');

const MARGIN = 8;
let cell = 14;
function computeCell(){
  const avail = Math.min(window.innerWidth - 360, 1100);
  cell = Math.max(2, Math.min(18, Math.floor(avail / Math.max(N,1))));
}

let maxW = 0;
for (const c of DATA.cells) if (c[2] > maxW) maxW = c[2];
if (maxW <= 0) maxW = 1;

function draw(){
  computeCell();
  const dim = MARGIN + N*cell;
  canvas.width = dim; canvas.height = dim;
  ctx.clearRect(0,0,dim,dim);

  // community band backgrounds along the diagonal
  for (const r of DATA.communities){
    const p0 = useFile ? null : r.start;
    if (!useFile){
      const x = MARGIN + r.start*cell, w = r.size*cell;
      ctx.fillStyle = commColor(r.id, 0.10);
      ctx.fillRect(x, MARGIN, w, N*cell);   // column band
      ctx.fillRect(MARGIN, x, N*cell, w);   // row band
      ctx.strokeStyle = commColor(r.id, focusComm===r.id?0.95:0.55);
      ctx.lineWidth = focusComm===r.id?2:1;
      ctx.strokeRect(x, x, w, w);           // diagonal block outline
    }
  }

  // cells
  for (const c of DATA.cells){
    const r = pos(c[0]), col = pos(c[1]);
    if (focusComm!==null){
      const cr = DATA.nodes[c[0]].comm, cc = DATA.nodes[c[1]].comm;
      if (cr!==focusComm && cc!==focusComm) continue;
    }
    const a = 0.25 + 0.75*(c[2]/maxW);
    ctx.fillStyle = `rgba(120,190,255,${a})`;
    ctx.fillRect(MARGIN+col*cell, MARGIN+r*cell, cell-0.5, cell-0.5);
  }

  // god-object margin markers (top + left)
  ctx.fillStyle = '#ff8f6b';
  for (let id=0; id<N; id++){
    if (!DATA.nodes[id].god) continue;
    const p = pos(id);
    ctx.beginPath(); ctx.moveTo(MARGIN+p*cell, 0); ctx.lineTo(MARGIN+p*cell+cell, 0);
    ctx.lineTo(MARGIN+p*cell+cell/2, MARGIN-1); ctx.fill();
    ctx.beginPath(); ctx.moveTo(0, MARGIN+p*cell); ctx.lineTo(0, MARGIN+p*cell+cell);
    ctx.lineTo(MARGIN-1, MARGIN+p*cell+cell/2); ctx.fill();
  }
}

// hover tooltip
const cellIndex = new Map();
function rebuildHover(){
  cellIndex.clear();
  for (const c of DATA.cells){
    cellIndex.set(pos(c[0])+'x'+pos(c[1]), c);
  }
}
canvas.addEventListener('mousemove', (e)=>{
  const rect = canvas.getBoundingClientRect();
  const col = Math.floor((e.clientX-rect.left-MARGIN)/cell);
  const row = Math.floor((e.clientY-rect.top-MARGIN)/cell);
  const hit = cellIndex.get(row+'x'+col);
  if (hit){
    const s = DATA.nodes[hit[0]], t = DATA.nodes[hit[1]];
    const kinds = hit[3] ? ' ('+hit[3]+')' : '';
    tip.innerHTML = `<b>${esc(s.label)}</b> → <b>${esc(t.label)}</b>${esc(kinds)}<br>`+
                    `<span style="color:#8a93a6">weight ${hit[2]}</span>`;
    tip.style.display='block'; tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+14)+'px';
  } else { tip.style.display='none'; }
});
canvas.addEventListener('mouseleave', ()=> tip.style.display='none');

function esc(s){ return String(s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

// legend
function buildLegend(){
  const el = document.getElementById('legend');
  el.innerHTML='';
  for (const r of DATA.communities){
    const div = document.createElement('div');
    div.className='legend-item'; div.dataset.comm=r.id;
    const extra = DATA.meta.aggregated ? ` · ${r.members} symbols` : ` · ${r.size} symbols`;
    div.innerHTML = `<span class="swatch" style="background:${commColor(r.id)}"></span>`+
      `<span class="legend-label">${esc(r.label)}`+
      (r.has_god?` <span class="god">◤god</span>`:``)+
      `<div class="legend-sub">cohesion ${r.cohesion}${extra}</div></span>`;
    div.onclick = ()=>{ focusComm = (focusComm===r.id)?null:r.id; syncFocusUI(); draw(); };
    el.appendChild(div);
  }
}
function syncFocusUI(){
  for (const it of document.querySelectorAll('.legend-item'))
    it.classList.toggle('dim', focusComm!==null && +it.dataset.comm!==focusComm);
}

document.getElementById('toggleOrder').onclick = (e)=>{
  useFile = !useFile;
  e.target.textContent = 'Order: by ' + (useFile?'file':'community');
  rebuildHover(); draw();
};
document.getElementById('clearFocus').onclick = ()=>{ focusComm=null; syncFocusUI(); draw(); };

document.getElementById('summary').textContent =
  `${M.node_count} symbols · ${M.edge_count} edges · ${M.community_count} communities · `+
  `modularity ${M.modularity} · ${M.god_count} god-object${M.god_count===1?'':'s'}`;

if (M.aggregated){
  const b = document.getElementById('banner');
  b.style.display='block';
  b.textContent = `Large graph (${M.node_count} > ${M.max_nodes} symbols): showing the `+
    `community × community aggregate. Each row is a whole community; the diagonal is internal coupling.`;
}

buildLegend(); rebuildHover(); draw();
window.addEventListener('resize', ()=>{ rebuildHover(); draw(); });
</script>
</body>
</html>
"""
