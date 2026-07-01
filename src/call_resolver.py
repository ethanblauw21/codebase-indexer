"""call_resolver.py — baseline CALLS-edge resolution (ADR-021).

The adapters (ADR-003) emit ``CALLS`` edges whose ``target`` is the **bare callee
name** (``lowerBound``, ``enqueue``); only ``IMPORTS`` edges get a ``resolved_target``.
So the call-graph traversal (``db._CALL_GRAPH_SQL``) has nothing to resolve a bare name
against and the structural Traverse step returns only ``file_path=NULL`` nodes that the
retriever discards — the graph layer is a retrieval no-op (ADR-019 arm A ≈ arm B).

This pass fills ``resolved_target`` for the calls it can prove point at exactly one
in-repo symbol, and leaves the rest ``NULL``. It is precision-first (prefer-unknown,
ADR-011 §2 / ADR-008 §5): it never writes a target it cannot prove unique — a wrong edge
is worse than a missing one. The hard, ambiguous receiver-typed case (``recv.Method()``
where several classes define ``Method``) is out of scope here — that is ADR-011.

Resolution order for a ``CALLS`` edge ``(source_fqn, bare_name)``:
    1. unique repo-wide  — exactly one symbol named ``bare_name``.
    2. same-file         — exactly one candidate in ``source_fqn``'s own file.
    3. import-scoped     — exactly one candidate in a file the source file IMPORTS.
    4. else              — 0 candidates (external) or still ≥2 (collision) → leave NULL.

The pass recomputes **all** CALLS edges each run, so a resolution is demoted back to NULL
if a later-added symbol makes its name ambiguous — a stale resolution never outlives the
uniqueness that justified it (ADR-021 §3).
"""
from __future__ import annotations

from collections import defaultdict


def _source_file(source_fqn: str) -> str:
    """The file path embedded in an FQN (``<file>::<symbol>`` → ``<file>``)."""
    return source_fqn.split("::", 1)[0]


def resolve_call_edges(db) -> dict:
    """Populate ``resolved_target`` on CALLS edges with a provable unique target.

    Operates on the CodeDB's live connection; commits via the DB's transaction helper
    and invalidates the graph cache. Returns counts: ``resolved`` / ``ambiguous`` /
    ``external`` (0 in-repo candidates).
    """
    conn = db._conn

    # name → [(fqn, file_id)]  and  file path ↔ id
    by_name: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for fqn, name, file_id in conn.execute("SELECT fqn, name, file_id FROM symbols"):
        by_name[name].append((fqn, file_id))

    path_to_id: dict[str, int] = {}
    for fid, path in conn.execute("SELECT id, path FROM files"):
        path_to_id[path] = fid

    # importing file path → set of imported file ids (IMPORTS.resolved_target is a path)
    imported_ids: dict[str, set[int]] = defaultdict(set)
    for src, rt in conn.execute(
        "SELECT source_fqn, resolved_target FROM edges "
        "WHERE kind = 'IMPORTS' AND resolved_target IS NOT NULL"
    ):
        fid = path_to_id.get(rt)
        if fid is not None:
            imported_ids[src].add(fid)

    updates: list[tuple[str | None, int]] = []
    stats = {"resolved": 0, "ambiguous": 0, "external": 0}

    for eid, source_fqn, bare in conn.execute(
        "SELECT id, source_fqn, target FROM edges WHERE kind = 'CALLS'"
    ):
        cands = by_name.get(bare, [])
        resolved: str | None = None

        if len(cands) == 1:
            resolved = cands[0][0]
        elif len(cands) > 1:
            src_id = path_to_id.get(_source_file(source_fqn))
            same = [fqn for fqn, fid in cands if fid == src_id]
            if len(same) == 1:
                resolved = same[0]
            else:
                imp = imported_ids.get(_source_file(source_fqn), set())
                scoped = [fqn for fqn, fid in cands if fid in imp]
                if len(scoped) == 1:
                    resolved = scoped[0]

        if resolved is not None:
            stats["resolved"] += 1
        elif not cands:
            stats["external"] += 1
        else:
            stats["ambiguous"] += 1
        updates.append((resolved, eid))

    if updates:
        with db._tx() as cur:
            cur.executemany(
                "UPDATE edges SET resolved_target = ? WHERE id = ?", updates
            )
        db.invalidate_graph_cache()

    return stats
