#!/usr/bin/env python3
"""
CoIR data preparation — materialize queries + qrels (and any missing corpus) from
the locally cached CoIR-Retrieval HuggingFace datasets into benchmarks/coir/<task>/.

ADR-007 §5/§7: benchmarks/ holds the pinned CoIR subset the harness reads; the
runner then indexes CoIR's OWN corpus (atomic-doc projection) and grades the
returned doc-ids against these qrels. This script does the one-time extraction so
the runner is fully offline (Mantra 1) and the data is reproducible/auditable.

For each subtask we write, alongside the committed corpus.jsonl:
  corpus.jsonl  {"id","text","lang"}   (only re-written when missing — e.g. the
                                         large CodeSearchNet corpora are git-ignored)
  queries.jsonl {"id","text"}          (TEST-split queries only — the CoIR protocol)
  qrels.tsv     query_id<TAB>corpus_id<TAB>score  (TEST split, with header)

Usage:
    python tools/coir_prepare.py [--tasks cosqa,stackoverflow-qa,...] [--split test]

Requires the CoIR datasets to be present in the HF cache (run once online to pull;
thereafter HF_HUB_OFFLINE=1 is honoured).
"""
import argparse
import gc
import json
import os

# CoIR-Retrieval HF dataset name per subtask. The repo task name (left) maps to
# the HF dataset prefix (right); they differ only in casing for CodeSearchNet.
HF_PREFIX = {
    "cosqa": "cosqa",
    "stackoverflow-qa": "stackoverflow-qa",
    "codefeedback-mt": "codefeedback-mt",
    "CodeSearchNet-python": "CodeSearchNet-python",
    "CodeSearchNet-javascript": "CodeSearchNet-javascript",
    "CodeSearchNet-go": "CodeSearchNet-go",
    "CodeSearchNet-java": "CodeSearchNet-java",
    "CodeSearchNet-ruby": "CodeSearchNet-ruby",
    "CodeSearchNet-php": "CodeSearchNet-php",
}

CORE = [
    "cosqa",
    "stackoverflow-qa",
    "codefeedback-mt",
    "CodeSearchNet-python",
    "CodeSearchNet-javascript",
]

_HERE = os.path.dirname(__file__)
_BENCH = os.path.abspath(os.path.join(_HERE, "..", "benchmarks", "coir"))


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def prepare(task: str, split: str) -> None:
    from datasets import load_dataset

    prefix = HF_PREFIX[task]
    out_dir = os.path.join(_BENCH, task)
    os.makedirs(out_dir, exist_ok=True)

    qrels_ds = load_dataset(f"CoIR-Retrieval/{prefix}-qrels")
    if split not in qrels_ds:
        raise SystemExit(f"{task}: qrels has no '{split}' split (have {list(qrels_ds)})")
    qrels = qrels_ds[split]

    # qrels.tsv (graded split)
    qrels_path = os.path.join(out_dir, "qrels.tsv")
    with open(qrels_path, "w", encoding="utf-8") as f:
        f.write("query_id\tcorpus_id\tscore\n")
        for row in qrels:
            f.write(f"{row['query_id']}\t{row['corpus_id']}\t{row['score']}\n")

    # queries.jsonl — only the queries referenced by the graded split
    needed = set(qrels["query_id"])
    n_qrels = len(qrels)
    del qrels, qrels_ds  # no longer needed once needed-set is built

    qc = load_dataset(f"CoIR-Retrieval/{prefix}-queries-corpus")
    q_rows = [
        {"id": r["_id"], "text": r["text"]}
        for r in qc["queries"]
        if r["_id"] in needed
    ]
    del needed
    n_queries = len(q_rows)
    _write_jsonl(os.path.join(out_dir, "queries.jsonl"), q_rows)
    del q_rows

    # corpus.jsonl — only (re)write when missing; the small corpora are committed.
    corpus_path = os.path.join(out_dir, "corpus.jsonl")
    corpus_n = "(exists, kept)"
    if not os.path.exists(corpus_path):
        corpus_n = str(len(qc["corpus"]))  # capture before the generator consumes qc
        c_rows = (
            {"id": r["_id"], "text": r["text"], "lang": r["language"]}
            for r in qc["corpus"]
        )
        _write_jsonl(corpus_path, c_rows)
    del qc

    print(
        f"{task:28} corpus={corpus_n:>14}  queries={n_queries:6}  "
        f"qrels_rows={n_qrels:6}  -> {out_dir}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=",".join(CORE),
                    help="comma-separated subtask names (default: Wave-0 core set)")
    ap.add_argument("--split", default="test", help="qrels split to grade on (default: test)")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

    for task in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        if task not in HF_PREFIX:
            raise SystemExit(f"unknown task '{task}' (known: {', '.join(HF_PREFIX)})")
        prepare(task, args.split)
        gc.collect()  # reclaim HF dataset Arrow buffers before next task loads


if __name__ == "__main__":
    main()
