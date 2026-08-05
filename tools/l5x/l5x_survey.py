#!/usr/bin/env python3
"""L5X corpus survey - Phase 0 diagnostic for ADR-013.

Reports on the shape of an L5X corpus so that extraction design can be
grounded in what the files actually contain. This script extracts nothing
into the index, imports nothing from src/, and adds no dependencies. It is
a throwaway. Once the findings are written into the ADR-013 implementation
log, this can be deleted.

Usage:
    python l5x_survey.py <dir-or-file> [<dir-or-file> ...]
    python l5x_survey.py ./corpus --json survey.json
    python l5x_survey.py ./corpus --sample-rungs 5

Notes:
    Uses xml.etree.ElementTree rather than lxml on purpose. ADR-013 picks
    lxml for the adapter; a survey does not need it, and not adding a
    dependency for a spike keeps this deletable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

# Rockwell neutral text: MNEMONIC(operand,operand,...)
# Matches the instruction token immediately preceding an open paren.
MNEMONIC_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,15})\(")

# Instructions whose first operand is a routine name, i.e. call edges.
CALL_MNEMONICS = {"JSR", "SBR", "RET", "JXR"}

# Instructions that write a tag (destination semantics). Rough starting
# list only - the histogram this script prints is what should correct it.
WRITE_MNEMONICS = {"OTE", "OTL", "OTU", "MOV", "CPT", "ADD", "SUB", "MUL",
                   "DIV", "CLR", "COP", "FLL", "SSV", "MSG"}


def localname(tag: str) -> str:
    """Strip any namespace. L5X normally has none, but do not assume it."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def load_root(path: Path):
    """Return (root, meta) or (None, meta) if the file will not parse."""
    raw = path.read_bytes()
    meta = {
        "bytes": len(raw),
        "sha256_12": hashlib.sha256(raw).hexdigest()[:12],
        "has_bom": raw.startswith(b"\xef\xbb\xbf"),
        "declared_encoding": None,
        "parse_error": None,
    }
    head = raw[:200].lstrip(b"\xef\xbb\xbf")
    m = re.search(rb'encoding=["\']([^"\']+)["\']', head)
    if m:
        meta["declared_encoding"] = m.group(1).decode("ascii", "replace")

    body = raw[3:] if meta["has_bom"] else raw
    try:
        return ET.fromstring(body), meta
    except ET.ParseError as exc:
        meta["parse_error"] = str(exc)
        return None, meta


def text_of(elem) -> str:
    """Full text of an element including tails of children (CDATA lands here)."""
    return "".join(elem.itertext())


def depth_of(root) -> int:
    best = 0
    stack = [(root, 1)]
    while stack:
        node, d = stack.pop()
        best = max(best, d)
        for child in node:
            stack.append((child, d + 1))
    return best


def survey_file(path: Path, sample_rungs: int) -> dict:
    root, meta = load_root(path)
    out: dict = {"file": path.name, "path": str(path), **meta}
    if root is None:
        return out

    out["root_tag"] = localname(root.tag)
    # Export header. TargetType + ContainsContext decide whether this file is
    # a whole controller or a fragment exported with surrounding context.
    for attr in ("SchemaRevision", "SoftwareRevision", "TargetName",
                 "TargetType", "TargetSubType", "ContainsContext",
                 "ExportDate", "Owner", "ExportOptions"):
        if attr in root.attrib:
            out[attr] = root.attrib[attr]

    all_elems = list(root.iter())
    out["element_count"] = len(all_elems)
    out["max_depth"] = depth_of(root)
    out["element_histogram"] = dict(
        Counter(localname(e.tag) for e in all_elems).most_common()
    )

    controller = root.find("Controller")
    if controller is not None:
        out["controller_name"] = controller.attrib.get("Name")
        out["controller_type"] = controller.attrib.get("ProcessorType")
        out["controller_use"] = controller.attrib.get("Use")

    # --- data types, modules, AOIs -----------------------------------
    out["udt_names"] = [d.attrib.get("Name") for d in root.iter("DataType")]
    out["module_count"] = sum(1 for _ in root.iter("Module"))
    aois = list(root.iter("AddOnInstructionDefinition"))
    out["aoi"] = [
        {
            "name": a.attrib.get("Name"),
            "revision": a.attrib.get("Revision"),
            "parameters": sum(1 for _ in a.iter("Parameter")),
            "local_tags": sum(1 for _ in a.iter("LocalTag")),
            "routines": [r.attrib.get("Name") for r in a.iter("Routine")],
        }
        for a in aois
    ]

    # --- tags ---------------------------------------------------------
    # Scope is determined by the ancestor, so walk down rather than using
    # root.iter("Tag"), which flattens controller and program scope together.
    tag_scopes: dict[str, int] = {}
    tag_types = Counter()
    tag_datatypes = Counter()
    alias_count = 0
    described_tags = 0
    total_tags = 0

    def collect_tags(container, scope_label: str) -> int:
        nonlocal alias_count, described_tags, total_tags
        n = 0
        for tags_elem in container.findall("Tags"):
            for tag in tags_elem.findall("Tag"):
                n += 1
                total_tags += 1
                tag_types[tag.attrib.get("TagType", "?")] += 1
                tag_datatypes[tag.attrib.get("DataType", "?")] += 1
                if tag.attrib.get("AliasFor"):
                    alias_count += 1
                if tag.find("Description") is not None:
                    described_tags += 1
        return n

    if controller is not None:
        tag_scopes["<controller>"] = collect_tags(controller, "controller")

    # --- programs and routines ---------------------------------------
    programs = []
    routine_types = Counter()
    rung_lengths: list[int] = []
    rung_total = 0
    rung_with_comment = 0
    st_line_total = 0
    mnemonics = Counter()
    call_targets = Counter()
    write_operands = Counter()
    rung_samples: list[dict] = []

    for prog in root.iter("Program"):
        pname = prog.attrib.get("Name", "?")
        tag_scopes[pname] = collect_tags(prog, "program")
        routines = []
        for routine in prog.iter("Routine"):
            rtype = routine.attrib.get("Type", "?")
            routine_types[rtype] += 1
            entry = {"name": routine.attrib.get("Name"), "type": rtype}

            rungs = list(routine.iter("Rung"))
            if rungs:
                entry["rungs"] = len(rungs)
                rung_total += len(rungs)
                for rung in rungs:
                    t = rung.find("Text")
                    body = text_of(t) if t is not None else ""
                    rung_lengths.append(len(body))
                    if rung.find("Comment") is not None:
                        rung_with_comment += 1
                    found = MNEMONIC_RE.findall(body)
                    mnemonics.update(found)
                    for mn in found:
                        if mn in CALL_MNEMONICS:
                            m = re.search(mn + r"\(([^,)]+)", body)
                            if m:
                                call_targets[m.group(1).strip()] += 1
                        elif mn in WRITE_MNEMONICS:
                            m = re.search(mn + r"\(([^,)]+)", body)
                            if m:
                                write_operands[m.group(1).strip()] += 1
                    if len(rung_samples) < sample_rungs:
                        rung_samples.append({
                            "routine": entry["name"],
                            "number": rung.attrib.get("Number"),
                            "text": body[:400],
                        })

            lines = list(routine.iter("Line"))
            if lines:
                entry["st_lines"] = len(lines)
                st_line_total += len(lines)

            sheets = list(routine.iter("Sheet"))
            if sheets:
                entry["fbd_sheets"] = len(sheets)

            routines.append(entry)

        programs.append({
            "name": pname,
            "main_routine": prog.attrib.get("MainRoutineName"),
            "tags": tag_scopes.get(pname, 0),
            "routine_count": len(routines),
            "routines": routines,
        })

    out["tags"] = {
        "total": total_tags,
        "by_scope": tag_scopes,
        "by_tag_type": dict(tag_types.most_common()),
        "top_datatypes": dict(tag_datatypes.most_common(15)),
        "aliases": alias_count,
        "with_description": described_tags,
    }
    out["programs"] = programs
    out["routine_types"] = dict(routine_types.most_common())
    out["tasks"] = [
        {"name": t.attrib.get("Name"), "type": t.attrib.get("Type"),
         "scheduled": [s.text for s in t.iter("ScheduledProgram")]}
        for t in root.iter("Task")
    ]
    out["rungs"] = {
        "total": rung_total,
        "with_comment": rung_with_comment,
        "text_len_min": min(rung_lengths) if rung_lengths else None,
        "text_len_median": statistics.median(rung_lengths) if rung_lengths else None,
        "text_len_max": max(rung_lengths) if rung_lengths else None,
    }
    out["st_lines"] = st_line_total
    out["mnemonics"] = dict(mnemonics.most_common())
    out["call_targets"] = dict(call_targets.most_common(25))
    out["write_operands"] = dict(write_operands.most_common(25))
    out["rung_samples"] = rung_samples
    return out


def find_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found.extend(
                f for f in sorted(path.rglob("*"))
                if f.is_file() and f.suffix.lower() == ".l5x"
            )
        elif path.is_file():
            found.append(path)
        else:
            print(f"skipped, not found: {p}", file=sys.stderr)
    return found


def print_report(results: list[dict]) -> None:
    print("=" * 72)
    print(f"L5X CORPUS SURVEY - {len(results)} file(s)")
    print("=" * 72)

    for r in results:
        print(f"\n{r['file']}  ({r['bytes']:,} bytes, sha {r['sha256_12']})")
        if r.get("parse_error"):
            print(f"  DID NOT PARSE: {r['parse_error']}")
            continue
        print(f"  root={r.get('root_tag')}  schema={r.get('SchemaRevision')} "
              f"software={r.get('SoftwareRevision')}")
        print(f"  target={r.get('TargetType')}:{r.get('TargetName')}  "
              f"contains_context={r.get('ContainsContext')}")
        print(f"  elements={r['element_count']:,}  max_depth={r['max_depth']}")
        if r.get("controller_name"):
            print(f"  controller={r['controller_name']} "
                  f"({r.get('controller_type')}) use={r.get('controller_use')}")
        t = r["tags"]
        print(f"  tags={t['total']} across {len(t['by_scope'])} scope(s), "
              f"{t['aliases']} alias, {t['with_description']} described")
        print(f"  udts={len(r['udt_names'])}  modules={r['module_count']}  "
              f"aois={len(r['aoi'])}  programs={len(r['programs'])}")
        print(f"  routine types: {r['routine_types'] or 'none'}")
        rg = r["rungs"]
        if rg["total"]:
            print(f"  rungs={rg['total']} ({rg['with_comment']} commented), "
                  f"text len min/med/max = "
                  f"{rg['text_len_min']}/{rg['text_len_median']}/{rg['text_len_max']}")
        if r["st_lines"]:
            print(f"  structured text lines={r['st_lines']}")

    # ---- corpus aggregates -----------------------------------------
    parsed = [r for r in results if not r.get("parse_error")]
    if not parsed:
        return

    print("\n" + "=" * 72)
    print("CORPUS AGGREGATE")
    print("=" * 72)

    targets = Counter(r.get("TargetType") for r in parsed)
    contexts = Counter(r.get("ContainsContext") for r in parsed)
    schemas = Counter(r.get("SchemaRevision") for r in parsed)
    softwares = Counter(r.get("SoftwareRevision") for r in parsed)
    print(f"TargetType:      {dict(targets)}")
    print(f"ContainsContext: {dict(contexts)}")
    print(f"SchemaRevision:  {dict(schemas)}")
    print(f"SoftwareRevision:{dict(softwares)}")

    rtypes = Counter()
    for r in parsed:
        rtypes.update(r["routine_types"])
    print(f"\nRoutine types across corpus: {dict(rtypes)}")

    elem_hist = Counter()
    for r in parsed:
        elem_hist.update(r["element_histogram"])
    print("\nTop 30 element names:")
    for name, n in elem_hist.most_common(30):
        print(f"  {n:>8,}  {name}")

    mn = Counter()
    for r in parsed:
        mn.update(r["mnemonics"])
    print(f"\nInstruction mnemonics ({len(mn)} distinct, "
          f"{sum(mn.values()):,} occurrences):")
    for name, n in mn.most_common(60):
        marker = ""
        if name in CALL_MNEMONICS:
            marker = "   <- call edge"
        elif name in WRITE_MNEMONICS:
            marker = "   <- tag write"
        print(f"  {n:>8,}  {name}{marker}")
    if len(mn) > 60:
        print(f"  ... and {len(mn) - 60} more")

    calls = Counter()
    for r in parsed:
        calls.update(r["call_targets"])
    if calls:
        print("\nJSR/SBR first operands (candidate routine call targets):")
        for name, n in calls.most_common(25):
            print(f"  {n:>8,}  {name}")

    writes = Counter()
    for r in parsed:
        writes.update(r["write_operands"])
    if writes:
        print("\nMost-written operands (candidate write edges):")
        for name, n in writes.most_common(25):
            print(f"  {n:>8,}  {name}")

    samples = [s for r in parsed for s in r["rung_samples"]]
    if samples:
        print("\nSample rung text:")
        for s in samples[:10]:
            print(f"  [{s['routine']} #{s['number']}] {s['text']}")

    print("\n" + "=" * 72)
    print("QUESTIONS THIS SHOULD HAVE ANSWERED")
    print("=" * 72)
    print("""
  1. Are these whole-controller exports or fragments? Check TargetType and
     ContainsContext above. If ContainsContext is true anywhere, the adapter
     has to distinguish real content from surrounding context, and the file
     is not a controller even though it has a Controller element.
  2. How much of the corpus is ladder versus Structured Text versus FBD?
     FBD has no linear text and may not be worth supporting at all.
  3. Is the instruction vocabulary small enough to enumerate? If sixty
     mnemonics cover the corpus, edge semantics can be a table rather than
     a parser.
  4. Do JSR targets resolve to routine names present in these files, or do
     they point outside the corpus? That determines whether call edges can
     be resolved or have to be marked candidate.
  5. What is the rung text length distribution? That is the input to any
     chunking decision, and it should be measured, not assumed.
  6. Are five files enough to design against, or is more needed from Egan?
""")


def main() -> int:
    ap = argparse.ArgumentParser(description="Survey an L5X corpus.")
    ap.add_argument("paths", nargs="+", help="L5X files or directories")
    ap.add_argument("--json", metavar="OUT", help="write full results as JSON")
    ap.add_argument("--sample-rungs", type=int, default=3,
                    help="rung text samples to keep per file (default 3)")
    args = ap.parse_args()

    files = find_files(args.paths)
    if not files:
        print("no .L5X files found", file=sys.stderr)
        return 1

    results = [survey_file(f, args.sample_rungs) for f in files]
    print_report(results)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())