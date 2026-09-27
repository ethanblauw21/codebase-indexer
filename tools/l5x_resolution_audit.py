#!/usr/bin/env python3
"""Measure L5X read/write resolution at the adapter's own call sites (ADR-013 §5).

A permanent instrument, not a retiring diagnostic. Like `l5x_role_audit.py` it
cannot become a fixture: it needs a corpus of real controllers, and the only
corpus available is confidential.

## Why it exists

ADR-013 §5.0 retracted a "100.000% tag resolution" figure. The claim was not
false about what it measured -- it was false about what it was presented as
measuring. The script behind it counted **base operands at TAG positions**,
while the adapter additionally emits reads for identifiers inside array
subscripts and inside expression operands, and essentially every failure lives
there. A pooled rate hides that, so this tool reports the three sites apart.

The counters live on the extractor (`read_positions` / `read_resolved`), which
is what makes these numbers a claim about the shipping code rather than about a
second implementation of the same parse.

## Output

Counts and rates only. No tag names, routine names or rung text, so the output
is safe to paste into an issue or a commit message.

    python tools/l5x_resolution_audit.py <corpus-dir>
"""
from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.l5x_adapter import _Extractor  # noqa: E402

SITES = ("base", "subscript", "expression")


def measure(corpus: Path):
    """Run the shipping extractor over every export and total its counters."""
    reads: dict[str, int] = {s: 0 for s in SITES}
    resolved: dict[str, int] = {s: 0 for s in SITES}
    writes = writes_resolved = 0
    nested = scanned = 0
    files = 0

    # Deduplicated by resolved path: on a case-insensitive filesystem globbing
    # both spellings returns every file twice and doubles every count.
    paths = {p.resolve(): p
             for pat in ("*.L5X", "*.l5x")
             for p in corpus.rglob(pat)}

    for path in sorted(paths.values()):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        if root.attrib.get("TargetType") != "Controller":
            continue
        ex = _Extractor(str(path), text, root)
        ex.run()
        files += 1
        for site in SITES:
            reads[site] += ex.read_positions.get(site, 0)
            resolved[site] += ex.read_resolved.get(site, 0)
        writes += ex.write_positions
        writes_resolved += ex.write_resolved
        nested += ex.nested_instructions_skipped
        scanned += ex.instructions_scanned

    return files, reads, resolved, writes, writes_resolved, nested, scanned


def rate(num: int, den: int) -> str:
    return f"{100.0 * num / den:6.2f}%" if den else "     --"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("corpus", help="directory of .L5X exports to measure")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"not a directory: {corpus}", file=sys.stderr)
        return 2

    files, reads, resolved, writes, wres, nested, scanned = measure(corpus)
    if not files:
        print("no whole-controller L5X exports found", file=sys.stderr)
        return 2

    total = sum(reads.values())
    total_res = sum(resolved.values())

    print(f"whole-controller exports: {files}")
    print()
    print("read positions, by where the identifier came from")
    print(f"  {'site':<12}{'positions':>10}{'resolved':>10}{'rate':>9}")
    for site in SITES:
        print(f"  {site:<12}{reads[site]:>10}{resolved[site]:>10}"
              f"{rate(resolved[site], reads[site]):>9}")
    print(f"  {'TOTAL':<12}{total:>10}{total_res:>10}{rate(total_res, total):>9}")
    print(f"  unresolved: {total - total_res}")
    print()
    print(f"write positions: {writes}, resolved {wres} ({rate(wres, writes).strip()})")
    print()
    print(f"instructions scanned: {scanned}")
    print(f"  nested inside an enclosing operand, not rescanned: {nested}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
