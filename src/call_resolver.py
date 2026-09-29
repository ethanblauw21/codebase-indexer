"""call_resolver.py — CALLS-edge resolution (ADR-021 baseline + ADR-011 receiver types).

The adapters (ADR-003) emit ``CALLS`` edges whose ``target`` is the **bare callee name**
(``lowerBound``, ``enqueue``); only ``IMPORTS`` edges get a ``resolved_target``. So the
call-graph traversal (``db._CALL_GRAPH_SQL``) has nothing to resolve a bare name against and
the structural Traverse step returns only ``file_path=NULL`` nodes the retriever discards —
the graph layer is a retrieval no-op (ADR-019 arm A ≈ arm B).

This pass fills ``resolved_target`` for the calls it can prove point at exactly one in-repo
symbol, and leaves the rest ``NULL``. It is precision-first (prefer-unknown, ADR-011 §2 /
ADR-008 §5): it never writes a target it cannot prove unique — a wrong edge is worse than a
missing one.

Two resolution regimes, by whether the adapter attached a receiver-type hint (ADR-011):

**Receiver-typed** — the edge carries ``receiver_type`` (``recv.Method()`` where the pass
inferred ``recv``'s type). This is the hard case ADR-021 could not settle: several classes
may define ``Method``, and only the receiver's type says which. Resolution is restricted to
candidates whose **owning type's name matches the hint**; a unique match resolves with a
graded ``confidence`` (§3). If the hint matches zero or several candidates, the edge stays
**unresolved** — it does *not* fall back to the positional strategies below, because a known
receiver type is stronger evidence than "happens to be in an imported file", and guessing
past it would risk a wrong edge (§2, prefer-unknown).

**Bare** — no hint (``Foo()``, or a receiver the pass could not type). The ADR-021 order:
    1. unique repo-wide  — exactly one symbol named ``bare_name``.
    2. same-file         — exactly one candidate in ``source_fqn``'s own file.
    3. import-scoped     — exactly one candidate in a file the source file IMPORTS.
    4. else              — 0 candidates (external) or still ≥2 (collision) → leave NULL.

**Bound** (ADR-044) — the adapter recorded that the callee, or its receiver, is bound to one
of the file's imports (``bound_module``). That import decides the scope, before the order above:
    - a dependency (``external`` = 1)  → external, unresolved, whatever the repo defines.
    - in-repo, resolved to a file      → exactly one candidate in that file. When the file has
      none (a barrel that only re-exports), the unique repo-wide rule alone may still apply.
    - in-repo but unresolved           → the unique repo-wide rule alone.
    - unknown                          → unresolved (prefer-unknown).
An unbound member call (``member_call`` = 1, ``self.x()`` / ``store.get()``) skips the
import-scoped step: nothing says the method lives in an imported file. Edges from before
ADR-044 (``member_call`` NULL, and every edge of an adapter that does not compute it) resolve
as before, except that a ``.py`` source skips import-scoping, which it never reached before.

The pass recomputes **all** CALLS edges each run, so a resolution is demoted back to NULL if
a later-added symbol makes its name ambiguous — a stale resolution never outlives the
uniqueness that justified it (ADR-021 §3).
"""
from __future__ import annotations

from collections import defaultdict

from db import EDGE_CONFIDENCE_FLOOR

# ADR-011 §3: the confidence a receiver-typed resolution carries. Below 1.0 (the fully
# unique-name-resolved grade) because local type inference has a small residual risk —
# reassignment or shadowing the exact strategies don't fully track — but comfortably above
# the verdict floor, so a typed resolution reads as verified. One exact-strategy grade ships
# in Stage 1; heuristic member-chain strategies add lower grades in a follow-up.
_TYPED_CONFIDENCE = 0.9
assert _TYPED_CONFIDENCE >= EDGE_CONFIDENCE_FLOOR, "typed resolution must clear the floor"


def _source_file(source_fqn: str) -> str:
    """The file path embedded in an FQN (``<file>::<symbol>`` → ``<file>``)."""
    return source_fqn.split("::", 1)[0]


def resolve_call_edges(db) -> dict:
    """Populate ``resolved_target`` (and, for typed hits, ``confidence``) on CALLS edges.

    Operates on the CodeDB's live connection; commits via the DB's transaction helper and
    invalidates the graph cache. Returns counts: ``resolved`` (bare, unique) / ``typed``
    (receiver-type disambiguated) / ``ambiguous`` / ``external`` (0 in-repo candidates).
    """
    conn = db._conn

    # name → [(fqn, file_id)];  fqn → symbol name;  file path ↔ id
    by_name: dict[str, list[tuple[str, int]]] = defaultdict(list)
    fqn_to_name: dict[str, str] = {}
    for fqn, name, file_id in conn.execute("SELECT fqn, name, file_id FROM symbols"):
        by_name[name].append((fqn, file_id))
        fqn_to_name[fqn] = name

    path_to_id: dict[str, int] = {}
    for fid, path in conn.execute("SELECT id, path FROM files"):
        path_to_id[path] = fid

    # member fqn → owning type fqn (ADR-011 receiver-type match runs over OWNS, not FQN
    # string-parsing, so it stays language-neutral).
    owner_of: dict[str, str] = {}
    for type_fqn, member_fqn in conn.execute(
        "SELECT source_fqn, target FROM edges WHERE kind = 'OWNS'"
    ):
        owner_of[member_fqn] = type_fqn

    # importing file path → set of imported file ids (IMPORTS.resolved_target is a path), and
    # (importing file, specifier) → (imported file id, external) for bound calls (ADR-044)
    imported_ids: dict[str, set[int]] = defaultdict(set)
    import_of: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for src, target, rt, external in conn.execute(
        "SELECT source_fqn, target, resolved_target, external FROM edges WHERE kind = 'IMPORTS'"
    ):
        fid = path_to_id.get(rt) if rt is not None else None
        if fid is not None:
            imported_ids[src].add(fid)
        import_of[(src, target)] = (fid, external)

    updates: list[tuple[str | None, float | None, int]] = []
    stats = {"resolved": 0, "typed": 0, "ambiguous": 0, "external": 0,
             "bound_scoped": 0, "bound_external": 0}

    for eid, source_fqn, bare, receiver_type, confidence, bound, member in conn.execute(
        "SELECT id, source_fqn, target, receiver_type, confidence, bound_module, member_call "
        "FROM edges WHERE kind = 'CALLS'"
    ):
        cands = by_name.get(bare, [])
        resolved: str | None = None
        new_conf: float | None = confidence   # preserve any pre-set confidence by default
        src_file = _source_file(source_fqn)

        if receiver_type:
            # ADR-011: restrict to candidates owned by a type whose name matches the hint.
            typed = [
                fqn for fqn, _ in cands
                if fqn_to_name.get(owner_of.get(fqn, "")) == receiver_type
            ]
            if len(typed) == 1:
                resolved = typed[0]
                new_conf = _TYPED_CONFIDENCE
                stats["typed"] += 1
            elif not cands:
                stats["external"] += 1
            else:
                # hint matched zero or several: unknown, and NO positional fallback.
                stats["ambiguous"] += 1
        elif bound is not None:
            # ADR-044 §3: the import the call goes through decides the scope.
            fid, external = import_of.get((src_file, bound), (None, None))
            in_file = [fqn for fqn, cid in cands if fid is not None and cid == fid]
            if external == 1:
                stats["bound_external"] += 1
            elif external == 0 and len(in_file) == 1:
                resolved = in_file[0]
                stats["bound_scoped"] += 1
            elif external == 0 and not in_file and len(cands) == 1:
                resolved = cands[0][0]        # a barrel, or an import that did not resolve
                stats["resolved"] += 1
            elif not cands:
                stats["external"] += 1
            else:
                stats["ambiguous"] += 1
        elif len(cands) == 1:
            resolved = cands[0][0]
            stats["resolved"] += 1
        elif len(cands) > 1:
            src_id = path_to_id.get(src_file)
            same = [fqn for fqn, fid in cands if fid == src_id]
            if len(same) == 1:
                resolved = same[0]
            elif member == 0 or (member is None and not src_file.endswith(".py")):
                # Import-scoping is sound only for a bare call (ADR-044 §3).
                imp = imported_ids.get(src_file, set())
                scoped = [fqn for fqn, fid in cands if fid in imp]
                if len(scoped) == 1:
                    resolved = scoped[0]
            if resolved is not None:
                stats["resolved"] += 1
            else:
                stats["ambiguous"] += 1
        else:
            stats["external"] += 1

        updates.append((resolved, new_conf, eid))

    if updates:
        with db._tx() as cur:
            cur.executemany(
                "UPDATE edges SET resolved_target = ?, confidence = ? WHERE id = ?",
                updates,
            )
        db.invalidate_graph_cache()

    return stats
