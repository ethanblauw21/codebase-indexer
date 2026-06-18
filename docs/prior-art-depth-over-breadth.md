# Depth over Breadth: The Case for Fewer Languages, Provable Accuracy

> **Status:** position / rationale document (not an ADR — it underpins ADR-004 and the
> [research-informed design proposal](./design-research-informed-improvements.md)).
> **Date:** 2026-06-18
> **Companion to:** [references-code-intelligence.md](./references-code-intelligence.md),
> [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md)

## The thesis

> Support **fewer languages**, but claim **higher accuracy**, and **prove it**. A high language count is a
> *recall* claim ("we can emit structure for N languages"); it says nothing about *precision* ("the structure we
> emit is correct"). The competitor's lead is breadth; ours is verified correctness — and the verification is
> the product, not an afterthought.

This document assembles the academic backing for that position. The short version: the trade-off we are choosing
is not a quirk of our roadmap — it is a **theorem-level constraint** that every code-analysis tool faces, and the
literature is explicit that the *only* way to substantiate an accuracy claim is curated, ground-truth-verified
benchmarking. That is exactly what our conformance suite is.

---

## 1. Why breadth and accuracy genuinely trade off (it's not a choice we invented)

**Rice's theorem** (ref [14]) makes any non-trivial semantic property of a program undecidable. For call graphs
this cashes out as a hard rule: you cannot have both **soundness** (resolve every real call) and **completeness/
precision** (emit no false edges). Every tool picks a point on that trade-off.

- The GNN call-graph paper (ref [8]) states it plainly: *"an ideal call graph should possess soundness … and
  completeness … However, Rice's theorem asserts that achieving both these properties is generally impractical,
  so existing analyses strive for reasonable trade-offs."*
- Industrial practice already takes our side: **WALA is configured to be "precise but incomplete"** (ref [8]) —
  i.e. it would rather omit an edge than emit a wrong one. That is the academic form of our **"prefer unknown
  over a wrong resolved edge"** principle, which ADR-004 operationalizes as the `candidate=True` contract and
  the verdict-tool gate ("insufficient — candidate-only").

**Implication for positioning:** a flat "150+ languages" (or the paper's actual 66) is a *high-recall, unstated-
precision* claim. It advertises the easy half of an unavoidable trade-off and stays silent on the hard half.

---

## 2. The competitor is the empirical proof of the trade-off

The Codebase-Memory paper (ref [1]) is the breadth-first system, and its own data is the cleanest evidence for
our thesis (full analysis: [study-codebase-memory-mcp.md](./study-codebase-memory-mcp.md)):

- **Macro-heavy C scores 0.58 vs 1.00** — *"because macros are not represented in the AST."* The language is in
  the count; the accuracy isn't there. *Can do it ≠ can do it well.*
- Their **6-strategy resolution cascade bottoms out at confidence 0.30–0.55** for cross-module references and
  dynamic dispatch — a built-in admission that many emitted edges are guesses, not facts.
- Their **own threats-to-validity** concede the proof is missing: *"31 languages but each represented by a single
  repository,"* and *"a systematic comparison against embedding-based RAG, ctags/LSP, and other graph systems
  remains future work."* They never measured per-language precision/recall against ground truth.
- Their accuracy numbers come from **grading by the first author** — not reproducible, not a benchmark.

So the competitor demonstrates the *cost* of breadth but never pays the *price* of proving accuracy. That gap is
our opening.

---

## 3. The literature says proof requires curated, verified ground truth — which is our conformance suite

This is the affirmative half, and it maps one-to-one onto what this project already does.

- **Total Recall? (ISSTA 2024, ref [2])** — the keystone. It exists *because* call-graph accuracy claims were
  going unverified. Its findings: generic test suites inadequately assess accuracy; you need systematically
  constructed corpora with **executed (dynamic) ground truth** to measure how much static extraction actually
  captures. Translation: breadth claims are meaningless until checked against known truth.
- **Judge / CATS (ISSTA 2019, ref [3])** — defines the method we use, in academic terms: *hand-crafted
  micro-benchmark suites of small programs, each crafted to exercise individual language features; run the
  analysis; check the resulting graph contains the expected calls.* **That is our golden-snapshot conformance
  fixture suite.** We can cite [3] to say our rigor is the established standard, and that a language without such
  a suite is unverified by definition.
- **Systematic unsoundness eval (ref [4])** and **Sui & Dietrich (ref [5])** — no mainstream tool (Soot, WALA,
  Doop) is sound across all features; soundness is a measured spectrum. This legitimizes a *tiered* model:
  honesty about where on the spectrum each language sits beats a binary "supported" claim.
- **Deblometer (ref [6])** — 59 curated cases with **manually curated ground truth** enabling precise precision/
  recall. Precedent that per-language curated truth is how the field proves correctness.
- **TraceEval (2026, ref [10])** — newest, multi-language, ground truth by **execution** not human judgment. The
  strongest "prove it" standard, and a template for an execution-verified accuracy layer above our snapshots.

---

## 4. How this maps to the existing design (ADR-004)

| Thesis element | Academic backing | Where it already lives |
|---|---|---|
| Fewer languages, but *proven* | Judge/CATS [3], Total Recall [2] | Tier-A conformance suite = curated feature-exercising micro-benchmarks |
| "Prefer unknown over a wrong edge" | Rice [14]; WALA "precise but incomplete" [8] | ADR-004 `candidate=True` contract + verdict gating |
| Support is a spectrum, not binary | Unsoundness evals [4][5] | ADR-004 Tier A / B / C model |
| Language count alone proves nothing | Total Recall [2]; competitor's own caveats [1] | ADR-004 positioning vs "undifferentiated 150-languages claim" |
| Which language to deepen next is *measured* | ground-truth precision as a signal [2][3] | ADR-005 `get_flagged_summary()` promotion backlog |

**The gap this exposes (and the design proposal addresses):** ADR-004 makes the *honesty* claim (tiers +
candidate edges), but the project does not yet **publish a precision/recall number per language** the way the
literature says you must to *prove* accuracy. Closing that — turning conformance from pass/fail snapshots into
reported precision/recall against curated (and ideally executed) ground truth — is what converts "we claim
accuracy" into "here is the measured accuracy, reproduce it." See
[design-research-informed-improvements.md](./design-research-informed-improvements.md) §B.

---

## 5. The one-paragraph pitch (for README / external use)

> Code structure extraction faces a theorem-level limit (Rice's theorem): no tool can be both *sound* (catch
> every relationship) and *precise* (emit no wrong ones). Breadth-first indexers chase soundness across hundreds
> of languages via generic grammar extraction and stay silent on precision — and where precision is measured,
> it collapses (a leading system scores 0.58/1.00 on macro-heavy C). We make the opposite, defensible choice:
> fewer languages, each with a **curated, ground-truth-verified conformance suite** (the methodology of Judge/CATS
> [ISSTA'19] and Total Recall [ISSTA'24]), reporting precision per language and **preferring an honest "unknown"
> over a confidently wrong edge**. We claim less, and we prove what we claim.

*(Citations refer to [references-code-intelligence.md](./references-code-intelligence.md).)*
