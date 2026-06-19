# ADR-015: Local Graph & Retrieval Explorer (UI) — Making the Structural Output Human-Legible

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-015-local-graph-retrieval-explorer`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-006 (graph-analytics) — needs the **community partition + centrality output** to render the community-map view. The static DSM (ADR-006 §3) is the precursor this UI supersedes; ADR-006 deliberately kept the DSM single-file and dependency-free so this is an additive step, not a rewrite.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-015 bucket + build kit), suggestions S5.
> This is a UX deliverable, **not research**.

## Context

The engine's structural output is currently legible only to agents (MCP tools) and, partially, to a TUI.
There is no way for a *human* to **see** the codebase the way the engine models it: the community map
(ADR-006), the blast radius of a symbol, a call path, or how a retrieval query actually scores. The
structural richness exists; it just has no human-facing window.

The hard constraint is architectural and explicit in `src/CLAUDE.md`: **no web server, no build pipeline**
(and, per Mantra 1, **no runtime CDN** — any JS is vendored locally, never `<script src="https://…">`). The
engine is a local, offline tool; bolting on a Node/Vite/Next dev server would betray that and add a
long-lived process to babysit. So the explorer is a **static bundle over the index**. Crucially, a static
browser bundle *cannot* open `graph.db` (SQLite) or a FAISS file directly — the proven pattern is the one
ADR-006's DSM already uses: **Python pre-exports the data and embeds it as inline JSON in a self-contained
HTML** (`src/graph_viz.py`: `_HTML_TEMPLATE.replace("__DATA__", _safe_json(payload))`). So the views render
*pre-exported* data, with at most the stdlib `http.server` as an optional convenience, never a framework or
a build step.

ADR-006's static DSM (`architecture_matrix.html`) is the precursor: a single self-contained HTML file
rendering the community structure. This ADR generalizes that into an interactive explorer. It is Wave 3 UX
work — valuable but not on the research critical path, and it depends only on ADR-006's community output.

## Decision

Ship a **zero-config local web explorer** — a static HTML/JS bundle over the index, no framework and no
build pipeline — with three views: a **community map** (ADR-006), an interactive **blast-radius / call-path**
explorer, and an **RTR retrieval playground**. Honor the "no web server / no build pipeline" constraint by
shipping a static bundle that reads the index directly.

### §1 — Static bundle, no framework (S5)

A new `src/web/` static bundle: hand-written HTML + vanilla JS (no React/Vite/Next, no bundler, **no CDN —
libs vendored locally**), rendering **data pre-exported from `graph.db` and FAISS** (the `graph_viz.py`
embed-as-JSON pattern), *not* live reads of those files. Reuse ADR-006's DSM renderer (`src/graph_viz.py`)
as one component. Optional stdlib `http.server` for local serving; nothing long-lived or framework-bound.
This honors the `src/CLAUDE.md` constraint directly and avoids the orphaned-dev-server class of problem
entirely.

### §2 — Community map view

Render ADR-006's community partition + centrality as an interactive map: communities as regions, god-objects
flagged, click-through to members. This is the DSM's successor — same data, richer interaction. ADR-006
designed the DSM to be superseded additively, so this view consumes ADR-006's `GraphAnalysis` output rather
than recomputing anything.

### §3 — Interactive blast-radius / call-path

Pick a symbol, see its blast radius (callers/callees, transitive) and call paths interactively — the visual
counterpart to the `analyze_blast_radius` MCP tool. Reads the existing edge graph; computes traversals
client-side or via a thin local endpoint.

### §4 — RTR retrieval playground

A query box that runs the retrieval pipeline and **shows how results scored** — dense vs sparse vs reranker
contributions (ADR-009's fusion made visible), so a human can see *why* a result ranked where it did. Unlike
the graph views (which render pre-exported data), the playground needs **live retrieval over arbitrary
queries** — which is why FAISS-in-browser is the one genuinely hard part. The Open Question is **exposing
FAISS query without a long-running server**: pre-computing for a fixed query set (fully honors the
constraint), a short-lived stdlib `http.server` query endpoint (a documented compromise — it *is* a brief
server), or a WASM FAISS shim (immature) — resolved at implementation.

## Consequences

**Better:**
- The engine's structural output becomes **human-legible** for the first time beyond a TUI — community map,
  blast radius, and retrieval scoring all visible.
- Honors the no-server/no-build constraint, so it adds zero long-lived processes and no framework
  maintenance — a static bundle, not an app.
- Additive over ADR-006: the DSM was designed to be superseded this way, so the community view is reuse, not
  a rewrite.

**Worse:**
- Running FAISS retrieval from a browser without a server is genuinely awkward (the central Open Question);
  each option (pre-compute / short-lived endpoint / WASM) has trade-offs.
- Hand-written vanilla JS for interactive graph views is more effort than reaching for a framework — the
  cost of honoring the no-build constraint.
- A UI is ongoing surface to keep in sync with schema/pipeline changes, with no conformance harness to catch
  drift (it's UX, not measured structure).

**Neutral:**
- Read-only over the index — never mutates `graph.db` or FAISS, so no migration risk.
- Effort M–H; Wave 3, off the research critical path. Nothing depends on it.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| React/Vite/Next SPA with a dev server | Violates the explicit no-web-server / no-build-pipeline constraint (`src/CLAUDE.md`); adds a long-lived process and a build step the project deliberately avoids. |
| Extend the TUI instead of a web view | The TUI can't render a community map / interactive graph legibly; the visual structural views need a 2-D canvas. |
| Force-directed node-link graph (Graphify-style) | ADR-006 already chose DSM over node-link deliberately for coupling analysis; the explorer builds on the DSM lineage rather than reintroducing the rejected idiom. |
| Long-running local server backing the FAISS playground | Reintroduces the server the constraint forbids; prefer pre-compute / short-lived endpoint / WASM, decided at implementation. |
| Ship only the community map, drop the retrieval playground | The playground (seeing *why* a result scored) is much of the human value; keep it, resolve the FAISS-in-browser question instead of cutting it. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] `src/web/` static bundle (vanilla HTML/JS, no framework/build); optional stdlib `http.server`.
- [ ] Community-map view consuming ADR-006 `GraphAnalysis`; reuse the DSM renderer.
- [ ] Interactive blast-radius / call-path view over the edge graph.
- [ ] RTR retrieval playground showing dense/sparse/reranker score contributions (ADR-009 fusion).
- [ ] Resolve FAISS-in-browser without a long-running server (pre-compute / short-lived endpoint / WASM).

**Notes:**
<!-- 2026-06-18: Wave 3 UX, not research. Default: static bundle, no framework (honors the no-web-server/no-build constraint in src/CLAUDE.md). Successor to ADR-006's static DSM. Static bundle renders PRE-EXPORTED embedded JSON (the graph_viz.py __DATA__ pattern), NOT live graph.db/FAISS reads; no CDN (Mantra 1). Done when the local explorer renders community map + blast-radius + an RTR retrieval playground. Open: only LIVE FAISS retrieval (the playground) needs solving without a long-running server. Effort M–H. -->
