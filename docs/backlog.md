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
| [B-009](#b-009) | Eval result files don't record which models produced them | reranker provenance miss, 2026-07-27 | S | shaped |
| [B-010](#b-010) | The same chunk text is returned twice, as separate tier-2 and tier-3 hits | first live search on the rebuilt index, 2026-07-27 | S | shaped |
| [B-011](#b-011) | Multi-tier RRF **cannot** reinforce — the tier name is inside the FAISS id, so the tiers are disjoint document sets | same run, 2026-07-27 | M | shaped |

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

#### Implementation survey (2026-07-27) — what the fix actually touches

**Smaller than it looks, for three reasons.**

1. **There is already one source of truth.** Both consumers import from `incremental_indexer`:
   `scan_disk()` (`:156-177`) and the MCP watchdog filter (`MCPServer.py:1894`). Nothing to
   reconcile or de-duplicate.
2. **The migration is free.** `scan_disk()` returns the disk view, and `DiffResult.deleted` is
   "present in SQLite, absent from disk" (`:184-188`). Newly-ignored files therefore land in
   `deleted` and are purged on the next incremental run — **an existing bloated index cleans itself,
   with no forced full reindex.**
3. **The config path has an exact template.** `config.py` is 39 lines returning a plain dict;
   `core.py`'s `_emb_cfg()` (`:39-45`) is the cached-read-with-defaults pattern a `[scan]` block
   would copy verbatim.

**Effort:** constants-only ≈ 10 lines / under an hour. Config-driven `[scan]` block ≈ half a day.
Neither is architecturally risky.

**The real work is tests: there are none.** No test in `tests/` references `scan_disk`,
`IGNORE_DIRS`, or `IGNORE_ROOT_DIRS` — the scan gate, which decides what the entire product looks at,
is completely uncovered. Any fix here should land the first tests for it, and that is the bulk of the
effort.

**Two genuine decisions, which is what keeps this off the "just do it" pile:**

- **Name-matching is the wrong detector for virtualenvs.** Blanket-ignoring `env` at any depth would
  skip legitimate source directories (`src/env/` is common), and a venv named `.venv-3.12` is missed
  entirely. **A virtualenv is precisely identifiable by a `pyvenv.cfg` at its root** — content
  detection is both stricter and more complete than a name list. Recommended, but it is a design
  change, not a string addition.
- **Removing `"indexer"`, `"public"` and `"mocks"` is a behaviour change.** `public/` is a real source
  directory in many web projects, so today's list silently under-indexes them. Fixing that is
  correct, but it means some users' next index gets meaningfully larger — worth a release note.

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

---

<a id="b-009"></a>
### B-009 — Eval result files don't record which models produced them

**Source:** the reranker provenance miss, 2026-07-27 · **Status:** shaped · **Size:** S

Every row in `benchmarks/real_repo/*.jsonl` carries `repo`, `language`, `arm`, `n`, `git_sha` and the
metrics — but **not the model ids it ran with**. So a result cannot state its own stack, and
reconstructing it means checking out the recorded `git_sha` and reading `indexer.toml`. That is
exactly the archaeology that had to be done on 2026-07-27 to discover that every reranker number
predated the `bge-code-v1` swap by two hours.

`git_sha` looks like it should be enough, and isn't: the authoritative n=148 run records
`git_sha: "unknown"` because the cloud bundle is not a git checkout (noted in ADR-009 §P4). The one
field meant to carry provenance is empty on the most important run.

**The want:** the harness stamps `embedder_model_id`, `embedder_dim`, `reranker_model_id` and
`summarizer_model_id` into every result row it writes — read from the same `load_indexer_config()`
the run itself used, so it cannot drift from reality. Then a stale baseline is self-evident instead
of being a two-hour reconstruction.

**Why it's small:** `tools/eval_common.py` already owns the shared `append_baseline` path, and
`config.py` already exposes the values. It is a dict update plus a doc line, not new machinery.

**Makes structural** the convention now written into
[`CONTRIBUTING.md` §4.2](../CONTRIBUTING.md#42-measurement-provenance--a-baseline-names-the-stack-it-was-measured-on).
Worth doing precisely because a convention people must remember is the thing that failed here.

---

<a id="b-010"></a>
### B-010 — The same chunk text is returned twice, as separate tier-2 and tier-3 hits

**Source:** first live search on the rebuilt index, 2026-07-27 · **Status:** shaped · **Size:** S

A query for *"how are stable FAISS ids computed from a file and offset"* returned, at ranks 2 and 3:

```
0.0167  tier=tier2_component      fid=511099690527721806  src/stable_id.py:Full File_part_1  len=3794
0.0167  tier=tier3_architectural  fid=111169463933075209  src/stable_id.py:Full File_part_1  len=3794
```

Distinct FAISS ids, distinct tiers, **identical text** — 3794 characters, twice, in a top-5. The
same shape appeared for `src/RecFileSearch.py` and `src/hybrid_retriever.py` in other queries, so
roughly a fifth of the returned slots were a duplicate of another slot.

**Why it happens:** when a file is smaller than the tier-2 window, the tier-2 and tier-3 sliding
windows both degenerate to the whole file. Both get embedded, both get stored, both can be retrieved.
The tiers are meant to offer *different granularities of the same code*; for small files there is
only one granularity available, and nothing collapses the redundancy afterwards.

**Why it's worth fixing on its own:** it spends the context budget the packer in `core.py` exists to
protect, and it does so invisibly — the caller sees five results and gets four. It needs no eval to
justify, because returning the same bytes twice is not a ranking trade-off, it is waste. This is the
*cheap half* of what the 2026-07-27 search surfaced; the ranking half is [B-011](#b-011).

**The want:** dedupe on a content hash when assembling the final result list, keeping the hit from the
most specific tier. **No schema change is needed** — `chunks` already stores `tier`, `start_line`,
`end_line` and `text` (`db.py:167-177`), and `chunk_summaries` is already keyed on `MD5(chunk_text)`,
so the hash is computed at ingestion today and there is precedent for using it as an identity.

#### Two fixes, and they are not alternatives (2026-07-28)

**Read-time dedup** is the correctness half: ~5 lines over the ~150 candidates already in memory,
strictly local to the retriever, and it fixes **existing** indexes with no rebuild. Keep it
permanently even after the second fix, because it also catches byte-identical content in *different*
files, which no ingest-side rule will.

**Ingestion-time gating** is the efficiency half, proposed by @edb: when a file fits entirely inside
tier N's budget, tier N and tier N+1 both degenerate to the whole file — identical text, therefore
identical embedding — so skip the higher tier. Saves embedding time and index size. Three caveats
that make it the *second* step, not the first:

- **It does not fix existing indexes.** Unchanged files are never re-ingested (md5 match), so
  duplicates already written stay written. Unlike the ADR-026 scan-gate migration this one is *not*
  free — it needs a forced rebuild or a one-off sweep.
- **It makes the index heterogeneous.** Nothing may then assume every file has a tier-3
  representation. Check the eval harness's `tier_projection` and the `core.py` context packer before
  landing it.
- It is an optimization of a decision that has not been made yet — see [B-011](#b-011), which may
  change what tier membership is *for*.

---

<a id="b-011"></a>
### B-011 — Multi-tier RRF cannot reinforce, because the tiers are disjoint document sets

**Source:** same run as [B-010](#b-010), 2026-07-27 · root cause found 2026-07-28 ·
**Status:** shaped · **Size:** M

Across four unrelated queries on the freshly rebuilt index, **every** returned score was one of two
values — `0.0167` (`1/60`) or `0.0164` (`1/61`).

**The root cause is structural, not statistical.** The FAISS vector id is
`stable_id(tier_name, file_path, scope)` (`src/stable_id.py:40-55`) — **the tier name is the first
component of the hash.** So the same code, chunked at two granularities, is two different documents to
FAISS and therefore two different documents to RRF. A document exists in exactly one tier's index, by
construction. Cross-tier reinforcement is not merely absent in this sample: **it is impossible.**
`1/60` and `1/61` are the only two values the current fusion can ever emit at rank 0 and 1.

That means what the pipeline calls Reciprocal Rank *Fusion* is, in this configuration, a three-way
rank interleave. There is nothing to fuse, because no document is ever seen twice.

*(The first version of this item, written 2026-07-27, described this as "nothing happened to be
reinforced" and proposed tuning `k`. That misread the symptom as the cause. Retained here because the
correction is the useful part.)*

The ranking was still correct on all four queries — `scan_disk`, `get_stale_ids`,
`search_three_tier_rrf` and `run_incremental` each came back at rank 1 — which is why this is a
ranking-quality want and not a bug report. But the ordering is decided by tier iteration order, not by
score.

**Why it matters beyond aesthetics.** A flat score distribution gives every downstream signal nothing
to work with. This is the same signature already recorded for the graph layer, where structural
expansion turned out to be inert under RRF because the fused scores it was meant to reorder were
already indistinguishable — **that finding and this one now have the same explanation.** Any future
work that tries to *adjust* ranking (graph weighting, recency, tags) lands on the same flat surface.
This is upstream of all of it.

#### The shape of a fix (2026-07-28)

The retriever needs a **document identity independent of chunk id**. That single change subsumes
[B-010](#b-010): dedup and reinforcement are the same operation at two granularities — collapse
identical, reinforce overlapping. Two candidate keys, doing different jobs:

- **Content hash** — collapses byte-identical chunks. This is B-010, and it is the safe half.
- **Containment** — a tier-1 symbol hit and the tier-2/3 windows whose line range contains it are the
  same underlying code, so they should reinforce as one document. `chunks.start_line` /
  `chunks.end_line` already exist (`db.py:167-177`), so **this needs no schema change either** — only
  a query-time pass over the ~150 candidates already in memory, which is free at that size.

**The known hazard, and the reason this is an ADR and not a patch:** containment-based reinforcement
biases toward large files. A god object contributes many tier-1 symbol hits that all reinforce the
same tier-2/3 window, so it accumulates score for being *big* rather than relevant — the opposite of
what the tier design is for. Needs normalization, a cap, or a different key. That is a real decision
with real alternatives, which is what earns an ADR under
[`CONTRIBUTING.md` §4](../CONTRIBUTING.md#4-working-lists--backlog-roadmap-adrs).

Still open, and unaffected by the root cause above:
- Feed more than the top 1–2 per tier into the fusion, so ranks spread even without reinforcement.
- Reconsider `k=60` — the published default is tuned for fusing many *independent* systems; three
  tiers over one embedder is a different regime.
- Carry the dense similarity through as a tiebreak within an RRF tie rather than discarding it.
- Accept flat RRF and treat the top-k as an unordered candidate set, which is closer to how the
  consuming agent actually uses it.

**Sequencing:** B-010's read-time dedup ships first and independently — it is a strict improvement
needing no eval. Reinforcement is a ranking change, so it cannot be validated locally.

**Measurement caveat, per [`CONTRIBUTING.md` §4.2](../CONTRIBUTING.md#42-measurement-provenance--a-baseline-names-the-stack-it-was-measured-on):**
this observation is from `BAAI/bge-code-v1` (dim 1536), reranker off, `fusion_mode = "rrf"`, on a
98-file index of this repo. It is four queries on one small corpus — a real signal about the score
*distribution*, not a measurement of retrieval quality. Anything that changes fusion needs the ADR-007
harness, which needs the T4, which is GPU-gated.
