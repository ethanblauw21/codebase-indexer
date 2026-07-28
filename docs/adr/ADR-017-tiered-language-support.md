# ADR-017: Tiered Language Support — Fitting Adapters + Generic Fallback

> **Renumbered from a draft ADR-004.** The 004 slot was taken by the merged
> *CI Observability & Workflow Tooling* ADR; this proposal moved to 017 (the next
> free slot) to resolve the collision. Other roadmap docs that still reference an
> "ADR-004 tiered-language" should be read as pointing here.

**Status:** proposed — **partially built.** The P1 data-model slice landed on `master` (PR #22): `Edge.candidate` flows through the extraction data model and is consumed by ADR-023's verdict tools and ADR-008's graded confidence. **The tier model itself — the Tier-A/B/C split, the generic fallback, and the Tier-B→Tier-A promotion path — is unbuilt**, so this stays `proposed` and sits in the legacy set (`docs/roadmap.md#the-legacy-unbuilt-set`). Promote when a Tier-B language is genuinely wanted. *(Annotated 2026-07-27.)*
**Date:** 2026-06-18
**Branch:** `feature/adr-017-tiered-language-support`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-003 — the `LanguageAdapter` Protocol, the conformance-suite gate, and the `candidate`-edge mechanism (ADR-003 §2.3) that Tier-B reuses verbatim.
- ADR-005 — the adapter `version` field and the version-drift `recheck` migration that powers the Tier-B→Tier-A promotion path (§6, Phase 4).
**Depended on by:**
- ADR-005 — its self-healing scorer flags the Tier-B/C output defined here and measures which Tier-B language to promote (mutual; see ADR-005 §6).
- ADR-008 *(planned — docs/adr-backlog.md)* — Measured Conformance builds on the tier model and evolves the `candidate` boolean into a graded `confidence`.
- ADR-011 *(planned)* — High-Precision Call Resolution targets the Tier-A promotion path.
- ADR-013 *(planned)* — DSL/industrial adapters register as tiers and reuse the conformance machinery.
- ADR-018 *(planned)* — syntactic clone matching's structural coefficient is available exactly for the tree-sitter-parsed tiers (A/B) defined here.
- ADR-023 — consumes the `Edge.candidate` field (§3) and **implements §7's three-state verdict + safe-direction rule** in the MCP verdict tools (deferred out of the ADR-017 P1 data-model slice because it requires making those tools edge-aware, which is ADR-023's unification refactor).

> Pressure-tested via `/grill-plan` on 2026-06-18. The grill resolved: `tags.scm`
> sourcing and a runtime capability probe (§2.2–2.3), B1/B2 capability classes
> (§2.4), grammar version pinning + provenance manifest (§5), a floating
> "experimental" lane (§6), verdict-tool semantics across tiers + two-stage
> verification (§7), and the project license (§8). See the Implementation Log
> notes for the empirical findings that drove these.

## Context

ADR-003 established the `LanguageAdapter` Protocol and shipped five hand-written
adapters (Python, TypeScript, JavaScript, C#, C++), each gated by a conformance
fixture suite. A passing conformance suite is the definition of "supported
language." This is the project's core differentiator: structure we can *prove*,
not breadth we can only claim.

That same gate is the bottleneck. Adding a language costs a full adapter plus an
authored golden suite, which is why we support five languages while breadth-first
competitors (e.g. `codebase-memory-mcp`) claim 66 (their paper's own figure) by vendoring tree-sitter
grammars and doing generic, unverified extraction. Today the cliff between
"supported" and "unsupported" is total: `get_adapter(ext)` returns `None` for an
unknown extension (`adapters/__init__.py:55`), and `chunk_file_ast` degrades to
`fallback_token_chunker` (`ast_chunker.py:139`) — a **graph-blind** line/token
chunker. An unknown-language file becomes searchable text with zero symbols and
zero edges. There is no middle ground.

We want breadth without surrendering the conformance moat. The resolution is to
stop treating "supported" as a binary and publish it as a spectrum, where every
result carries its own confidence. Critically, breadth here must be achieved by
running the **correct grammar** for a language, never by routing an unknown
language to a *similar* language's adapter — source-text similarity does not
predict grammar compatibility, and a borrowed grammar produces confidently-wrong
structure, which is the opposite of the moat (see the rejected centroid router in
Alternatives).

## Decision

Introduce a **three-tier language support model** with a runtime **capability
probe** that auto-classifies each generic language by what its grammar actually
provides. Tier A is unchanged. Tier B is a single new `GenericTreeSitterAdapter`
driven by each grammar's own `tags.scm`. Tier C is the existing text fallback.
Every chunk and edge records which tier and capability produced it, and the
verdict tools trust tiers differently (§7).

### §1 — The three tiers

| Tier | Name | Mechanism | Guarantees | Today |
|------|------|-----------|------------|-------|
| **A** | Fitting | Hand-written adapter + conformance suite | FQN identity, full edge types, skeletonization, project resolver, risk packs | Py, TS, JS, C#, C++ |
| **B** | Generic | One adapter consuming a grammar's `tags.scm` | Symbols + (capability-dependent) candidate edges; file-scoped FQNs only | *(new)* |
| **C** | Text | `fallback_token_chunker` | Embeddings only, no symbols, no graph | everything else |

Tier B is the entire breadth play. It is one flat tier in the headline; its
internal capability variance (§2.4) is carried in a per-language `capabilities`
field, not as extra headline tiers — keeping the published story "A = proven,
B = best-effort, C = text" while remaining honest about what each B language can
actually do.

### §2 — `GenericTreeSitterAdapter` (Tier B)

A single adapter consumes any grammar's `tags.scm` — the standardized GitHub
code-navigation query file capturing `@definition.*` and `@reference.*` — and
emits `Symbol`s and `Edge`s with **zero per-language Python**. It satisfies the
existing Protocol (`adapters/base.py`) unchanged. A new Tier-B language costs a
**registration row** (§5), not a hand-written adapter.

#### §2.1 — Empirical grounding

Verified against the five installed grammars (all bundle `queries/tags.scm` in
the pip artifact):

| Grammar | `tags.scm` | Definitions | `@reference.call` (edges) |
|---|---|---|---|
| Python | ✅ | class, constant, function | ✅ |
| JavaScript | ✅ | class, function, method, constant | ✅ (+ `reference.class`) |
| TypeScript | ✅ | yes | yes |
| C# | ✅ | yes | — |
| **C++** | ✅ | class, function, method, type | ❌ **none** |

The variance is real and present in our own tree: **C++'s `tags.scm` provides
definitions but no call references.** A Tier-B language therefore delivers
"symbols, and candidate edges *only if the grammar supplies reference captures*."
This is not hypothetical — it forces the capability probe (§2.3) and the B1/B2
classes (§2.4).

#### §2.2 — `tags.scm` sourcing ladder

There is **no single curated cross-language `tags.scm` table.** (`nvim-treesitter`
is *not* it — its query repos are editor-feature-focused; the Go directory ships
highlights/indents/folds/injections/locals and **no `tags.scm`**.) The de-facto
source is per-grammar, from the official `tree-sitter/tree-sitter-<lang>` repo's
`queries/tags.scm` — the same files GitHub maintains for code navigation and the
pip packages re-bundle when present. The one standardized thing is the **capture
vocabulary** (`@definition.*`, `@reference.*`, `@name`), giving the probe a
canonical target even when a grammar implements a subset.

Sourcing order per language, at registration:
1. Use the installed pip package's bundled `queries/tags.scm` if present.
2. Else vendor the official grammar repo's `tags.scm`, **version-matched** to the
   installed grammar, into `adapters/queries/<lang>/tags.scm` with a manifest
   entry (§5).
3. Else (no `tags.scm`, or definitions-only) → B2 with optional synthesized
   edges (§3), or Tier C.

#### §2.3 — Capability probe (the self-healing core)

The generic adapter introspects its grammar at registration rather than trusting
a human declaration. The probe:

1. Locates the package via `os.path.dirname(tree_sitter_X.__file__)`.
2. Loads the candidate `tags.scm` and **compiles it against the installed
   grammar** — this is the load-bearing check: it catches grammar/query version
   drift (a query written for a different grammar version fails to compile on
   node-type mismatch), not merely file presence.
3. Inspects the compiled query's capture names against the canonical vocabulary.
4. Emits a `Capability` descriptor: `{ has_definitions, has_references,
   class }` where `class ∈ {B1, B2}` (§2.4).

The descriptor is the language's self-declared contract. "The language tells you
what it supports" replaces "you promise on its behalf."

#### §2.4 — B1 / B2 capability classes

| Class | Probe result | Output | Examples |
|---|---|---|---|
| **B1** | definitions **and** references | symbols + candidate call edges | Python-via-tags, JS-via-tags |
| **B2** | definitions only | symbols + navigable scopes; **no native edges** (optional synthesized candidate edges, §3) | C++-via-tags |

Both are flat "Tier B" in the headline; the class lives in the `capabilities`
field. Grammars with no `tags.scm` or zero extracted symbols fall through to
Tier C — `chunk_file_ast` already handles the empty-`symbols` case
(`ast_chunker.py:145`).

```python
class GenericTreeSitterAdapter:
    """Tier-B. Any grammar with a compilable tags.scm. No FQN/edge guarantees."""
    language_id: str
    extensions:  frozenset[str]
    version = "v1"                       # ADR-005 method string: f"generic-{language_id}/{version}"

    def __init__(self, language_id, extensions, ts_language, tags_scm):
        self._lang       = ts_language
        self._tags_query = Query(ts_language, tags_scm)        # compiled — raises on version drift (§2.3)
        self.capability  = probe_capability(self._tags_query)  # B1 | B2

    def parse(self, path, src):
        caps = QueryCursor(self._tags_query).captures(parse(self._lang, src))
        symbols = [self._to_symbol(path, n)                    # file-scoped FQN: f"{path}::{name}"
                   for cap in DEFINITION_CAPTURES for n in caps.get(cap, [])]
        edges = [Edge(self._enclosing_fqn(path, n), text(n), "call", candidate=True)
                 for n in caps.get("reference.call", [])]      # B1 only; ALWAYS candidate (§3)
        if self.capability.class_ == "B2" and SYNTHESIZE_EDGES:
            edges += self._synthesize_candidate_edges(symbols, src)   # loose, candidate, opt-in (§3)
        return ParseResult(symbols, edges, [], [])
```

### §3 — Candidate-edge honesty contract

**Every Tier-B edge is `candidate=True`, by construction** — `tags.scm` gives
name-based references with no scope/type resolution, so a Tier-B "call" is a
*possible* call, never verified. The `Edge.candidate: bool` field is **introduced
by the ADR-003 §2.3 amendment** (for C++ overload sets); **Tier-B is its second
consumer**, and ADR-008 later evolves it to a graded `confidence`. The verdict tools treat
candidate edges per the safe-direction rule in §7. The field, repeated here for
context (`adapters/base.py:42`):

```python
@dataclass
class Edge:
    source_fqn: str
    target: str
    kind: str
    resolved_target: Optional[str] = None
    candidate: bool = False          # NEW: name-based / unresolved (Tier-B, C++ overload sets)
```

Defaults `False`, so introducing it (in ADR-003) leaves every existing Tier-A
edge unchanged; Tier-B simply sets it `True`.

**Synthesized edges (B2, opt-in).** For definitions-only languages, the adapter
*may* manufacture loose candidate edges — "an identifier whose text matches a
known definition name, in a call-shaped node, becomes a `candidate` call edge."
This fills the edge hole natively. It is noisy, and "call-shaped node" reintroduces
a little per-grammar knowledge, so it is **opt-in per language** and safe only
because every such edge is `candidate` and therefore firewalled from every verdict
(§7). Synthesis is the residual safety net; sourcing a references-bearing
`tags.scm` (§2.2) is preferred and raises a language to B1.

### §4 — Tier-B FQNs are deliberately shallow

File-scoped (`path::name`) only — no cross-file identity, namespace merge, or
`Outer+Inner` nesting, which `tags.scm` cannot supply. Documented as a tier
property, not faked. The `stable_id` formula already keys on `file_path::scope`
(`stable_id.py:40`), so Tier-B IDs are stable within a file — all incremental
indexing needs.

### §5 — Grammar version pinning + provenance manifest (decision: A3 + B1)

Tier-B capability is *derived* from version-coupled query files, so floating
grammar versions would make capability non-deterministic — poison for a tool
selling auditable structure. Decision:

- **Pin curated grammars** (extend the ADR-003 precedent: `c-sharp==0.23.5`,
  `cpp==0.23.4`; pin the unpinned three and every new Tier-B grammar).
- **CI-gate drift (loud at the boundary):** the §2.3 compile-probe runs in CI;
  a query that fails to compile against the pinned grammar is a CI failure, not a
  silent downgrade.
- **Surface runtime drift, never swallow it:** if a capability change somehow
  occurs at runtime, it is recorded through ADR-005's flag/summary channel
  (capability regression = a flag), so degradation is observable.
- **Provenance manifest = the registration row.** One artifact satisfies pinning,
  provenance, capability reproducibility, and licensing at once:

```yaml
# adapters/queries/registry.yaml  — one row per Tier-B language
- language: go
  extensions: [".go"]
  grammar: "tree-sitter-go==0.21.0"                 # the pin
  tags_scm_source: "tree-sitter/tree-sitter-go@<commit>"
  license: MIT                                       # SPDX — provenance
  capability: B1                                     # probe result, recorded
```

- **Permissive-only gate:** registration records each grammar query's SPDX id and
  **rejects** anything not on an allowlist (MIT/Apache-2.0/BSD/ISC). A non-permissive
  grammar query never enters the tree. Apache-modified files note the change in
  the manifest. Licensing becomes a CI check, not a latent liability.

### §6 — Floating grammars: experimental lane only

Floating (unpinned) grammars are permitted **only** in an opt-in *experimental
discovery* lane for un-curated languages — never for the curated B1/B2 set. The
determinism argument is weaker here because experimental languages carry no
structural guarantee anyway. Two non-negotiables:

1. **Stamp the actual grammar version used at index time** into chunk provenance
   (ADR-005 `chunker_method` extended: `generic-go/v1@ts-go-0.21.3`). The index
   stays *auditable* even when not *reproducible*.
2. **Flag the language `experimental`** and exclude it from any reproducibility /
   shared-graph guarantee.

This gives a clean symmetry: experimental floating = the language-acquisition
on-ramp; **promotion = getting pinned + a manifest row + a smoke fixture.** The
ADR-005 scorer measures which experimental language has earned promotion.

### §7 — Verdict-tool semantics across tiers

Edges are near-always intra-language, so a verdict's evidence is dominated by the
*queried symbol's* tier, plus the already-present case of Tier-A languages that
emit candidate edges (C++ overload sets, ADR-003 §2.3). A flat "insufficient" is
wrong because verdicts are **asymmetric** — each has a dangerous direction and a
safe one. Rule, one sentence: **a candidate edge never confirms a positive
verdict, but always prevents the dangerous negative one, and is always labeled
unverified.**

| Tool | Dangerous wrong answer | Candidate edges… |
|---|---|---|
| `find_dead_code` | "dead" → user deletes live code | **block** the dead verdict: any candidate ref ⇒ "not provably dead — N unverified references" |
| `analyze_blast_radius` | under-reports → missed caller | **expand** the radius in a separate *unverified* bucket |
| `detect_pattern_violations` / unabstracted-reads | false accusation | **soften** to "possible violation — review" |

**Three response states**, replacing the binary gate:
- **VERIFIED** — all evidence non-candidate, or *single-candidate* (the existing
  "one possible target = resolved" nuance, generalized).
- **ADVISORY** — verified core + a labeled candidate set.
- **INSUFFICIENT** — genuinely no evidence (the only case that keeps the old
  bare "insufficient").

`find_dead_code` is the strict exception: candidate evidence *always* blocks a
"dead" verdict, because deletion is the one verdict whose wrong answer destroys
data.

> **Evolution → ADR-008.** The `candidate: bool` here is Phase 1. ADR-008
> (Measured Conformance & Edge Confidence) graduates it to a graded
> `Edge.confidence: float` (the competitor's 6-strategy 0.30–0.95 cascade is the
> reference). At that point the three states above stay, but the VERIFIED/ADVISORY
> boundary becomes a **measured, tunable confidence threshold** (the "prefer
> unknown over wrong" policy, reported with its precision/recall trade-off) rather
> than the boolean split. So this is not in tension with the "confidence-scored
> verdicts" entry in Alternatives — that rejected fractional *verdict outputs*;
> ADR-008 adds graded *edge confidence* feeding the same three discrete states.

#### §7.1 — Two-stage verification

ADVISORY verdicts return a **bounded candidate checklist** (`caller_fqn, file,
line`). Verification is an opt-in second pass, and the verifier is the **host
agent** — reading each candidate's snippet and confirming — consistent with the
project's token-efficient, delegate-to-agent philosophy and requiring no new
per-language machinery. The existing reranker may pre-sort the checklist by
plausibility. The single new surface is a **snippet-returning verify mode**
(`verify_candidate_edges`, or an `include_snippets` flag) that reuses the chunk
store (`DocumentStore`) and contains **zero resolution logic**.

- **Advisory is enough for estimation** ("how big is this refactor?").
- **The verify pass is required before irreversible action** — which is exactly
  the `find_dead_code` safety rule in action: candidate refs block deletion, the
  agent verifies them, then deletes or not.

This reframes candidate edges as a **cheap pre-filter that makes exact
verification tractable** — the tool finds the 7 needles, the agent confirms them —
rather than as untrustworthy noise. A heavyweight per-language static verifier
(on-demand clangd/LSP) is rejected: that is Tier-A promotion by another name (§9).

### §8 — Tier-B conformance is a smoke test, not a golden snapshot

Per registered Tier-B grammar, one parameterized CI test asserts: the grammar +
`tags.scm` compile (§2.3), ≥1 symbol extracted from a known fixture, every emitted
edge is `candidate=True`, and a degenerate file falls through to Tier C without
error. A Tier-B fixture is a few lines, not an asserted-graph golden repo.

### §9 — Promotion path

A Tier-B language earns Tier A when someone writes its real adapter + golden
suite. **Which language to promote is measured, not guessed** — the ADR-005
self-healing scorer's structural/coherence flags concentrate on weak Tier-B/Tier-C
output, so `get_flagged_summary()` grouped by language is the prioritized backlog.
When a fitting adapter ships (`generic-go/v1` → `go/v1`), ADR-005's version-drift
`recheck` auto-reindexes affected files. ADR-017 supplies the tiers; ADR-005
supplies the demand signal and migration trigger.

### §10 — First Tier-B wave

Register, by manifest row (§5): **Go, Java, Rust, Ruby, PHP, Kotlin.** Each is a
pinned grammar + a probed/sourced `tags.scm` + a smoke fixture. The probe assigns
B1/B2 per language; none requires hand-written parse logic. Publish the tier table
(with per-language capability) in the README beside the existing limits prose.

### §D — Project license (Apache-2.0)

The vendoring obligation surfaced the project-license question. Decision:
**Apache-2.0**, chosen for maximum adoption and a corporate-friendly,
patent-granting professional signal — the project is a portfolio/resume artifact
with no monetization intent, so reach outranks copyleft control. (AGPL was
considered and rejected: its forced-open-fork control buys nothing here and
suppresses the corporate adoption that is the actual goal.) Apache's NOTICE
mechanism already enforces attribution; a **non-binding** openness/contribution
request goes in the README (never in `LICENSE`, to avoid muddying the terms).
Apache-2.0 is compatible with all MIT/Apache vendored `tags.scm` files.

## Consequences

**Better:**
- Breadth without abandoning conformance: A = proven, B = honest best-effort
  (probed per language), C = text; the user always knows which they queried.
- The unsupported-language cliff becomes a ramp; the capability probe makes each
  language self-declare what it can do (self-healing, no human promise).
- Unit economics drop to a manifest row; the row simultaneously pins the grammar,
  records provenance/SPDX, and reproduces capability.
- Candidate edges become a *pre-filter for tractable agent verification*, not
  dead weight — verdict tools stay useful in polyglot repos while `find_dead_code`
  stays safe.
- Apache-2.0 maximizes adoption (the portfolio goal) with attribution preserved.

**Worse:**
- "Conformance = support" becomes a spectrum that must be named, not hidden; the
  `candidate` gate, capability field, and tier labels are the mitigation.
- Verdict tools grow from two states to three (VERIFIED/ADVISORY/INSUFFICIENT)
  plus a `verify_candidate_edges` surface — more branches and tests.
- Pinned grammars + vendored, version-matched `tags.scm` + a manifest + a
  permissive-license gate are ongoing maintenance; grammar bumps are deliberate
  work, and CI must enforce the manifest.
- Vendoring grammars grows install size and dependency surface.

**Neutral:**
- Tier B reuses the Protocol, registry, three-tier index, embeddings, stable IDs,
  and RTR pipeline untouched — it is an additional adapter, one additive `Edge`
  field, a probe, a manifest, and verdict-tool semantics.
- Experimental floating languages are auditable (version-stamped) though not
  reproducible — an intentional, labeled trade.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Best-fit **centroid router** (unknown ext → cosine vs adapter centroids → route to closest) | Source-text similarity ≠ grammar compatibility; `tree-sitter-rust` cannot parse Zig however similar they embed. Manufactures confidently-wrong structure that verdict tools would trust — direct assault on the moat. Valid only for graceful-degradation *text* chunkers (document indexer), not grammar-bound code. |
| `nvim-treesitter` as the `tags.scm` source | Editor-feature queries (highlights/indents/folds); ships no `tags.scm` (Go confirmed). Wrong corpus. Official grammar repos are the source. |
| **Floating grammars for curated languages** | Non-deterministic capability under transitive bumps; silent regression of verdicts; breaks team index reproducibility. Already bit us once (the 0.25 query-API removal). Floating allowed only in the labeled experimental lane (§6). |
| **AGPL-3.0** project license | Forced-open-fork control buys nothing for a non-monetized portfolio project and suppresses the corporate adoption that is the goal. Apache-2.0 maximizes reach; attribution still enforced via NOTICE. |
| **Confidence-scored verdicts** (per-edge weights → "radius 4.2 @ 0.7") | Less actionable than "3 verified + 5 to review." Binary verified/candidate with safe-direction handling is what a developer can act on. |
| **Heavyweight per-language static verifier** (on-demand clangd/LSP for the candidate set) | That *is* Tier-A promotion by another name; duplicates the per-language burden Tier B exists to avoid. Verification is delegated to the host agent (§7.1). |
| Tier-B golden snapshots like Tier A | Infeasible across many grammars; re-creates the authoring bottleneck. Smoke conformance (§8) is the right rigor. |
| B1/B2 as separate headline tiers | Fractures the clean "A/B/C" story. Capability variance lives in a per-language field instead (§2.4). |

## Testing Additions

| Area | Type | Notes |
|------|------|-------|
| `Edge.candidate` field | Unit | Default `False`; Tier-A edges unchanged; round-trips through `db.upsert_file` (`db.py:582`) |
| Capability probe | Unit | Compile `tags.scm` against installed grammar; B1 for refs-bearing grammar, B2 for defs-only (C++-via-tags), fail-loud on version mismatch |
| Generic adapter parse | Unit | `tags.scm` → symbols + candidate edges (B1, Go) end-to-end |
| Synthesized edges (B2) | Unit | Opt-in; all synthesized edges `candidate=True`; off by default |
| Tier-B smoke conformance | Integration (parameterized) | Every registered grammar: compiles, ≥1 symbol, all edges candidate, degenerate → Tier C |
| Verdict three-state | Unit | VERIFIED / ADVISORY / INSUFFICIENT; single-candidate promotes to VERIFIED |
| Safe-direction rule | Unit | `find_dead_code` candidate ref blocks "dead"; blast-radius candidate expands unverified bucket; pattern-violation softens |
| Two-stage verify | Unit | `verify_candidate_edges` returns snippets from the chunk store with zero resolution logic |
| Provenance/license gate | Unit | Registration rejects non-allowlisted SPDX; manifest row required per Tier-B language |
| Pinning / drift | Integration | Compile-probe fails CI on grammar/query version mismatch |
| Tier-A regression | Integration — merge blocker | ADR-003 golden snapshots remain byte-identical (Tier A untouched) |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

**Phase 0 — Tier-A scan-gate fix: C#/C++ source indexing — DONE 2026-07-01**
- [x] Add `.cs`/`.cpp`/`.cc`/`.cxx`/`.h`/`.hpp` to `incremental_indexer.INDEXABLE_EXTS` so the disk scan **chunks + embeds** C#/C++ source. The Tier-A adapters (ADR-003) and §1's "C#/C++ = Tier A" claim already existed; the scan gate silently omitted these extensions, so C#/C++ **code was never indexed** — only `.csproj`/`.sln` descriptors (edges-only). Surfaced while implementing ADR-019 (its real-repo eval needs C#/C++ chunks for arm A). Validated end-to-end on the fixtures: `sample.cs`/`sample.cpp` → 19 symbol chunks each → embedded (768-d) → FAISS (`tier1_surgical` ntotal 38). Regression test: `tests/test_cs_cpp_indexing.py`.
- **Scope:** this is *only* the Tier-A integration fix. The Tier-B `GenericTreeSitterAdapter`, capability probe, and candidate-edge semantics (Phases 1–4 below) are untouched — ADR-017 stays `proposed`.

**Phase 1 — Edge.candidate + verdict semantics**
- [x] Add `candidate: bool = False` to `Edge`; thread through `db.upsert_file` edge write and read path
- [~] Backfill C++ overload-set edges to `candidate=True` (ADR-003 §2.3) — **no-op in current code (documented finding)**
- [x] Implement three-state verdicts (VERIFIED/ADVISORY/INSUFFICIENT) + safe-direction rule in `analyze_blast_radius`, `find_dead_code`, `detect_pattern_violations` — **landed in ADR-023 Phase 2 (retriever-unification PR)**
- [x] `verify_candidate_edges` snippet tool over `DocumentStore` (no resolution logic); optional reranker pre-sort — **landed in ADR-023 Phase 2** (reranker pre-sort deferred as opt-in follow-up)

**2026-07-08 — Phase-1 data-model slice landed (branch `feature/adr-017-p1-edge-candidate-verdicts`).** Shipped the additive `Edge.candidate: bool = False` field and threaded it end-to-end: `src/adapters/base.py` (the field), `src/db.py` edges schema (new `candidate INTEGER NOT NULL DEFAULT 0` column + idempotent `ALTER`-based `_migrate_edge_candidate()` for pre-candidate DBs), the write path (`upsert_file`), the bidirectional call-graph CTE (carries `e.candidate` through both recursive branches), and `CallGraphNode.candidate` via `MIN(cg.candidate)` over reaching edges (a node with **any** resolved edge in a direction reads as verified — the safe default for §7). Tests: `tests/test_edge_candidate.py` (5 — dataclass default, write round-trip, CTE propagation, MIN semantics, additive migration); full suite **171 passed**, no snapshot drift, no GPU. This is the "additive field ships first" pattern ADR-008 §4 relies on: default `False` leaves every existing Tier-A edge unchanged.

- **Deviation — C++ overload-set backfill is a no-op (honest finding).** The §2.3 amendment cited "C++ overload sets" as the field's motivating first producer, but the C++ adapter emits **no overload-multiplicity edges** to mark: it produces one `Edge(kind='call')` per call site, and its own docstring records "operator overloads: indexed as symbols but rarely earn call edges." Call ambiguity is handled downstream by ADR-021's resolver (leaves `resolved_target = NULL`), not by emitting multiple candidate edges. So there is nothing to backfill in current code — consistent with the field having been designed but never produced. The **first live producer is Tier-B** (Phase 2, §3: every Tier-B edge is `candidate=True` by construction). A secondary future producer is marking ambiguous in-repo `CALLS` edges (≥2 name matches) in the resolver — deferred (ADR-021/ADR-011 territory).
- **Deviation — verdict machinery deferred to the retriever-unification PR.** §7's three-state verdicts + safe-direction rule and the §7.1 `verify_candidate_edges` snippet tool require the verdict tools (`analyze_blast_radius`, `find_dead_code`, `detect_pattern_violations`) to be **edge-aware**; today they are regex/FAISS heuristics that never touch the edge graph. Making them edge-aware is exactly the retriever-unification work (Item 2), so the §7 machinery moves there to avoid building the tools' edge path twice. Phase 1 here delivers the field + plumbing the unification consumes.

**2026-07-08 — §7 verdict machinery landed in ADR-023 Phase 2 (branch `feature/adr-023-unify-mcp-retrieval`).** The three verdict tools are now edge-aware via shared helpers in `MCPServer.py` (`_db()`, `_resolve_symbol_fqns()`, `_caller_evidence()`), consuming `CallGraphNode.candidate` (the Phase-1 field) to split callers into verified vs candidate. The **safe-direction rule** is implemented per §7: `find_dead_code` treats any candidate reference as INSUFFICIENT ("not provably dead", never a delete green-light), `analyze_blast_radius` expands candidate neighbours into a separate UNVERIFIED bucket, `detect_pattern_violations` softens candidate-reached accusations to "possible violation". The `verify_candidate_edges` snippet tool (§7.1) returns each candidate caller's source via `db.get_symbol().text` with zero resolution logic — the host agent verifies. Tests: `tests/test_verdict_edge_evidence.py` (4 — anchor-scoped FQN resolution, verified/candidate split, resolved-sighting-wins dedup, empty-symbol); live golden-diff on `.code-index` confirms the three tools produce edge-aware output without regression. **Caveat:** no `candidate=True` producer exists until Tier-B (Phase 2 below), so the ADVISORY/INSUFFICIENT/softened branches are correct-by-construction and unit-tested on synthetic edges, but not yet exercised by the live index — Tier-B will be their first real producer, closing the loop end-to-end.

**Phase 2 — Generic adapter + capability probe**
- [ ] `probe_capability()`: locate package, compile `tags.scm` against grammar, classify B1/B2
- [ ] `GenericTreeSitterAdapter`; capture-name → `kind` map; opt-in B2 edge synthesis
- [ ] Wire below Tier-A lookup, above Tier-C fallback, in `chunk_file_ast`
- [ ] Tier-B smoke-conformance harness (parameterized)

**Phase 3 — Registration, pinning, licensing**
- [ ] `adapters/queries/registry.yaml` manifest (grammar pin + `tags.scm` source@commit + SPDX + capability)
- [ ] Permissive-only SPDX gate at registration; CI compile-probe drift gate
- [ ] Pin the three unpinned grammars; vendor any missing `tags.scm` version-matched
- [ ] First wave: Go, Java, Rust, Ruby, PHP, Kotlin (manifest + smoke fixture each)
- [ ] Publish tier+capability table in README; add Apache-2.0 `LICENSE` + non-binding openness note

**Phase 4 — Experimental lane + promotion (depends on ADR-005)**
- [ ] Opt-in floating "experimental" lane: grammar-version stamping + `experimental` flag, excluded from reproducibility
- [ ] Confirm `generic-go/v1` → `go/v1` triggers ADR-005 `recheck` reindex

**Notes:**
<!-- 2026-07-01: Phase 0 landed independently of the Tier-B work — a correctness fix wiring the existing Tier-A C#/C++ adapters into incremental_indexer's scan gate (INDEXABLE_EXTS). Found while building ADR-019's real-repo eval, which surfaced that §1's "C#/C++ = Tier A" was aspirational: the adapters were built + snapshot-tested (ADR-003), but the shipping indexer never scanned .cs/.cpp source, so C#/C++ code retrieval did not actually work. -->
<!-- 2026-06-18: Grilled via /grill-plan. EMPIRICAL FINDINGS that drove the design: all five installed grammars bundle queries/tags.scm; C++ tags.scm has definitions but NO @reference.call (proves capability variance is real, forces the probe + B1/B2). nvim-treesitter ships no tags.scm (editor queries only) — official tree-sitter grammar repos are the source. Decisions: A3+B1 pinning/provenance, permissive-only gate, floating only in experimental lane, Apache-2.0, three-state verdicts with safe-direction + two-stage agent verification. -->
<!-- 2026-06-11: Centroid best-fit router rejected for code (grammar incompatibility); it belongs to the document/filesystem indexer where text chunkers degrade gracefully. -->
