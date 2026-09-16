#!/usr/bin/env python3
"""Redact an L5X survey JSON down to shareable structure.

Builds the output from an explicit allowlist. Any field not named below is
dropped, so a field added to the survey later does not leak by default.

What survives: counts, distributions, vendor instruction mnemonics, schema
and software revisions, rung text length statistics.

What does not: file names and paths, controller/program/routine/tag/UDT
names, the Owner attribute, export dates, rung text samples, and the
operand name histograms.

FIXED 2026-08-06 -- this allowlist used to leak.
------------------------------------------------
The allowlist named permitted *fields*, but several permitted fields are
histograms whose **keys** are free text. An Add-On Instruction invocation
appears in rung text as its own mnemonic, so custom AOI names rode into the
output as keys of `mnemonics`, `mnemonics_program`, `mnemonics_aoi`, and
`operands.write_dest_unknown`. `survey_redacted_v2.json` disclosed 24 customer
AOI names while labelled redacted.

Root cause worth remembering: `l5x_survey_v2.py` *does* know which mnemonics are
AOIs -- it builds `aoi_names` while parsing. Redaction runs as a later stage over
the JSON, by which point that knowledge is gone. Filtering at output can only
ever be as good as what the output still remembers.

Two changes below. Mnemonic histograms are now bucketed against a closed vendor
vocabulary, so an unknown name degrades to a count instead of a disclosure. And
`verify_no_free_text` walks the finished structure and raises if any key is not
provably vendor or structural -- the allowlist is now checked rather than
trusted. A field added later cannot leak silently; it fails loudly instead.

Usage:
    python redact_survey.py survey.json > survey_redacted.json
    python redact_survey.py survey.json --check   # list what was dropped
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Rockwell built-in data types. Tag DataType values are a mix of these and UDT
# names, and UDT names describe the process, so only these are ever emitted.
BUILTIN_TYPES = frozenset({
    "BOOL", "SINT", "INT", "DINT", "LINT", "REAL", "LREAL", "STRING",
    "TIMER", "COUNTER", "CONTROL", "MESSAGE", "AXIS_SERVO",
    "AXIS_SERVO_DRIVE", "MOTION_GROUP", "MOTION_INSTRUCTION",
    "PID", "PIDE", "ALARM", "ALARM_ANALOG", "ALARM_DIGITAL",
    "CONNECTION_STATUS", "PHASE", "SFC_ACTION", "SFC_STEP",
    "SFC_STOP", "SERIAL_PORT_CONTROL", "BYTE", "USINT", "UINT",
    "UDINT", "ULINT",
})

# Rockwell's published ladder/ST instruction set. A mnemonic key is emitted by
# name only if it appears here; everything else is bucketed. Vendor terms, not
# customer terms.
VENDOR_MNEMONICS = frozenset({
    "ABL", "ABS", "ACB", "ACL", "ACOS", "ACS", "ADD", "AFI", "AHL",
    "ALARM", "ALM", "ALMA", "ALMD", "AND", "ARD", "ARL", "ASIN", "ASN",
    "ATAN", "ATN", "AVE", "AWA", "AWT", "BRK", "BSL", "BSR", "BTD",
    "BTDT", "BTR", "BTW", "CBCM", "CIO", "CLR", "CMP", "CONCAT", "COP",
    "COS", "CPS", "CPT", "CROUT", "CTD", "CTU", "CTUD", "D2SD", "D3SD",
    "DCM", "DCS", "DCSRT", "DCSTL", "DCSTM", "DDT", "DEDT", "DEG",
    "DELETE", "DERV", "DIV", "DTOS", "DTR", "ELSE", "EOT", "EQU", "ESEL",
    "ESTOP", "EVENT", "FAL", "FBC", "FFL", "FFU", "FGEN", "FIND", "FLL",
    "FOR", "FPMS", "FRD", "FSC", "GEQ", "GRT", "GSV", "HLL", "HPF",
    "INSERT", "INTG", "IOT", "JMP", "JSR", "JXR", "LBL", "LC", "LDLG",
    "LEQ", "LES", "LFL", "LFU", "LIM", "LN", "LOG", "LOWER", "LPF",
    "MAAT", "MADT", "MAFR", "MAG", "MAH", "MAHD", "MAJ", "MAM", "MAPC",
    "MAR", "MAS", "MASD", "MASK", "MASR", "MATC", "MAVE", "MAW", "MAXC",
    "MCCD", "MCCM", "MCCP", "MCD", "MCLM", "MCR", "MDF", "MDO", "MDR",
    "MDS", "MDW", "MEQ", "MGS", "MGSD", "MGSP", "MGSR", "MID", "MINC",
    "MOD", "MOV", "MRAT", "MRP", "MSF", "MSG", "MSO", "MSTD", "MUL",
    "MUX", "MVM", "MVMT", "NEG", "NEQ", "NOP", "NOT", "NTCH", "ONS", "OR",
    "OSF", "OSFI", "OSR", "OSRI", "OTE", "OTL", "OTU", "PAUSE", "PID",
    "PIDE", "PMUL", "POSP", "RAD", "RES", "RESET", "RET", "RLIM", "RMPS",
    "RTO", "RTOR", "RTOS", "SBR", "SCL", "SCP", "SCRV", "SEL", "SFP",
    "SFR", "SIN", "SIZE", "SMAT", "SNEG", "SQI", "SQL", "SQO", "SQR",
    "SQRT", "SRT", "SRTP", "SRT_IO", "SSUM", "SSV", "STD", "STOD", "STOR",
    "SUB", "SWPB", "TAN", "THRS", "THRSE", "TND", "TOD", "TOF", "TOFR",
    "TON", "TONR", "TOT", "TRIM", "TRN", "TRUNC", "TSSI", "UID", "UIE",
    "UPDN", "UPPER", "XIC", "XIO", "XOR", "XPY",
})

# The single bucket every non-vendor mnemonic collapses into. Deliberately one
# bucket: distinguishing "custom AOI" from "unknown" requires the AOI name list,
# which this stage does not have. Counting them together is honest; guessing is
# not.
CUSTOM_BUCKET = "custom-or-unrecognized"

# Fixed strings this tool emits, plus vendor terms that legitimately appear as
# keys or values. Anything outside this and VENDOR_MNEMONICS fails verification.
STRUCTURAL_VALUES = frozenset({
    CUSTOM_BUCKET, "RLL", "ST", "FBD", "SFC", "Controller", "Program",
    "Routine", "AddOnInstruction", "Target", "Context", "true", "false",
    "RSLogix5000Content",
    # Task types and tag types -- small closed vendor vocabularies.
    # L5X writes these uppercase; both spellings are vendor terms either way.
    "Periodic", "Continuous", "Event",
    "PERIODIC", "CONTINUOUS", "EVENT",
    "Base", "Alias", "Produced", "Consumed", "Public",
})

# Fields whose keys are XML *element* names from the Rockwell schema. These are
# safe for a structural reason, not a listed one: customer identifiers live in
# attribute values (Name="..."), never in element names. The schema's element
# vocabulary is fixed by Rockwell, so a CamelCase key here cannot be a customer
# string. No other field gets this exemption.
SCHEMA_KEYED = frozenset({"element_histogram"})

# Histogram fields whose keys are mnemonic-shaped and must be bucketed.
MNEMONIC_KEYED = ("mnemonics", "mnemonics_program", "mnemonics_aoi", "st_functions")

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

# Dict fields where both keys and values are safe. Keys here are XML element
# names or snake_case labels this toolchain defines -- never export content.
#
# `mnemonics`, `mnemonics_program`, `mnemonics_aoi`, `st_functions` and
# `operands` USED TO BE IN THIS LIST. They are not safe: their keys are
# mnemonics, and an AOI invocation is a mnemonic. They are handled explicitly
# in redact_file() now. Do not add a field here without checking its KEYS.
DICT_ALLOW = [
    "element_histogram",   # XML element names, all vendor schema
    "routine_types",       # RLL / ST / FBD / SFC
    "rungs",               # counts and text-length stats only
    "routine_totals",
    "alias_resolution",
    "calls",
]


def bucket_mnemonics(hist: dict) -> dict:
    """Keep vendor mnemonics by name; collapse everything else into a count.

    This is the fix. A name absent from the vendor vocabulary is either a
    customer AOI or an instruction we have not catalogued, and this stage
    cannot tell which -- so it emits neither name.
    """
    kept = {k: v for k, v in hist.items() if str(k).upper() in VENDOR_MNEMONICS}
    suppressed = {k: v for k, v in hist.items() if str(k).upper() not in VENDOR_MNEMONICS}
    if suppressed:
        kept[CUSTOM_BUCKET] = sum(suppressed.values())
        kept[CUSTOM_BUCKET + "-distinct"] = len(suppressed)
    return kept


def redact_operands(operands: dict) -> dict:
    """`operands` is a mix of numeric sub-dicts and one mnemonic-keyed histogram."""
    out: dict = {}
    for key, value in operands.items():
        if not isinstance(value, dict):
            out[key] = value
        elif key == "write_dest_unknown":
            # Keys here are the mnemonics whose destination operand could not be
            # located -- overwhelmingly AOI calls, since an AOI's outputs are
            # defined per-instance rather than at a fixed position.
            out[key] = bucket_mnemonics(value)
        else:
            out[key] = value
    return out


def verify_no_free_text(redacted: list[dict]) -> list[str]:
    """Walk the finished output and report any key that is not provably safe.

    The allowlist is now checked rather than trusted. A key survives only if it
    is a vendor mnemonic, a structural literal, or a snake_case/lowercase label
    this toolchain defines. Customer identifiers -- `AOI_DigIn_5_02`,
    `PalletizerLine3` -- match none of those.
    """
    offenders: list[str] = []
    emitted_fields = frozenset(SCALAR_ALLOW) | frozenset(DICT_ALLOW) | frozenset(MNEMONIC_KEYED)

    def ok(key: str, in_schema_field: bool) -> bool:
        k = str(key)
        if k.upper() in VENDOR_MNEMONICS or k in STRUCTURAL_VALUES:
            return True
        if k in emitted_fields:                   # field names this tool chose
            return True
        if k.split("[")[0] in BUILTIN_TYPES:      # DINT, DINT[10], TIMER
            return True
        if re.fullmatch(r"[a-z0-9_.\-]+", k):     # our own snake_case labels
            return True
        # Only inside element_histogram: XML element names from the schema.
        # NCName-shaped, so underscores and digits are legal.
        return in_schema_field and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", k))

    def walk(node, path: str, in_schema_field: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not ok(key, in_schema_field):
                    offenders.append(f"{path}.{key}")
                walk(value, f"{path}.{key}", in_schema_field or key in SCHEMA_KEYED)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", in_schema_field)

    for i, rec in enumerate(redacted):
        walk(rec, f"file_index {i}")
    return offenders


def redact_file(rec: dict, index: int) -> dict:
    out: dict = {"file_index": index}

    for key in SCALAR_ALLOW:
        if key in rec:
            out[key] = rec[key]

    for key in DICT_ALLOW:
        if key in rec:
            out[key] = rec[key]

    # Mnemonic-keyed histograms: bucket anything not in the vendor vocabulary.
    for key in MNEMONIC_KEYED:
        if key in rec and isinstance(rec[key], dict):
            out[key] = bucket_mnemonics(rec[key])

    if isinstance(rec.get("operands"), dict):
        out["operands"] = redact_operands(rec["operands"])

    # Tags: keep the type and description distributions, drop scope names.
    tags = rec.get("tags")
    if tags:
        datatypes = tags.get("top_datatypes", {})
        # Keep only builtins; see BUILTIN_TYPES.
        kept = {k: v for k, v in datatypes.items() if k.split("[")[0] in BUILTIN_TYPES}
        udt_typed = sum(v for k, v in datatypes.items()
                        if k.split("[")[0] not in BUILTIN_TYPES)
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
    ap.add_argument("--explain-leak", action="store_true",
                    help="on a verification failure, print the offending key "
                         "paths to stderr. Local diagnosis only -- the output "
                         "contains the customer names that failed the check.")
    args = ap.parse_args()

    records = json.loads(Path(args.survey_json).read_text(encoding="utf-8"))
    if isinstance(records, dict):
        records = [records]

    redacted = [redact_file(r, i) for i, r in enumerate(records)]

    # Fail closed. Nothing is written until the output has been checked, so a
    # future field whose keys are free text stops the tool instead of leaking.
    offenders = verify_no_free_text(redacted)
    if offenders:
        print(
            f"REFUSING TO WRITE: {len(offenders)} key(s) are neither vendor "
            f"vocabulary nor structural labels.\n"
            f"The offending paths are not printed, because printing them would "
            f"be the disclosure this tool exists to prevent.\n"
            f"Run with --explain-leak to see them locally.",
            file=sys.stderr,
        )
        if args.explain_leak:
            for path in offenders:
                print(f"  {path}", file=sys.stderr)
        return 1

    if args.check:
        for i, (orig, red) in enumerate(zip(records, redacted)):
            print(f"file_index {i} dropped: {', '.join(dropped_keys(orig, red))}")
        return 0

    json.dump(redacted, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
