#!/usr/bin/env python3
"""Falsification checks for two ADR-013 assumptions.

CHECK 1 - AOI positional binding
    Assumption: an Add-On Instruction call site passes the backing instance
    tag first, then every Required parameter in declaration order, with
    EnableIn and EnableOut implicit. If that holds, operand count at each
    call site equals 1 + the number of required non-Enable parameters, and
    output parameters can be located positionally to produce write edges.

    This check compares expected against actual arity at every call site. If
    it fails anywhere, positional binding is unsafe and would silently
    produce wrong write edges.

CHECK 2 - mnemonic alias table
    Assumption: MOVE is MOV, EQ is EQU, and so on - the same instruction
    under a different spelling, not different instructions.

    This checks that no single file uses both spellings of a pair, and that
    both spellings take the same operand counts. Either failing means the
    pair is not an alias and the table is wrong.

Prints counts. Add --names to include AOI names in the output; leave it off
if the AOI names describe the customer's process.

Usage:
    python l5x_check_bindings.py ./corpus
    python l5x_check_bindings.py ./corpus --names
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from l5x_text import iter_instructions

MNEMONIC_ALIASES = {
    "MOVE": "MOV",
    "EQ": "EQU",
    "NE": "NEQ",
    "LT": "LES",
    "GT": "GRT",
    "LE": "LEQ",
    "GE": "GEQ",
    "LIMIT": "LIM",
}

IMPLICIT_PARAMS = {"EnableIn", "EnableOut"}


def load_root(path: Path):
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"{path.name}: parse error: {exc}", file=sys.stderr)
        return None


def text_of(elem) -> str:
    return "".join(elem.itertext())


def aoi_signature(aoi) -> dict:
    """Declared parameter list for one AOI, in document order."""
    params = []
    for p in aoi.iter("Parameter"):
        params.append({
            "name": p.attrib.get("Name"),
            "usage": p.attrib.get("Usage"),
            "required": p.attrib.get("Required") == "true",
            "visible": p.attrib.get("Visible") == "true",
            "datatype": p.attrib.get("DataType"),
        })
    passed = [p for p in params
              if p["required"] and p["name"] not in IMPLICIT_PARAMS]
    return {
        "name": aoi.attrib.get("Name"),
        "revision": aoi.attrib.get("Revision"),
        "params": params,
        "passed": passed,
        "expected_arity": 1 + len(passed),
        # Operand index of each output, given positional binding. Index 0 is
        # the instance tag, which is always written.
        "output_indices": [i + 1 for i, p in enumerate(passed)
                           if p["usage"] in ("Output", "InOut")],
        "usage_counts": Counter(p["usage"] for p in params),
    }


def check_file(path: Path):
    root = load_root(path)
    if root is None:
        return None

    sigs = {}
    for aoi in root.iter("AddOnInstructionDefinition"):
        sig = aoi_signature(aoi)
        sigs[sig["name"]] = sig

    call_arities: dict[str, Counter] = defaultdict(Counter)
    mnemonic_arities: dict[str, Counter] = defaultdict(Counter)
    mnemonics_seen = Counter()

    for routine in root.iter("Routine"):
        for rung in routine.iter("Rung"):
            t = rung.find("Text")
            body = text_of(t) if t is not None else ""
            for mnemonic, operands in iter_instructions(body):
                mnemonics_seen[mnemonic] += 1
                mnemonic_arities[mnemonic][len(operands)] += 1
                if mnemonic in sigs:
                    call_arities[mnemonic][len(operands)] += 1

    return {
        "file": path.name,
        "sigs": sigs,
        "call_arities": call_arities,
        "mnemonic_arities": mnemonic_arities,
        "mnemonics_seen": mnemonics_seen,
    }


def find_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found.extend(f for f in sorted(path.rglob("*"))
                         if f.is_file() and f.suffix.lower() == ".l5x")
        elif path.is_file():
            found.append(path)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--names", action="store_true",
                    help="print AOI names as well as counts")
    args = ap.parse_args()

    files = find_files(args.paths)
    if not files:
        print("no .L5X files found", file=sys.stderr)
        return 1

    results = [r for r in (check_file(f) for f in files) if r]

    # ------------------------------------------------ CHECK 1
    print("=" * 72)
    print("CHECK 1 - AOI POSITIONAL BINDING")
    print("=" * 72)

    total_sites = 0
    matching_sites = 0
    mismatches: list[tuple] = []
    recoverable_writes = 0
    usage_totals = Counter()
    uncalled = 0
    defs_total = 0

    for r in results:
        idx = results.index(r)
        for name, sig in r["sigs"].items():
            defs_total += 1
            usage_totals.update(sig["usage_counts"])
            arities = r["call_arities"].get(name)
            if not arities:
                uncalled += 1
                continue
            for actual, n in arities.items():
                total_sites += n
                if actual == sig["expected_arity"]:
                    matching_sites += n
                    # instance tag plus each output parameter position
                    recoverable_writes += n * (1 + len(sig["output_indices"]))
                else:
                    label = name if args.names else f"<aoi in file {idx}>"
                    mismatches.append(
                        (idx, label, sig["expected_arity"], actual, n,
                         len(sig["params"]), len(sig["passed"])))

    print(f"\nAOI definitions: {defs_total} ({uncalled} never called in-file)")
    print(f"Parameter usage across all definitions: {dict(usage_totals)}")
    print(f"\nCall sites checked: {total_sites}")
    if total_sites:
        pct = 100 * matching_sites / total_sites
        print(f"Arity matches 1 + required non-Enable params: "
              f"{matching_sites} ({pct:.1f}%)")
        print(f"Write edges recoverable if binding holds: {recoverable_writes}")

    if mismatches:
        print(f"\nMISMATCHES ({len(mismatches)} distinct arity/AOI combos):")
        print("  file  aoi                   expected  actual  sites  "
              "params  required")
        for idx, label, exp, act, n, nparams, nreq in sorted(mismatches):
            print(f"  {idx:>4}  {label:<20}  {exp:>8}  {act:>6}  {n:>5}  "
                  f"{nparams:>6}  {nreq:>8}")
        print("\n  Positional binding is NOT safe as specified. Every mismatch"
              "\n  above would produce wrong write edges. Look at whether the"
              "\n  gap equals the count of Visible=false or InOut parameters -"
              "\n  that would point at the actual rule.")
    else:
        print("\n  No mismatches. Positional binding holds across the corpus.")

    # ------------------------------------------------ CHECK 2
    print("\n" + "=" * 72)
    print("CHECK 2 - MNEMONIC ALIAS TABLE")
    print("=" * 72)

    print("\n  alias -> canonical    files using alias / canonical / both")
    conflict = False
    for alias, canon in sorted(MNEMONIC_ALIASES.items()):
        a_files, c_files, both = [], [], []
        for i, r in enumerate(results):
            has_a = r["mnemonics_seen"].get(alias, 0) > 0
            has_c = r["mnemonics_seen"].get(canon, 0) > 0
            if has_a and has_c:
                both.append(i)
            elif has_a:
                a_files.append(i)
            elif has_c:
                c_files.append(i)
        flag = ""
        if both:
            flag = "  <- SAME FILE USES BOTH"
            conflict = True
        print(f"  {alias:>6} -> {canon:<6}    {a_files} / {c_files} / "
              f"{both}{flag}")

    print("\n  operand arity per spelling (should match within each pair):")
    for alias, canon in sorted(MNEMONIC_ALIASES.items()):
        a_ar, c_ar = Counter(), Counter()
        for r in results:
            a_ar.update(r["mnemonic_arities"].get(alias, {}))
            c_ar.update(r["mnemonic_arities"].get(canon, {}))
        if not a_ar and not c_ar:
            continue
        same = set(a_ar) == set(c_ar) or not a_ar or not c_ar
        mark = "" if same else "  <- ARITY DIFFERS, NOT AN ALIAS"
        if not same:
            conflict = True
        print(f"  {alias:>6} {dict(sorted(a_ar.items()))}   "
              f"{canon:>6} {dict(sorted(c_ar.items()))}{mark}")

    if conflict:
        print("\n  At least one pair is suspect. Do not canonicalize that pair"
              "\n  until it is understood.")
    else:
        print("\n  No pair appears in the same file, and arities agree where"
              "\n  both are observed. Consistent with the alias reading, though"
              "\n  a corpus this size cannot prove it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())