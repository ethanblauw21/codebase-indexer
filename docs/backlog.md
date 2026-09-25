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
| [B-001](#b-001) | `IGNORE_DIRS` doesn't exclude Python virtualenvs — an in-tree `venv/` gets fully indexed | ADR-025 review, 2026-07-17 | S | **promoted → ADR-026** |
| [B-002](#b-002) | Summarizer model id is hardcoded while every other model reads `indexer.toml` | ADR-009/020 sweeps | S | **promoted → ADR-026** |
| [B-003](#b-003) | ~~Grow the private eval slice to de-noise the clean-TypeScript reranker signal~~ | ADR-019 §6 clause-3 FAIL, 2026-07-07 | M | **dropped** |
| [B-004](#b-004) | ~~Per-language reranking — the reranker helped Python and hurt TypeScript~~ | ADR-019 §6, 2026-07-07 | M | **dropped** |
| [B-005](#b-005) | Stale "150+ languages" / "ADR-004 tiers" pointers in the research docs | doc sweep, 2026-07-27 | S | raw |
| [B-006](#b-006) | Supply-chain release verification (SBOM, signing) | study §9.6 | L | raw · trigger-gated |
| [B-007](#b-007) | Verifiable retrieval — Merkle proofs over served index results | study §9.6 | L | raw · trigger-gated |
| [B-008](#b-008) | Indexer crashes on a Windows `cp1252` console before indexing a single file | found 2026-07-27 | S | shaped |
| [B-009](#b-009) | Eval result files don't record which models produced them | reranker provenance miss, 2026-07-27 | S | shaped |
| [B-010](#b-010) | The same chunk text is returned twice, as separate tier-2 and tier-3 hits | first live search on the rebuilt index, 2026-07-27 | S | shaped |
| [B-011](#b-011) | Multi-tier RRF **cannot** reinforce — the tier name is inside the FAISS id, so the tiers are disjoint document sets | same run, 2026-07-27 | M | shaped |
| [B-022](#b-022) | The summarizer runs one chunk at a time on the GPU | GPU baseline session, 2026-09-24 | M | **promoted → ADR-027** |
| [B-026](#b-026) | Class members lose their docs, private methods and getters from the index, and a method arrives without its class | ADR-030 p-queue diagnosis, grill + jury, 2026-09-25 | L | Stage 1 promoted → ADR-034 |
| [B-027](#b-027) | Whole-file chunks are 512-token-blind slices, so file-level retrieval rests on their summaries | same grill + jury, 2026-09-25 | L | raw |
| [B-028](#b-028) | Symbols that share an FQN leave ghost vectors: FAISS holds vectors whose text the database no longer has | jury review, counted 2026-09-25 | S | promoted → ADR-031 |
| [B-029](#b-029) | Parser and chunker changes never reach existing indexes: incremental re-indexing keys only on file content | jury review, 2026-09-25 | S–M | promoted → ADR-033 |
| [B-030](#b-030) | MCP search output stops at the first chunk that does not fit the token budget | jury review, 2026-09-25 | S | promoted → ADR-032 |
| [B-031](#b-031) | The embedder loads in fp32 and fills the 8 GB card on its own | ADR-028 gate, 2026-09-25 | S | promoted → ADR-035 |
| [B-025](#b-025) | Appended summaries make intent retrieval worse; the same summaries help when kept apart | retrieval check, 2026-09-25 | M | **promoted → ADR-030** |
| [B-032](#b-032) | A save during a running watchdog reindex starts a second reindex in parallel | daemon queue review, 2026-09-25 | S | shaped |
| [B-033](#b-033) | Two MCP servers on one project write the same index with no lock, and FAISS files are overwritten in place | daemon queue review, 2026-09-25 | M | shaped |
| [B-034](#b-034) | A changed file is re-embedded in full, even chunks whose text did not change | daemon queue review, 2026-09-25 | S–M | raw |

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
work, 2026-07-17 · **Status:** promoted → [ADR-026](./adr/ADR-026-configuration-authority.md) · **Size:** S

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

**Source:** noted in `src/CLAUDE.md` during the ADR-009/ADR-020 model sweeps · **Status:** promoted →
[ADR-026](./adr/ADR-026-configuration-authority.md) · **Size:** S

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

---

<a id="b-022"></a>
### B-022 — The summarizer runs one chunk at a time on the GPU

**Source:** GPU baseline session, 2026-09-24 · **Status:** promoted → [ADR-027](./adr/ADR-027-summarizer-adaptive-batching.md) · **Size:** M

With the summarizer on, a full index of this repository is dominated by summarization. On the
repaired 8 GB RTX PRO 1000, embedding 1,731 chunks took 106 s, while the summarization pass ran at
about 3 s per chunk and used 3.65 GB of the card. The isolated worker hardcodes `batch_size=1`
(`src/summarizer.py:116`), so most of the card and a good part of its compute sit idle.

**The want:** a faster summarization pass that stays safe on memory. If there is not enough free
memory, it should back off or pause rather than spill into system RAM, because on Windows that spill
does not raise an error, it just runs about 50 times slower.

Numbers 012 to 021 are used on other branches; this entry takes the next free number.

### B-025 — Appended summaries make intent retrieval worse; the same summaries help when kept apart

**Source:** retrieval check, 2026-09-25 · **Status:** **promoted → [ADR-030](./adr/ADR-030-separate-summary-index.md)** · **Size:** M

Each chunk is embedded as its code with the LLM summary appended. On 55 queries that describe what a function's body does, that scored 0.380 MRR@10 against 0.436 with no summaries, and on the original 83 queries it made no difference. The summary pulls the chunk toward its stated purpose, and for 68 to 77 percent of tier-2/3 chunks it falls past the embedder's 512-token window and is never seen.

The same summaries embedded on their own and fused by RRF with the code ranking scored 0.613 and 0.606 on the two sets, against 0.457 and 0.450 for code alone (`gpu-crash-repro/summary_store_eval.py`; ADR-027's log has the details).

**The want:** keep the summaries' value without their harm, and without paying the summarizer again for indexes that already have them cached.

### B-026 — Class members lose their docs, private methods and getters from the index, and a method arrives without its class

**Source:** ADR-030 p-queue diagnosis, a grill with @edb, and a five-reviewer jury, 2026-09-25 (`CHUNK_SHAPE_PLAN_REVIEW.md`) · **Status:** Stage 1 promoted → ADR-034 (`feature/adr-034-class-member-chunks`); Stage 2 shaped · **Size:** L

**Where it came from.** Under ADR-030's summary fusion, p-queue's original query set (24 queries)
scored 0.511 MRR@10, against 0.537 with no summaries. Three named queries lose: `pq-concurrency`,
`pq2-enqueue` and `pq2-on-error`; whether others moved as well is not yet tabulated. They lose to
the parts of `index.ts`'s class chunk (`PQueue_part_1..7`), which rank in both the code list and
the summary list. A fusion knob cannot fix it, because ADR-030 Verification 3 showed that
down-weighting whole-file chunks costs file-level questions as much as it gains on symbol ones.

**Provenance for every number here.**
- **Code:** `feature/adr-030-summary-index` at e1e9491, not merged. Line numbers below are at that
  commit.
- **Builds:**
  - `store` is ADR-030's variant: code-only vectors plus `summary.faiss`, with the summaries seeded
    from the batched build.
  - `none` has no summaries.
  - Both live under `gpu-crash-repro/telemetry/retrieval/<variant>/<repo>/`.
- **Harness:** the gitignored kit under `gpu-crash-repro/` (`retrieval_summaries.py`, plus the
  replay scripts behind `results_file_level.json`). The grader is `tools/real_repo_eval.py`,
  deduping on (file, scope without `_part_N`).
- **Stack:** bge-code-v1 in bf16, arm B (graph on, reranker off, RRF).
- **All numbers carry the ghost vectors of B-028.**

**What the code already does.**
- **Class chunks are already skeletons.** `skeletonize` (`_treesitter.py:33`) stubs member bodies
  with ` ...`, and TypeScript and Python share it (`ts_adapter.py:303`, `python_adapter.py:111`).
  C# and C++ have **separate** skeletonizers (`csharp_adapter.py:150`, `cpp_adapter.py:236`).
- **`PQueue`'s bloat is documentation, not code.** Its chunk has 48 stubs but still runs to 11,545
  characters in 7 parts. The bulk is JSDoc, and field docs too.
- **Where the parsers look for symbols.**
  - The TypeScript parser does not look inside *named* functions and methods (`ts_adapter.py:361`).
  - Its generic fallthrough (`ts_adapter.py:403`) **does** walk into anonymous callbacks. That is
    how helpers declared inside `it(() => …)` are extracted, with no parent. This corrects an
    earlier version of this item.
  - Python, C# and C++ do not look inside function bodies (`python_adapter.py:157`,
    `csharp_adapter.py:432`, `cpp_adapter.py:638`).

**The mechanism differs by query, so each needs its own proof before building.**
- **`pq-concurrency`: a name collision, not JSDoc.** Its gold symbol `PQueue.concurrency` has
  **no JSDoc**. `get concurrency()` and `set concurrency()` share one FQN, and only the setter is
  kept.
- **`pq2-on-error` and `pq2-enqueue`: possibly JSDoc bloat in the skeleton parts.** This is
  unproven.

**Defects, all data loss today, whatever else ships:**

1. **Member doc comments live only in the class skeleton.** A TypeScript `/** … */` is a sibling
   node of its `method_definition`, so the method chunk leaves it out. The JSDoc blocks in
   `index.ts` exist only in `PQueue_part_N`, so `@param` text never sits next to its body. C# `///`
   and C++ doc comments sit between members the same way. Python is not affected, because its
   docstrings are inside the body.
2. **Private `#` methods get no chunk and no call edges.**
   - `_TS_NAME_TYPES` (`ts_adapter.py:101`) lacks `private_property_identifier`. The skeleton
     stubs the bodies, so the bodies of `PQueue`'s 21 `#` methods and accessors exist in tier 1
     nowhere.
   - `_CALL_QUERY` (`ts_adapter.py:59-65`) matches only `property_identifier`, so
     `this.#x()` produces no call edge. Once `#` members are indexed, `find_dead_code` would call
     every one of them dead unless the call and reference queries are fixed in the same change.
3. **Arrow-function class fields are neither extracted nor stubbed.** `private doWork = () => {…}`
   is a `public_field_definition`, whatever its modifier. It is not extracted, and `skeletonize`
   finds no body inside it to stub. None occur in the three eval repos.
4. **Symbols sharing an FQN overwrite each other.** Only the last one's text survives
   (`db.py:745`, `symbols.fqn` UNIQUE at `db.py:135`), and every one of them leaves a vector
   behind (B-028). This comes in two kinds that need different fixes:
   - **Contiguous siblings of one concept:** a getter with its setter (p-queue: 1) and `@overload`
     sets (click has 36 `@overload` decorators in `src/`). These should merge.
   - **Scattered, unrelated redeclarations:** helpers with the same name declared in separate test
     callbacks (zustand: 92, e.g. `useBoundStore` 15 times in `tests/basic.test.tsx`). Merging
     these would stitch unrelated bodies into one chunk, so they must **not** merge. B-027's
     test-block scoping is what gives them distinct names.

**The want:** every member reachable on its own with its docs; a class skeleton that is small
because it holds signatures, not documentation; and a returned method that brings its class
context with it.

**Order of work:** B-028 → B-029 → Stage 1 → B-027 → any Stage 2 change to the schema or
`retrieve()`. Stage 2's display-only grouping may come before B-027 (see Stage 2).

**Before any ADR is written** (the jury's gate):
- **ADR-030:** merge it, or pin it at e1e9491 with the `store` artifact paths.
- **A per-query baseline:** tabulate all 24 p-queue original queries, with ranks under `none` and
  under `store`, and state how many changed.
- **A mechanism per losing query:** prove the mechanism for `pq2-on-error` and `pq2-enqueue` with
  a CPU probe on a hand-edited chunk set (docs moved, header repeated), before paying for a
  summary rebuild.
- **Write down the rules below:** scope, merge, doc-move and gate.

**Prerequisites measured, 2026-09-25** (pinned: `none031` / `store031`, ADR-030 + ADR-031 at
`feature/adr-030-summary-index` 56ea132, arm B, shipped weights, bge-code-v1 bf16; script
`pq_mechanism.py`, results `gpu-crash-repro/telemetry/retrieval/results_pq_mechanism.json`). The
numbers at the top of this item carried ghost vectors; these replace them.
- **Per-query baseline, 24 original p-queue queries:** MRR@10 0.555 without summaries, 0.554 with.
  9 queries improve with summaries, 4 get worse (`pq-add` 11→12, `pq-running-tasks` 6→13,
  `pq2-on-error` 1→9, `pq2-enqueue` 1→11), 10 stay at rank 1, and `pq-concurrency` is not found
  by either.
- **Probes** (tier-1 chunks of `index.ts` edited and re-embedded; summary vectors left as built;
  skeleton parts stripped in place, not re-split):

  | Probe | orig, no summaries | orig, summaries | intent (25), summaries | file (10) |
  |---|---|---|---|---|
  | as built | 0.555 | 0.554 | 0.619 | 0.883 |
  | JSDoc removed from the skeleton | 0.587 | 0.578 | 0.634 | 0.883 |
  | **+ each member's JSDoc in its own chunk** | **0.735** | **0.696** | **0.640** | 0.883 |
  | + class declaration repeated in each member | 0.640 | 0.598 | 0.609 | 0.883 |
  | getter and setter merged (alone) | 0.555 | 0.554 | 0.619 | 0.883 |

- **Moving docs onto members is proven:** +0.18 and +0.14 on the original set. With summaries,
  `pq-pause` goes 4→1, `pq-sizeby` 7→1, `pq2-is-rate-limited` 4→1, `pq-saturated` 2→1,
  `pq2-on-pending-zero` 9→2 and `pq2-pending` 10→4. The skeleton shrank by 31% (11,545 → 7,996
  characters).
- **Do not embed a repeated class header.** It costs 0.10 against doc-move alone, because every
  member then shares one long line. Stage 2's parent context stays display-only.
- **Mechanism per losing query, and none of them is JSDoc:**
  - `pq2-on-error` and `pq2-enqueue`: **fusion.** Each answer is rank 1 in the code list but
    rank 40 or absent in the summary list (the query is one word, "enqueue", and the summary says
    "adds a new item"). RRF sums credit, so chunks ranked moderately in *both* lists pass a
    one-list rank 1. Doc-move leaves them at 10 and 10. This is a question for ADR-030's fusion
    rule, not for chunk shape.
  - `pq-concurrency`: **a vocabulary gap.** The answer is not in the top 50 in any probe. Neither
    accessor has a doc comment; "limit how many tasks run at once" is written only in
    `options.ts`. Merging the getter back changes nothing, so the accessor merge is correct
    hygiene with no retrieval payoff here.
  - `pq-running-tasks` (6→13) and `pq-add` (11→12): whole-file and `Global_part_N` chunks that
    rank in both lists crowd them out. That is the same fusion effect, and part of B-027.
- The measurement stays the same for Stage 1's real build, which re-summarizes the changed
  chunks: the gate compares against these per-query ranks.

**Stage 1: parser fixes** (TypeScript and Python adapters plus `ast_chunker.py`).
- **Scope.** TypeScript and Python carry retrieval claims. C# and C++ are fixtures only: their
  forked skeletonizers keep docs inside the skeleton for now. Say so in the ADR, so the difference
  is not later mistaken for a regression.
- **Doc-move rule.**
  - A leading doc comment moves from the skeleton to its member **only when that member is emitted
    as a `Symbol` in the same parse.** Documented plain fields, index signatures, abstract members
    and overload signatures keep their docs in the skeleton.
  - The ADR defines "leading": the comment immediately before the member, allowing for decorators
    and at most one blank line.
  - Report each member's token count after the move. A moved doc must not push members past
    tier 1's 500 tokens into `_part_N`. The largest measured case is about 290–335 estimated
    tokens (`setPriority`, `onError`, `runningTasks`), so this needs a real token count.
- **`#` members.** Extract them with the `#` kept in the FQN (`PQueue.#tryToStartAnother`), and
  add `private_property_identifier` to the call and reference queries. `#` in an FQN is a public
  contract, so check that symbol lookups by FQN accept it.
- **Arrow fields.** Extract them, and teach `skeletonize` to find the body inside a field
  definition.
- **Merge policy.**
  - Only contiguous same-FQN siblings merge: accessor pairs and overload sets.
  - The implementation comes **first** and the stubs follow, so the implementation stays inside
    the embedder's 512-token window.
  - The merged symbol spans from the first line to the last.
  - The ADR names which declaration's type wins, because `symbol_types` holds one row per FQN.
  - Scattered same-FQN symbols are left alone. Their ghost vectors are B-028's fix.
- **Skeleton header, capped and measured.**
  - Every skeleton part repeats the class declaration, generics and heritage, with no decorators
    and no doc.
  - Each part records a body-only line range that is separate from the header's, so Stage 2 can
    pick the right part.
- **Fixtures (ADR-008) and unit tests:**
  - a documented plain field, whose doc stays in the skeleton;
  - a documented method, whose doc moves to it;
  - a decorated method;
  - a `#` method with a caller;
  - an arrow field, extracted and stubbed;
  - a getter/setter pair, giving one chunk and one symbol row;
  - Python `@property` with a setter;
  - Python `@overload`, implementation first;
  - scattered redeclarations: not merged, and no duplicate ids.
- **Measurement: one arm per change.**
  - Arms: docs moved alone; then `#` members and arrow fields added; then the merge; then the
    header.
  - Each arm is first a code-list-only arm on the CPU. It is diagnostic only, because it is biased
    against scopes that have no summary yet.
  - Then one full build that regenerates summaries on the GPU. It logs summary-cache hits and
    misses.
- **The Stage 1 gate.** Every criterion has a threshold; none is report-only.
  - Each named p-queue query ranks no worse than under `store`.
  - p-queue original is at or above 0.537.
  - In each set, at most 2 queries lose 3 or more ranks against `store`. The sets and their
    `store` scores are original 0.535, intent 0.555, file 0.811 (any) and file 0.500 (whole).
  - The means are reported with the paired CI95 (`tools/eval_common.py:84`) but not gated on it:
    at n = 24 the interval cannot resolve a 0.026 change.
  - The largest skeleton part is under a threshold set in the ADR.
  - B-029's chunker version is bumped.
  - The MCP Inspector run passes. It lists the tools, a search returns `#` FQNs, a lookup by a
    `#` FQN works, and errors come back as `isError`.

**Stage 2: class context in results.**
- **The parent is text, not an id.** Keep `class_context` or a `parent_fqn` text column on
  `symbols`, which the parser already computes. Never store a stable id: parts and tiers make it
  ambiguous.
  - `NULL` means "no extracted ancestor". A helper inside an `it(() => …)` callback has no parent
    today, and B-027 will change that for test files.
  - If a column is migrated, it must tell "never filled" apart from "no parent".
- **Grouping is MCP display only until B-027's FQN decision.**
  - Results are ordered by class. Each group sits at its best member's rank; within the group the
    member comes first, then its class skeleton, once per class. If the skeleton is too big, a
    header-only skeleton is used.
  - The skeleton part is picked by the body-only ranges from Stage 1.
  - Grouping inside `retrieve()` would change what the eval grades, so it waits for B-027.
- **B-030 lands first.** The MCP result loop must skip an oversized chunk instead of stopping at it.
- **The Stage 2 gate:**
  - the eval MRR is unchanged, as it must be for a display-only change;
  - for 10 fixed queries, every top-5 member is still present after grouping;
  - tokens per result list are reported before and after;
  - the MCP Inspector run passes, including an empty result and an oversized class.

**Depends on:** B-028 and B-029, and ADR-030 merged or pinned.

### B-027 — Whole-file chunks are 512-token-blind slices, so file-level retrieval rests on their summaries

**Source:** same grill and jury, 2026-09-25 · **Status:** raw · **Size:** L, likely to split into two or three items when shaped

Tier 2 cuts every file into 1,500-token slices and tier 3 into 4,000-token slices
(`stable_id.py:23-27`, `fallback_token_chunker` with `parent_scope="Full File"`). The embedder reads
only the first 512 tokens (`core.py:51`), so a tier-3 slice's code vector sees about an eighth of
its text. The file view is carried almost entirely by the slices' summaries: file questions scored
0.500 MRR@10 ("whole" grading, meaning only a tier-2/3 chunk of the gold file counts) with tier-2/3
summaries and under 0.10 without them (ADR-030 Verification 3). Their scopes are a bare
`Full File_part_N`, and tier 2 and tier 3 often hold identical text (B-010). Files with no symbols,
which is most test files, fall back to token slices even at tier 1 (`Global_part_N`).

**The want:** a file-level representation the embedder can actually read, that gives a file-level
question one strong target instead of N generic slices, and that does not flood symbol questions.

**Ideas from the grill, not yet decided:**
- **An outline chunk per file:** imports plus exported signatures, replacing the slices as the
  file's code vector. A god file, such as a barrel re-exporting 200 modules, needs a size cap or
  paging.
- **Test files:** treat `describe`/`it`/`test` blocks as symbols, but only those framework calls,
  not `useEffect` or `app.get`.
  - A `describe` skeleton keeps its setup and teardown bodies (`beforeEach` and the rest) and stubs
    the `it` bodies, so a single `it` is not stranded without its setup.
  - This also gives B-026's scattered test-helper collisions distinct names.

**Must be decided before this starts** (the jury's gate):
- **Test-block FQN syntax.** Test names contain spaces, quotes, `.` and `::`, which break
  `split(".")[-1]` (`hybrid_retriever.py:652`) and the grader's suffix match. Every test-file
  symbol will get a new id and a fresh summary.
- **A home for free-floating comments** (license text, region markers, TODOs). B-026 moves them out
  of the skeleton, and replacing the slices would remove them from the index entirely.
- **One summary per file versus one per slice.** One per file means summarizing input longer than
  the model's window. Choose page-and-merge or truncation, and cost it in GPU-minutes on the 8 GB
  card.
- **A B-029 version bump,** with a warning to users.

Measure on the file-level set, where 7 of the 15 gold files are tests or benchmarks. A global
fusion weight is ruled out, because it trades file questions against symbol ones (ADR-030
Verification 3).

**Measured 2026-09-25, on clean indexes (ADR-030 Verification 5, 40 file questions):**
- **The 512-token window is not the limit.** Embedding tier-2/3 slices with a 4,096-token window
  changed whole-file MRR by +0.001. The title's premise is wrong. Summaries are what carry
  file-level retrieval (whole 0.259 → 0.500 with them).
- **BM25 is a lead.** Convex BM25 fusion lifts whole-file MRR by +0.30 without summaries and
  +0.13 with them, but costs symbol questions 0.12 to 0.28. A file-level-only sparse signal, or
  routing by query type, may beat a new chunk shape.
- `file_chunk_weight` 0.75 still trades +0.03 to +0.04 on symbol questions for −0.28 on file
  whole, so the trade is confirmed with 40 file questions.

**Depends on:** B-026 Stage 1 shipped and gated, with its numbers as the baseline, and B-029.
Related: B-010.

### B-028 — Symbols that share an FQN leave ghost vectors: FAISS holds vectors whose text the database no longer has

**Source:** jury review of B-026, confirmed by counting, 2026-09-25 · **Status:** promoted → [ADR-031](adr/ADR-031-one-vector-per-chunk-row.md) · **Size:** S · **Do first** · line numbers at `feature/adr-030-summary-index` e1e9491

Ingest adds one vector per chunk with `add_with_ids` and does not dedupe ids
(`incremental_indexer.py:665`, and `:673` for the summary index). `IndexIDMap` accepts duplicate
ids. The database keeps only the last row per (file, scope, tier) (`INSERT OR REPLACE`,
`db.py:745`). So every same-FQN collision leaves an extra vector under an id whose text belongs to
another symbol: a query can match the getter's vector and return the setter's text.

Measured on the `store` builds (e1e9491):

| Repo | Tier-1 vectors | Tier-1 rows | Summary vectors | Chunk rows, all tiers |
|---|---|---|---|---|
| p-queue | 76 | 75 | 137 | 136 |
| zustand | 323 | 231 | 464 | 372 |
| click | 1,334 | 1,298 | 1,625 | 1,589 |

Tiers 2 and 3 match exactly, because their scopes are unique by construction. **Every ADR-030
measurement includes these ghosts.**

**Fix:**
- Dedupe `(tier, id)` within a file before `add_with_ids`, for the code and summary indexes,
  keeping the same record the database keeps (the last). Log how many were dropped.
- Add a test asserting FAISS `ntotal` equals the chunk-row count per tier. Add the same check to
  `index_status`.
- Rebuild the three eval indexes and re-measure ADR-030's `none` and `store` numbers, which
  become the baseline for B-026.
- Existing user indexes keep their ghosts until rebuilt, which is B-029's warning.

**Depends on:** nothing. Blocks B-026's baseline.

### B-029 — Parser and chunker changes never reach existing indexes: incremental re-indexing keys only on file content

**Source:** jury review of B-026, 2026-09-25 · **Status:** promoted → [ADR-033](adr/ADR-033-chunker-version.md) · **Size:** S–M · line numbers at e1e9491

`compute_diff` (`incremental_indexer.py:278`) marks a file modified only when its MD5 changes.
There is no chunker or parser version; `schema_version` (`db.py:456`) covers the table layout only.
After any parser change (B-026, B-027, B-028), unchanged files keep their old chunks, vectors and
summaries forever, and edited files get new ones: a mixed-generation index that says nothing.

**Fix:**
- Write a `chunker_version` into `index_meta`.
- **On a mismatch, warn; do not rebuild automatically.** Put the warning in the header of search
  output and in `index_status`, not as `isError`, and ask for an explicit full re-index. An
  automatic rebuild at MCP startup could block the first search for minutes, or for GPU-hours with
  summaries on, and a run cut off midway would leave a mixed index marked as clean.
- Write the new version only after a full build succeeds. Test it by killing a build midway and
  checking that the marker is unchanged.
- Consider building aside and swapping in.
- Each chunk-shape change bumps the version in its own commit.

**Depends on:** nothing. Blocks B-026 Stage 1 and B-027.

### B-030 — MCP search output stops at the first chunk that does not fit the token budget

**Source:** jury review of B-026, confirmed in code, 2026-09-25 · **Status:** promoted → [ADR-032](adr/ADR-032-search-budget-skips-oversized.md) · **Size:** S · line numbers at e1e9491

`semantic_code_search` formats results into a 4,000-token budget and **`break`s** at the first chunk
that does not fit (`MCPServer.py:100-111`). One large chunk, such as a big skeleton part or a
member with a long docstring, hides every result ranked below it behind a single "truncated" note.
B-026 makes this likelier, because moved docs enlarge members and Stage 2 puts skeletons at the
head of groups.

**Fix:** skip an oversized chunk and continue. Say how many were skipped. Optionally shorten an
oversized chunk to its header or signature rather than dropping it. Test: a result list with one
oversized chunk in the middle still shows everything after it.

**Depends on:** nothing. Blocks B-026 Stage 2.

### B-031 — The embedder loads in fp32 and fills the 8 GB card on its own

**Source:** ADR-028's gate and the GPU work of 2026-09-24/25 · **Status:** promoted → ADR-035 · **Size:** S

`core._get_embed_model` (`core.py:66-84`) builds `SentenceTransformer(model_id, trust_remote_code=True,
device=device)` with no dtype, so `bge-code-v1` (1.5B parameters) loads in fp32: about 6.2 GB of an
8 GB card. That leaves no room for ADR-027's 1 GB reserve, let alone the summarizer, and WDDM pages
to system RAM silently instead of raising an OOM.

- **Every measurement since 2026-09-24 already embedded in bf16.** The eval kit patches
  `core.SentenceTransformer` to pass `torch_dtype=bfloat16` for index and queries alike, and the MCP
  Inspector wrapper does the same. Production is the one path that doesn't.
- **ADR-028 is gated on this.** Its log says the host doesn't get turned on for real until it lands.
- **CPU stays fp32.** bf16 matmuls on most CPUs are slower, not faster.
- **Open question:** an index built in fp32 and queried in bf16 mixes precisions. How much that moves a
  cosine should be measured, not assumed.

**Depends on:** none. **Blocks:** ADR-028.

### B-032 — A save during a running watchdog reindex starts a second reindex in parallel

**Source:** review of the watchdog and daemon queue with @edb, 2026-09-25 · **Status:** shaped · **Size:** S · line numbers at `master` 0c8d7a6

`_ReindexDebouncer` (`MCPServer.py:1878-1913`) collapses a burst of watchdog events into a single
`run_incremental` after 3 s of quiet. A formatter run or a branch switch counts as one burst, which
is the event-storm half of the problem, and it is handled.

**The gap is overlap.**
- `_fire` clears the timer and then runs the whole reindex in the timer's thread.
- An event that arrives during that run schedules a new timer. That timer fires 3 s later and
  starts a **second** `run_incremental` while the first is still running.
- Nothing guards it: `_lock` covers only the timer.
- The two runs race on SQLite writes, on `MultiIndexManager.save_all`, and, with summaries on, for
  the GPU.

**Fix:**
- A running flag. An event during a run sets "dirty" instead of starting a timer, and the run
  schedules one follow-up when it finishes and finds it set.
- Test: a slow fake `run_incremental` plus events during it gives exactly two sequential runs, never
  two at once.

**Depends on:** none. Related: B-033, the cross-process version of the same race; ADR-028 §5, save
path through the host.

### B-033 — Two MCP servers on one project write the same index with no lock, and FAISS files are overwritten in place

**Source:** review of the watchdog and daemon queue with @edb, 2026-09-25 · **Status:** shaped · **Size:** M

Each MCP server process loads its own copy of the FAISS indexes (`MultiIndexManager.load_or_create`,
`core.py:180`) and starts its own watchdog (`MCPServer.py:1995`).
- **Within one process, reloads are safe.** `_reload_indexes` builds new objects and swaps them
  under `_reload_lock`.
- **Across processes, nothing coordinates.** Two agents each with an MCP server on the same project
  means two watchdogs and two writers.
- **SQLite is not the problem.** WAL is on (`db.py:96`), so readers are never blocked by a writer.
  Two writers still serialize and can time out.
- **FAISS is the problem.**
  - `save_all` calls `faiss.write_index` straight onto the live file (`core.py:198-200`). A process
    loading it at that moment can read a truncated index.
  - Two writers can each save a version built from different diffs, so the last one to save silently
    drops the other's vectors. The SQLite rows would then disagree with FAISS, which is the
    ghost-vector class of bug ADR-031 removed.

**ADR-028 does not cover this.** Its host owns the models only, and by design "never opens a
project's FAISS or SQLite files".

**Fix:**
1. **One writer per project.** Take an index lock file (`.code-index/write.lock`, `msvcrt.locking`
   / `flock`, as ADR-028's `host.lock` does) around `run_incremental`. A second server's watchdog
   skips the run, or waits for the lock, and just reloads afterwards.
2. **Atomic saves.** Write `name.faiss.tmp`, then `os.replace` it onto `name.faiss`, so a reader
   sees the old file or the new one, never half of one.
3. **Reload on change.** A server that did not write notices the files changed (mtime, or a
   generation counter in `index_meta`) and runs `_reload_indexes` before its next search.

**Depends on:** none. It becomes more pressing once the watchdog daemon runs live with several
agents on one repo.

### B-034 — A changed file is re-embedded in full, even chunks whose text did not change

**Source:** review of the watchdog and daemon queue with @edb, 2026-09-25 · **Status:** raw · **Size:** S–M

Change detection works per file: an MD5 of the file's content (`compute_diff`,
`incremental_indexer.py:278`). A modified file has all its chunks removed and every chunk embedded
again, in every tier.
- **Line numbers don't drive any of this.** Chunk ids are `stable_id(tier, file, scope)`, and
  `start_line`/`end_line` are provenance only. A line inserted near the top of a file changes no id.
- **Summaries, the expensive step, are already reused.** The summary cache is keyed by an MD5 of the
  chunk's text (`chunk_text_hash`), so an unchanged method keeps its summary even when its lines
  shift.

**What is left:**
1. **Tier-1 embeddings.**
   - Skip re-embedding a chunk whose text hash is unchanged: reuse its vector and update only its
     line range. That needs vectors to be retrievable by id (`IndexIDMap` over `IndexFlat` can
     `reconstruct`) or cached by text hash.
   - Embedding is the cheap step: about 1.5 min for this whole repository on the GPU, against about
     16 min for summaries. So the gain is on large files that are saved often, under the daemon.
2. **Tier-2/3 slices.**
   - These are fixed token windows, so one inserted line changes the text of every slice after it.
     Their summaries and embeddings are then all redone.
   - That is the unmerged `feature/adr-029-position-independent-summary-key` branch's territory, and
     B-027's (whole-file chunk shape).
   - A structural outline chunk (B-027) would make this item mostly disappear for tiers 2 and 3.

**Depends on:** B-027 for tiers 2 and 3. Tier 1 can go alone.
