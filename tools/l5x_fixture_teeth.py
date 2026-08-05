#!/usr/bin/env python3
"""Do the L5X conformance fixtures have teeth? (ADR-013 §5.2)

A fixture that scores 1.000 both with and without a fix guards nothing. This
reverts each established correction in memory, re-scores every fixture, and
checks that the intended one — and only the intended one — fails.

Run it after changing `l5x_instructions.py` or the operand scanner. It needs no
corpus; it runs entirely against the committed synthetic fixtures.

One reversion is expected NOT to be caught: collapsing empty operand slots is
masked because GSV and SSV are the only instructions carrying holes and their
role tuples absorb a positional shift. That is recorded in ADR-013 §5.2 rather
than papered over, and is asserted at the scanner in tests/test_l5x_adapter.py.

    python tools/l5x_fixture_teeth.py           # report
    python tools/l5x_fixture_teeth.py --check   # exit 1 on a missing guard
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
FIX = ROOT / "tests" / "fixtures" / "conformance" / "l5x"

import adapters.l5x_adapter as A  # noqa: E402
import adapters.l5x_instructions as I  # noqa: E402


def score(feature):
    src = FIX / f"{feature}.L5X"
    exp = json.loads((FIX / f"{feature}.expected.json").read_text(encoding="utf-8"))
    r = A.L5xAdapter().parse(str(src), src.read_bytes())
    got_s = {(s.fqn, s.kind) for s in r.symbols}
    want_s = {(s["fqn"], s["kind"]) for s in exp["symbols"]}
    got_e = {(e.source_fqn, e.target, e.kind) for e in r.edges}
    want_e = {(e["source"], e["target"], e["kind"]) for e in exp["edges"]}
    return got_s == want_s and got_e == want_e, (got_e - want_e, want_e - got_e)


FEATURES = ["write_position_swpb_btd", "gsv_ssv_system", "fal_expression",
            "module_nameless", "whole_controller", "array_subscript_2d",
            "mnemonic_canonical", "mnemonic_alias", "aoi_definition",
            "alias_module_io", "jsr_same_program"]

_ARGS = argparse.ArgumentParser(description=__doc__.split("\n")[0])
_ARGS.add_argument("--check", action="store_true",
                   help="exit 1 if a correction has no guard that can fail")
ARGS = _ARGS.parse_args()

FAILURES: list[str] = []

print("baseline:")
for f in FEATURES:
    ok, _ = score(f)
    if not ok:
        FAILURES.append(f"baseline: {f} does not score clean")
    print(f"  {f:<28} {'PASS' if ok else 'FAIL'}")


def teeth(label, mutate, restore, expect_fail, unguardable_reason=None):
    """Revert one fix; the intended fixture must notice.

    `unguardable_reason` marks a correction no fixture can catch. It is
    tolerated rather than counted as a gap, but the reason must be stated —
    and it is a fact about this corpus rather than a permanent property.
    """
    mutate()
    broken = [f for f in FEATURES if not score(f)[0]]
    restore()
    hit = expect_fail in broken

    if hit:
        status = f"caught by {expect_fail}"
    elif unguardable_reason:
        status = f"not catchable — {unguardable_reason}"
    else:
        status = "*** NOT CAUGHT — this correction has no guard ***"
        FAILURES.append(f"{label}: reverted with no fixture noticing")

    print(f"  {label:<44} {status}")
    if broken and broken != [expect_fail]:
        print(f"{'':46}   (also failing: {broken})")


print("\nteeth checks — revert a fix, see which fixture notices:")

_gsv = I.INSTRUCTIONS["GSV"]
teeth("GSV[1] typed TAG again (false read edge)",
      lambda: I.INSTRUCTIONS.__setitem__("GSV", I._sig(
          (I.KEYWORD, I.TAG, I.KEYWORD, I.TAG), writes=(-1,), last_role=I.TAG)),
      lambda: I.INSTRUCTIONS.__setitem__("GSV", _gsv),
      "gsv_ssv_system")

_ssv = I.INSTRUCTIONS["SSV"]
teeth("SSV given a write position",
      lambda: I.INSTRUCTIONS.__setitem__("SSV", I._sig(
          (I.KEYWORD, I.KEYWORD, I.KEYWORD, I.TAG), writes=(-1,), last_role=I.TAG)),
      lambda: I.INSTRUCTIONS.__setitem__("SSV", _ssv),
      "gsv_ssv_system")

_swpb = I.INSTRUCTIONS["SWPB"]
teeth("SWPB destination back to index 1",
      lambda: I.INSTRUCTIONS.__setitem__("SWPB", I._sig(
          (I.TAG, I.KEYWORD, I.TAG), writes=(1,))),
      lambda: I.INSTRUCTIONS.__setitem__("SWPB", _swpb),
      "write_position_swpb_btd")

_btd = I.INSTRUCTIONS["BTD"]
teeth("BTD destination back to last (per docs)",
      lambda: I.INSTRUCTIONS.__setitem__("BTD", I._sig(
          (I.TAG, I.LITERAL, I.TAG, I.LITERAL, I.LITERAL), writes=(4,))),
      lambda: I.INSTRUCTIONS.__setitem__("BTD", _btd),
      "write_position_swpb_btd")

_fal = I.INSTRUCTIONS["FAL"]
teeth("FAL destination back to last (per docs)",
      lambda: I.INSTRUCTIONS.__setitem__("FAL", I._sig(
          (I.TAG, I.LITERAL, I.LITERAL, I.KEYWORD, I.TAG, I.EXPR), writes=(0, 5))),
      lambda: I.INSTRUCTIONS.__setitem__("FAL", _fal),
      "fal_expression")

_scan = A.scan_instructions
_orig_src = None


def drop_empties():
    def scan_dropping(text):
        for mn, args, s, e in _scan(text):
            yield mn, [a for a in args if a.strip()], s, e
    A.scan_instructions = scan_dropping


teeth("empty operand slots dropped (position shift)",
      drop_empties,
      lambda: setattr(A, "scan_instructions", _scan),
      "gsv_ssv_system",
      unguardable_reason="masked ON THIS CORPUS ONLY; see note below")

_fqn = A._Extractor._module_fqn


def first_port():
    def mf(self, mod):
        name = mod.attrib.get("Name")
        if name:
            return name
        parent = mod.attrib.get("ParentModule")
        pid = mod.attrib.get("ParentModPortId")
        addr = next((p.attrib.get("Address") for p in mod.iter("Port")
                     if p.attrib.get("Address")), None)
        if not parent or pid is None or addr is None:
            return None
        return f"{parent}:{pid}:{addr}"
    A._Extractor._module_fqn = mf


teeth("module address from first port, not upstream",
      first_port,
      lambda: setattr(A._Extractor, "_module_fqn", _fqn),
      "module_nameless")


def drop_nameless():
    def mf(self, mod):
        return mod.attrib.get("Name")
    A._Extractor._module_fqn = mf


teeth("nameless modules skipped entirely (the original bug)",
      drop_nameless,
      lambda: setattr(A._Extractor, "_module_fqn", _fqn),
      "module_nameless")


print(
    "\nNote on the unguardable row. It is unguardable ON THIS CORPUS, not in\n"
    "principle. GSV and SSV are the only instructions here that carry operand\n"
    "holes, and their role tuples are uniform keywords followed by a\n"
    "last-position destination, so a positional shift re-types every operand to\n"
    "what it already was. An instruction with heterogeneous roles and an omitted\n"
    "operand would be mis-typed immediately. A different controller mix makes\n"
    "this catchable, so the row is a corpus fact rather than a permanent\n"
    "property — do not read seven-of-eight as a ceiling."
)

if ARGS.check:
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for problem in FAILURES:
            print(f"  - {problem}")
        raise SystemExit(1)
    print("\nEvery correction has a guard that can fail.")

raise SystemExit(0)
