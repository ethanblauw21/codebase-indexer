#!/usr/bin/env python3
"""Shared neutral-text splitter for the L5X Phase 0 diagnostics.

Three scripts (`l5x_survey_v2.py`, `l5x_check_bindings.py`, and `Scrubber.rung`
in `l5x_scrub_rungs.py`) each carried their own copy of the instruction
splitter, and each copy had the same bug. They now all call this one.

The bug: the splitter tracked parenthesis depth but not bracket depth, so a
two-dimensional array subscript like `Recipe[Row,Col]` split at the comma
inside the subscript and yielded an extra operand. That inflated operand
counts and fed malformed base names into `classify_operand`, which in turn
inflated the unresolved-operand count.

The earlier docstring justified ignoring brackets by noting that ladder
*branch* brackets `[a,b]` sit at paren depth zero, so their commas cannot
split an enclosing instruction's operands. That much is true, but it is a
statement about a different use of brackets. Subscript brackets *inside* an
instruction's parens are the case that breaks, and they were never considered.

A comma separates operands only where paren depth is 1 and bracket depth is 0.

This is diagnostic code, not production code. It imports nothing from `src/`
and adds no dependencies.
"""

from __future__ import annotations

import re

# The instruction token immediately preceding an open paren. Identical to the
# three copies it replaces, so results stay comparable to the pre-fix survey.
MNEMONIC_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,31})\(")


def scan_instructions(text: str):
    """Yield `(mnemonic, raw_operands, start, end, closed)` for each instruction.

    `start` indexes the first character of the mnemonic and `end` the position
    just past the matching close paren, so a caller can rewrite the span in
    place. `closed` is False when the text ran out before the paren closed;
    callers that care about malformed input can count those rather than
    silently absorbing them.

    Operands are returned unstripped. Nested instruction calls and bracketed
    subscripts are left intact inside the operand that contains them.
    """
    for m in MNEMONIC_RE.finditer(text):
        i = m.end()
        start, paren, brack, args = i, 1, 0, []
        closed = False
        while i < len(text):
            c = text[i]
            if c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
                if paren == 0:
                    args.append(text[start:i])
                    closed = True
                    break
            elif c == "[":
                brack += 1
            elif c == "]":
                # max() guards against a stray close bracket unbalancing the
                # rest of the rung.
                brack = max(0, brack - 1)
            elif c == "," and paren == 1 and brack == 0:
                args.append(text[start:i])
                start = i + 1
            i += 1
        if not closed:
            # Unterminated: keep the trailing fragment rather than dropping it,
            # so operand counts do not silently under-report.
            args.append(text[start:i])
        yield m.group(1), args, m.start(), i + 1, closed


def iter_instructions(text: str):
    """Yield `(mnemonic, [operands])`, operands stripped and blanks dropped.

    The signature the three scripts already expected.
    """
    for mnemonic, args, _start, _end, _closed in scan_instructions(text):
        yield mnemonic, [a.strip() for a in args if a.strip()]
