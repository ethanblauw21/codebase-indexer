#!/usr/bin/env python
"""footprint_probe.py — memory after each import step, per OPENBLAS_NUM_THREADS (ADR-054, B-062).

Re-measures B-062's table with one command. Each setting runs in a fresh child
process, which imports the same modules in the order ``MCPServer.py`` meets them
and prints its memory after each step:

    Windows  PrivateUsage (commit charge) via GetProcessMemoryInfo — what B-062 measured
    Linux    VmSize and VmRSS from /proc/self/status. Linux overcommits, so neither
             is commit charge: VmSize counts reserved address space, VmRSS only
             the pages touched. Neither will match a Windows number.

``--search`` instead times flat FAISS search per setting, the check B-062 asked
for before shipping: a synthetic 50,000 x 1536 float32 IndexFlatIP, 100 queries,
top-50, both as one batch (which goes through BLAS) and one query at a time (what
a search does: one query per tier).

Usage:
    python tools/footprint_probe.py                    # settings: unset, 4, 1
    python tools/footprint_probe.py --threads unset 1
    python tools/footprint_probe.py --steps faiss core
    python tools/footprint_probe.py --search --threads unset 1

The ``unset`` child imports numpy/faiss directly, before ``core`` would set the
default, so it shows what the old order cost. Nothing here loads a model.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")

# The order MCPServer.py imports them in (B-062's table), plus numpy on its own.
STEPS = {
    "numpy": ["numpy"],
    "faiss": ["faiss"],
    "torch": ["torch"],
    "transformers": ["transformers", "sentence_transformers"],
    "core": ["core"],
}


def memory() -> dict:
    """This process's memory in GB: PrivateUsage on Windows, VmSize/VmRSS on Linux."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):     # PROCESS_MEMORY_COUNTERS_EX
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                    "PrivateUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                                          counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return {"PrivateUsage": counters.PrivateUsage / 2**30,
                "WorkingSet": counters.WorkingSetSize / 2**30}
    out = {}
    with open("/proc/self/status", encoding="utf-8") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            if key in ("VmSize", "VmRSS"):
                out[key] = int(value.split()[0]) / 2**20       # kB -> GB
    return out


def _child(steps: list[str]) -> None:
    """Import each step in turn and print one JSON line per step."""
    sys.path.insert(0, _SRC)
    print(json.dumps({"step": "bare python", **memory()}), flush=True)
    for step in steps:
        try:
            for module in STEPS[step]:
                __import__(module)
        except ImportError as exc:
            print(json.dumps({"step": step, "error": f"not installed ({exc.name})"}), flush=True)
            continue
        print(json.dumps({"step": f"import {step}", **memory(),
                          "torch_loaded": "torch" in sys.modules}), flush=True)
    try:        # not a dependency; sentence-transformers brings it in through scikit-learn
        from threadpoolctl import threadpool_info
    except ImportError:
        return
    # Which BLAS each library loaded and the threads it took. An OpenMP-threaded OpenBLAS
    # (faiss-cpu's on Linux) follows OMP_NUM_THREADS and ignores OPENBLAS_NUM_THREADS.
    for pool in threadpool_info():
        print(json.dumps({"blas": os.path.basename(pool["filepath"]), "api": pool["internal_api"],
                          "threads": pool["num_threads"],
                          "layer": pool.get("threading_layer") or "-"}), flush=True)


def _search_child(n: int, dim: int, queries: int, k: int, repeats: int) -> None:
    """Time flat search the way core sets faiss up (OMP pinned to 1); print one JSON line."""
    import time
    import numpy as np
    import faiss
    faiss.omp_set_num_threads(1)
    rng = np.random.default_rng(0)
    xb = rng.standard_normal((n, dim), dtype=np.float32)
    xq = rng.standard_normal((queries, dim), dtype=np.float32)
    index = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
    index.add_with_ids(xb, np.arange(n, dtype=np.int64))
    del xb
    index.search(xq[:2], k)                      # warm up

    def best(fn) -> float:
        times = []
        for _ in range(repeats):
            started = time.perf_counter()
            fn()
            times.append(time.perf_counter() - started)
        return min(times)

    batch = best(lambda: index.search(xq, k))
    single = best(lambda: [index.search(xq[i:i + 1], k) for i in range(queries)])
    print(json.dumps({"batch_s": batch, "one_at_a_time_s": single,
                      "env": os.environ.get("OPENBLAS_NUM_THREADS")}), flush=True)


def _run(setting: str, steps: list[str], search: bool = False) -> list[dict]:
    env = dict(os.environ)
    env.pop("OPENBLAS_NUM_THREADS", None)
    if setting != "unset":
        env["OPENBLAS_NUM_THREADS"] = setting
    child = ["--search-child"] if search else ["--child", *steps]
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), *child],
                          env=env, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        sys.exit(f"probe child for OPENBLAS_NUM_THREADS={setting} failed:\n{proc.stderr}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--threads", nargs="+", default=["unset", "4", "1"],
                    help='OPENBLAS_NUM_THREADS values to try; "unset" removes it')
    ap.add_argument("--steps", nargs="+", default=list(STEPS), choices=list(STEPS))
    ap.add_argument("--search", action="store_true", help="time flat FAISS search instead")
    ap.add_argument("--repeats", type=int, default=5, help="--search: best of this many runs")
    ap.add_argument("--child", nargs="*", help=argparse.SUPPRESS)
    ap.add_argument("--search-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child is not None:
        _child(args.child)
        return
    if args.search_child:
        _search_child(50_000, 1536, 100, 50, args.repeats)
        return
    if args.search:
        print(f"python {sys.version.split()[0]} on {sys.platform}, {os.cpu_count()} CPUs; "
              "flat IP search, 50,000 x 1536 float32, 100 queries, top-50, best of "
              f"{args.repeats}")
        for setting in args.threads:
            row = _run(setting, [], search=True)[0]
            print(f"  OPENBLAS_NUM_THREADS={setting:<6} batch of 100 {row['batch_s'] * 1000:8.1f} ms   "
                  f"one at a time {row['one_at_a_time_s'] * 1000:8.1f} ms")
        return

    print(f"python {sys.version.split()[0]} on {sys.platform}; memory in GB, cumulative per step")
    if sys.platform != "win32":
        print("Linux overcommits: VmSize is reserved address space and VmRSS touched pages. "
              "Neither is Windows commit charge.")
    for setting in args.threads:
        print(f"\nOPENBLAS_NUM_THREADS={setting}")
        for row in _run(setting, args.steps):
            if "blas" in row:
                print(f"  {row['api']:<8} {row['threads']:>3} threads ({row['layer']}) {row['blas']}")
                continue
            if "error" in row:
                print(f"  {row['step']:<22} {row['error']}")
                continue
            cols = "  ".join(f"{k} {v:6.2f}" for k, v in row.items()
                             if k not in ("step", "torch_loaded"))
            print(f"  {row['step']:<22} {cols}")


if __name__ == "__main__":
    main()
