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

from adapters.l5x_adapter import (
    L5xAdapter,
    expression_terms,
    scan_instructions,
    split_operand,
)
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


def test_nested_call_is_not_its_own_instruction():
    """A mnemonic inside another instruction's operands is not an instruction.

    `ABS(` matches the mnemonic regex like anything else, so the first scanner
    yielded it separately and scanned the same text twice — once as an operand
    of `CPT` and once as a call of its own.
    """
    found = list(scan_instructions("CPT(Dest,ABS(Src) + 1);"))
    assert [m for m, _, _, _ in found] == ["CPT"]
    assert found[0][1] == ["Dest", "ABS(Src) + 1"]


def test_nested_calls_are_counted_not_silently_dropped():
    """`on_nested` fires once per suppressed match, so an instrument can count."""
    seen = []
    list(scan_instructions("CPT(D,ABS(A) + SQR(B));", lambda: seen.append(1)))
    assert len(seen) == 2


def test_unterminated_call_does_not_swallow_what_follows():
    """One malformed rung must not suppress every instruction after it.

    The skip window only advances past a call that actually closed. Without
    that, an unclosed paren runs to end of text and everything later looks
    nested.
    """
    found = list(scan_instructions("MOV(A,B XIC(Run)OTE(Motor)"))
    assert [m for m, _, _, _ in found] == ["MOV", "XIC", "OTE"]


def test_expression_terms_drops_functions_operators_and_literals():
    """An expression is not a bag of identifiers.

    A function name is recognised structurally — an identifier followed by
    `(` — so an uncatalogued function costs nothing. Bare-word operators come
    from a short vendor list, and string and numeric literals never match.
    """
    terms = list(expression_terms("ABS(Level) + 3.5 * Rate AND Enable"))
    assert terms == ["Level", "Rate", "Enable"]
    assert list(expression_terms("'Batch ready' + Status")) == ["Status"]


def test_expression_term_keeps_its_member_path_and_subscript():
    """Terms stay operand-shaped so `split_operand` can take them apart.

    Yielding bare identifiers is what made `Count` in `Recipe.Count` a tag
    reference of its own, and a controller tag can share a name with a
    structure member — see the expression_member_path fixture.
    """
    assert list(expression_terms("Recipe.Count * Buffer[Idx,2].Value")) == [
        "Recipe.Count", "Buffer[Idx,2].Value",
    ]


def test_subscript_interior_is_read_as_a_term_not_an_identifier_sweep():
    """`Buffer[Step.Index]` reads `Step`, not `Step` and a phantom `Index`.

    A subscript is an expression too, and the identifier sweep made every
    member name inside one a tag reference — 44 of the 72 unresolved subscript
    reads in the survey corpus were exactly this.
    """
    assert split_operand("Buffer[Step.Index]") == ("Buffer", ["Step"])
    assert split_operand("Recipe[Row,Col]") == ("Recipe", ["Row", "Col"])


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

    14 of 72 do. The point is that the label was applied by someone going and
    looking, not inferred from the table being plausible.
    """
    from adapters.l5x_instructions import INSTRUCTIONS

    unverified = {m for m, s in INSTRUCTIONS.items() if not s.verified}
    assert unverified, "the unverified marker must not quietly disappear"
    for mnemonic, sig in INSTRUCTIONS.items():
        assert isinstance(sig.verified, bool), (
            f"{mnemonic} has a non-boolean verified flag"
        )


# ------------------------------------------------ rung-level write provenance

def _refs(result, name=None, kind=None):
    out = result.references
    if name is not None:
        out = [r for r in out if r.symbol_name == name]
    if kind is not None:
        out = [r for r in out if r.ref_kind == kind]
    return out


def test_one_write_edge_can_hide_two_rungs():
    """The reason rung-level references exist at all.

    `Valve_Open` is energised on rung 1 and unlatched on rung 3. Edges dedupe on
    (source, target, kind), so both writes collapse into ONE `writes` edge and
    the edge cannot say which rung. An engineer on the phone asking "what
    activates this valve" needs both rungs, and rung 3 is the one that would be
    lost.
    """
    result, _ = _parse("rung_addressing")

    write_edges = [e for e in result.edges
                   if e.target == "Valve_Open" and e.kind == "writes"]
    assert len(write_edges) == 1, "edges dedupe; that is the premise here"

    rungs = sorted(r.line for r in _refs(result, "Valve_Open", "WRITE"))
    assert rungs == [1, 3]


def test_reference_line_is_a_rung_number_not_a_source_line():
    """`line` carries the RUNG number for L5X, and nothing else.

    An L5X export is one XML document per controller — 7.9 MB in the survey
    corpus — so its source line numbers are useless to a human, while `[12]` is
    already how the chunk body addresses a rung. This asserts the two cannot be
    confused: every reference here sits in rungs 0-3, while the fixture file
    itself is dozens of lines long.
    """
    result, src = _parse("rung_addressing")

    assert result.references, "the fixture must produce references at all"
    assert max(r.line for r in result.references) == 3
    assert len(src.read_text(encoding="utf-8").splitlines()) > 20


def test_tag_touched_twice_in_one_rung_is_one_reference():
    """`EQU(Step,3)MOV(Step,Step_Last)` reads Step twice in rung 2.

    Two operand positions, one place to look. References are deduped per
    (name, routine, rung, kind) because reference counts feed density scoring
    (`db.get_reference_density`), and counting operand positions there would
    overstate how referenced a tag is.
    """
    result, _ = _parse("rung_addressing")

    step_reads = _refs(result, "Step", "READ")
    assert len(step_reads) == 1
    assert step_reads[0].line == 2


def test_read_and_write_of_one_tag_are_distinguished_by_ref_kind():
    """`Seq_Active` is written on rung 0 and read on rung 1.

    Both directions are real and they are different answers to different
    questions, so `ref_kind` — not the caller's guesswork — is what separates
    "what sets this" from "what uses this".
    """
    result, _ = _parse("rung_addressing")

    assert [r.line for r in _refs(result, "Seq_Active", "WRITE")] == [0]
    assert [r.line for r in _refs(result, "Seq_Active", "READ")] == [1]


def test_reference_carries_the_resolved_fqn_and_owning_routine():
    """Unlike the tree-sitter adapters, this one already knows what it resolved.

    Python/TS/C#/C++ emit `symbol_fqn=None` and leave resolution to a later
    pass. The L5X scan resolves against the tag registry before emitting the
    edge, so throwing that away here would make the reference weaker than the
    edge beside it. `idx_refs_fqn` exists to serve exactly this lookup.
    """
    result, _ = _parse("rung_addressing")

    ref = _refs(result, "Valve_Open", "WRITE")[0]
    assert ref.symbol_fqn == "Valve_Open"
    assert ref.context_fqn == "Filler.Sequence"


def test_references_do_not_disturb_symbols_or_edges():
    """Adding provenance must not change what was already extracted.

    The corpus check that matters is recorded in ADR-013: symbols and edges
    stayed at 6,011 / 13,316 across this change. This is the fixture-scale
    version of the same assertion, so a regression fails in CI rather than only
    against a corpus no CI runner can see.
    """
    result, _ = _parse("rung_addressing")

    assert len(result.symbols) == 9
    assert len(result.edges) == 9
    assert sum(1 for e in result.edges if e.kind == "writes") == 3
