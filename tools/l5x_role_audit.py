#!/usr/bin/env python3
"""Audit the L5X operand-role table against a real corpus (ADR-013 §5.1).

This is a permanent check, not one of the retiring diagnostics under
`tools/l5x/`. It lives here with `conformance_eval.py` and `resolution_eval.py`
because it cannot become a fixture: it needs a corpus of real controllers, and
the only corpus available is confidential.

## What it checks

Every table entry makes falsifiable predictions about each operand position.
Three predicates, and the third exists because the first two have a blind spot
that hid a live bug:

1. A LITERAL/KEYWORD/LABEL position should not hold a declared tag.
   Catches a role that is too narrow.
2. A WRITE position should hold something resolvable, or the write edge is
   bogus. Catches a destination pointed at the wrong operand.
3. A TAG position should not hold *undeclared names*. Catches a role that is
   too broad — the case predicates 1 and 2 cannot see, because a wrong TAG is
   neither a keyword holding a tag nor a write that fails to resolve.

Predicate 3 is what found `GSV[1]`. That position was typed TAG on the
reasoning that a keyword typed as TAG merely fails to resolve while a tag typed
as KEYWORD loses a real edge. Sound in general, false there: profiling what the
operand actually named showed every resolvable one was a *module*, and the
handful matching a declared tag were that module's alias tag colliding by name.
The result was 29 spurious resolution failures and 6 false read edges.

Note that a low tag *fraction* at a TAG position is not a defect. `ADD(A,5,D)`
legitimately puts a literal at index 1, and literals are skipped harmlessly.
Only undeclared names matter, which is why predicate 3 counts those and not
"positions that mostly hold literals" — an earlier draft did the latter and
produced 22 false alarms against 1 real finding.

## Output

Counts and mnemonics only. No tag names, routine names or rung text, so the
output is safe to paste into an issue or a commit message.

    python tools/l5x_role_audit.py <corpus-dir>
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.l5x_adapter import (  # noqa: E402
    is_module_io, scan_instructions, split_operand,
)
from adapters.l5x_instructions import (  # noqa: E402
    EXPR, INSTRUCTIONS, KEYWORD, LABEL, LITERAL, TAG,
    canonical_mnemonic,
)

_NON_TAG = (LITERAL, KEYWORD, LABEL)


def collect(corpus: Path):
    """(mnemonic -> (arity, position) -> operand-kind counts), plus occurrences."""
    obs = defaultdict(lambda: defaultdict(Counter))
    seen = Counter()

    # Deduplicated by resolved path: on a case-insensitive filesystem globbing
    # both spellings returns every file twice and doubles every count.
    files = {p.resolve(): p
             for p in list(corpus.rglob("*.L5X")) + list(corpus.rglob("*.l5x"))}

    for path in sorted(files.values()):
        try:
            root = ET.fromstring(path.read_bytes().decode("utf-8-sig"))
        except (ET.ParseError, UnicodeDecodeError):
            print(f"  ! skipped unparseable {path.name}", file=sys.stderr)
            continue

        declared = set()
        for kind in ("Tag", "Parameter", "LocalTag"):
            for elem in root.iter(kind):
                if elem.attrib.get("Name"):
                    declared.add(elem.attrib["Name"])

        # Names of CIP objects an instruction can address by name. A GSV or SSV
        # instance operand names one of these, and it is NOT a tag read even
        # when a tag happens to share the name — in this corpus the collisions
        # were module alias tags, which is how a keyword position came to look
        # like a tag position.
        objects = set()
        for kind in ("Module", "Program", "Task", "Routine",
                     "AddOnInstructionDefinition"):
            for elem in root.iter(kind):
                if elem.attrib.get("Name"):
                    objects.add(elem.attrib["Name"])

        for rung in root.iter("Rung"):
            node = rung.find("Text")
            if node is None:
                continue
            body = "".join(node.itertext())
            spans = [(s, e) for _m, _o, s, e in scan_instructions(body)]

            for raw, operands, start, end in scan_instructions(body):
                # A nested call's operands are expression fragments belonging
                # to the enclosing instruction; scoring them by their own
                # signature is meaningless.
                if any(s2 < start and e2 >= end
                       for s2, e2 in spans if (s2, e2) != (start, end)):
                    continue
                mnemonic = canonical_mnemonic(raw)
                if mnemonic not in INSTRUCTIONS:
                    continue

                operands = [o.strip() for o in operands]
                seen[mnemonic] += 1
                arity = len(operands)
                for i, operand in enumerate(operands):
                    if not operand:
                        obs[mnemonic][(arity, i)]["empty"] += 1
                        continue
                    base, _ = split_operand(operand)
                    if base is None:
                        kind = "literal"
                    elif is_module_io(base):
                        kind = "tag"
                    elif base in declared and base in objects:
                        # Ambiguous: names a tag AND a CIP object. Reported,
                        # never counted as evidence that a role is wrong.
                        kind = "tag+object"
                    elif base in declared:
                        kind = "tag"
                    elif base in objects:
                        kind = "object"
                    else:
                        kind = "undeclared"
                    obs[mnemonic][(arity, i)][kind] += 1
    return obs, seen


def audit(obs, seen):
    problems = []
    positions = 0

    for mnemonic in sorted(seen):
        sig = INSTRUCTIONS[mnemonic]
        for (arity, pos), kinds in sorted(obs[mnemonic].items()):
            positions += 1
            role = sig.role_at(pos, arity)
            total = sum(kinds.values())
            writes = sig.writes_at(pos, arity) or sig.both_at(pos, arity)

            # 1 — a non-tag role holding real tags
            if role in _NON_TAG and kinds["tag"]:
                problems.append(
                    f"{mnemonic}[{pos}] of {arity}-operand form is typed "
                    f"{role} but {kinds['tag']}/{total} occurrences are real "
                    f"tags — role too narrow, edges are being dropped")

            # 2 — a write position that cannot resolve
            if writes and (kinds["undeclared"] or kinds["literal"]):
                bad = kinds["undeclared"] + kinds["literal"]
                problems.append(
                    f"{mnemonic}[{pos}] of {arity}-operand form is a WRITE "
                    f"position but {bad}/{total} occurrences are not "
                    f"resolvable tags — destination points at the wrong operand")

            # 2b — a write position the adapter will skip before reaching it
            if writes and role in (LITERAL, KEYWORD, LABEL, EXPR):
                problems.append(
                    f"{mnemonic}[{pos}] of {arity}-operand form is a WRITE "
                    f"position typed {role}; non-tag roles are skipped BEFORE "
                    f"`writes` is consulted, so this write is silently lost")

            # 3 — a TAG role holding undeclared names or CIP object names
            if role == TAG and kinds["object"]:
                problems.append(
                    f"{mnemonic}[{pos}] of {arity}-operand form is typed TAG "
                    f"but {kinds['object']}/{total} occurrences name a CIP "
                    f"object (module/program/task), not a tag — this emits "
                    f"false read edges")
            if role == TAG and kinds["undeclared"]:
                problems.append(
                    f"{mnemonic}[{pos}] of {arity}-operand form is typed TAG "
                    f"but {kinds['undeclared']}/{total} occurrences name "
                    f"nothing declared — role too broad, likely a keyword "
                    f"position emitting unresolved reads")

    return problems, positions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("corpus", help="directory of .L5X exports to audit")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"not a directory: {corpus}", file=sys.stderr)
        return 2

    obs, seen = collect(corpus)
    if not seen:
        print("no L5X instructions found", file=sys.stderr)
        return 2

    problems, positions = audit(obs, seen)
    never = sorted(m for m in INSTRUCTIONS if m not in seen)
    unverified = sorted(m for m, s in INSTRUCTIONS.items() if not s.verified)

    print(f"table entries:        {len(INSTRUCTIONS)}")
    print(f"exercised by corpus:  {len(seen)}")
    print(f"never exercised:      {len(never)}")
    print(f"carried unverified:   {len(unverified)}")
    print(f"positions checked:    {positions}")
    print()

    if problems:
        print(f"PREDICTION FAILURES ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
    else:
        print("No prediction failed.")

    # These two sets are NOT the same set, and an earlier version of this
    # output implied they were -- it printed the never-exercised list under a
    # "carried as verified=False" caption, and ADR-013 copied the wrong count
    # out of it. They disagree in both directions, and each direction means
    # something different.
    if never:
        print(f"\nNo corpus evidence ({len(never)}):")
        print("  " + ", ".join(never))
    if unverified:
        print(f"\nCarried as verified=False ({len(unverified)}):")
        print("  " + ", ".join(unverified))

    exercised_but_unverified = [m for m in unverified if m in seen]
    unexercised_but_verified = [m for m in never if m not in unverified]
    if exercised_but_unverified:
        print("\nExercised by the corpus yet still marked unverified -- the "
              "evidence to settle these is in hand:")
        print("  " + ", ".join(sorted(exercised_but_unverified)))
    if unexercised_but_verified:
        print("\nNo corpus evidence yet marked verified -- justified only "
              "where the entry makes no positional claim (a zero-operand "
              "instruction cannot have its operand order be wrong):")
        print("  " + ", ".join(sorted(unexercised_but_verified)))

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
