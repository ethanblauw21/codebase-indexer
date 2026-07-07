#!/usr/bin/env python3
"""ADR-019 — real-repo retrieval eval: the four-arm, multi-language scorecard.

Drives the REAL ``HybridRetriever`` (src/hybrid_retriever.py) against the indexes
built by ``tools/real_repo_prepare.py`` and grades its returned chunks against the
hand-authored gold fixtures in ``benchmarks/real_repo/fixtures/<repo>.jsonl``.

Four ablation arms (ADR-019 §3), each a toggle on the real pipeline — so the number
reflects what ships, not a re-implementation:

    arm  graph  rerank  fusion    equivalent production config
    A     off     off    rrf      (semantic only)
    B     on      off    rrf      today's default
    C     on      on     rrf      [reranker].enabled = true
    D     on      off    convex   [retrieval].fusion_mode = "convex"

Three paired lifts fall out, one per stalled decision (ADR-019 §5):
    graph    = B − A   (what the Traverse step earns; CoIR cannot measure it)
    reranker = C − B   (drives [reranker].enabled)
    sparse   = D − B   (drives [retrieval].fusion_mode; the literal-identifier re-test)

Lifts are PAIRED (same queries per arm) so sampling noise cancels; every mean carries
a 95%% CI half-width (eval_common.ci95). Metrics mirror ADR-007 verbatim for
cross-scorecard comparability. Results append (deduped, git-SHA stamped) to
``benchmarks/real_repo_baseline.jsonl``.

Usage:
    python tools/real_repo_eval.py                       # all prepared repos, all arms
    python tools/real_repo_eval.py --repos p-queue       # one repo
    python tools/real_repo_eval.py --arms A,B,D          # skip the heavy reranker arm
    python tools/real_repo_eval.py --limit 5 --verbose   # smoke
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_REAL = os.path.join(_ROOT, "benchmarks", "real_repo")
_MANIFEST = os.path.join(_REAL, "repos.toml")
# ADR-019 §6: git-ignored private-slice manifest, merged when present (see prepare tool).
_PRIVATE_MANIFEST = os.path.join(_REAL, "repos.private.toml")
_FIXTURES = os.path.join(_REAL, "fixtures")
_INDEX = os.path.join(_REAL, "index")
_BASELINE = os.path.join(_REAL, "real_repo_baseline.jsonl")

# Per-query progress log. Purely a side effect (never touches scoring). Each line is
# flushed to disk immediately, so a crash mid-run still leaves every completed query on
# disk and you can `tail -f` it to watch live ETA instead of waiting blind. Override the
# destination with EVAL_PROGRESS_LOG.
_PROGRESS_LOG = os.environ.get("EVAL_PROGRESS_LOG", os.path.join(_REAL, "eval_progress.log"))


def _progress(msg):
    """Append a timestamped, flushed progress line and echo it (unbuffered) to stdout."""
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    try:
        with open(_PROGRESS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except OSError:
        pass
    print(line, flush=True)

sys.path.insert(0, os.path.join(_ROOT, "src"))
from eval_common import score_query, ci95 as _ci95, git_sha as _git_sha, append_baseline as _append_baseline  # noqa: E402

# The reranker (arm C) is loaded inside each HybridRetriever construction — i.e. once
# per repo — and on CPU that reload dominates wall-clock. Cache the load so the 0.6B
# model is fetched once and reused across every repo/arm in a run. Patch the name in
# hybrid_retriever's namespace (where its _load_reranker resolves it).
import functools  # noqa: E402
import hybrid_retriever as _hr  # noqa: E402
_hr.load_reranker = functools.lru_cache(maxsize=4)(_hr.load_reranker)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Arm → pipeline toggles. Constructor args to HybridRetriever; the eval never edits
# indexer.toml, so a live production config can't skew a run (explicit args win).
ARMS = {
    "A": dict(graph_enabled=False, reranker_enabled=False, fusion_mode="rrf"),
    "B": dict(graph_enabled=True,  reranker_enabled=False, fusion_mode="rrf"),
    "C": dict(graph_enabled=True,  reranker_enabled=True,  fusion_mode="rrf"),
    "D": dict(graph_enabled=True,  reranker_enabled=False, fusion_mode="convex"),
}


def _auto_device():
    """Pick the torch device the eval should run the reranker on.

    Local dev has a CPU-only torch build → this resolves to ``cpu`` (unchanged
    behavior). On a GPU box (e.g. the cloud eval) it resolves to ``cuda`` so the
    reranker — the whole arm-C bottleneck — actually uses the accelerator instead of
    silently staying on CPU. Production ``HybridRetriever`` is untouched: it still
    defaults to ``cpu`` and only loads a reranker when ``[reranker].enabled``. Override
    with ``EVAL_DEVICE=cpu|cuda`` for a forced comparison.
    """
    forced = os.environ.get("EVAL_DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


_DEVICE = _auto_device()
# (label, minuend arm, subtrahend arm) — the three §5 paired lifts.
LIFTS = [("graph", "B", "A"), ("reranker", "C", "B"), ("sparse", "D", "B")]
LIFT_METRICS = ("mrr@10", "ndcg@10")  # the §5 gate metrics

_PART_RE = re.compile(r"_part_\d+$")


def _norm(scope):
    """Strip the chunk-split ``_part_N`` suffix so a symbol grades as one unit."""
    return _PART_RE.sub("", scope or "")


def _matches(scope, gold):
    """A returned chunk matches a gold FQN by normalized suffix.

    Robust across adapters: JS/Python scopes are ``file::Symbol`` while C#/C++ scopes
    are bare qualified names. Suffix match (not exact) lets gold be authored as the
    trailing symbol identifier (e.g. ``PQueue.add``), and the boundary check on
    ``endswith`` avoids ``.add`` spuriously matching ``.addAll``.
    """
    s = _norm(scope)
    return (s == gold or s.endswith("::" + gold)
            or s.endswith("." + gold) or s.endswith("/" + gold))


def load_manifest(path=_MANIFEST):
    import tomllib
    with open(path, "rb") as f:
        repos = tomllib.load(f).get("repos", [])
    # Merge the git-ignored private slice (§6) if present — same harness, clean code.
    if os.path.exists(_PRIVATE_MANIFEST):
        with open(_PRIVATE_MANIFEST, "rb") as f:
            repos = repos + tomllib.load(f).get("repos", [])
    return repos


def load_fixtures(repo_name, limit=0):
    """Read benchmarks/real_repo/fixtures/<repo>.jsonl → list of fixture dicts."""
    path = os.path.join(_FIXTURES, f"{repo_name}.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            rows.append(json.loads(line))
    if limit:
        rows = rows[:limit]
    return rows


def index_dir_for(repo_name):
    return os.path.join(_INDEX, repo_name)


def run_arm(repo_name, arm, fixtures, verbose=False):
    """Run every fixture query through one arm; return {qid: metrics} + rows.

    ranked ids are the returned chunks' ``scope`` strings (== ``symbols.fqn``), graded
    against the gold FQNs by exact match — both sides come from the same fqn formula.
    """
    from hybrid_retriever import HybridRetriever

    idx = index_dir_for(repo_name)
    db_path = os.path.join(idx, "graph.db")
    per_query = {}
    rows = []
    n = len(fixtures)
    t0 = time.time()
    _progress(f"{repo_name}/{arm}: start — {n} queries (device={_DEVICE})")
    with HybridRetriever(index_dir=idx, db_path=db_path, device=_DEVICE, **ARMS[arm]) as r:
        for i, fx in enumerate(fixtures):
            qid = fx.get("id", f"{repo_name}:{i}")
            gold = fx["gold"]
            rel = {g: 1 for g in gold}
            chunks = r.retrieve(fx["query"])
            # Map each returned chunk to the gold it matches (or a unique miss token),
            # preserving rank. Scope format is language-dependent (JS "file::Sym",
            # C# "Ns.Class.M/arity", plus "_part_N" chunk-split suffixes), so match by
            # normalized suffix rather than exact string.
            seen, ranked = set(), []
            for c in chunks:
                key = _norm(c.scope)
                if not key or key in seen:
                    continue
                seen.add(key)
                hit = next((g for g in gold if _matches(c.scope, g)), None)
                ranked.append(hit if hit else f"__miss__{len(ranked)}")
            m = score_query(ranked, rel)
            per_query[qid] = m
            rows.append({"qid": qid, "class": fx.get("class", "semantic"),
                         "feature": fx.get("feature", ""), "metrics": m})
            done = i + 1
            elapsed = time.time() - t0
            avg = elapsed / done
            eta = avg * (n - done)
            _progress(f"{repo_name}/{arm}: q {done}/{n}  elapsed {elapsed / 60:.1f}m  "
                      f"avg {avg:.1f}s/q  eta {eta / 60:.1f}m  mrr={m['mrr@10']:.3f}")
            if verbose:
                hit = "HIT " if m["mrr@10"] > 0 else "miss"
                print(f"    [{arm}] {hit} mrr={m['mrr@10']:.3f} ndcg={m['ndcg@10']:.3f}  {fx['query'][:60]}")
    _progress(f"{repo_name}/{arm}: done — {n} queries in {(time.time() - t0) / 60:.1f}m")
    return per_query, rows


def _mean(rows, metric, cls=None):
    vals = [r["metrics"][metric] for r in rows if cls is None or r["class"] == cls]
    return float(np.mean(vals)) if vals else 0.0


def _mean_ci(rows, metric, cls=None):
    vals = [r["metrics"][metric] for r in rows if cls is None or r["class"] == cls]
    return (float(np.mean(vals)) if vals else 0.0), _ci95(vals)


def paired_lift(minuend, subtrahend, metric):
    """Per-query diff (arm X − arm Y) on one metric → (mean, ci95, n)."""
    qids = [q for q in minuend if q in subtrahend]
    diffs = [minuend[q][metric] - subtrahend[q][metric] for q in qids]
    mean = float(np.mean(diffs)) if diffs else 0.0
    return mean, _ci95(diffs), len(diffs)


def evaluate(repo, arms, limit=0, verbose=False):
    """Evaluate one repo across the requested arms; return (arm_rows, records)."""
    name = repo["name"]
    lang = repo["language"]
    fixtures = load_fixtures(name, limit=limit)
    if not fixtures:
        print(f"  {name}: no fixtures — skipped")
        return None
    print(f"\n══ {name} ({lang}) — {len(fixtures)} queries, arms {','.join(arms)} ══")

    per_query = {}      # arm → {qid: metrics}
    arm_rows = {}       # arm → list of per-query rows
    for arm in arms:
        pq, rows = run_arm(name, arm, fixtures, verbose=verbose)
        per_query[arm] = pq
        arm_rows[arm] = rows

    records = []
    # Per-arm records (overall + per query class).
    for arm in arms:
        rows = arm_rows[arm]
        rec = {"repo": name, "language": lang, "arm": arm, "n": len(rows),
               "git_sha": _git_sha(_ROOT)}
        for metric in ("mrr@10", "ndcg@10", "map", "recall@1", "recall@5",
                       "recall@10", "success@1", "success@5", "success@10"):
            mean, ci = _mean_ci(rows, metric)
            rec[metric] = round(mean, 4)
            rec[f"{metric}_ci95"] = round(ci, 4)
        rec["by_class"] = {
            cls: {m: round(_mean(rows, m, cls), 4) for m in ("mrr@10", "ndcg@10")}
            for cls in sorted({r["class"] for r in rows})
        }
        records.append(rec)

    # Paired-lift records (only when both arms of the pair were run).
    for label, x, y in LIFTS:
        if x not in per_query or y not in per_query:
            continue
        rec = {"repo": name, "language": lang, "arm": f"lift:{label}",
               "pair": f"{x}-{y}", "git_sha": _git_sha(_ROOT)}
        for metric in LIFT_METRICS:
            mean, ci, n = paired_lift(per_query[x], per_query[y], metric)
            rec[metric] = round(mean, 4)
            rec[f"{metric}_ci95"] = round(ci, 4)
            rec["n"] = n
        records.append(rec)

    return {"name": name, "language": lang, "arm_rows": arm_rows,
            "per_query": per_query, "records": records}


def print_scorecard(evals):
    print("\n" + "=" * 72)
    print("REAL-REPO RETRIEVAL SCORECARD (real-repo retrieval, {langs})".format(
        langs=", ".join(sorted({e["language"] for e in evals}))))
    print("Contamination caveat: public repos may overlap model training data;")
    print("the private slice (§6) is the contamination-free control. Not 'true accuracy'.")
    print("=" * 72)
    for e in evals:
        print(f"\n{e['name']} ({e['language']})")
        print(f"  {'arm':<5} {'mrr@10':>10} {'ndcg@10':>12} {'success@5':>11}")
        for rec in e["records"]:
            if rec["arm"].startswith("lift:"):
                continue
            arm = rec["arm"]
            print(f"  {arm:<5} {rec['mrr@10']:>10.4f} {rec['ndcg@10']:>12.4f} "
                  f"{rec['success@5']:>11.4f}")
        for rec in e["records"]:
            if not rec["arm"].startswith("lift:"):
                continue
            lab = rec["arm"].split(":")[1]
            bits = " | ".join(
                f"{m} {rec[m]:+.4f} ±{rec[f'{m}_ci95']:.4f}" for m in LIFT_METRICS)
            print(f"    lift {lab:<9} ({rec['pair']}, n={rec.get('n', 0)}): {bits}")


def verdict(evals, private=False):
    """Print the §5 PASS/FAIL for the two ADR-009 decisions, pooled across repos.

    Clause 1: mean lift > 0 on BOTH mrr@10 and ndcg@10 with 95%% CI excluding zero.
    Clause 2: no target-language regression (no language's mean lift < 0 on mrr@10).
    Clause 3 (private slice) is checked by re-running this on the §6 slice — reported
    separately; a public PASS is necessary, not sufficient.

    ``private=True`` means the evaluated repos ARE the §6 contamination-free slice, so
    this run's clause-1/2 result IS the clause-3 confirmation — relabel accordingly.
    """
    print("\n" + "=" * 72)
    if private:
        print("§5 DECISION VERDICTS (§6 PRIVATE slice — this run IS clause 3)")
    else:
        print("§5 DECISION VERDICTS (public eval — clause 3 needs the private slice)")
    print("=" * 72)
    decisions = [("reranker", "[reranker].enabled", "reranker"),
                 ("sparse", "[retrieval].fusion_mode", "sparse")]
    for lift_label, flag, _ in decisions:
        # Pool per-query diffs across all repos for the CI; track per-language means.
        all_diffs = {m: [] for m in LIFT_METRICS}
        per_lang = defaultdict(lambda: [])
        pair = None
        for e in evals:
            x, y = next((a, b) for lbl, a, b in LIFTS if lbl == lift_label)
            pair = f"{x}-{y}"
            if x not in e["per_query"] or y not in e["per_query"]:
                continue
            px, py = e["per_query"][x], e["per_query"][y]
            for q in px:
                if q in py:
                    for m in LIFT_METRICS:
                        all_diffs[m].append(px[q][m] - py[q][m])
                    per_lang[e["language"]].append(px[q]["mrr@10"] - py[q]["mrr@10"])

        if not all_diffs["mrr@10"]:
            print(f"\n{flag}: arms {pair} not both run — no verdict")
            continue

        clause1 = True
        lines = []
        for m in LIFT_METRICS:
            mean = float(np.mean(all_diffs[m]))
            ci = _ci95(all_diffs[m])
            excludes_zero = (mean - ci) > 0
            ok = mean > 0 and excludes_zero
            clause1 = clause1 and ok
            lines.append(f"    {m}: {mean:+.4f} ±{ci:.4f}  "
                         f"{'CI>0 ✓' if excludes_zero else 'CI includes 0 ✗'}")
        regressions = [lg for lg, ds in per_lang.items() if float(np.mean(ds)) < 0]
        clause2 = not regressions

        overall = "PASS" if (clause1 and clause2) else "FAIL"
        scope = "§6 private" if private else "public"
        print(f"\n{flag}  (lift {lift_label}, {pair})  →  {overall} ({scope})")
        for ln in lines:
            print(ln)
        print(f"    no-regression: {'✓' if clause2 else '✗ ' + ','.join(regressions)}")
        if private:
            print("    → this IS clause 3: private slice "
                  f"{'AGREES with' if overall == 'PASS' else 'DISAGREES with'} the public verdict")
        else:
            print("    clause 3 (private slice): PENDING — run --repos on the §6 slice")


def main():
    ap = argparse.ArgumentParser(description="ADR-019 real-repo retrieval eval (arms A/B/C/D).")
    ap.add_argument("--repos", help="comma-separated repo names (default: all prepared)")
    ap.add_argument("--arms", default="A,B,C,D", help="comma-separated arms to run")
    ap.add_argument("--limit", type=int, default=0, help="cap queries per repo (smoke)")
    ap.add_argument("--baseline", default=_BASELINE, help="output baseline jsonl path")
    ap.add_argument("--no-write", action="store_true", help="don't append to the baseline")
    ap.add_argument("--verbose", action="store_true", help="per-query hit/miss lines")
    args = ap.parse_args()

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ARMS]
    if bad:
        raise SystemExit(f"Unknown arm(s): {bad}. Valid: {list(ARMS)}")

    manifest = load_manifest()
    want = set(args.repos.split(",")) if args.repos else None
    repos = [r for r in manifest if (want is None or r["name"] in want)]
    # A repo carrying `path` (not url+sha) is a §6 private-slice repo. If EVERY evaluated
    # repo is private, this run is the clause-3 confirmation, not the public eval.
    private_names = {r["name"] for r in manifest if r.get("path")}
    # Safety: the §6 private slice is "numbers only, never committed". Refuse to append a
    # private repo's records to the COMMITTED baseline even if --no-write was forgotten
    # (an explicit --baseline to a git-ignored path is still honored).
    writing_committed = os.path.abspath(args.baseline) == os.path.abspath(_BASELINE)

    evals = []
    for repo in repos:
        if not os.path.exists(os.path.join(index_dir_for(repo["name"]), "graph.db")):
            print(f"  {repo['name']}: not prepared (no index) — skipped")
            continue
        e = evaluate(repo, arms, limit=args.limit, verbose=args.verbose)
        if e:
            evals.append(e)
            repo_private = repo["name"] in private_names
            if repo_private and writing_committed and not args.no_write:
                print(f"  {repo['name']}: §6 private slice — NOT written to the committed "
                      f"baseline (numbers-only; use --baseline <ignored-path> to persist).")
            elif not args.no_write:
                for rec in e["records"]:
                    _append_baseline(rec, args.baseline, key_fields=("repo", "arm"))

    if not evals:
        raise SystemExit("No repos evaluated — prepare indexes + author fixtures first.")

    is_private = bool(evals) and all(e["name"] in private_names for e in evals)
    print_scorecard(evals)
    verdict(evals, private=is_private)
    if not args.no_write:
        print(f"\nBaseline updated: {os.path.relpath(args.baseline, _ROOT)}")


if __name__ == "__main__":
    main()
