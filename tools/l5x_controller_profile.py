#!/usr/bin/env python3
"""Structural triage of an L5X controller — what an engineer reads FIRST.

Asked what they do when handed a controller they did not write, a practicing
controls engineer described a structural profile, not a search:

    "I usually start by looking at how many routines there are, and how big
    (how many rungs) each routine is. Sometimes you could have only 1 to 3
    routines, but they contain all the logic for the whole system [...] Also,
    another key factor is if the tags have good descriptions/any descriptions.
    Or if they are named in an easy to follow, logical way. Rung comments are
    also helpful, but not a necessity."

Every quantity in that answer was already computed by the Phase-0 survey and
then thrown away. This is a report, not a retrieval feature, and it is gated on
nothing -- no gold queries, no ranking, no embedder, no GPU. See
docs/l5x-retrieval-requirements.md F-3.

## What it deliberately does NOT do

It does not score naming quality. "Named in an easy to follow, logical way" is a
judgment an engineer makes in a second and a metric would get wrong in ways that
are hard to notice; the separator and token-shape distributions are reported so a
reader can make that judgment, and no number pretends to have made it for them.

## Redaction

`--redact` replaces every controller, program, routine and AOI name with a
stable placeholder, so the output can be pasted into an issue or a commit
message. The corpus this was built against is live customer control logic; run
it redacted unless the output is staying on your own machine.

    python tools/l5x_controller_profile.py <file-or-dir> [--redact] [--top N]
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.l5x_adapter import _describe, _text_of  # noqa: E402

# A routine at or above this share of the controller's rungs is doing the job
# several routines usually split. The engineer's "1 to 3 routines that contain
# all the logic for the whole system" is exactly this shape.
MONOLITH_SHARE = 0.25


class _Redactor:
    """Stable placeholder per real name, so two mentions still line up."""

    def __init__(self, on: bool):
        self.on = on
        self._seen: dict[tuple[str, str], str] = {}

    def __call__(self, kind: str, name: str) -> str:
        if not self.on:
            return name
        key = (kind, name)
        if key not in self._seen:
            self._seen[key] = f"<{kind}{len(self._seen) + 1:03d}>"
        return self._seen[key]


def _rungs(routine) -> tuple[int, int]:
    """(rung count, how many carry a comment)."""
    total = commented = 0
    for rung in routine.iter("Rung"):
        if not _text_of(rung.find("Text")).strip():
            continue
        total += 1
        if _text_of(rung.find("Comment")).strip():
            commented += 1
    return total, commented


def _name_shape(names: list[str]) -> dict:
    """Separator and token-shape distribution. Evidence, not a score."""
    sep = Counter()
    tokens = []
    for n in names:
        if "_" in n:
            sep["underscore"] += 1
        elif any(c.isupper() for c in n[1:]) and not n.isupper():
            sep["camel/Pascal"] += 1
        else:
            sep["single token"] += 1
        tokens.append(len([t for t in n.replace("_", " ").split() if t]) or 1)
    return {
        "separators": dict(sep.most_common()),
        "median_len": int(statistics.median([len(n) for n in names])) if names else 0,
        "median_tokens": int(statistics.median(tokens)) if tokens else 0,
    }


def profile(path: Path, red: _Redactor) -> dict:
    root = ET.parse(path).getroot()
    controller = root.find("Controller")
    if controller is None:
        return {"path": path.name, "error": "no Controller element"}

    target_type = root.attrib.get("TargetType", "?")
    contains_context = root.attrib.get("ContainsContext", "?")

    routines: list[dict] = []
    rung_total = comment_total = 0
    by_type = Counter()

    for prog in controller.iter("Program"):
        pname = prog.attrib.get("Name") or "?"
        for r in prog.iter("Routine"):
            rname = r.attrib.get("Name") or "?"
            rtype = r.attrib.get("Type", "RLL")
            n, c = _rungs(r)
            by_type[rtype] += 1
            rung_total += n
            comment_total += c
            routines.append({
                "fqn": f"{red('prog', pname)}.{red('rtn', rname)}",
                "type": rtype, "rungs": n, "commented": c,
            })

    aoi_rungs = 0
    aoi_count = 0
    for aoi in controller.iter("AddOnInstructionDefinition"):
        aoi_count += 1
        for r in aoi.iter("Routine"):
            n, c = _rungs(r)
            aoi_rungs += n
            rung_total += n
            comment_total += c

    tag_names: list[str] = []
    described = aliased = 0
    for tags_elem in controller.iter("Tags"):
        for tag in tags_elem.findall("Tag"):
            name = tag.attrib.get("Name")
            if not name:
                continue
            tag_names.append(name)
            if _describe(tag):
                described += 1
            if tag.attrib.get("AliasFor"):
                aliased += 1

    counts = [r["rungs"] for r in routines if r["rungs"]]
    return {
        "path": path.name if not red.on else red("file", path.name),
        "controller": red("ctlr", controller.attrib.get("Name") or "?"),
        "target_type": target_type,
        "contains_context": contains_context,
        "sw_rev": root.attrib.get("SoftwareRevision", "?"),
        "processor": controller.attrib.get("ProcessorType", "?"),
        "programs": sum(1 for _ in controller.iter("Program")),
        "routines": len(routines),
        "routine_types": dict(by_type.most_common()),
        "aois": aoi_count,
        "aoi_rungs": aoi_rungs,
        "rungs": rung_total,
        "commented": comment_total,
        "rung_stats": {
            "median": statistics.median(counts) if counts else 0,
            "mean": round(statistics.mean(counts), 1) if counts else 0,
            "max": max(counts) if counts else 0,
        },
        "tags": len(tag_names),
        "described": described,
        "aliased": aliased,
        "naming": _name_shape(tag_names),
        "biggest": sorted(routines, key=lambda r: -r["rungs"]),
    }


def _pct(n, d) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    --"


def render(p: dict, top: int) -> None:
    if "error" in p:
        print(f"{p['path']}: {p['error']}")
        return

    print(f"=== {p['path']}  ({p['controller']}) ===")
    print(f"  {p['processor']}, SoftwareRevision {p['sw_rev']}, "
          f"TargetType={p['target_type']} ContainsContext={p['contains_context']}")
    if p["target_type"] != "Controller" or p["contains_context"] != "false":
        print("  !! not a whole-controller export -- the adapter refuses these")
    print()

    print(f"  SIZE      {p['programs']} program(s), {p['routines']} routine(s), "
          f"{p['aois']} AOI definition(s), {p['rungs']} rung(s)")
    print(f"            routine types: {p['routine_types'] or '{}'}")
    st = p["rung_stats"]
    print(f"            rungs per routine: median {st['median']}, "
          f"mean {st['mean']}, max {st['max']}")
    if p["rungs"]:
        print(f"            {_pct(p['aoi_rungs'], p['rungs'])} of the ladder is "
              f"inside AOI definitions ({p['aoi_rungs']} rung(s))")
    print()

    print(f"  READABLE  tag descriptions   {p['described']:>5}/{p['tags']:<5} "
          f"{_pct(p['described'], p['tags'])}")
    print(f"            rung comments      {p['commented']:>5}/{p['rungs']:<5} "
          f"{_pct(p['commented'], p['rungs'])}")
    print(f"            aliases (to I/O)   {p['aliased']:>5}/{p['tags']:<5} "
          f"{_pct(p['aliased'], p['tags'])}")
    n = p["naming"]
    print(f"            tag naming: {n['separators']}, "
          f"median {n['median_len']} chars / {n['median_tokens']} token(s)")
    print("            (naming quality is deliberately not scored -- judge it "
          "from the shape above)")
    print()

    print(f"  WHERE THE LOGIC IS  (top {top} routines by rung count)")
    if not p["biggest"]:
        print("            no routines with rungs")
    for r in p["biggest"][:top]:
        share = r["rungs"] / p["rungs"] if p["rungs"] else 0
        flag = "  <-- MONOLITH" if share >= MONOLITH_SHARE else ""
        print(f"            {r['rungs']:>5} rung(s) {_pct(r['rungs'], p['rungs'])}"
              f"  {r['fqn']} [{r['type']}]"
              f"  comments {_pct(r['commented'], r['rungs'])}{flag}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="an .L5X file or a directory of them")
    ap.add_argument("--redact", action="store_true",
                    help="replace every real name with a stable placeholder")
    ap.add_argument("--top", type=int, default=10,
                    help="how many routines to list by size (default 10)")
    args = ap.parse_args()

    target = Path(args.target)
    files = sorted(target.glob("*.L5X")) if target.is_dir() else [target]
    if not files:
        print(f"no .L5X files under {target}")
        return 1

    red = _Redactor(args.redact)
    for f in files:
        render(profile(f, red), args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
