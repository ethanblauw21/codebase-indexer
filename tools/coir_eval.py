#!/usr/bin/env python3
"""
CoIR retrieval harness (ADR-007) — atomic-doc projection (§7), the Wave-0 baseline.

Indexes CoIR's OWN corpus with the project's stack embedder (one embedding per
corpus doc), retrieves top-k per test query, and grades returned doc-ids against
CoIR qrels. It NEVER touches the repo .code-index (that would score ~0 — §7).

Per (subtask × config) it emits one JSON record to benchmarks/baseline.jsonl
(deduped on append, git-SHA stamped — §6) plus a human-readable table.

Metrics (§1):
  Quality      MRR@10, NDCG@10, Recall@{1,5,10}, Success@{1,5,10}, MAP
  Token econ.  query tokens, returned-context tokens, token-efficiency,
               budget-adherence + truncation rate, corpus-embedding tokens (separate)
  Operational  tool-calls/query (=1, schema parity), latency mean/p50/p95

Configs (§8):  dense  (embedder alone)   |   dense+reranker  (cross-encoder rerank)
               dense+sparse  (BM25 + convex fusion — ADR-009 §P3 validation arm)

Resumable: corpus embeddings are sharded to benchmarks/coir/<task>/_emb/ so an
interrupted 11h run resumes without re-embedding finished shards (§6 batchability).

Usage:
    python tools/coir_eval.py                         # Wave-0 core set, dense
    python tools/coir_eval.py --subtasks cosqa        # one subtask
    python tools/coir_eval.py --config dense+reranker # add the rerank pass
    python tools/coir_eval.py --config dense+sparse   # BM25 + convex fusion (P3)
    python tools/coir_eval.py --limit-queries 50      # smoke / CI tripwire
"""
import argparse
import gc
import json
import os
import random
import subprocess
import sys
import time
import tomllib
from collections import defaultdict

import numpy as np

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_BENCH = os.path.join(_ROOT, "benchmarks")
_COIR = os.path.join(_BENCH, "coir")

# The reranker scorer lives in src/ (the single canonical implementation, shared
# with the production retriever). Make it importable from this standalone script.
sys.path.insert(0, os.path.join(_ROOT, "src"))
from reranker import load_reranker  # noqa: E402
from fusion import tokenize, convex_fuse  # noqa: E402

CORE = [
    "cosqa",
    "stackoverflow-qa",
    "codefeedback-mt",
    "CodeSearchNet-python",
    "CodeSearchNet-javascript",
]

K = 10                  # metric depth
EMB_SHARD = 20_000      # docs per checkpoint shard
EMB_BATCH = 64          # encode batch size
BUDGET_TOKENS = 8_000   # §1 budget-adherence threshold for top-k returned context
PROGRESS_EVERY = 25     # log a progress line every N queries (the slow rerank path)


# ---------------------------------------------------------------------------
# Config / data loading
# ---------------------------------------------------------------------------

def load_config():
    path = os.path.join(_ROOT, "indexer.toml")
    cfg = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    emb = cfg.get("embeddings", {})
    rer = cfg.get("reranker", {})
    ret = cfg.get("retrieval", {})
    ev = cfg.get("eval", {})
    return {
        "model_id": emb.get("model_id", "jinaai/jina-embeddings-v2-base-code"),
        "max_seq_length": emb.get("max_seq_length", 512),
        "reranker_id": rer.get("model_id", "jinaai/jina-reranker-v2-base-code"),
        "dense_weight": float(ret.get("dense_weight", 0.7)),
        "sparse_weight": float(ret.get("sparse_weight", 0.3)),
        "budget_tokens": ev.get("budget_tokens", BUDGET_TOKENS),
        "tier_projection": ev.get("tier_projection", "atomic"),
        "rerank_depth": ev.get("rerank_depth", 100),
        "rerank_sample_queries": ev.get("rerank_sample_queries", 0),
        "sparse_sample_queries": ev.get("sparse_sample_queries", 0),
        "sample_seed": ev.get("sample_seed", 13),
    }


# ---------------------------------------------------------------------------
# Reranker (§8): the Qwen3 logit-scorer + load_reranker() factory live in
# src/reranker.py (the single canonical implementation, shared with production).
# Imported at the top of this module.
# ---------------------------------------------------------------------------


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_subtask(task):
    d = os.path.join(_COIR, task)
    corpus_ids, corpus_text = [], []
    for r in _read_jsonl(os.path.join(d, "corpus.jsonl")):
        corpus_ids.append(r["id"])
        corpus_text.append(r["text"])
    queries = [(r["id"], r["text"]) for r in _read_jsonl(os.path.join(d, "queries.jsonl"))]
    qrels = {}
    with open(os.path.join(d, "qrels.tsv"), encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            qid, cid, score = line.rstrip("\n").split("\t")
            if int(score) > 0:
                qrels.setdefault(qid, {})[cid] = int(score)
    return corpus_ids, corpus_text, queries, qrels


def git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Embedding (sharded, resumable)
# ---------------------------------------------------------------------------

def embed_corpus(model, task, texts, model_tag):
    """Embed corpus with checkpoint shards. Returns (matrix float32, total_tokens)."""
    emb_dir = os.path.join(_COIR, task, "_emb", model_tag)
    os.makedirs(emb_dir, exist_ok=True)
    tok_path = os.path.join(emb_dir, "tokens.txt")
    n = len(texts)
    shards = []
    total_tokens = 0
    have_tokens = os.path.exists(tok_path)

    for start in range(0, n, EMB_SHARD):
        end = min(start + EMB_SHARD, n)
        shard_path = os.path.join(emb_dir, f"shard_{start:08d}.npy")
        if os.path.exists(shard_path):
            shards.append(np.load(shard_path))
            continue
        vecs = model.encode(
            texts[start:end], batch_size=EMB_BATCH, normalize_embeddings=True,
            show_progress_bar=True, convert_to_numpy=True,
        ).astype(np.float32)
        np.save(shard_path, vecs)
        shards.append(vecs)
        print(f"    [{task}] embedded {end}/{n}", flush=True)

    if have_tokens:
        with open(tok_path) as f:
            total_tokens = int(f.read().strip())
    else:
        total_tokens = count_tokens(model, texts)
        with open(tok_path, "w") as f:
            f.write(str(total_tokens))

    stacked = np.vstack(shards)
    shards.clear()  # release individual shard arrays before returning the merged copy
    return stacked, total_tokens


def count_tokens(model, texts, max_len=None):
    """Token count under the model tokenizer, truncated to what is embedded."""
    tok = model.tokenizer
    if max_len is None:
        max_len = model.max_seq_length
    total = 0
    for i in range(0, len(texts), 512):
        enc = tok(texts[i:i + 512], truncation=True, max_length=max_len,
                  add_special_tokens=True)
        total += sum(len(ids) for ids in enc["input_ids"])
        del enc
    return total


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def dcg(gains):
    return sum(g / np.log2(i + 2) for i, g in enumerate(gains))


def score_query(ranked_ids, rel):
    """ranked_ids: top-K doc ids (best first). rel: {doc_id: gain}."""
    relset = set(rel)
    hits = [1 if d in relset else 0 for d in ranked_ids]
    n_rel = len(relset)

    mrr = 0.0
    for i, d in enumerate(ranked_ids):
        if d in relset:
            mrr = 1.0 / (i + 1)
            break

    # MAP
    num_correct, ap = 0, 0.0
    for i, d in enumerate(ranked_ids):
        if d in relset:
            num_correct += 1
            ap += num_correct / (i + 1)
    ap = ap / n_rel if n_rel else 0.0

    # NDCG@K
    gains = [rel.get(d, 0) for d in ranked_ids]
    ideal = sorted(rel.values(), reverse=True)[:len(ranked_ids)]
    idcg = dcg(ideal)
    ndcg = (dcg(gains) / idcg) if idcg else 0.0

    out = {"mrr@10": mrr, "ndcg@10": ndcg, "map": ap}
    for k in (1, 5, 10):
        topk = set(ranked_ids[:k])
        out[f"recall@{k}"] = len(topk & relset) / n_rel if n_rel else 0.0
        out[f"success@{k}"] = 1.0 if (topk & relset) else 0.0
    return out


# ---------------------------------------------------------------------------
# Run one subtask
# ---------------------------------------------------------------------------

def _ci95(values):
    """Half-width of the 95% confidence interval for the mean (normal approx).

    Returned alongside each sampled metric so a subsampled score is reported as an
    interval, never as false exactness. For a paired lift, pass the per-query
    differences and the CI accounts for the cancelled sampling noise automatically.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size < 2:
        return 0.0
    return 1.96 * float(arr.std(ddof=1)) / (arr.size ** 0.5)


def run_subtask(task, model, reranker, cfg, limit_queries, seed, config_name="dense"):
    import faiss

    use_sparse = config_name == "dense+sparse"
    # Both the reranker and the sparse-fusion arms produce a fused ranking measured
    # PAIRED against dense-only on the same queries (the diff cancels sampling noise).
    paired = reranker is not None or use_sparse

    t0 = time.time()
    corpus_ids, corpus_text, queries, qrels = load_subtask(task)
    # Gradable queries only (need a qrels entry), then an optional SEEDED RANDOM
    # sample. Random (not first-N) keeps the sample unbiased; the seed keeps it
    # reproducible. Sampling widens the CI but does not bias the score (§8 validity).
    gradable = [(qid, qt) for qid, qt in queries if qid in qrels]
    universe = len(gradable)
    sampled = bool(limit_queries) and limit_queries < universe
    queries = random.Random(seed).sample(gradable, limit_queries) if sampled else gradable
    tag = "reranked" if reranker is not None else ("fused" if use_sparse else "scored")
    extra = f" (sampled from {universe}, seed {seed})" if sampled else ""
    print(f"  [{task}] corpus={len(corpus_ids)} queries={len(queries)}{extra}", flush=True)

    model_tag = cfg["model_id"].replace("/", "__")
    mat, corpus_tokens = embed_corpus(model, task, corpus_text, model_tag)
    dim = mat.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(mat)
    # The sparse arm needs the corpus matrix for full-corpus cosine (mat @ qvec), so
    # keep it; otherwise free it (FAISS holds its own copy — can be several GB).
    if not use_sparse:
        del mat

    bm25 = None
    if use_sparse:
        from rank_bm25 import BM25Okapi
        print(f"  [{task}] building BM25 over {len(corpus_text)} docs...", flush=True)
        bm25 = BM25Okapi([tokenize(t) for t in corpus_text])
        dense_w, sparse_w = cfg["dense_weight"], cfg["sparse_weight"]

    id_arr = np.array(corpus_ids)
    rerank_depth = cfg["rerank_depth"] if paired else K

    per_q = defaultdict(list)        # headline metrics (reranked if reranker, else dense)
    per_q_dense = defaultdict(list)  # dense-only on the SAME queries → paired lift baseline
    n = 0
    lat = []
    q_tokens_total = 0
    ret_tokens_total = 0
    eff_total = 0.0
    eff_count = 0
    budget_ok = 0
    truncated = 0
    budget = cfg["budget_tokens"]
    loop_t0 = time.time()

    for qid, qtext in queries:
        ts = time.time()
        qvec = model.encode([qtext], normalize_embeddings=True,
                            convert_to_numpy=True).astype(np.float32)

        if use_sparse:
            # Dense via FAISS — the SAME tie-breaking the dense baseline uses, so the
            # paired dense arm reproduces it exactly (CoIR has many duplicate docs with
            # tied scores; a different tie order silently shifts MRR). BM25 supplies the
            # sparse signal; we fuse the union of each signal's top-N.
            D, I = index.search(qvec, rerank_depth)
            dense_pos = [int(i) for i in I[0] if i != -1]
            dense_score = {p: float(s) for p, s in zip(dense_pos, D[0])}
            bm25_all = np.asarray(bm25.get_scores(tokenize(qtext)), dtype=float)
            bm25_top = np.argsort(-bm25_all, kind="stable")[:rerank_depth]
            # Union, dense-ranked first so combined-score ties resolve toward dense order.
            cand = list(dict.fromkeys(dense_pos + [int(i) for i in bm25_top]))
            # Dense score: FAISS inner product for dense hits; cosine via mat for the
            # bm25-only candidates FAISS didn't return.
            dense_arr = [dense_score[c] if c in dense_score else float(mat[c] @ qvec[0])
                         for c in cand]
            combined = convex_fuse(dense_arr, [float(bm25_all[c]) for c in cand],
                                   dense_w, sparse_w)
            order = np.argsort(-combined, kind="stable")[:K]
            final_cand = [cand[o] for o in order]
            dense_cand = dense_pos[:K]
        else:
            _, idx = index.search(qvec, rerank_depth)
            cand = [int(i) for i in idx[0] if i != -1]
            dense_cand = cand[:K]
            if reranker is not None:
                pairs = [[qtext, corpus_text[c]] for c in cand]
                scores = np.asarray(reranker.predict(pairs, batch_size=32), dtype=float)
                # STABLE descending sort: ties keep dense order (cand is dense-ranked).
                # Plain argsort(scores)[::-1] reverses tie groups, sending the dense-#1
                # doc to the BOTTOM of its tie — catastrophic on CoIR's duplicate docs,
                # where the reranker scores identical text identically. Stable sort makes
                # reranking change order only on a real score difference.
                order = np.argsort(-scores, kind="stable")[:K]
                final_cand = [cand[o] for o in order]
            else:
                final_cand = dense_cand

        lat.append(time.time() - ts)
        rel = qrels[qid]

        ranked_ids = [id_arr[c] for c in final_cand]
        for kk, vv in score_query(ranked_ids, rel).items():
            per_q[kk].append(vv)
        if paired:
            for kk, vv in score_query([id_arr[c] for c in dense_cand], rel).items():
                per_q_dense[kk].append(vv)
        n += 1

        # token economy (on the returned top-K context)
        q_tokens_total += count_tokens(model, [qtext])
        ctx_texts = [corpus_text[c] for c in final_cand]
        ctx_tokens = count_tokens(model, ctx_texts)
        ret_tokens_total += ctx_tokens
        rel_retrieved = sum(1 for d in ranked_ids if d in rel)
        if rel_retrieved:
            eff_total += ctx_tokens / rel_retrieved
            eff_count += 1
        if ctx_tokens <= budget:
            budget_ok += 1
        else:
            truncated += 1

        if n % PROGRESS_EVERY == 0:
            el = time.time() - loop_t0
            rate = n / el if el else 0.0
            eta = (len(queries) - n) / rate if rate else 0.0
            print(f"    [{task}] {tag} {n}/{len(queries)}  "
                  f"({rate:.2f} q/s, elapsed {el/60:.1f}m, eta {eta/60:.1f}m)", flush=True)

    quality = {k: float(np.mean(per_q[k])) for k in per_q}
    quality_ci95 = {k: _ci95(per_q[k]) for k in per_q}
    lat_arr = np.array(lat)
    record = {
        "subtask": task,
        "config": config_name,
        "tier_projection": cfg["tier_projection"],
        "model_id": cfg["model_id"],
        "reranker_id": cfg["reranker_id"] if reranker is not None else None,
        "fusion_weights": ({"dense": dense_w, "sparse": sparse_w} if use_sparse else None),
        "rerank_depth": rerank_depth if paired else None,
        "git_sha": git_sha(),
        "corpus_docs": len(corpus_ids),
        "n_queries": n,
        "query_universe": universe,
        "sampled": sampled,
        "sample_seed": seed if sampled else None,
        "quality": {k: round(v, 4) for k, v in quality.items()},
        "quality_ci95": {k: round(v, 4) for k, v in quality_ci95.items()},
        "token_economy": {
            "query_tokens_mean": round(q_tokens_total / n, 1),
            "returned_context_tokens_mean": round(ret_tokens_total / n, 1),
            "token_efficiency": round(eff_total / eff_count, 1) if eff_count else None,
            "budget_tokens": budget,
            "budget_adherence": round(budget_ok / n, 4),
            "truncation_rate": round(truncated / n, 4),
            "corpus_embedding_tokens": corpus_tokens,
        },
        "operational": {
            "tool_calls_per_query": 1,
            "latency_ms_mean": round(float(lat_arr.mean()) * 1000, 1),
            "latency_ms_p50": round(float(np.percentile(lat_arr, 50)) * 1000, 1),
            "latency_ms_p95": round(float(np.percentile(lat_arr, 95)) * 1000, 1),
        },
        "wall_clock_s": round(time.time() - t0, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if paired:
        # Paired comparison: dense-only vs the fused arm (reranker or sparse) on the
        # SAME queries. The per-query difference cancels sampling noise, so the lift
        # CI stays tight even when n is small — that's why the headline lift is
        # trustworthy at n=500 despite the wider absolute-score CI.
        dense_quality = {k: float(np.mean(per_q_dense[k])) for k in per_q_dense}
        diffs = {k: np.asarray(per_q[k]) - np.asarray(per_q_dense[k]) for k in per_q}
        record["quality_dense_same_sample"] = {k: round(v, 4) for k, v in dense_quality.items()}
        record["lift"] = {k: round(float(diffs[k].mean()), 4) for k in diffs}
        record["lift_ci95"] = {k: round(_ci95(diffs[k]), 4) for k in diffs}
    return record


# ---------------------------------------------------------------------------
# Baseline append (dedupe on subtask×config — §6)
# ---------------------------------------------------------------------------

def append_baseline(record):
    path = os.path.join(_BENCH, "baseline.jsonl")
    existing = []
    if os.path.exists(path):
        existing = list(_read_jsonl(path))
    key = (record["subtask"], record["config"])
    existing = [r for r in existing if (r["subtask"], r["config"]) != key]
    existing.append(record)
    existing.sort(key=lambda r: (r["subtask"], r["config"]))
    with open(path, "w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def print_table(records):
    cols = ["mrr@10", "ndcg@10", "map", "recall@1", "recall@10",
            "success@1", "success@10"]
    w = 28
    print("\n" + "=" * 110)
    print(f"  CoIR semantic-retrieval baseline (atomic-doc, §7)  —  git {records[0]['git_sha']}")
    print("=" * 110)
    head = f"  {'subtask / config':<{w}}" + "".join(f"{c:>11}" for c in cols)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for r in records:
        label = f"{r['subtask']}/{r['config']}"
        q = r["quality"]
        line = f"  {label:<{w}}" + "".join(f"{q[c]:>11.4f}" for c in cols)
        print(line)
    print("=" * 110)

    # Sampling provenance + 95% CIs + paired reranker lift (only prints when relevant).
    print("  Sampling & confidence (± = 95% CI half-width; lift is paired dense->fused):")
    for r in records:
        label = f"{r['subtask']}/{r['config']}"
        nq = r.get("n_queries", 0)
        uni = r.get("query_universe", nq)
        ci = r.get("quality_ci95", {})
        q = r["quality"]
        prov = (f"sampled {nq}/{uni} (seed {r.get('sample_seed')})"
                if r.get("sampled") else f"all {nq} queries")
        print(f"    {label:<{w}} {prov}; "
              f"mrr@10={q.get('mrr@10', 0):.4f}±{ci.get('mrr@10', 0):.4f}, "
              f"ndcg@10={q.get('ndcg@10', 0):.4f}±{ci.get('ndcg@10', 0):.4f}")
        if r.get("lift"):
            lf, lci = r["lift"], r.get("lift_ci95", {})
            lift_label = "sparse lift" if r.get("config") == "dense+sparse" else "reranker lift"
            print(f"    {'':<{w}}   {lift_label}: "
                  f"mrr@10={lf.get('mrr@10', 0):+.4f}±{lci.get('mrr@10', 0):.4f}, "
                  f"ndcg@10={lf.get('ndcg@10', 0):+.4f}±{lci.get('ndcg@10', 0):.4f} (paired)")
    print("=" * 110)
    print("  Label: 'CoIR semantic-retrieval, {Python,JavaScript}' — NOT system accuracy (ADR-007 §9).")
    print("  C#/C++ and the structural-graph layer are unmeasurable on CoIR; covered by the planned internal-repo eval.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subtasks", default=",".join(CORE),
                    help="comma-separated subtasks (default: Wave-0 core set)")
    ap.add_argument("--config", choices=["dense", "dense+reranker", "dense+sparse"],
                    default="dense")
    ap.add_argument("--limit-queries", type=int, default=0,
                    help="cap queries/subtask via SEEDED RANDOM sample (0=all). Overrides "
                         "[eval].rerank_sample_queries; use for smoke/CI tripwire.")
    ap.add_argument("--seed", type=int, default=None,
                    help="random seed for query sampling (default: [eval].sample_seed)")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    cfg = load_config()
    tasks = [t.strip() for t in args.subtasks.split(",") if t.strip()]

    from sentence_transformers import SentenceTransformer
    print(f"Loading embedder: {cfg['model_id']}", flush=True)
    model = SentenceTransformer(cfg["model_id"], trust_remote_code=True)
    model.max_seq_length = cfg["max_seq_length"]

    reranker = None
    if args.config == "dense+reranker":
        reranker = load_reranker(cfg["reranker_id"])

    seed = args.seed if args.seed is not None else cfg["sample_seed"]
    # Sampling is opt-in: an explicit --limit-queries wins; otherwise the two heavy
    # arms self-sample for feasibility — the reranker per [eval].rerank_sample_queries,
    # the BM25 sparse-fusion arm per [eval].sparse_sample_queries (its per-query cost is
    # a full-corpus BM25 scan in pure Python, so large corpora are CPU-bound). The dense
    # baseline stays full-precision (all queries) unless the user caps it explicitly.
    limit = args.limit_queries
    if limit == 0 and args.config == "dense+reranker":
        limit = cfg["rerank_sample_queries"]
    elif limit == 0 and args.config == "dense+sparse":
        limit = cfg["sparse_sample_queries"]

    records = []
    for task in tasks:
        print(f"\n=== {task} [{args.config}] ===", flush=True)
        rec = run_subtask(task, model, reranker, cfg, limit, seed, config_name=args.config)
        path = append_baseline(rec)
        records.append(rec)
        print(f"  -> appended to {path}", flush=True)
        gc.collect()  # reclaim FAISS index, corpus arrays, and embeddings from prior subtask

    print_table(records)


if __name__ == "__main__":
    main()
