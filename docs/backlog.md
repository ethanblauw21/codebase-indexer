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
| [B-003](#b-003) | ~~Grow the private eval slice to de-noise the clean-TypeScript reranker signal~~ | ADR-019 §6 clause-3 FAIL, 2026-07-07 | M | **dropped** |
| [B-004](#b-004) | ~~Per-language reranking — the reranker helped Python and hurt TypeScript~~ | ADR-019 §6, 2026-07-07 | M | **dropped** |
| [B-005](#b-005) | Stale "150+ languages" / "ADR-004 tiers" pointers in the research docs | doc sweep, 2026-07-27 | S | raw |
| [B-006](#b-006) | Supply-chain release verification (SBOM, signing) | study §9.6 | L | raw · trigger-gated |
| [B-007](#b-007) | Verifiable retrieval — Merkle proofs over served index results | study §9.6 | L | raw · trigger-gated |
| [B-008](#b-008) | Indexer crashes on a Windows `cp1252` console before indexing a single file | found 2026-07-27 | S | shaped |

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

**Confirmed in practice, 2026-07-27.** A CPU reindex of this very repo was launched and immediately
began embedding `benchmarks/real_repo/corpus/click/…` — the cloned eval corpora. **503 of the 601
indexable files in this tree live under `benchmarks/`**, so an unpatched run produces an index that
is ~84 % third-party corpus. The same list is missing this repo's own generated trees
(`gpu-crash-repro/`, `graphify-out/`). So this is not a latent bug about *other people's* repos — it
misindexes its own, today. A working exclusion set is proven out in the run wrapper at
`scratchpad/run_cpu_index.py` and can be lifted straight into the fix.

**Why it's still a want, not a decision:** the fix is obvious, but the *scope* isn't — hardcoded
additions are a one-line change, whereas moving the set to `indexer.toml` with a documented default
touches the config contract and is arguably an ADR. Note the same config/constant drift affects
[B-002](#b-002); deciding it once for both is probably the right move.

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

**Second half of the same bug, found 2026-07-27:** `[summarization].enabled` in `indexer.toml` is
**not read by the indexer either**. The gate is `ENABLE_SUMMARIZATION`, a module constant at
`src/incremental_indexer.py:92`, so setting `enabled = false` in config does nothing and the only way
to turn summarization off is to edit source. Config that silently does nothing is worse than no
config. Same constant-vs-toml drift as [B-001](#b-001) — worth one decision covering both.

---

<a id="b-003"></a>
### B-003 — ~~Grow the private eval slice~~ · DROPPED

**Source:** the reranker clause-3 FAIL, 2026-07-07 (ADR-019 §6) · **Status:** **dropped 2026-07-27**

Was: author enough contamination-free fixtures that a per-language reranker verdict is powered on its
own (TS sat at n=19 with a ±0.162 CI — wider than any plausible effect), plus clean controls for
JS/C#/C++, which had none.

**Why dropped — see [B-004](#b-004) for the full reasoning.** Specific to this item: it was scoped
against a stack that no longer exists. Every reranker number was measured on
`jina-embeddings-v2-base-code`; `bge-code-v1` became the default 2 h 19 m after the verdict was
recorded. De-noising a jina-era signal tells you nothing about the retriever you ship.

---

<a id="b-004"></a>
### B-004 — ~~Per-language reranking~~ · DROPPED

**Source:** the same 2026-07-07 verdict · **Status:** **dropped 2026-07-27**

Was: replace the single global `[reranker].enabled` boolean with a per-language decision, since the
reranker measurably helped Python (+0.187 on clean code, CI excludes 0) while clean TypeScript went
slightly negative.

**Why both items are dropped.** The reranker was compensating for a weak first stage, and §P1
replaced the first stage. On Python and C#, **bge dense-only now scores above jina-plus-reranker**
(+0.034 / +0.058 mrr@10) — for free, at 0.3–2 s/query instead of ~90 s on CPU. Three reasons closed
the thread:

1. **The measurement has no decision value.** The outcome is "ship it as an opt-in flag, off by
   default" whether a rerun is positive or negative. A test that cannot change the action isn't
   worth its cost.
2. **The consumer is an agent, and the agent is the better judge.** These tools are read over MCP by
   a frontier model that re-ranks the top-10 anyway. A 0.6B cross-encoder pre-sorting a list a
   stronger judge is about to re-sort is duplicated work — the same reasoning that pushed the graph
   layer toward agent-driven expansion instead of fused scoring.
3. **Latency settles the default with no eval at all.** ~90 s/query on CPU, which is what most people
   cloning this repo will run.

**What survives:** `[reranker].enabled` stays a real, working opt-in in `indexer.toml` for someone
with a GPU and a Python-heavy repo. Retired is the *research thread*, not the feature — and
deliberately **not** a per-language config surface, which would be new machinery built on evidence
we no longer trust. Recorded in ADR-009 §P4 and ADR-019 §6.

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

---

<a id="b-008"></a>
### B-008 — Indexer crashes on a Windows `cp1252` console

**Source:** found 2026-07-27, launching a CPU reindex · **Status:** shaped · **Size:** S

`run_incremental()` prints a banner containing box-drawing characters
(`src/incremental_indexer.py:605`). When stdout is not UTF-8 — a stock Windows console, or any piped
/ redirected run under the default `cp1252` code page — this raises
`UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-1` **before a single file
is indexed**. Verified: exit code 0 from the wrapper, traceback in the log, no index produced.

**Why this one matters more than its size suggests.** It is the *first thing* a Windows user cloning
this repo experiences, and the failure is maximally confusing — a Unicode error from a code indexer,
with nothing indexed and no hint that the console is the problem. Given the project is meant to be
shared on GitHub for local use, this is a first-run blocker, not cosmetic.

**Two candidate fixes**, and the choice is the decision:
- Reconfigure stdout to UTF-8 with `errors="replace"` at entry (what
  `scratchpad/run_cpu_index.py` does — proven to work), or
- drop the non-ASCII characters from the banners entirely.

The second is smaller and cannot regress; the first keeps the nicer output. Either way, check every
`print` on the indexing path, not just line 605 — the ingest logging uses `✓` too.
