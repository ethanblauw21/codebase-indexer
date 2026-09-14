#!/usr/bin/env python3
"""Find Add-On Instructions that claim one revision and carry different logic.

The failure this exists for, from a practicing controls engineer:

    "We found a couple PLCs that had slight differences in the same version, so
    someone changed it, and didn't rev it or make comments on the AOI for what
    they changed. We couldn't tell what project had the master, vs the modified
    one, as we have no naming convention for someone making customizations to an
    AOI."

Declared revision is a **claim**. This checks it against the logic, by hashing
each definition and grouping definitions that share a name and a revision. Where
one (name, revision) yields more than one hash, somebody edited a copy in place.

Nothing here is ranked, embedded, or retrieved -- it is a group-by over an
extraction the adapter already performs, so it needs no gold queries and no GPU.
See docs/l5x-retrieval-requirements.md F-4.

## What is hashed, and why separately

`signature` is the ordered parameter list -- name, datatype, usage, required --
excluding the implicit EnableIn/EnableOut. `logic` is the rung text of every
routine in document order. They are hashed apart because a changed parameter
list is a different and worse problem than a changed rung: it breaks every call
site positionally, and one combined hash could not tell you which you had.

Comments are excluded from the logic hash -- re-wording a comment is not an edit
to the logic -- but a comment-only difference is still reported, because on this
evidence it means two people touched the same definition.

Normalisation matters more than it looks: whitespace, attribute order and the
UTF-8 BOM all vary between exports, and ADR-013 already records the BOM as a
stable-ID hazard. Everything is whitespace-collapsed before hashing.

## Redaction

`--redact` replaces AOI and file names with stable placeholders. The corpus this
was built against is live customer control logic and AOI names ride in rung text
as their own mnemonic -- the exact leak ADR-013's redactor was rewritten to close.

    python tools/l5x_aoi_drift.py <dir> [--redact] [--all]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.l5x_adapter import _text_of  # noqa: E402

IMPLICIT = frozenset({"EnableIn", "EnableOut"})


def _h(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _norm(s: str) -> str:
    return " ".join(s.split())


def fingerprint(aoi) -> dict:
    """Signature, logic and comment hashes for one AOI definition."""
    sig: list[str] = []
    for p in aoi.iter("Parameter"):
        name = p.attrib.get("Name")
        if not name or name in IMPLICIT:
            continue
        sig.append("|".join((
            name,
            p.attrib.get("DataType", "?"),
            p.attrib.get("Usage", "Input"),
            p.attrib.get("Required", "false"),
        )))

    logic: list[str] = []
    comments: list[str] = []
    rungs = 0
    for r in aoi.iter("Routine"):
        logic.append(f"@routine:{r.attrib.get('Name', '?')}:{r.attrib.get('Type', 'RLL')}")
        for rung in r.iter("Rung"):
            body = _norm(_text_of(rung.find("Text")))
            if not body:
                continue
            rungs += 1
            logic.append(body)
            comments.append(_norm(_text_of(rung.find("Comment"))))

    return {
        "params": len(sig),
        "rungs": rungs,
        "sig_hash": _h(sig),
        "logic_hash": _h(logic),
        "comment_hash": _h(comments),
    }


def collect(corpus: Path) -> dict[str, list[dict]]:
    """name -> one record per definition found, across every export."""
    found: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(corpus.glob("*.L5X")):
        root = ET.parse(path).getroot()
        # An invocation appears in rung text as the AOI's own mnemonic, which is
        # how a call site is counted without resolving anything.
        text = "".join(
            _text_of(rung.find("Text"))
            for rung in root.iter("Rung")
        )
        for aoi in root.iter("AddOnInstructionDefinition"):
            name = aoi.attrib.get("Name")
            if not name:
                continue
            rec = fingerprint(aoi)
            rec["file"] = path.name
            rec["revision"] = "{}.{}".format(
                aoi.attrib.get("Revision", "?"),
                aoi.attrib.get("RevisionExtension", ""),
            ).rstrip(".")
            rec["calls_here"] = text.count(name + "(")
            found[name].append(rec)
    return found


def classify(records: list[dict]) -> str:
    """DRIFT / SIGNATURE DRIFT / COMMENT DRIFT / consistent / single."""
    by_rev: dict[str, set] = defaultdict(set)
    sig_by_rev: dict[str, set] = defaultdict(set)
    com_by_rev: dict[str, set] = defaultdict(set)
    for r in records:
        by_rev[r["revision"]].add(r["logic_hash"])
        sig_by_rev[r["revision"]].add(r["sig_hash"])
        com_by_rev[r["revision"]].add(r["comment_hash"])

    if any(len(v) > 1 for v in sig_by_rev.values()):
        return "SIGNATURE DRIFT"
    if any(len(v) > 1 for v in by_rev.values()):
        return "DRIFT"
    if any(len(v) > 1 for v in com_by_rev.values()):
        return "COMMENT DRIFT"
    if len(records) == 1:
        return "single"
    return "consistent"


ORDER = {"SIGNATURE DRIFT": 0, "DRIFT": 1, "COMMENT DRIFT": 2,
         "consistent": 3, "single": 4}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("corpus", help="directory of .L5X exports")
    ap.add_argument("--redact", action="store_true",
                    help="replace AOI and file names with stable placeholders")
    ap.add_argument("--all", action="store_true",
                    help="also list definitions that agree, and single copies")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"{corpus} is not a directory")
        return 2

    found = collect(corpus)
    if not found:
        print(f"no AOI definitions under {corpus}")
        return 1

    names: dict[str, str] = {}
    files: dict[str, str] = {}

    def rn(n):
        if not args.redact:
            return n
        return names.setdefault(n, f"<aoi{len(names) + 1:03d}>")

    def rf(n):
        if not args.redact:
            return n
        return files.setdefault(n, f"<file{len(files) + 1:02d}>")

    verdicts = {n: classify(r) for n, r in found.items()}
    tally: dict[str, int] = defaultdict(int)
    for v in verdicts.values():
        tally[v] += 1

    total_defs = sum(len(r) for r in found.values())
    print(f"{len(list(corpus.glob('*.L5X')))} export(s), "
          f"{len(found)} distinct AOI name(s), {total_defs} definition(s)")
    print()
    for k in ORDER:
        if tally.get(k):
            print(f"  {k:<16} {tally[k]}")
    print()

    drifted = 0
    for name in sorted(found, key=lambda n: (ORDER[verdicts[n]], n)):
        verdict = verdicts[name]
        if verdict in ("consistent", "single") and not args.all:
            continue
        drifted += verdict in ("SIGNATURE DRIFT", "DRIFT")
        records = found[name]
        print(f"{rn(name)}  [{verdict}]  {len(records)} copy/copies")
        by_rev: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            by_rev[r["revision"]].append(r)
        for rev in sorted(by_rev):
            group = by_rev[rev]
            variants = sorted({r["logic_hash"] for r in group})
            marker = "  <-- same revision, different logic" if len(variants) > 1 else ""
            print(f"    revision {rev}: {len(group)} copy/copies, "
                  f"{len(variants)} distinct logic hash(es){marker}")
            for r in sorted(group, key=lambda x: (x["logic_hash"], x["file"])):
                print(f"      {rf(r['file']):<28} logic {r['logic_hash']}  "
                      f"sig {r['sig_hash']}  "
                      f"{r['params']} param(s), {r['rungs']} rung(s), "
                      f"{r['calls_here']} call site(s) in that file")
        print()

    if drifted:
        print("Declared revision is a claim, not evidence. For each DRIFT above, "
              "the copy with the most call sites across the archive is the likely "
              "master -- but that is a heuristic, and nothing in the export records "
              "who edited it or why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
