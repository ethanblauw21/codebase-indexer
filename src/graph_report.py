"""
graph_report.py — markdown rendering for the graph-analytics layer (ADR-006 §2).

Turns a ``graph_analytics.GraphAnalysis`` into the AI-facing markdown report that
``map_module_communities`` returns.  Kept separate from both the engine
(``graph_analytics`` stays UI-agnostic) and the MCP server (``MCPServer`` pulls in
FAISS) so the rendering is pure and unit-testable on its own: this module imports
nothing but the standard library and the engine's result types.

Community **labels** are filled here, not in the engine — heuristically, from the
member file/symbol names (no LLM, offline, deterministic).  Honesty rules from the
ADR are enforced in the output: raw cohesion/modularity numbers are never collapsed
to a symbol, the header states the report is exploratory and not an accuracy claim,
split suggestions repeat their per-suggestion caveat, and an audit note records that
only EXTRACTED edges were analysed.

Attribution: the *idea* of community-detecting a code graph and surfacing
god-objects is inspired by Graphify (Safi Shamsi, MIT); see ADR-006.  No Graphify
source is used.
"""
from __future__ import annotations

import os
from collections import Counter

from graph_analytics import GOD_MIN_COMMUNITIES, GraphAnalysis

# Stated once at the top of every report (decision: ship exploratory, bar → ADR-008).
EXPLORATORY_DISCLAIMER = (
    "> ⚠️ **Exploratory structural view — not an accuracy claim.** This maps the "
    "*shape* of the EXTRACTED code graph (community structure + centrality) to aid "
    "navigation and refactor planning. Findings are heuristic, not verified. A measured "
    "quality bar for this output is deferred to ADR-008 (Measured Conformance)."
)

# Provenance honesty — our answer to Graphify's EXTRACTED/INFERRED tagging.
AUDIT_NOTE = (
    "Analysis used **only EXTRACTED edges** — real parser-emitted relationships "
    "(`CALLS`, `IMPORTS`, `EXTENDS`, `IMPLEMENTS`, `INSTANTIATES`, `OWNS`). No INFERRED "
    "or LLM-derived edges participated. Community labels are heuristic, derived from the "
    "file/symbol names of each community's members; treat them as navigation aids, not "
    "ground truth."
)


# ---------------------------------------------------------------------------
# Heuristic labeling — derived from member names only (no centrality, no LLM)
# ---------------------------------------------------------------------------
def _module_of(fqn: str) -> str:
    """Basename (no extension) of the file segment of an FQN.

    FQNs are ``file::scope`` (e.g. ``src/db.py::CodeDB.get_call_graph``); a bare
    target with no ``::`` is returned as-is.
    """
    head = fqn.split("::", 1)[0]
    base = os.path.basename(head.replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    return stem or base or fqn


def _short_name(fqn: str) -> str:
    """Last scope segment of an FQN, for naming a representative member."""
    tail = fqn.split("::")[-1]
    return tail.split(".")[-1] or tail


def label_community(members: list[str], god_fqns: set[str]) -> str:
    """A short heuristic label for a community.

    Built from the dominant source module among the members, annotated with how
    many other modules the community reaches into, and — when a flagged
    god-object is itself a member — the central symbol's name.  Deterministic and
    offline; intentionally a navigation aid, never a ground-truth claim.
    """
    if not members:
        return "(empty)"
    counter = Counter(_module_of(m) for m in members)
    top_mod, _ = counter.most_common(1)[0]
    extra = len(counter) - 1

    label = f"`{top_mod}`"
    if extra > 0:
        label += f" (+{extra} module{'s' if extra > 1 else ''})"

    central = [m for m in members if m in god_fqns]
    if central:
        label += f" — central: `{_short_name(central[0])}`"
    return label


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_report(
    analysis: GraphAnalysis,
    target_path: str = "",
    min_community_size: int = 3,
    max_members_shown: int = 8,
) -> str:
    """Render a ``GraphAnalysis`` as the AI-facing markdown report (ADR-006 §2).

    ``min_community_size`` filters which communities are *displayed* (all are still
    counted in the summary); singletons and file-mirror fragments are noise in the
    body but matter for the totals.  ``max_members_shown`` caps the member preview
    per community so large modules don't blow the context budget.
    """
    god_fqns = {g.fqn for g in analysis.god_objects}
    scope = f" — scoped to `{target_path}`" if target_path else ""
    lines: list[str] = [
        f"# Module Community Map{scope}",
        "",
        EXPLORATORY_DISCLAIMER,
        "",
        "---",
        "",
    ]

    if analysis.node_count == 0:
        lines += [
            "## Summary",
            "",
            "The code graph is **empty** — no symbols or edges were found"
            f"{' under this path' if target_path else ''}.",
            "",
            "Run `reindex` to (re)build the index, then call this tool again.",
            "",
        ]
        return "\n".join(lines)

    # --- Summary ---
    lines += [
        "## Summary",
        "",
        f"- **Symbols (nodes):** {analysis.node_count}",
        f"- **Edges:** {analysis.edge_count}",
        f"- **Communities:** {len(analysis.communities)}",
        f"- **Modularity:** {analysis.modularity:.4f}  "
        "*(raw Newman modularity; higher = cleaner cluster separation)*",
        f"- **God-objects flagged:** {len(analysis.god_objects)}",
        "",
    ]

    # --- Module Communities ---
    shown = sorted(
        (c for c in analysis.communities if len(c.members) >= min_community_size),
        key=lambda c: len(c.members),
        reverse=True,
    )
    hidden = len(analysis.communities) - len(shown)

    lines += ["## Module Communities", ""]
    if not shown:
        lines += [
            f"_No community has ≥ {min_community_size} members; "
            f"all {len(analysis.communities)} are smaller. "
            "Lower `min_community_size` to inspect them._",
            "",
        ]
    for c in shown:
        label = label_community(c.members, god_fqns)
        lines.append(
            f"### Community {c.id}: {label} — {len(c.members)} symbols, "
            f"cohesion {c.cohesion:.3f}"
        )
        preview = c.members[:max_members_shown]
        for m in preview:
            marker = " **[god-object]**" if m in god_fqns else ""
            lines.append(f"- `{m}`{marker}")
        if len(c.members) > max_members_shown:
            lines.append(f"- … and {len(c.members) - max_members_shown} more")
        lines.append("")
    if hidden > 0:
        lines += [
            f"_{hidden} smaller communit{'y' if hidden == 1 else 'ies'} "
            f"(< {min_community_size} members) omitted from this view._",
            "",
        ]

    # --- God-Objects ---
    lines += ["## God-Objects", ""]
    if analysis.god_objects:
        lines += [
            "High-coupling chokepoints whose owned members sprawl across many "
            "*coupling* communities (span computed on a separate coupling-only view "
            "so an `OWNS`-clustered class cannot self-mask). Ranked by composite score "
            "`0.5·betweenness + 0.3·fan-in + 0.2·span`.",
            "",
            "| Symbol | Betweenness | Fan-in | Fan-out | Communities spanned | Score |",
            "| ------ | ----------: | -----: | ------: | ------------------: | ----: |",
        ]
        for g in analysis.god_objects:
            lines.append(
                f"| `{g.fqn}` | {g.betweenness:.4f} | {g.fan_in} | {g.fan_out} "
                f"| {g.communities_spanned} | {g.score:.4f} |"
            )
        lines.append("")
    else:
        lines += [
            "_None detected._ A god-object requires **coupling** edges "
            "(`CALLS`/`IMPORTS`/`EXTENDS`/`IMPLEMENTS`/`INSTANTIATES`) whose owned "
            f"members span ≥ {GOD_MIN_COMMUNITIES} coupling communities. If the "
            "index was built before call/import extraction landed (only `OWNS` edges "
            "present), rebuild with `reindex` before trusting this section.",
            "",
        ]

    # --- Suggested Splits (only when requested AND produced) ---
    if analysis.splits:
        lines += [
            "## Suggested Splits",
            "",
            "> **[HEURISTIC — unverified]** The decompositions below are derived from "
            "Louvain clustering of EXTRACTED edges. They are starting points, not "
            "recommendations — validate against the actual call graph before acting.",
            "",
        ]
        for s in analysis.splits:
            lines.append(f"### Split candidate: `{s.fqn}`")
            lines.append("")
            lines.append(s.rationale)
            lines.append("")
            for name, group in s.proposed_modules:
                lines.append(f"- **{name}** ({len(group)} members)")
                for m in group:
                    lines.append(f"  - `{m}`")
            lines.append("")
            lines.append(f"> _{s.caveat}_")
            lines.append("")

    # --- Audit note ---
    lines += ["---", "", "## Audit", "", AUDIT_NOTE, ""]
    return "\n".join(lines)
