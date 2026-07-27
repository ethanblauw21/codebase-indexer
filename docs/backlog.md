# Backlog — codebase-indexer

**This file holds wants. [`adr/`](./adr/) holds decisions.** The difference is what the document
asserts, not how big it is:

| | Backlog item | ADR |
|---|---|---|
| Asserts | a problem or a want | a committed solution |
| Written when | someone asks for it, or you find it | you start building it |
| Can sit unresolved | **yes, indefinitely** — that's the point | no; an unbuilt ADR is a lie |
| Lives on | `master`, here | its feature branch, then `master` at merge |

Three rules follow, and they're the load-bearing part:

1. **Not every item becomes an ADR.** Most work is just work. An item only earns an ADR when there
   is a real decision — alternatives, consequences you accept.
2. **One item can become several ADRs.** If shaping an item produces four independently-reversible
   decisions, that's four ADRs, not one 700-line file.
3. **No ADR without an item behind it.** This file is the intake; the ADR is the outcome. An ADR's
   header carries `**Backlog:** B-NNN` back to its origin.

**Promotion:** the ADR is created in the *first commit of the branch that builds it* — never on
`master` beforehand. That's what stops this pile from re-forming: a decision you thought about and
abandoned dies with the branch instead of accumulating as a permanent `proposed` file. See
[`CONTRIBUTING.md` §4.1](../CONTRIBUTING.md#41-architecture-decision-records-adrs).

**Statuses:** `raw` (captured, not thought through) · `shaped` (understood well enough to promote)
· `promoted → ADR-NNN` · `done` · `dropped`.

Sequencing and dependency order live in [`roadmap.md`](./roadmap.md), not here.

---

## Index

| ID | Want | Source | Size | Status |
|---|---|---|---|---|
| [B-001](#b-001) | `IGNORE_DIRS` doesn't exclude Python virtualenvs — an in-tree `venv/` gets fully indexed | ADR-025 review, 2026-07-17 | S | shaped |
| [B-002](#b-002) | Summarizer model id is hardcoded while every other model reads `indexer.toml` | ADR-009/020 sweeps | S | shaped |
| [B-003](#b-003) | Grow the private eval slice so the clean-TypeScript reranker signal isn't noise | ADR-019 §6 clause-3 FAIL, 2026-07-07 | M | shaped |
| [B-004](#b-004) | Per-language reranking — the reranker helped Python and hurt TypeScript | ADR-019 §6, 2026-07-07 | M | raw |
| [B-005](#b-005) | Stale "150+ languages" / "ADR-004 tiers" pointers in the research docs | doc sweep, 2026-07-27 | S | raw |
| [B-006](#b-006) | Supply-chain release verification (SBOM, signing) | study §9.6 | L | raw · trigger-gated |
| [B-007](#b-007) | Verifiable retrieval — Merkle proofs over served index results | study §9.6 | L | raw · trigger-gated |

> **Not tracked here:** open work that a built ADR already owns. ADR-025's GPU-blocked end-to-end
> reindex, ADR-011's Stage 2b member chains, ADR-006's Leiden backend and ADR-008's confidence-curve
> sweep are open checkboxes in those ADRs' Implementation Logs. The ADR is the truth for its own
> build; duplicating it here is how `adr-backlog.md` went stale. They are summarised — not owned — in
> [`roadmap.md`](./roadmap.md#deliberate-gates--not-forgotten-waiting-on-a-trigger).

---

## Legacy: wants that are already ADR files

Nine ADRs on `master` were written as plans and never built. Under the rule above they would be
backlog items, not ADRs — they are the pile this document exists to stop. **They were deliberately
left in place** (2026-07-27): each carries real research — build kits, dependency analysis, citations
— that a one-paragraph backlog item would destroy, and the numbers are cross-referenced from other
ADRs' `Depends on` fields.

**ADR-005 · 010 · 012 · 013 · 014 · 015 · 016 · 018 · 022.** Plus **ADR-017**, whose Phase-1
data-model slice (`Edge.candidate`) shipped while the tier model itself did not.

Treat them as read-only wants: **this set is closed and will not grow.** Anything new goes in the
index above, and any of these that gets built follows the ordinary branch-born rule from wherever its
work actually starts. See [`roadmap.md`](./roadmap.md#the-legacy-unbuilt-set) for how they sequence.

---

<a id="b-001"></a>
### B-001 — `IGNORE_DIRS` doesn't exclude Python virtualenvs

**Source:** flagged in ADR-025 §(`ADR-025-index-freshness-metadata.md:140`) during the freshness
work, 2026-07-17 · **Status:** shaped · **Size:** S

`IGNORE_DIRS` (`src/incremental_indexer.py:94-99`) excludes the JS/TS world thoroughly —
`node_modules`, `.next`, `dist`, `build` — and the Python world not at all. Missing: `venv`, `.venv`,
`env`, `site-packages`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.tox`.

**The symptom:** index any Python repo with an in-tree virtualenv and the scan walks the entire
`site-packages` tree — thousands of third-party `.py` files chunked, embedded and graphed as if they
were the user's code. On this project's own dogfood target that is the difference between indexing a
codebase and indexing PyPI. Retrieval quality degrades and the embed cost is unbounded.

**Two related smells found alongside it**, worth folding into the same fix:
- `IGNORE_DIRS` contains `"indexer"`, `"public"` and `"mocks"` — leftovers from the JS project this
  list was seeded from. `"indexer"` is actively hostile *here*: it would skip a directory named after
  this project.
- The list is a module constant, not `indexer.toml` config, while every neighbouring knob
  (`[embeddings]`, `[reranker]`, `[retrieval]`) is config-driven.

**Why it's still a want, not a decision:** the fix is obvious, but the *scope* isn't — hardcoded
additions are a one-line change, whereas moving the set to `indexer.toml` with a documented default
touches the config contract and is arguably an ADR. Pick when promoting.

---

<a id="b-002"></a>
### B-002 — Summarizer model id is hardcoded

**Source:** noted in `src/CLAUDE.md` during the ADR-009/ADR-020 model sweeps · **Status:** shaped
· **Size:** S

The embedder reads `[embeddings]` and the reranker reads `[reranker]`, both from `indexer.toml`.
`src/summarizer.py` still hardcodes its model id, so the `[summarization]` config block is
half-decorative — you can read a model name there that the code does not use.

**The trap that makes it worth a real look:** ADR-020's implementation found **two summarizer
classes**, and a change that migrates one and not the other reproduces exactly the split-brain
device-resolution bug ADR-020 was written to kill. Whoever takes this should confirm both load paths
read the same config key.

---

<a id="b-003"></a>
### B-003 — Grow the private eval slice

**Source:** the reranker clause-3 FAIL, 2026-07-07 (ADR-019 §6) · **Status:** shaped · **Size:** M

The reranker decision is **settled off**, and this does not reopen it — it improves the instrument
that settled it. The private contamination-free slice failed clause 3 with a pooled CI including 0
and a TypeScript regression on clean code. The slice is small enough that a single language's noise
can swing the pooled verdict, so "TypeScript regresses" and "TypeScript is under-sampled" are not
currently distinguishable.

**The want:** enough private fixtures that a per-language verdict is powered on its own, rather than
only the pooled one. Then a rerun says something new instead of re-litigating n=148.

**Note the cost gate:** any rerun is a GPU workload, and the GPU is off limits (see
[`roadmap.md`](./roadmap.md#the-constraint-that-shapes-everything)). Fixture authoring is CPU-only
and can proceed; the rerun cannot.

---

<a id="b-004"></a>
### B-004 — Per-language reranking

**Source:** the same 2026-07-07 verdict · **Status:** raw · **Size:** M

The public n=148 run showed the reranker helping Python for real and hurting TypeScript on clean
code. Today `[reranker].enabled` is one global boolean, so the only available answers are "on
everywhere" and "off everywhere" — and off everywhere is what the evidence forced, discarding a real
Python lift as collateral.

**The want:** let the enable decision be per-language. **The decision it needs** — and what would
make it an ADR — is where that dispatch lives: a per-language config map, a property of the tier
model (ADR-017), or a learned weight (ADR-014). Also unanswered: what a *mixed-language* query does,
since the retrieval pool is not language-partitioned.

---

<a id="b-005"></a>
### B-005 — Stale competitor-figure and ADR-004 pointers

**Source:** doc sweep, 2026-07-27 · **Status:** raw · **Size:** S

Two stale pointers survive in the research docs:

- `study-codebase-memory-mcp.md:341` still carries a **⚠️ fact-check flag** saying "ADR-004
  attributes 150+" and asking for a correction to "66 (claimed)". ADR-004 is *CI observability* — the
  tier model moved to ADR-017 long ago, and ADR-017 does not carry the wrong figure. ADR-008 §
  already states the corrected number. **The flag is discharged; only the note remains.**
- `adr-backlog.md` lines 130 and 157 name the same amendment against "ADR-004 (Tiers)".

Pure doc hygiene — no `src/` change, so it is a Minor change per `CONTRIBUTING.md` §1. Listed so the
next person reading the study doc doesn't re-open a closed question.

---

<a id="b-006"></a>
### B-006 — Supply-chain release verification

**Source:** `study-codebase-memory-mcp.md` §9.6 · **Status:** raw · **Size:** L · **trigger-gated**

SBOM generation, artifact signing, and AV/malware gating on release. Carried over from the competitor
study as explicitly deferred.

**Trigger:** a distributed binary or a published package. Today the project is a local `pip install
-e .` utility with no release artifact, so there is nothing to sign. Revisit the moment a wheel or an
executable ships to anyone else.

---

<a id="b-007"></a>
### B-007 — Verifiable retrieval

**Source:** `study-codebase-memory-mcp.md` §9.6 · **Status:** raw · **Size:** L · **trigger-gated**

VeriANN-style Merkle proofs so a *remote* consumer can verify that returned results really came from
the claimed index, rather than trusting the server.

**Trigger:** the index is ever served over a network boundary. The entire design today is
local-first — MCP over stdio, FAISS and SQLite on disk — so the threat model this addresses does not
exist yet. Related, but not the same thing: ADR-010's Merkle tree is for *drift detection* locally
and would supply the structure this builds on.
