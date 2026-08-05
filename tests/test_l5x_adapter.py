"""L5X adapter guards for facts the conformance harness cannot express.

The ADR-008 harness scores symbols and edges. Three established facts about
this adapter are neither: what the chunk body contains, which symbol kinds
become tier-1 chunks, and whether a dropped element stays silent. Each was
established by measuring the corpus and had no durable guard (ADR-013 §5.2).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from adapters.l5x_adapter import L5xAdapter, scan_instructions
from ast_chunker import chunk_file_ast

FIXTURES = Path(__file__).parent / "fixtures" / "conformance" / "l5x"


def _parse(name: str):
    src = FIXTURES / f"{name}.L5X"
    return L5xAdapter().parse(str(src), src.read_bytes()), src


# ------------------------------------------------------- neutral text in body

def test_chunk_body_carries_neutral_text_verbatim():
    """The rung's neutral text must survive into the chunk body.

    This adapter assembles its embedding payload from prose because
    `XIC(a)XIO(b)OTE(c)` carries almost no natural language. The measured
    reason the neutral text is ALSO kept: only ~30% of corpus rungs carry a
    comment, so a prose-only chunk would leave ~70% of the ladder unreachable
    by any lexical query, and lexical matching is half of hybrid retrieval.

    An engineer searching for the instruction or tag they actually typed has
    to find it, so this asserts the literal instruction text is present.
    """
    result, _ = _parse("mnemonic_canonical")
    routine = next(s for s in result.symbols
                   if s.fqn == "LevelProgram.Monitor")

    assert "LIM(Low_Limit,Level,High_Limit)OTE(In_Range)" in routine.text
    assert "EQU(Mode,0)MOV(1,Mode)" in routine.text


def test_chunk_body_carries_prose_and_rung_numbers():
    """Comments are kept, and rungs stay individually addressable.

    The routine is the chunk unit but the rung is the addressable sub-unit, so
    a hit has to be traceable back to a specific rung.
    """
    result, _ = _parse("mnemonic_canonical")
    routine = next(s for s in result.symbols
                   if s.fqn == "LevelProgram.Monitor")

    assert "Level must sit between the configured limits" in routine.text
    assert "[1]" in routine.text


# ------------------------------------------------------------ chunking policy

def test_aoi_internal_declarations_are_not_tier1_chunks():
    """Parameters and local tags are declarations, not retrievable units.

    There is no query whose best answer is an AOI's internal local tag in
    isolation. On the survey corpus these were 3,227 of 6,011 tier-1 chunks.
    """
    src = FIXTURES / "aoi_definition.L5X"
    chunks = chunk_file_ast(str(src), src.read_text(encoding="utf-8"))
    scopes = {c.scope for c in chunks}

    result, _ = _parse("aoi_definition")
    kinds = {s.fqn: s.kind for s in result.symbols}

    excluded = {f for f in kinds if kinds[f] in ("parameter", "local_tag")}
    assert excluded, "fixture must contain AOI parameters to be meaningful"
    assert not (excluded & scopes), "AOI internals must not be tier-1 chunks"


def test_excluded_kinds_are_still_graph_nodes():
    """Filtering chunks must not cost symbols or edges.

    Symbols reach the graph through `parse_file`, not through the chunker, so
    a non-chunkable kind is still a first-class node with all of its edges.
    """
    result, _ = _parse("aoi_definition")
    kinds = {s.kind for s in result.symbols}
    assert "parameter" in kinds

    params = {s.fqn for s in result.symbols if s.kind == "parameter"}
    owned = {e.target for e in result.edges if e.kind == "owns"}
    assert params <= owned, "every parameter keeps its ownership edge"


def test_other_languages_are_unaffected_by_the_chunk_filter():
    """`chunkable_kinds` is opt-in; an adapter without it chunks everything."""
    from adapters import get_adapter

    for ext in (".py", ".ts", ".cs", ".cpp"):
        adapter = get_adapter(ext)
        if adapter is not None:
            assert not hasattr(adapter, "chunkable_kinds"), (
                f"{ext} adapter declared chunkable_kinds; the L5X chunk filter "
                f"was meant to be adapter-local policy"
            )


# --------------------------------------------------------------- loud failure

def test_element_missing_its_name_is_reported_not_swallowed(caplog):
    """A dropped element must never leave the parse quietly reporting success.

    `if not name: continue` discarded 22 of 83 corpus modules — a quarter of
    the hardware tree. Six other element kinds share the pattern and are
    zero-missing on that corpus, which is a fact about the corpus rather than
    a property of the adapter.
    """
    src = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<RSLogix5000Content SchemaRevision="1.0" TargetType="Controller" '
        'ContainsContext="false">'
        '<Controller Use="Target" Name="SkipFixture">'
        '<Tags>'
        '<Tag Name="Good" TagType="Base" DataType="DINT"/>'
        '<Tag TagType="Base" DataType="DINT"/>'
        '</Tags>'
        '<Programs>'
        '<Program TestEdits="false"><Tags/><Routines/></Program>'
        '</Programs>'
        '</Controller></RSLogix5000Content>'
    ).encode("utf-8")

    with caplog.at_level(logging.WARNING, logger="adapters.l5x_adapter"):
        result = L5xAdapter().parse("skip.L5X", src)

    assert {s.fqn for s in result.symbols} == {"Good"}

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("discarded" in w for w in warnings), (
        "a parse that drops structure must say so"
    )
    joined = " ".join(warnings)
    assert "Tag=1" in joined and "Program=1" in joined


# --------------------------------------------------- operand slot positioning

def test_empty_operand_slot_is_a_hole_not_an_absent_slot():
    """`GSV(Class,,Attr,Dest)` is four operands with a hole, not three.

    Roles are assigned positionally, so collapsing the hole renumbers every
    operand after it. This currently changes no corpus edge — GSV and SSV are
    the only instructions carrying holes and their role tuples absorb a shift —
    so it is asserted directly at the scanner rather than through a fixture.
    """
    (_, operands, _, _), = scan_instructions("GSV(WallClockTime,,DateTime,Dest)")
    assert len(operands) == 4
    assert operands[1].strip() == ""
    assert operands[3] == "Dest"


def test_empty_parens_are_zero_operands():
    """`NOP()` is a zero-operand call, not one empty operand. 639 corpus sites."""
    (_, operands, _, _), = scan_instructions("NOP()")
    assert operands == []


def test_two_dimensional_subscript_is_one_operand():
    """The bracket-depth split: `Recipe[Row,Col]` must not split at the comma."""
    (_, operands, _, _), = scan_instructions("MOV(Recipe[Row,Col],Dest)")
    assert operands == ["Recipe[Row,Col]", "Dest"]


# ----------------------------------------------- operand-role table invariants

def test_every_write_position_is_typed_as_a_tag():
    """A write position must also be typed TAG, at every arity it can take.

    This is the invariant the shipped `GSV`/`SSV` bug violated. The adapter
    skips LITERAL/KEYWORD/LABEL operands *before* it consults `writes`, so a
    write position carrying a non-tag role is not a wrong edge — it is a
    silently missing one, and nothing downstream can tell it apart from an
    instruction that genuinely writes nothing.

    Checked here rather than only against a corpus because the corpus cannot
    be committed and exercises 56 of the 72 table entries; the other 16 would
    go unchecked until a controller that used them showed up.
    """
    from adapters.l5x_instructions import (
        EXPR, INSTRUCTIONS, LABEL, KEYWORD, LITERAL, TAG,
    )

    offenders = []
    for mnemonic, sig in sorted(INSTRUCTIONS.items()):
        for arity in range(1, max(len(sig.roles) + 2, 8)):
            for i in range(arity):
                if not (sig.writes_at(i, arity) or sig.both_at(i, arity)):
                    continue
                role = sig.role_at(i, arity)
                if role in (LITERAL, KEYWORD, LABEL, EXPR):
                    offenders.append(f"{mnemonic}[{i}] of {arity} is {role}")

    assert not offenders, (
        "write positions typed as non-tag roles are skipped before `writes` is "
        "consulted, so the write edge is silently lost:\n  "
        + "\n  ".join(offenders)
    )


def test_repeat_marker_only_appears_last():
    """`_REPEAT` extends the final role; anywhere else it silently truncates."""
    from adapters.l5x_instructions import INSTRUCTIONS, _REPEAT

    for mnemonic, sig in sorted(INSTRUCTIONS.items()):
        if _REPEAT in sig.roles:
            assert sig.roles[-1] == _REPEAT, (
                f"{mnemonic} has a repeat marker before the last position"
            )
            assert len(sig.roles) >= 2, f"{mnemonic} is a bare repeat marker"


def test_unverified_entries_are_declared_not_implied():
    """Entries with no corpus evidence must carry `verified=False`.

    16 of them do. The point is that the label was applied by someone going
    and looking, not inferred from the table being plausible.
    """
    from adapters.l5x_instructions import INSTRUCTIONS

    unverified = {m for m, s in INSTRUCTIONS.items() if not s.verified}
    assert unverified, "the unverified marker must not quietly disappear"
    for mnemonic, sig in INSTRUCTIONS.items():
        assert isinstance(sig.verified, bool), (
            f"{mnemonic} has a non-boolean verified flag"
        )
