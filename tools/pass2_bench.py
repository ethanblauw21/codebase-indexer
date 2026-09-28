"""Time pass 2 (embedding) three ways on an already-built index (B-050, B-055).

Each arm rebuilds the repository into a throwaway index folder that starts with
only the real index's summary cache, so pass 1 finds every summary cached and
pass 2 does the same work in every arm: embed every chunk and every summary.
The real index is only read.

    old      one embed call per (file, tier) for code, then per tier for
             summaries: the pattern before B-050
    b050     256-text windows across files, embed and prepare back to back
    b055     256-text windows, the next window prepared while one embeds

Run from the repository whose index you want to time, with that repository's
indexer interpreter (the GPU one), and nothing else using the GPU:

    cd C:\\path\\to\\repo
    python C:\\Users\\edb\\Documents\\indexer\\tools\\pass2_bench.py
    python ...\\pass2_bench.py --arms b050,b055 --repeat 2

Each arm prints the indexer's own output, including its "Pass 2 timing" line;
the table at the end collects them. Arms run in the order given, so run a
repeat to see whether the first arm paid for loading the embedder.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import incremental_indexer as ii  # noqa: E402
import index_location  # noqa: E402
from db import CodeDB  # noqa: E402
from stable_id import TIER_CONFIGS  # noqa: E402

ARMS = ("old", "b050", "b055")
_TIMING_RE = re.compile(
    r"Pass 2 timing: (\d+) text\(s\) in (\d+) embed call\(s\): embed ([\d.]+)s, "
    r"waited ([\d.]+)s, prepare ([\d.]+)s, write ([\d.]+)s, wall ([\d.]+)s")


class _Tee(io.TextIOBase):
    """Print through to the console and keep a copy to parse."""

    def __init__(self, stream) -> None:
        self.stream, self.kept = stream, io.StringIO()

    def write(self, s: str) -> int:
        self.stream.write(s)
        self.kept.write(s)
        return len(s)

    def flush(self) -> None:
        self.stream.flush()


def _seed(scratch: str, source_db: str) -> int:
    """A fresh index database holding only the real index's summary cache."""
    with CodeDB(os.path.join(scratch, "graph.db")) as db:
        # Only read from; CodeDB's connection is not opened in URI mode, so no ?mode=ro.
        db._conn.execute("ATTACH DATABASE ? AS src", (source_db,))
        db._conn.execute("INSERT OR IGNORE INTO chunk_summaries SELECT * FROM src.chunk_summaries")
        db._conn.commit()
        db._conn.execute("DETACH DATABASE src")
        return db._conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0]


def _old_style_embed(real_embed, plans: list):
    """Split each file's single call into the calls made before B-050: one per tier
    of code, then one per tier of summaries (only with a window of one file)."""
    def embed(texts):
        plan = plans[-1]
        sizes = [len(plan.tier_texts.get(t, ())) for t, _, _ in TIER_CONFIGS]
        sizes += [len(plan.summary_items.get(t, ())) for t, _, _ in TIER_CONFIGS]
        assert sum(sizes) == len(texts), "the old arm needs a window of one file"
        import numpy as np
        parts, offset = [], 0
        for n in sizes:
            if n:
                parts.append(real_embed(texts[offset:offset + n]))
            offset += n
        return np.concatenate(parts)
    return embed


def run_arm(arm: str, repo: str, source_db: str) -> dict:
    scratch = tempfile.mkdtemp(prefix=f"pass2-{arm}-")
    saved = {k: getattr(ii, k) for k in
             ("INDEX_DIR", "DB_PATH", "_EMBED_WINDOW_TEXTS", "embed_overlap",
              "_embed_texts", "_prepare_file")}
    try:
        n_summaries = _seed(scratch, source_db)
        ii.INDEX_DIR, ii.DB_PATH = scratch, os.path.join(scratch, "graph.db")
        ii.embed_overlap = lambda: arm == "b055"
        if arm == "old":
            plans: list = []
            real_prepare = saved["_prepare_file"]

            def prepare(**kw):
                plans.append(real_prepare(**kw))
                return plans[-1]
            ii._prepare_file = prepare
            ii._EMBED_WINDOW_TEXTS = 1           # flush after every file
            ii._embed_texts = _old_style_embed(saved["_embed_texts"], plans)
        tee = _Tee(sys.stdout)
        started = time.monotonic()
        with contextlib.redirect_stdout(tee):
            print(f"\n===== arm {arm}: {n_summaries} cached summaries seeded into {scratch}")
            ii.run_incremental(repo, interactive=False)
        total = time.monotonic() - started
        m = _TIMING_RE.search(tee.kept.getvalue())
        if not m:
            raise RuntimeError(f"arm {arm}: no 'Pass 2 timing' line in the output")
        texts, calls, embed, waited, prep, write, wall = m.groups()
        return {"arm": arm, "texts": int(texts), "calls": int(calls), "embed": float(embed),
                "waited": float(waited), "prepare": float(prep), "write": float(write),
                "pass2": float(wall), "total": total}
    finally:
        for k, v in saved.items():
            setattr(ii, k, v)
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arms", default=",".join(ARMS), help="comma-separated: old,b050,b055")
    ap.add_argument("--repeat", type=int, default=1, help="run the whole set this many times")
    args = ap.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ARMS]
    if bad:
        ap.error(f"unknown arm(s): {', '.join(bad)}")

    repo = os.getcwd()
    source_db = os.path.join(repo, index_location.resolve_index_dir(repo), "graph.db")
    if not os.path.exists(source_db):
        sys.exit(f"No built index at {source_db}. Build it first (code-indexer).")
    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as con:
        cached = con.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0]
    if not cached:
        sys.exit("The index has no cached summaries, so every arm would summarize. "
                 "Build it with summarization on first.")

    results = [run_arm(a, repo, source_db) for _ in range(args.repeat) for a in arms]

    print("\narm    texts  calls   embed_s  waited_s  prepare_s  write_s  pass2_s  ms/text")
    for r in results:
        print(f"{r['arm']:<6} {r['texts']:>6} {r['calls']:>6} {r['embed']:>9.1f} "
              f"{r['waited']:>9.1f} {r['prepare']:>10.1f} {r['write']:>8.1f} {r['pass2']:>8.1f} "
              f"{1000 * r['pass2'] / max(r['texts'], 1):>8.1f}")


if __name__ == "__main__":
    main()
