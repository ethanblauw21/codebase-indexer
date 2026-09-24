"""ADR-027 Verification 1: do batched summaries match batch-size-1 summaries?

Samples chunks from the repository at the working directory with a fixed seed,
summarizes them twice in one process (max batch 1, then the batched setting), and
reports the exact-match rate, timings, and every pair that differs. The differing
pairs are the part to read: a wording change is fine, lost or invented content is not.

GPU run. Load the summarizer the same way the indexer's worker does, in this process.

    python tools/summarizer_batch_equivalence.py [--n 200] [--seed 27] [--max-batch 16]
                                                  [--out tools/results/summarizer_batch_equivalence.json]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.getcwd(), "src"))

import config                                   # noqa: E402
import summarizer as sm                         # noqa: E402
from device import resolve_device               # noqa: E402
from incremental_indexer import chunk_all_tiers, scan_disk   # noqa: E402


def sample_chunks(n: int, seed: int) -> list[dict]:
    chunks = []
    for rel in sorted(scan_disk(os.getcwd())):
        try:
            with open(rel, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            continue
        for tier, items in chunk_all_tiers(rel, content).items():
            chunks.extend({"file": rel, "tier": tier, "text": c.text} for c in items)
    random.Random(seed).shuffle(chunks)
    return chunks[:n]


def timed(codes: list[str], max_batch: int, reserve_mb: int) -> tuple[list[str], dict, float]:
    # The worker carries its batch size between calls; each timed run starts fresh,
    # or the batch-1 run would leave the batched run starting at size 1.
    sm._w_next_batch, sm._w_next_streak = sm._START_BATCH_SIZE, 0
    start = time.time()
    results, stats = sm._worker_summarize(codes, sm._MAX_NEW_TOKENS, max_batch, reserve_mb)
    return results, stats, time.time() - start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=27)
    ap.add_argument("--max-batch", type=int, default=config.summarizer_max_batch_size())
    ap.add_argument("--out", default=os.path.join("tools", "results", "summarizer_batch_equivalence.json"))
    args = ap.parse_args()

    device = resolve_device()
    reserve = config.summarizer_vram_reserve_mb()
    chunks = sample_chunks(args.n, args.seed)
    codes = [c["text"] for c in chunks]
    print(f"{len(codes)} chunks, device={device}, max_batch={args.max_batch}, reserve={reserve} MiB", flush=True)

    sm._worker_init(config.summarizer_model_id(), device, "float16")
    one, one_stats, one_s = timed(codes, 1, reserve)
    print(f"batch 1:  {one_s:.1f}s  {one_stats}", flush=True)
    many, many_stats, many_s = timed(codes, args.max_batch, reserve)
    print(f"batched:  {many_s:.1f}s  {many_stats}", flush=True)

    diffs = [
        {**chunks[i], "text": chunks[i]["text"][:400], "batch_1": a, "batched": b}
        for i, (a, b) in enumerate(zip(one, many)) if a != b
    ]
    match = 1 - len(diffs) / max(1, len(codes))
    print(f"exact match {match:.1%} ({len(codes) - len(diffs)}/{len(codes)}), speedup {one_s / max(many_s, 1e-9):.2f}x")

    import torch
    report = {
        "git_sha": subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "model_id": config.summarizer_model_id(),
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "dtype": "float16",
        "max_new_tokens": sm._MAX_NEW_TOKENS,
        "n": len(codes), "seed": args.seed, "max_batch": args.max_batch, "reserve_mb": reserve,
        "batch_1": {"seconds": round(one_s, 1), "stats": one_stats},
        "batched": {"seconds": round(many_s, 1), "stats": many_stats},
        "exact_match": round(match, 4),
        "differing": diffs,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
