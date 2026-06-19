# ADR-014: Usage-Driven Adaptive Ranking — Learning From What Agents Actually Use

**Status:** proposed
**Date:** 2026-06-18
**Branch:** `feature/adr-014-usage-driven-adaptive-ranking`
**Reviewer:** @ethanblauw21
**Depends on:**
- ADR-007 — needs the **held-out evaluation split** to prove tuned weights beat static fusion *without overfitting* (a tuned ranker that wins on its training set proves nothing).
- ADR-009 — needs the **parameterized fusion stage** (the convex/weighted combination from P3); adaptive ranking learns *over* those weights, so they must be tunable rather than fixed RRF.
**Depended on by:** none yet.

> Source of record: [docs/adr-backlog.md](../adr-backlog.md) (ADR-014 bucket + build kit), suggestions S1,
> modernization P3 (Dynamic Alpha Tuning). Citations `[n]` index
> [references-code-intelligence.md](../references-code-intelligence.md): [15] ReFIT, [16], [17].
> **The most novel bucket — a potential original paper.**

## Context

The retrieval pipeline's fusion weights (ADR-009 P3) are *static*: a fixed blend of dense, sparse, and
reranker scores. But the engine has a signal it currently throws away — **which results agents actually
use.** When an agent retrieves 10 chunks and then reads/edits 2 of them, those 2 are implicit positive
relevance labels. Logging that feedback closes the loop: learn the fusion weights (and eventually adapt the
reranker) from real usage instead of guessing them once.

The research (suggestions S1; [15] ReFIT, [16], [17]; modernization P3's Dynamic Alpha Tuning) supports a
staged approach: start **label-free** — pseudo-relevance and weight tuning over logged outcomes — before any
heavyweight LoRA fine-tune of the reranker. Per-query weighting (Dynamic Alpha Tuning) is the natural
extension: different queries want different dense/sparse balances.

This is the **research-grade** bucket — the most novel, with original-paper potential — and correctly the
last wave: it needs the harness (ADR-007) to prove it works and the parameterized fusion (ADR-009) to have
weights worth learning.

## Decision

**Log retrieval outcomes as implicit relevance** and use them to tune the ADR-009 fusion weights (and,
optionally, LoRA-adapt the reranker), validated on an ADR-007 held-out split. Start label-free; escalate to
learned adaptation only if the simpler approach plateaus.

### §1 — Feedback logging (S1)

A new **feedback-log table** in `src/db.py` records, per retrieval: the query, the returned candidate set,
and which results were subsequently **used** (read/edited/cited). `src/MCPServer.py` captures the "used"
signal — and there are *two* open questions stacked here, not one:
- **What "used" means** (read vs. edited vs. cited carry different relevance strengths).
- **How the server even observes "used."** An MCP retrieval tool returns chunks, but the agent then
  reads/edits them in *its own* context, which the server does not directly see. Capture therefore needs
  either an **inferred** signal (a later tool call that touches the same file/symbol the retrieval surfaced)
  or an **explicit feedback call** from the agent — which of those is the load-bearing design choice.

The log is the training corpus; it is local, never leaves the machine.

### §2 — Weight tuning (start label-free)

Train fusion weights over the feedback log using **`scikit-learn`**, starting **label-free**: pseudo-relevance
(treat used results as positives, un-used-but-retrieved as weak negatives) to tune the convex-combination
weights ADR-009 P3 exposes. This is the cheap, robust first step — no model fine-tuning, just learning the
blend. **Per-query weighting** (Dynamic Alpha Tuning, modernization P3) is the next increment: learn a
weighting *function* of query features rather than one global blend.

### §3 — Optional reranker adaptation (LoRA)

Only **if weight tuning plateaus**, LoRA-adapt the reranker (`peft`) on the feedback log. This is gated
behind a demonstrated need — it's the heaviest option and the easiest to overfit, so it is explicitly *not*
the starting point.

### §4 — Validation on a held-out split (the anti-overfit gate)

Tuned weights must **beat static RRF/fusion on an ADR-007 held-out split** (a CoIR or usage holdout the
weights never saw). Two honest notes on the holdout: the **CoIR holdout inherits ADR-007 §9's coverage
limits** (Python/JS semantic retrieval only), so weights validated there are validated on that slice; the
**usage holdout** (a reserved fraction of the feedback log) is broader but only as representative as logged
usage. This is the load-bearing guard: usage-tuned ranking is prone to overfitting the logged distribution,
so a win only counts on held-out data. Offline training cadence (how often to retune) is an Open Question.

## Consequences

**Better:**
- Closes the loop: the engine learns from real agent behavior instead of static, hand-guessed weights —
  potentially the project's most novel, publishable contribution.
- Staged risk: label-free weight tuning is cheap and robust; LoRA is gated behind a demonstrated plateau, so
  the heavy/overfit-prone path is opt-in.
- Reuses ADR-009's parameterized fusion and ADR-007's harness/holdout — the learning target and the referee
  already exist.

**Worse:**
- Overfitting is the central risk: usage logs reflect *current* ranking, creating a feedback loop that can
  entrench it. The held-out gate (§4) is the mitigation, but it must be enforced, not assumed.
- "Used" is ambiguous (Open Question); a bad definition trains on noise.
- New dependencies (`scikit-learn`; optional `peft`) and a feedback-log table that grows over time and needs
  retention/training-cadence policy.
- A learned ranker is less transparent than static weights — harder to explain why a result ranked where it
  did.

**Neutral:**
- The feedback log is local and private; nothing is sent anywhere.
- Falls in the final wave; if the static ADR-009 stack is good enough, this can be deferred indefinitely
  without blocking anything (nothing depends on it).

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Keep static fusion weights | Discards a real signal (what agents use); leaves measurable ranking quality on the table. |
| Jump straight to LoRA reranker fine-tuning | Heaviest, most overfit-prone option; the research says start label-free with weight tuning and escalate only on a plateau. |
| Validate on the training distribution | Usage-tuned ranking trivially "wins" on its own logs; only a held-out split (ADR-007) proves a real gain. |
| Online/continuous learning from every query | Tight feedback loop entrenches current ranking and is hard to debug; offline retraining cadence is safer and auditable. |
| Global weights only (no per-query) | Misses that different queries want different dense/sparse balances (Dynamic Alpha Tuning); per-query weighting is the natural increment once global tuning works. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Feedback-log table in `src/db.py`; capture the "used" signal in `src/MCPServer.py` (define "used").
- [ ] Label-free weight tuning (`scikit-learn`) over the log → ADR-009 convex-fusion weights in `src/hybrid_retriever.py`.
- [ ] Per-query weighting (Dynamic Alpha Tuning) as the next increment.
- [ ] Validate: tuned weights beat static fusion on an ADR-007 held-out split (anti-overfit gate).
- [ ] (Optional, gated on plateau) LoRA reranker adaptation (`peft`).

**Notes:**
<!-- 2026-06-18: Wave 3, research-grade, most novel (potential original paper). Default: start label-free pseudo-relevance / weight tuning before any LoRA fine-tune. Done when tuned weights beat static RRF on a CoIR/usage holdout. Open: define "used" AND how the server observes it (inferred from later tool calls vs explicit feedback call); offline training cadence. Effort M–H. Nothing depends on it — deferrable. -->
