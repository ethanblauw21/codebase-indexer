"""Rockwell Logix instruction model: mnemonic canonicalization + operand roles.

Two tables, kept apart from the adapter because they are data, not logic — a
correction here is one line plus a reindex (ADR-013).

**Canonicalize first, then look up roles.** Both tables key on canonical
spellings. Looking up an alias spelling directly finds nothing, which is how
677 corpus occurrences of MOVE/LIMIT ended up with no operand role at all.

## Why roles rather than a destination index

An earlier model stored only "which operand is the destination". That is not
enough. It cannot tell a tag reference from a label, a mode keyword, or an
expression, so every non-tag operand was misread as an unresolved tag
reference — 520 of them across the survey corpus, which read as a 2.0% tag
resolution failure when the real rate is ~100%. Roles fix the destination
question and the classification question at once.

## Provenance

Operand orders were verified by profiling operand kinds by position across a
5-file, 3,935-rung production corpus: a Destination must be a declared tag,
while bit offsets, lengths and mode keywords are literals or keyword strings,
so a wrong ordering falsifies immediately. That method corrected two entries
that secondary documentation got wrong (BTD, FAL) — those sources describe the
Studio 5000 dialog field list, which is NOT the neutral-text operand order.

Entries marked INFERRED below did not appear in that corpus and are carried
from the instruction set without corpus confirmation. Treat a wrong entry as a
silent, permanent source of wrong edges: prefer emitting no write edge over
guessing one.
"""
from __future__ import annotations

# --------------------------------------------------------------- mnemonics

# Alias spelling -> canonical spelling. In the survey corpus one file (the only
# SoftwareRevision 36.04 export) used the alias spelling for all eight pairs and
# the other four used the canonical spelling for all eight, with no file mixing
# them. Only MOVE is semantically confirmed, from the EQ(t,0)MOVE(1,t)
# default-value idiom plus an operand-kind profile identical to MOV's. The other
# seven are inferred from that pattern and from matching arity within each pair.
#
# There is deliberately NO config flag for this. The canonical spelling feeds
# edges and stable IDs, so a flag that changed it would silently invalidate
# every ID in the index with no schema bump and no loud failure. Storing the
# raw spelling alongside the canonical one gives the opt-out without the knob.
MNEMONIC_ALIASES = {
    "MOVE": "MOV",
    "EQ":   "EQU",
    "NE":   "NEQ",
    "LT":   "LES",
    "GT":   "GRT",
    "LE":   "LEQ",
    "GE":   "GEQ",
    "LIMIT": "LIM",
}


def canonical_mnemonic(mnemonic: str) -> str:
    """Canonical spelling for `mnemonic`, or the input unchanged."""
    return MNEMONIC_ALIASES.get(mnemonic, mnemonic)


# ------------------------------------------------------------------- roles

TAG     = "tag"          # a tag reference; resolvable, and read unless written
LITERAL = "literal"      # an immediate value
KEYWORD = "keyword"      # instruction syntax (mode selectors, GSV class names)
LABEL   = "label"        # a JMP/LBL label name, not a tag
EXPR    = "expression"   # free-form expression text, parsed separately
ROUTINE = "routine"      # a JSR-style call target, resolved against routines

# Trailing role, repeated for any operand beyond the declared signature.
# JSR takes a variable number of parameter operands after its target and count.
_REPEAT = "..."


class Signature:
    """Operand roles for one instruction, plus which positions it writes.

    `writes` holds operand indices; -1 means the last operand, which matters
    for instructions like GSV that appear in both 3- and 4-operand forms.
    `both` holds indices that are read AND written (InOut-style semantics).
    """

    __slots__ = ("roles", "writes", "both", "verified", "last_role")

    def __init__(self, roles, writes=(), both=(), verified=True, last_role=None):
        self.roles = tuple(roles)
        self.writes = frozenset(writes)
        self.both = frozenset(both)
        self.verified = verified
        # Role of the FINAL operand regardless of arity. Needed by instructions
        # that appear in several arities with the meaningful operand last:
        # GSV/SSV take (Class, Instance, Attribute, Tag) but also a 3-operand
        # form, so a fixed-position role puts KEYWORD where the tag actually is.
        # That is not cosmetic - the adapter skips KEYWORD operands before it
        # ever consults `writes`, so a misplaced keyword silently suppresses a
        # write edge rather than merely mislabelling it.
        self.last_role = last_role

    def role_at(self, index: int, arity: int) -> str:
        if self.last_role is not None and index == arity - 1:
            return self.last_role
        if index < len(self.roles):
            role = self.roles[index]
            if role != _REPEAT:
                return role
        return self.roles[-2] if self.roles and self.roles[-1] == _REPEAT else TAG

    def writes_at(self, index: int, arity: int) -> bool:
        return index in self.writes or (-1 in self.writes and index == arity - 1)

    def both_at(self, index: int, arity: int) -> bool:
        return index in self.both or (-1 in self.both and index == arity - 1)


def _sig(roles, writes=(), both=(), verified=True, last_role=None):
    return Signature(roles, writes, both, verified, last_role)


INSTRUCTIONS: dict[str, Signature] = {
    # -- bit ------------------------------------------------------------
    "XIC": _sig((TAG,)),
    "XIO": _sig((TAG,)),
    "OTE": _sig((TAG,), writes=(0,)),
    "OTL": _sig((TAG,), writes=(0,)),
    "OTU": _sig((TAG,), writes=(0,)),
    "ONS": _sig((TAG,), writes=(0,)),
    "OSR": _sig((TAG, TAG), writes=(0, 1), verified=False),
    "OSF": _sig((TAG, TAG), writes=(0, 1), verified=False),

    # -- move / logical --------------------------------------------------
    "MOV":  _sig((TAG, TAG), writes=(1,)),
    "CLR":  _sig((TAG,), writes=(0,)),
    "CPT":  _sig((TAG, EXPR), writes=(0,)),
    # SWPB(Source, Order Mode, Dest). Was mapped to index 1, which pointed the
    # write edge at the REVERSE/WORD/HIGH-LOW keyword. 18 corpus occurrences.
    "SWPB": _sig((TAG, KEYWORD, TAG), writes=(2,)),
    # BTD(Source, Source Bit, Dest, Dest Bit, Length). Secondary documentation
    # claimed the destination was last; in all 17 corpus occurrences the last
    # two operands are literals and index 2 is always a declared tag.
    "BTD":  _sig((TAG, LITERAL, TAG, LITERAL, LITERAL), writes=(2,)),
    "MVM":  _sig((TAG, TAG, TAG), writes=(2,), verified=False),
    "AND":  _sig((TAG, TAG, TAG), writes=(2,)),
    "OR":   _sig((TAG, TAG, TAG), writes=(2,)),
    "XOR":  _sig((TAG, TAG, TAG), writes=(2,), verified=False),
    "NOT":  _sig((TAG, TAG), writes=(1,), verified=False),

    # -- compare (read-only) ---------------------------------------------
    "EQU": _sig((TAG, TAG)),
    "NEQ": _sig((TAG, TAG)),
    "LES": _sig((TAG, TAG)),
    "GRT": _sig((TAG, TAG)),
    "LEQ": _sig((TAG, TAG)),
    "GEQ": _sig((TAG, TAG)),
    "LIM": _sig((TAG, TAG, TAG)),
    "MEQ": _sig((TAG, TAG, TAG)),
    "CMP": _sig((EXPR,)),

    # -- math -------------------------------------------------------------
    "ADD": _sig((TAG, TAG, TAG), writes=(2,)),
    "SUB": _sig((TAG, TAG, TAG), writes=(2,)),
    "MUL": _sig((TAG, TAG, TAG), writes=(2,)),
    "DIV": _sig((TAG, TAG, TAG), writes=(2,)),
    "MOD": _sig((TAG, TAG, TAG), writes=(2,)),
    "ABS": _sig((TAG, TAG), writes=(1,)),
    "SQR": _sig((TAG, TAG), writes=(1,), verified=False),
    "NEG": _sig((TAG, TAG), writes=(1,), verified=False),

    # -- conversion --------------------------------------------------------
    "DTOS": _sig((TAG, TAG), writes=(1,)),
    "RTOS": _sig((TAG, TAG), writes=(1,)),
    "STOD": _sig((TAG, TAG), writes=(1,)),
    "STOR": _sig((TAG, TAG), writes=(1,), verified=False),

    # -- array / file ------------------------------------------------------
    "COP": _sig((TAG, TAG, TAG), writes=(1,)),
    "CPS": _sig((TAG, TAG, TAG), writes=(1,)),
    "FLL": _sig((TAG, TAG, TAG), writes=(1,)),
    "SIZE": _sig((TAG, LITERAL, TAG), writes=(2,)),
    # FAL(Control, Length, Position, Mode, Dest, Expression). Secondary
    # documentation put the destination last; corpus profiling shows index 5 is
    # always the expression and index 4 the only declared tag besides Control.
    # The Control structure is written as well as the destination.
    "FAL": _sig((TAG, LITERAL, LITERAL, KEYWORD, TAG, EXPR), writes=(0, 4)),
    # FSC has no destination - it updates only its Control structure.
    "FSC": _sig((TAG, LITERAL, LITERAL, KEYWORD, EXPR), writes=(0,)),

    # -- ASCII string -------------------------------------------------------
    "CONCAT": _sig((TAG, TAG, TAG), writes=(2,)),
    "MID":    _sig((TAG, TAG, LITERAL, TAG), writes=(3,)),
    "DELETE": _sig((TAG, TAG, LITERAL, TAG), writes=(3,)),
    "INSERT": _sig((TAG, TAG, LITERAL, TAG), writes=(3,)),
    "FIND":   _sig((TAG, TAG, LITERAL, TAG), writes=(3,), verified=False),

    # -- timer / counter -----------------------------------------------------
    "TON": _sig((TAG, LITERAL, LITERAL), writes=(0,)),
    "TOF": _sig((TAG, LITERAL, LITERAL), writes=(0,)),
    "RTO": _sig((TAG, LITERAL, LITERAL), writes=(0,)),
    "CTU": _sig((TAG, LITERAL, LITERAL), writes=(0,)),
    "CTD": _sig((TAG, LITERAL, LITERAL), writes=(0,), verified=False),
    "RES": _sig((TAG,), writes=(0,)),

    # -- system / message ------------------------------------------------------
    # GSV(Class, Instance, Attribute, Dest) - but the corpus carries 3-operand
    # forms too, and the destination is the LAST operand in both. A fixed index
    # wrote the short-form call sites to the wrong position.
    # Position 1 is the object Instance, which is a keyword for singleton
    # objects but names a real tag in 6 of 29 four-operand corpus occurrences.
    # It is typed TAG rather than KEYWORD because the failure modes are not
    # symmetric: a keyword string typed as TAG simply fails to resolve and
    # emits nothing, whereas a tag typed as KEYWORD is skipped and silently
    # loses a real read edge.
    "GSV": _sig((KEYWORD, TAG, KEYWORD, TAG), writes=(-1,), last_role=TAG),
    # SSV(Class, Instance, Attribute, Source) writes the system object, NOT a
    # tag. Its last operand is read. Giving it a write position would emit
    # false write edges - deliberately none.
    "SSV": _sig((KEYWORD, KEYWORD, KEYWORD, TAG), last_role=TAG),
    "MSG": _sig((TAG,), writes=(0,)),
    "PID": _sig((TAG, TAG, TAG, TAG), writes=(0, 3), verified=False),

    # -- control flow ------------------------------------------------------
    # JSR(Routine, ParamCount, <input params...>, <return params...>). Only the
    # target and count are fixed; trailing operands are ordinary tag operands.
    "JSR": _sig((ROUTINE, LITERAL, TAG, _REPEAT)),
    "JXR": _sig((ROUTINE, LITERAL, TAG, _REPEAT), verified=False),
    "SBR": _sig((TAG, _REPEAT), writes=(), verified=False),
    "RET": _sig((TAG, _REPEAT), verified=False),
    "JMP": _sig((LABEL,)),
    "LBL": _sig((LABEL,)),
    "MCR": _sig(()),
    "AFI": _sig(()),
    "NOP": _sig(()),
    "TND": _sig(()),
    "UID": _sig(()),
    "UIE": _sig(()),
}


def signature(mnemonic: str) -> Signature | None:
    """Signature for a canonical mnemonic, or None if the instruction is unknown.

    Callers must treat None as "emit reads, emit no writes". Guessing a
    destination for an unknown instruction is how false write edges get into
    the graph, and a missing write edge is recoverable where a wrong one is not.
    """
    return INSTRUCTIONS.get(mnemonic)


def unverified_mnemonics() -> set[str]:
    """Canonical mnemonics whose operand order is carried without corpus proof."""
    return {m for m, s in INSTRUCTIONS.items() if not s.verified}
