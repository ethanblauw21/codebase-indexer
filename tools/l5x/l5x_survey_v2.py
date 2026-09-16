#!/usr/bin/env python3
"""L5X corpus survey, v2 - Phase 0 diagnostic for ADR-013.

Reports on the shape of an L5X corpus so that extraction design can be
grounded in what the files actually contain. Extracts nothing into the
index, imports nothing from src/, adds no dependencies. Throwaway.

Changes from v1:
  - Walks AddOnInstructionDefinition routines as well as Program routines.
    v1 missed 55 percent of the rungs in the corpus because AOI logic lives
    outside the Programs subtree.
  - Removes the top-25 cap on call targets, and resolves JSR targets against
    the routine names actually present in the file.
  - Adds an operand resolution pass: extracts every operand from every
    instruction and reports how many resolve to a declared tag, how many are
    module I/O references, and how many do not resolve at all.
  - Splits mnemonic histograms by container (program rungs, AOI rungs) and
    scans Structured Text lines separately.
  - Adds --sample-mnemonic to print rungs containing a given mnemonic. Prints
    to the console only and never writes them to the JSON.

Usage:
    python l5x_survey_v2.py <dir-or-file> [...] [--out survey.json]
    python l5x_survey_v2.py ./corpus --sample-mnemonic MOVE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from l5x_text import iter_instructions

CALL_MNEMONICS = {"JSR", "SBR", "RET", "JXR"}

# Which operand holds the destination, per mnemonic. Rockwell's convention is
# mixed: OTE(Dest), MOV(Source, Dest), ADD(SourceA, SourceB, Dest). A value of
# -1 means the last operand. Anything absent from this table is counted in
# write_dest_unknown rather than guessed at.
#
# Written from general knowledge of the Logix instruction set, not from the
# instruction reference manual. Verify before trusting the write-edge numbers.
WRITE_DEST_INDEX = {
    "OTE": 0, "OTL": 0, "OTU": 0, "CLR": 0, "RES": 0, "CPT": 0,
    "TON": 0, "TOF": 0, "RTO": 0, "CTU": 0, "CTD": 0, "MSG": 0, "PID": 0,
    "MOV": 1, "COP": 1, "CPS": 1, "FLL": 1, "ABS": 1, "SQR": 1, "NEG": 1,
    "DTOS": 1, "RTOS": 1, "STOD": 1, "STOR": 1, "MVM": 2,
    # SWPB(Source, Order Mode, Dest) - three operands, destination last. This
    # was 1, which pointed the write edge at the Order Mode keyword
    # (REVERSE / WORD / HIGH-LOW) instead of the destination tag. 18
    # occurrences in the survey corpus. Verified against the Rockwell
    # instruction reference, not inferred.
    "SWPB": 2,
    "ADD": 2, "SUB": 2, "MUL": 2, "DIV": 2, "MOD": 2,
    # GSV(Class, Instance, Attribute, Dest) - but the corpus carries both
    # 4-operand (29) and 3-operand (6) forms, and the destination is the LAST
    # operand in each. A fixed index of 3 wrote the 6 short-form call sites to
    # the wrong position. -1 means "last operand". Confirmed by operand-kind
    # profiling: the final operand is a declared tag in all 35 occurrences
    # while the leading Class/Instance/Attribute operands are keyword strings.
    "GSV": -1,
    # SSV(Class, Instance, Attribute, Source) deliberately has NO entry: it
    # writes to the system object, not to a tag, and its last operand is the
    # source being read. Giving it a destination index would emit 5 false
    # write edges.
}

# Instructions known to read only, so their absence from WRITE_DEST_INDEX is
# expected rather than a gap. Keeps the unknown-destination list readable.
READ_ONLY_MNEMONICS = {
    "XIC", "XIO", "ONS", "OSR", "OSF", "AFI", "NOP", "TND", "JMP", "LBL",
    "JSR", "SBR", "RET", "JXR", "MCR", "UID", "UIE",
    "EQU", "NEQ", "LES", "GRT", "LEQ", "GEQ", "CMP", "LIM", "MEQ",
    "EQ", "NE", "LT", "GT", "LE", "GE",
}

BUILTIN_TYPES = {
    "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
    "REAL", "LREAL", "STRING", "BYTE", "TIMER", "COUNTER", "CONTROL",
    "MESSAGE", "PID", "PIDE", "ALARM", "ALARM_ANALOG", "ALARM_DIGITAL",
    "AXIS_SERVO", "AXIS_SERVO_DRIVE", "AXIS_VIRTUAL", "AXIS_GENERIC",
    "MOTION_GROUP", "MOTION_INSTRUCTION", "CAM", "CAM_PROFILE", "PHASE",
    "SFC_ACTION", "SFC_STEP", "SFC_STOP", "SERIAL_PORT_CONTROL",
    "CONNECTION_STATUS", "COORDINATE_SYSTEM", "OUTPUT_CAM",
}


# ---------------------------------------------------------------- parsing

def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def load_root(path: Path):
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
    return "".join(elem.itertext())


def depth_of(root) -> int:
    best, stack = 0, [(root, 1)]
    while stack:
        node, d = stack.pop()
        best = max(best, d)
        stack.extend((c, d + 1) for c in node)
    return best


LITERAL_START = set("0123456789-+.$'\"")


def classify_operand(operand: str):
    """Return (kind, base_name). base_name is None for literals."""
    s = operand.strip()
    if not s:
        return "empty", None
    if s[0] in LITERAL_START:
        return "literal", None
    if s.startswith("?"):
        return "unbound_placeholder", None
    stripped = re.sub(r"\[[^\]]*\]", "", s)
    if stripped.startswith("Program:"):
        return "cross_program", stripped.split(".", 1)[-1].split(".")[0]
    if ":" in stripped:
        return "module_io", stripped.split(".")[0]
    base = stripped.split(".")[0]
    if not base:
        return "unparsed", None
    return "tag", base


def destination_index(mnemonic: str, operands: list[str]):
    """Index of the destination operand, or None if not known."""
    idx = WRITE_DEST_INDEX.get(mnemonic)
    if idx is None:
        return None
    if idx == -1:
        return len(operands) - 1
    return idx


# ---------------------------------------------------------------- survey

def survey_file(path: Path, sample_rungs: int, sample_mnemonic: str | None) -> dict:
    root, meta = load_root(path)
    out: dict = {"file": path.name, "path": str(path), **meta}
    if root is None:
        return out

    out["root_tag"] = localname(root.tag)
    for attr in ("SchemaRevision", "SoftwareRevision", "TargetName",
                 "TargetType", "TargetSubType", "ContainsContext",
                 "ExportDate", "Owner", "ExportOptions"):
        if attr in root.attrib:
            out[attr] = root.attrib[attr]

    all_elems = list(root.iter())
    out["element_count"] = len(all_elems)
    out["max_depth"] = depth_of(root)
    out["element_histogram"] = dict(
        Counter(localname(e.tag) for e in all_elems).most_common())

    controller = root.find("Controller")
    if controller is not None:
        out["controller_name"] = controller.attrib.get("Name")
        out["controller_type"] = controller.attrib.get("ProcessorType")
        out["controller_use"] = controller.attrib.get("Use")

    out["udt_names"] = [d.attrib.get("Name") for d in root.iter("DataType")]
    out["module_count"] = sum(1 for _ in root.iter("Module"))

    # ------------------------------------------------------ name registries
    # Built first so that operand and call resolution have something to
    # resolve against.
    controller_tags: set[str] = set()
    program_tags: dict[str, set[str]] = {}
    aoi_locals: dict[str, set[str]] = {}
    tag_types = Counter()
    tag_datatypes = Counter()
    alias_targets: list[str] = []
    described_tags = 0
    total_tags = 0
    tag_scopes: dict[str, int] = {}

    def collect_tags(container, sink: set[str]) -> int:
        nonlocal described_tags, total_tags
        n = 0
        for tags_elem in container.findall("Tags"):
            for tag in tags_elem.findall("Tag"):
                name = tag.attrib.get("Name")
                if name:
                    sink.add(name)
                n += 1
                total_tags += 1
                tag_types[tag.attrib.get("TagType", "?")] += 1
                tag_datatypes[tag.attrib.get("DataType", "?")] += 1
                if tag.attrib.get("AliasFor"):
                    alias_targets.append(tag.attrib["AliasFor"])
                if tag.find("Description") is not None:
                    described_tags += 1
        return n

    if controller is not None:
        tag_scopes["<controller>"] = collect_tags(controller, controller_tags)

    programs = list(root.iter("Program"))
    for prog in programs:
        pname = prog.attrib.get("Name", "?")
        sink = program_tags.setdefault(pname, set())
        tag_scopes[pname] = collect_tags(prog, sink)

    aois = list(root.iter("AddOnInstructionDefinition"))
    aoi_names: set[str] = set()
    for aoi in aois:
        aname = aoi.attrib.get("Name", "?")
        aoi_names.add(aname)
        sink = aoi_locals.setdefault(aname, set())
        for param in aoi.iter("Parameter"):
            if param.attrib.get("Name"):
                sink.add(param.attrib["Name"])
        for lt in aoi.iter("LocalTag"):
            if lt.attrib.get("Name"):
                sink.add(lt.attrib["Name"])

    # Routine name registry, for JSR resolution.
    #
    # JSR targets are program-local: a JSR in program P can only reach a
    # routine declared in P. Resolving against a flat whole-file set makes two
    # programs that each declare a routine of the same name resolve each
    # other's calls, which inflates the rate. Both are kept so the scoped rate
    # can be reported against the flat one it replaces.
    routine_names: set[str] = set()
    routines_by_program: dict[str, set[str]] = {}
    for prog in programs:
        pname = prog.attrib.get("Name", "?")
        owned = routines_by_program.setdefault(pname, set())
        for r in prog.iter("Routine"):
            if r.attrib.get("Name"):
                routine_names.add(r.attrib["Name"])
                owned.add(r.attrib["Name"])

    # How much name collision exists at all - if no routine name is declared in
    # two programs, the flat set and the scoped one cannot disagree.
    _name_owners: Counter = Counter()
    for _p, _names in routines_by_program.items():
        for _n in _names:
            _name_owners[_n] += 1
    shared_routine_names = {n: c for n, c in _name_owners.items() if c > 1}

    # ------------------------------------------------------ logic walk
    routine_types = Counter()
    mnemonics_by_container = {"program": Counter(), "aoi": Counter()}
    st_functions = Counter()
    operand_kinds = Counter()
    operand_resolution = Counter()
    distinct_referenced: set[str] = set()
    unresolved_sample: Counter = Counter()
    write_operand_kinds = Counter()
    write_dest_unknown = Counter()
    call_targets = Counter()
    call_scope = Counter()
    rung_lengths: list[int] = []
    rung_total = 0
    rung_with_comment = 0
    st_line_total = 0
    rung_samples: list[dict] = []
    mnemonic_hits: list[str] = []
    per_container_rungs: dict[str, int] = {"program": 0, "aoi": 0}

    def scan_text(body: str, kind: str, scope_tags: set[str],
                  owner: str | None = None) -> None:
        for mnemonic, operands in iter_instructions(body):
            if kind == "st":
                st_functions[mnemonic] += 1
            else:
                mnemonics_by_container[kind][mnemonic] += 1

            if mnemonic in CALL_MNEMONICS and operands:
                call_targets[operands[0]] += 1
                # Resolve against the owning program only. `owner` is the
                # program name for program routines and None inside an AOI
                # definition, where a JSR cannot legally appear - counted
                # separately rather than assumed absent.
                target = classify_operand(operands[0])[1]
                if owner is None:
                    call_scope["from_aoi"] += 1
                elif target is None:
                    call_scope["unparsed_target"] += 1
                elif target in routines_by_program.get(owner, ()):
                    call_scope["program_local"] += 1
                elif target in routine_names:
                    # Present in the file, but not in the calling program.
                    # These are exactly the calls the flat set resolved falsely.
                    call_scope["other_program_only"] += 1
                else:
                    call_scope["unresolved"] += 1

            # Resolve the destination operand once per instruction, since a
            # -1 entry depends on how many operands this call was given.
            dest_idx = destination_index(mnemonic, operands)
            if (dest_idx is None and operands
                    and mnemonic not in READ_ONLY_MNEMONICS):
                write_dest_unknown[mnemonic] += 1

            for pos, operand in enumerate(operands):
                okind, base = classify_operand(operand)
                operand_kinds[okind] += 1
                if okind != "tag" or base is None:
                    continue
                distinct_referenced.add(base)
                if base in scope_tags:
                    resolution = "scope_local"
                elif base in controller_tags:
                    resolution = "controller"
                elif base in aoi_names:
                    resolution = "aoi_instance_name"
                else:
                    resolution = "unresolved"
                    unresolved_sample[base] += 1
                operand_resolution[resolution] += 1
                if pos == dest_idx:
                    write_operand_kinds[resolution] += 1

    def walk_routines(container, kind: str, scope_tags: set[str],
                      label: str, owner: str | None = None) -> list[dict]:
        nonlocal rung_total, rung_with_comment, st_line_total
        entries = []
        for routine in container.iter("Routine"):
            rtype = routine.attrib.get("Type", "?")
            if kind == "program":
                routine_types[rtype] += 1
            entry = {"name": routine.attrib.get("Name"), "type": rtype,
                     "container": kind}

            rungs = list(routine.iter("Rung"))
            if rungs:
                entry["rungs"] = len(rungs)
                rung_total += len(rungs)
                per_container_rungs[kind] += len(rungs)
                for rung in rungs:
                    t = rung.find("Text")
                    body = text_of(t) if t is not None else ""
                    rung_lengths.append(len(body))
                    if rung.find("Comment") is not None:
                        rung_with_comment += 1
                    scan_text(body, kind, scope_tags, owner)
                    if sample_mnemonic and re.search(
                            r"\b" + re.escape(sample_mnemonic) + r"\(", body):
                        mnemonic_hits.append(
                            f"[{label}/{entry['name']} #"
                            f"{rung.attrib.get('Number')}] {body[:500]}")
                    if len(rung_samples) < sample_rungs:
                        rung_samples.append({
                            "container": kind,
                            "routine": entry["name"],
                            "number": rung.attrib.get("Number"),
                            "text": body[:400],
                        })

            lines = list(routine.iter("Line"))
            if lines:
                entry["st_lines"] = len(lines)
                st_line_total += len(lines)
                for line in lines:
                    scan_text(text_of(line), "st", scope_tags, owner)

            sheets = list(routine.iter("Sheet"))
            if sheets:
                entry["fbd_sheets"] = len(sheets)
                entry["fbd_blocks"] = sum(1 for _ in routine.iter("Block"))
                entry["fbd_wires"] = sum(1 for _ in routine.iter("Wire"))

            entries.append(entry)
        return entries

    program_records = []
    for prog in programs:
        pname = prog.attrib.get("Name", "?")
        scope = controller_tags | program_tags.get(pname, set())
        routines = walk_routines(prog, "program", scope, pname, owner=pname)
        program_records.append({
            "name": pname,
            "main_routine": prog.attrib.get("MainRoutineName"),
            "tags": tag_scopes.get(pname, 0),
            "routine_count": len(routines),
            "routines": routines,
        })

    aoi_records = []
    for aoi in aois:
        aname = aoi.attrib.get("Name", "?")
        scope = aoi_locals.get(aname, set())
        routines = walk_routines(aoi, "aoi", scope, aname)
        aoi_records.append({
            "name": aname,
            "revision": aoi.attrib.get("Revision"),
            "parameters": sum(1 for _ in aoi.iter("Parameter")),
            "local_tags": sum(1 for _ in aoi.iter("LocalTag")),
            "routine_count": len(routines),
            "routines": routines,
        })

    # ------------------------------------------------------ assembly
    out["tags"] = {
        "total": total_tags,
        "by_scope": tag_scopes,
        "by_tag_type": dict(tag_types.most_common()),
        "top_datatypes": dict(tag_datatypes.most_common(30)),
        "aliases": len(alias_targets),
        "with_description": described_tags,
    }

    alias_bases = [classify_operand(a)[1] for a in alias_targets]
    known = set(controller_tags)
    for scope_set in program_tags.values():
        known |= scope_set
    out["alias_resolution"] = {
        "total": len(alias_targets),
        "resolved_in_file": sum(1 for b in alias_bases if b and b in known),
        "module_io_target": sum(
            1 for a in alias_targets if classify_operand(a)[0] == "module_io"),
        "unresolved": sum(
            1 for a, b in zip(alias_targets, alias_bases)
            if classify_operand(a)[0] == "tag" and (not b or b not in known)),
    }

    out["programs"] = program_records
    out["aoi"] = aoi_records
    out["routine_types"] = dict(routine_types.most_common())
    out["routine_totals"] = {
        "program_routines": sum(len(p["routines"]) for p in program_records),
        "aoi_routines": sum(len(a["routines"]) for a in aoi_records),
        "routine_elements_in_file": out["element_histogram"].get("Routine", 0),
    }
    out["tasks"] = [
        {"name": t.attrib.get("Name"), "type": t.attrib.get("Type"),
         "rate": t.attrib.get("Rate"),
         "scheduled": [s.text for s in t.iter("ScheduledProgram")]}
        for t in root.iter("Task")
    ]
    out["rungs"] = {
        "total": rung_total,
        "in_programs": per_container_rungs["program"],
        "in_aoi": per_container_rungs["aoi"],
        "rung_elements_in_file": out["element_histogram"].get("Rung", 0),
        "with_comment": rung_with_comment,
        "text_len_min": min(rung_lengths) if rung_lengths else None,
        "text_len_median": statistics.median(rung_lengths) if rung_lengths else None,
        "text_len_p90": (sorted(rung_lengths)[int(0.9 * len(rung_lengths))]
                         if rung_lengths else None),
        "text_len_max": max(rung_lengths) if rung_lengths else None,
    }
    out["st_lines"] = st_line_total
    out["mnemonics_program"] = dict(mnemonics_by_container["program"].most_common())
    out["mnemonics_aoi"] = dict(mnemonics_by_container["aoi"].most_common())
    out["st_functions"] = dict(st_functions.most_common())

    combined = Counter()
    combined.update(mnemonics_by_container["program"])
    combined.update(mnemonics_by_container["aoi"])
    out["mnemonics"] = dict(combined.most_common())

    # Flat resolution: the whole-file routine-name set. Retained only as the
    # upper bound it always was, so the scoped rate below can be compared to it.
    resolved_calls = sum(n for t, n in call_targets.items()
                         if classify_operand(t)[1] in routine_names)
    out["calls"] = {
        "occurrences": sum(call_targets.values()),
        "distinct_targets": len(call_targets),
        # Program-scoped resolution - the real rate.
        "program_local": call_scope["program_local"],
        "other_program_only": call_scope["other_program_only"],
        "unresolved": call_scope["unresolved"],
        "unparsed_target": call_scope["unparsed_target"],
        "from_aoi": call_scope["from_aoi"],
        # Flat, whole-file resolution - upper bound, superseded.
        "resolved_occurrences_flat": resolved_calls,
        "unresolved_occurrences_flat": sum(call_targets.values()) - resolved_calls,
        "distinct_resolved_flat": sum(1 for t in call_targets
                                      if classify_operand(t)[1] in routine_names),
        "counts_descending": sorted(call_targets.values(), reverse=True),
    }
    out["routine_name_collisions"] = {
        "names_declared_in_multiple_programs": len(shared_routine_names),
        "max_programs_sharing_a_name": (max(shared_routine_names.values())
                                        if shared_routine_names else 0),
    }
    out["call_targets"] = dict(call_targets.most_common())

    out["operands"] = {
        "by_kind": dict(operand_kinds.most_common()),
        "tag_resolution": dict(operand_resolution.most_common()),
        "distinct_tags_referenced": len(distinct_referenced),
        "declared_tags": total_tags,
        "write_position_resolution": dict(write_operand_kinds.most_common()),
        "write_dest_unknown": dict(write_dest_unknown.most_common()),
        "unresolved_distinct": len(unresolved_sample),
        "unresolved_counts_descending": sorted(
            unresolved_sample.values(), reverse=True)[:50],
    }
    out["unresolved_names"] = dict(unresolved_sample.most_common(40))
    out["rung_samples"] = rung_samples
    out["_mnemonic_hits"] = mnemonic_hits  # console only, see main()
    return out


# ---------------------------------------------------------------- report

def find_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found.extend(f for f in sorted(path.rglob("*"))
                         if f.is_file() and f.suffix.lower() == ".l5x")
        elif path.is_file():
            found.append(path)
        else:
            print(f"skipped, not found: {p}", file=sys.stderr)
    return found


def print_report(results: list[dict]) -> None:
    print("=" * 72)
    print(f"L5X CORPUS SURVEY v2 - {len(results)} file(s)")
    print("=" * 72)

    for r in results:
        print(f"\n{r['file']}  ({r['bytes']:,} bytes)")
        if r.get("parse_error"):
            print(f"  DID NOT PARSE: {r['parse_error']}")
            continue
        print(f"  target={r.get('TargetType')} context={r.get('ContainsContext')} "
              f"software={r.get('SoftwareRevision')} "
              f"processor={r.get('controller_type')}")
        rt = r["routine_totals"]
        print(f"  routines: {rt['program_routines']} in programs + "
              f"{rt['aoi_routines']} in AOIs = "
              f"{rt['program_routines'] + rt['aoi_routines']} "
              f"(file has {rt['routine_elements_in_file']})")
        rg = r["rungs"]
        print(f"  rungs: {rg['in_programs']} program + {rg['in_aoi']} AOI = "
              f"{rg['total']} (file has {rg['rung_elements_in_file']})")
        if rg["total"] != rg["rung_elements_in_file"]:
            print("    WARNING: rung counts do not reconcile, logic is being missed")
        print(f"  rung text len min/med/p90/max = {rg['text_len_min']}/"
              f"{rg['text_len_median']}/{rg['text_len_p90']}/{rg['text_len_max']}")
        c = r["calls"]
        print(f"  calls: {c['occurrences']} occurrences, "
              f"{c['distinct_targets']} distinct, "
              f"{c['program_local']} resolve within the calling program "
              f"({c['other_program_only']} name-match only in another program, "
              f"{c['unresolved']} unresolved)")
        o = r["operands"]
        print(f"  operands: {sum(o['by_kind'].values())} total, "
              f"{o['distinct_tags_referenced']} distinct tag names referenced "
              f"vs {o['declared_tags']} declared")
        print(f"    by kind: {o['by_kind']}")
        print(f"    tag resolution: {o['tag_resolution']}")
        print(f"    write-position resolution: {o['write_position_resolution']}")
        a = r["alias_resolution"]
        print(f"  aliases: {a['total']} total, {a['resolved_in_file']} to a tag, "
              f"{a['module_io_target']} to module I/O, {a['unresolved']} unresolved")

    parsed = [r for r in results if not r.get("parse_error")]
    if not parsed:
        return

    print("\n" + "=" * 72)
    print("CORPUS AGGREGATE")
    print("=" * 72)

    prog_mn, aoi_mn, st_fn = Counter(), Counter(), Counter()
    for r in parsed:
        prog_mn.update(r["mnemonics_program"])
        aoi_mn.update(r["mnemonics_aoi"])
        st_fn.update(r["st_functions"])

    print(f"\nProgram-rung mnemonics ({len(prog_mn)} distinct, "
          f"{sum(prog_mn.values()):,} occurrences):")
    for name, n in prog_mn.most_common(40):
        print(f"  {n:>7,}  {name}")

    print(f"\nAOI-rung mnemonics ({len(aoi_mn)} distinct, "
          f"{sum(aoi_mn.values()):,} occurrences):")
    for name, n in aoi_mn.most_common(40):
        print(f"  {n:>7,}  {name}")

    only_aoi = set(aoi_mn) - set(prog_mn)
    only_prog = set(prog_mn) - set(aoi_mn)
    print(f"\n  mnemonics only in AOI logic: {sorted(only_aoi)}")
    print(f"  mnemonics only in program logic: {sorted(only_prog)}")

    if st_fn:
        print(f"\nStructured Text function calls ({len(st_fn)} distinct, "
              f"{sum(st_fn.values()):,} occurrences):")
        for name, n in st_fn.most_common(40):
            print(f"  {n:>7,}  {name}")

    ops, res, wres, wunk = Counter(), Counter(), Counter(), Counter()
    for r in parsed:
        ops.update(r["operands"]["by_kind"])
        res.update(r["operands"]["tag_resolution"])
        wres.update(r["operands"]["write_position_resolution"])
        wunk.update(r["operands"]["write_dest_unknown"])

    total_tag_ops = sum(res.values())
    print(f"\nOperand kinds across corpus: {dict(ops)}")
    print(f"Tag operand resolution: {dict(res)}")
    if total_tag_ops:
        print(f"  unresolved rate: "
              f"{100 * res.get('unresolved', 0) / total_tag_ops:.1f}%")
    print(f"Write-position resolution: {dict(wres)} "
          f"({sum(wres.values()):,} write edges identified)")

    if wunk:
        print(f"\nMnemonics with no known destination operand "
              f"({len(wunk)} distinct, {sum(wunk.values()):,} occurrences).")
        print("Writes by these instructions are missing from the numbers above.")
        print("Add any that really do write to WRITE_DEST_INDEX:")
        for name, n in wunk.most_common(40):
            print(f"  {n:>7,}  {name}")
        if len(wunk) > 40:
            print(f"  ... and {len(wunk) - 40} more")

    calls = Counter()
    collisions = 0
    for r in parsed:
        calls["occ"] += r["calls"]["occurrences"]
        calls["local"] += r["calls"]["program_local"]
        calls["other"] += r["calls"]["other_program_only"]
        calls["unres"] += r["calls"]["unresolved"]
        calls["aoi"] += r["calls"]["from_aoi"]
        calls["flat"] += r["calls"]["resolved_occurrences_flat"]
        collisions += r["routine_name_collisions"][
            "names_declared_in_multiple_programs"]
    if calls["occ"]:
        pct = 100 * calls["local"] / calls["occ"]
        print(f"\nCall edges: {calls['occ']} occurrences, {calls['local']} "
              f"resolve within the calling program ({pct:.1f}%)")
        print(f"  name-match only in another program: {calls['other']}  "
              f"(these are what the flat whole-file set resolved falsely)")
        print(f"  unresolved anywhere in file: {calls['unres']}   "
              f"from inside an AOI: {calls['aoi']}")
        print(f"  flat whole-file upper bound was: {calls['flat']} "
              f"({100 * calls['flat'] / calls['occ']:.1f}%)")
        print(f"  routine names declared in more than one program: "
              f"{collisions}")

    print("\n" + "=" * 72)
    print("QUESTIONS THIS SHOULD HAVE ANSWERED")
    print("=" * 72)
    print("""
  1. Do rung counts reconcile now? If any file still prints the WARNING
     above, logic is living somewhere neither Programs nor AOIs cover.
  2. Is the AOI instruction vocabulary different from the program one? The
     only-in-AOI and only-in-program lists show whether AOI internals need
     different edge handling.
  3. What fraction of tag operands resolve to a declared tag? A low rate
     means tag edges cannot be built from neutral text alone and something
     in the operand parsing or scoping model is wrong.
  4. Do writes resolve better or worse than reads? The write-position line
     is the one that matters for "where is this tag written".
  5. What is in the unknown-destination list? Anything frequent there is a
     write edge the survey is not counting. Add-On Instructions will all land
     there, since their outputs are defined per-AOI rather than fixed.
  6. Do JSR targets resolve in-file? Unresolved calls mean the call graph
     needs cross-file resolution or has to be marked candidate.
  7. Where do aliases point - other tags, or module I/O? That decides
     whether aliasing is a tag-to-tag edge or a tag-to-hardware edge.
""")


def main() -> int:
    ap = argparse.ArgumentParser(description="Survey an L5X corpus.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", metavar="PATH",
                    help="write full results as JSON (UTF-8, no BOM)")
    ap.add_argument("--sample-rungs", type=int, default=3)
    ap.add_argument("--sample-mnemonic", metavar="MNEMONIC",
                    help="print rungs containing this mnemonic, console only")
    args = ap.parse_args()

    files = find_files(args.paths)
    if not files:
        print("no .L5X files found", file=sys.stderr)
        return 1

    results = [survey_file(f, args.sample_rungs, args.sample_mnemonic)
               for f in files]
    print_report(results)

    if args.sample_mnemonic:
        print("\n" + "=" * 72)
        print(f"RUNGS CONTAINING {args.sample_mnemonic} (console only)")
        print("=" * 72)
        for r in results:
            hits = r.get("_mnemonic_hits") or []
            if hits:
                print(f"\n-- {r['file']} ({len(hits)} rungs)")
                for h in hits[:20]:
                    print(f"  {h}")

    for r in results:
        r.pop("_mnemonic_hits", None)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())