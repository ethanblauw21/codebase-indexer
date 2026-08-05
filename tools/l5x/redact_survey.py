#!/usr/bin/env python3
"""Redact an L5X survey JSON down to shareable structure.

Builds the output from an explicit allowlist. Any field not named below is
dropped, so a field added to the survey later does not leak by default.

What survives: counts, distributions, vendor instruction mnemonics, schema
and software revisions, rung text length statistics.

What does not: file names and paths, controller/program/routine/tag/UDT
names, the Owner attribute, export dates, rung text samples, and the
operand name histograms.

Usage:
    python redact_survey.py survey.json > survey_redacted.json
    python redact_survey.py survey.json --check   # list what was dropped
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Top-level scalar fields that carry no customer information.
SCALAR_ALLOW = [
    "bytes",
    "has_bom",
    "declared_encoding",
    "parse_error",
    "root_tag",
    "element_count",
    "max_depth",
    "st_lines",
    "SchemaRevision",
    "SoftwareRevision",
    "TargetType",
    "TargetSubType",
    "ContainsContext",
    "controller_type",     # processor catalog number, e.g. 1756-L83E
    "controller_use",
    "module_count",
]

# Dict fields where both keys and values are safe.
DICT_ALLOW = [
    "element_histogram",   # XML element names, all vendor schema
    "routine_types",       # RLL / ST / FBD / SFC
    "mnemonics",           # Rockwell instruction set
    "rungs",               # counts and text-length stats only
    "mnemonics_program",
    "mnemonics_aoi",
    "st_functions",
    "routine_totals",
    "alias_resolution",
    "calls",
    "operands",
]


def redact_file(rec: dict, index: int) -> dict:
    out: dict = {"file_index": index}

    for key in SCALAR_ALLOW:
        if key in rec:
            out[key] = rec[key]

    for key in DICT_ALLOW:
        if key in rec:
            out[key] = rec[key]

    # Tags: keep the type and description distributions, drop scope names.
    tags = rec.get("tags")
    if tags:
        datatypes = tags.get("top_datatypes", {})
        # DataType values are a mix of builtins (BOOL, DINT, TIMER) and UDT
        # names, and UDT names describe the process. Keep only builtins.
        builtin = {
            "BOOL", "SINT", "INT", "DINT", "LINT", "REAL", "LREAL", "STRING",
            "TIMER", "COUNTER", "CONTROL", "MESSAGE", "AXIS_SERVO",
            "AXIS_SERVO_DRIVE", "MOTION_GROUP", "MOTION_INSTRUCTION",
            "PID", "PIDE", "ALARM", "ALARM_ANALOG", "ALARM_DIGITAL",
            "CONNECTION_STATUS", "PHASE", "SFC_ACTION", "SFC_STEP",
            "SFC_STOP", "SERIAL_PORT_CONTROL", "BYTE", "USINT", "UINT",
            "UDINT", "ULINT",
        }
        kept = {k: v for k, v in datatypes.items() if k.split("[")[0] in builtin}
        udt_typed = sum(v for k, v in datatypes.items()
                        if k.split("[")[0] not in builtin)
        out["tags"] = {
            "total": tags.get("total"),
            "scope_count": len(tags.get("by_scope", {})),
            "by_tag_type": tags.get("by_tag_type", {}),
            "aliases": tags.get("aliases"),
            "with_description": tags.get("with_description"),
            "builtin_datatypes": kept,
            "udt_typed_tag_count": udt_typed,
        }

    out["udt_count"] = len(rec.get("udt_names", []))

    # AOIs: shape only.
    aois = rec.get("aoi", [])
    out["aoi"] = {
        "count": len(aois),
        "parameter_counts": sorted(a.get("parameters", 0) for a in aois),
        "local_tag_counts": sorted(a.get("local_tags", 0) for a in aois),
        "routine_counts": sorted(len(a.get("routines", [])) for a in aois),
    }

    # Programs and routines: sizes, not names.
    programs = rec.get("programs", [])
    prog_shapes = []
    rung_counts: list[int] = []
    st_counts: list[int] = []
    for p in programs:
        routines = p.get("routines", [])
        prog_shapes.append({
            "tags": p.get("tags"),
            "routine_count": len(routines),
            "has_main_routine": bool(p.get("main_routine")),
        })
        for r in routines:
            if "rungs" in r:
                rung_counts.append(r["rungs"])
            if "st_lines" in r:
                st_counts.append(r["st_lines"])
    out["programs"] = {
        "count": len(programs),
        "shapes": prog_shapes,
        "rungs_per_routine": sorted(rung_counts),
        "st_lines_per_routine": sorted(st_counts),
    }

    # Tasks: type distribution only, names dropped.
    tasks = rec.get("tasks", [])
    out["tasks"] = {
        "count": len(tasks),
        "by_type": dict(Counter(t.get("type") for t in tasks)),
        "scheduled_program_counts": sorted(
            len(t.get("scheduled", [])) for t in tasks
        ),
    }

    # Operand histograms: shape only. How many distinct targets and how
    # concentrated they are answers the design question; the names do not.
    for key, label in (("call_targets", "call_targets"),
                       ("write_operands", "write_operands")):
        hist = rec.get(key, {})
        out[label] = {
            "distinct": len(hist),
            "occurrences": sum(hist.values()),
            "counts_descending": sorted(hist.values(), reverse=True),
        }

    return out


def dropped_keys(original: dict, redacted: dict) -> list[str]:
    return sorted(set(original) - set(redacted) - {"file", "path"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("survey_json")
    ap.add_argument("--check", action="store_true",
                    help="print dropped top-level keys instead of output")
    args = ap.parse_args()

    records = json.loads(Path(args.survey_json).read_text(encoding="utf-8"))
    if isinstance(records, dict):
        records = [records]

    redacted = [redact_file(r, i) for i, r in enumerate(records)]

    if args.check:
        for i, (orig, red) in enumerate(zip(records, redacted)):
            print(f"file_index {i} dropped: {', '.join(dropped_keys(orig, red))}")
        return 0

    json.dump(redacted, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())