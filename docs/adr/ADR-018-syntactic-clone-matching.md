# ADR-018: Syntactic Clone Matching — Provable Structural Duplication Beside Fuzzy Similarity

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-018-syntactic-clone-matching`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — the tree-sitter parsing infrastructure (`adapters/_treesitter.py`, `Node`/`Query`) and the `LanguageAdapter` parse path that produces the ASTs this matcher serializes. No AST, no structural match.
- ADR-017 — defines which languages have a tree-sitter parse (Tier A fitting + Tier B generic) vs. Tier C text; this ADR's structural coefficient is available exactly for the tree-sitter-parsed tiers.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (Kit 10) and
> [references-code-intelligence.md](../references-code-intelligence.md) ([AST clone detection / suffix-tree
> matching prior art]). Net-new ADR (Rule B): the next free integer after the existing 003–017 tree.

## Context

`find_similar_code` (`src/MCPServer.py:135`) today is **embedding-similarity only**: it embeds the snippet,
pulls the tier-1 FAISS top-15, and stratifies the hits into Origin / Callers / Parallels / Weak using cosine
score plus keyword and read/write operational-profile heuristics. It is good at "what code is *about* the
same thing," and it has a real ceiling: **cosine similarity conflates *semantically related* with
*structurally duplicated*.** Two functions that do completely different things over the same domain nouns
score high; a genuine copy-paste clone whose variables were renamed may score only moderately. For the one
job that most wants an exact answer — *refactoring* ("where is this block duplicated so I can extract it?") —
a fuzzy score is the wrong instrument.

The duplication that refactoring cares about is the **Type-2 clone**: identical structure, with identifiers,
literals, and comments changed. That is precisely the signal an AST carries and an embedding blurs. And it is
the kind of result the depth-over-breadth moat is built for: **structural identity is *provable*** (the ASTs
match or they don't), where embedding similarity is only ever suggestive. A competitor leaning on vector
similarity alone cannot make this claim.

The enabling machinery already exists: every Tier-A and Tier-B file is parsed with tree-sitter
(`adapters/_treesitter.py`), so the ASTs are obtainable on demand. What is missing is a matcher that turns
those ASTs into an exact duplication coefficient and reports it *beside* the cosine score, so a human (or
agent) gets both "looks related" and "is provably the same structure."

## Decision

Add an **AST structural-clone matcher** that runs as a **precise refinement over `find_similar_code`'s dense
candidate set**, emitting a **structural identity coefficient ∈ [0.0, 1.0]** alongside the existing cosine
score. The matcher detects Type-2 clones by comparing **normalized AST node-type sequences** via a
**suffix-tree** longest-common-substructure search.

### §1 — Normalized AST serialization (what makes it a *clone* matcher, not a text matcher)

Each candidate's AST is serialized to a **pre-order sequence of node-*type* tokens**, with identifier and
literal nodes collapsed to their type (`identifier`, `string_literal`, `number`) rather than their text.
Comments and whitespace are dropped (they are not AST structure). The consequence is the defining property:
**renaming every variable, changing every literal, and stripping every comment leaves the serialization
identical** — so the matcher sees a copy-paste-with-rename as the clone it is, which neither the embedding
nor a raw-text diff reliably does.

### §2 — Suffix-tree longest-common-substructure (the matching core)

Given the query's serialized sequence and a candidate's, build a generalized **suffix tree** and extract the
**longest common contiguous substructure**. Suffix-tree construction is **linear** in sequence length
(Ukkonen), so each pairwise comparison is `O(n)` in the serialized AST size — cheap enough to run per
candidate. The **structural identity coefficient** is the matched substructure size normalized against the
query's size:

```
coefficient = |largest common normalized-AST substructure| / |query normalized-AST|
            # 1.0 ⇒ the query's entire structure recurs in the candidate (exact Type-2 clone)
            # 0.0 ⇒ no shared substructure
```

This is an exact, reproducible number, not a learned estimate.

### §3 — Bounded cost: refine the candidates, never scan the world

The matcher does **not** do a global `O(files²)` all-pairs clone scan. It runs only over the **bounded FAISS
candidate set** `find_similar_code` already retrieves (top-15) — dense retrieval is the cheap pre-filter that
finds *plausible* clones, and the structural pass is the exact verifier that *proves* which of them are. This
is the same "cheap traversal pre-filters the expensive exact pass" discipline as Mantra 3 (graph-pre-filtered
reranking), applied to clone detection. ASTs are re-parsed on demand for the query snippet and the ≤15
candidate chunks (their text lives in the chunk store / `DocumentStore`); no AST is persisted.

### §4 — Output: the coefficient sits beside cosine, it does not replace it

`find_similar_code` returns both numbers per hit: the **cosine** score (semantic relatedness) and the
**structural coefficient** (provable duplication). The two answer different questions and are most useful
together — a high cosine + high structural coefficient is a true clone; high cosine + low structural is
"related but not duplicated"; low cosine + high structural is a renamed copy the embedding missed. The
stratification (Origin / Parallels / Weak) gains a structural lane.

### §5 — Coverage & limits (current state)

Stating the boundary is part of "correctness over breadth":
- **Tree-sitter tiers only.** The coefficient is available for **Tier-A and Tier-B** files (they have a
  tree-sitter parse, per ADR-017); **Tier-C** text-fallback files have no AST, so they report cosine only —
  honestly labeled, never faked.
- **Intra-grammar only.** Structural identity is per-grammar: a Python re-implementation of a TypeScript
  function has a *different* AST and is not a structural clone. Cross-language duplication remains the
  embedding's job, not this matcher's.
- **Type-2, not Type-3.** Exact-structure-with-renames is detected; *gapped* clones (inserted/removed
  statements — Type-3) are not matched by the contiguous-substructure core. A future approximate pass could
  extend to Type-3; it is explicitly out of scope here to keep the coefficient exact.

### §6 — Mantra 4 / schema

Read-only analysis: the matcher never mutates `graph.db`, FAISS, or `stable_id`. The only surface change is
an **additive output field** on the `find_similar_code` result. If clone results are later cached, that is an
additive table — `stable_id` and the existing schema are untouched.

## Consequences

**Better:**
- Refactoring gets a **provable** duplication signal (Type-2 clones survive renames/comment changes) instead
  of a fuzzy score — the depth-over-breadth moat applied to clone detection.
- Pairs an exact structural coefficient with the existing cosine score, so "related" and "duplicated" stop
  being conflated; each hit is qualified by both.
- Bounded cost: structural matching runs only over the dense candidate set, so it is a cheap exact verifier,
  not a quadratic scan.
- Reuses the existing tree-sitter parse path and `DocumentStore`; the net-new code is the serializer + suffix
  matcher.

**Worse:**
- Re-parsing the query snippet and candidate chunks on demand has a per-call cost (mitigated by the ≤15
  candidate bound); a persisted-AST cache is possible later but adds schema/sync burden.
- A correct suffix-tree implementation (or a vetted dependency) plus the normalization rules per grammar are
  real, careful work — the normalization (which node types collapse to their type) is per-language tuning.
- Type-3 (gapped) clones and cross-language duplication are out of reach by construction; the coefficient is
  deliberately narrow to stay exact.

**Neutral:**
- The matcher is additive over `find_similar_code`; the tool's interface gains a field but its existing
  cosine behavior is unchanged, so callers that ignore the new number are unaffected.
- Read-only; no migration risk.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Embedding similarity only (status quo) | Conflates *related* with *duplicated*; cannot *prove* a clone, and under-scores renamed copies — the exact refactoring case. The structural coefficient is the missing exactness. |
| Token-based clone detection (no AST) | Sequences of lexer tokens catch some Type-2 clones but lack structural scope — they over-report (matching incidental token runs) and miss block structure. AST node-type sequences carry the structure tokens lack. |
| Tree-edit-distance between ASTs | `O(n²)`–`O(n³)` per pair; too slow to run over candidates interactively. Suffix-tree longest-common-substructure is linear and sufficient for Type-2. |
| Global all-pairs structural clone index (`O(files²)`) | Infeasible at repo scale and unnecessary: dense retrieval already bounds the candidate set to the plausible few; structural matching only needs to *verify* those. |
| Fold the coefficient *into* the cosine score (one blended number) | Destroys the signal that "related vs. structurally identical" are different axes; reporting both is the whole value. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] AST→normalized-node-type-sequence serializer (identifiers/literals→type, comments/whitespace dropped); per-grammar node-type collapse rules over `adapters/_treesitter.py`.
- [ ] Suffix-tree longest-common-substructure matcher; structural coefficient = matched/query size.
- [ ] Wire into `find_similar_code` (`src/MCPServer.py`) as a refinement over the FAISS top-15 candidates (re-parse query + candidate chunks from `DocumentStore`); add the coefficient to each hit + a structural lane in the strata.
- [ ] Tier-C / cross-grammar candidates report cosine only, labeled (no faked coefficient).
- [ ] Tests: rename-invariance (renamed copy ⇒ coefficient ≈ 1.0), comment-invariance, unrelated code ⇒ low coefficient, Tier-C path returns cosine-only without error.
- [ ] (Optional, deferred) persisted clone/AST cache if re-parse cost proves material; (optional) Type-3 approximate pass.

**Notes:**
<!-- 2026-06-18: Kit 10, net-new ADR (Rule B). find_similar_code (MCPServer.py:135) is embedding+keyword only today; this adds an EXACT AST structural coefficient [0,1] beside cosine. Type-2 clones via normalized-AST-node-type-sequence + linear suffix-tree longest-common-substructure. Bounded to the FAISS top-15 candidate set (no O(files²) scan). Tier A/B only (need a tree-sitter parse); Tier C = cosine only. Intra-grammar only; Type-3/cross-language out of scope to keep the coefficient exact. Read-only, additive output — no stable_id/schema impact. -->
