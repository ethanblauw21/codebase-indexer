"""AOI drift detection: same name, same declared revision, different logic.

Synthetic exports built in the test rather than committed fixtures — the whole
subject is *pairs* of files that disagree, so the pair is the unit under test and
keeping both halves next to the assertion is what makes the disagreement legible.

Declared revision is a claim. These assert the tool checks it against the logic
instead of trusting it, and that it does not cry drift over whitespace, comment
wording, or a BOM — all three vary between exports for reasons that are not edits.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import l5x_aoi_drift as D  # noqa: E402


def _export(aoi_name, revision, rungs, params=(("In", "BOOL", "Input"),),
            comment="original comment", indent=""):
    """One whole-controller export carrying one AOI definition."""
    p = "".join(
        f'{indent}<Parameter Name="{n}" DataType="{d}" Usage="{u}" Required="true"/>\n'
        for n, d, u in params
    )
    r = "".join(
        f'{indent}<Rung Number="{i}" Type="N">'
        f'<Comment><![CDATA[{comment}]]></Comment>'
        f'<Text><![CDATA[{t}]]></Text></Rung>\n'
        for i, t in enumerate(rungs)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" TargetType="Controller" ContainsContext="false">
<Controller Use="Target" Name="C">
<AddOnInstructionDefinitions>
<AddOnInstructionDefinition Name="{aoi_name}" Revision="{revision}">
<Parameters>
{p}</Parameters>
<Routines><Routine Name="Logic" Type="RLL"><RLLContent>
{r}</RLLContent></Routine></Routines>
</AddOnInstructionDefinition>
</AddOnInstructionDefinitions>
<Programs><Program Name="Main"><Routines>
<Routine Name="MainRoutine" Type="RLL"><RLLContent>
<Rung Number="0" Type="N"><Text><![CDATA[{aoi_name}(Inst,Flag);]]></Text></Rung>
</RLLContent></Routine></Routines></Program></Programs>
</Controller>
</RSLogix5000Content>
"""


def _corpus(tmp_path, **files):
    for name, body in files.items():
        (tmp_path / f"{name}.L5X").write_text(body, encoding="utf-8")
    return tmp_path


def _verdicts(tmp_path):
    found = D.collect(tmp_path)
    return {n: D.classify(r) for n, r in found.items()}


def test_same_revision_different_logic_is_drift(tmp_path):
    """The reported failure, reduced to two files and one changed rung."""
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]),
        plant_b=_export("AOI_DigIn", "5.02", ["XIO(In)OTE(Out);"]),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "DRIFT"}


def test_identical_copies_are_not_drift(tmp_path):
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]),
        plant_b=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "consistent"}


def test_different_revisions_are_not_drift(tmp_path):
    """Changing the logic AND bumping the revision is what should happen."""
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]),
        plant_b=_export("AOI_DigIn", "5.03", ["XIO(In)OTE(Out);"]),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "consistent"}


def test_parameter_list_change_outranks_logic_change(tmp_path):
    """A changed signature is the worse finding and must not be reported as plain DRIFT.

    AOI call sites bind POSITIONALLY (ADR-013's 418/418 arity rule), so a
    definition that gained a parameter silently re-slots every operand after it
    for anyone copying a call site between the two projects.
    """
    _corpus(
        tmp_path,
        plant_a=_export("AOI_Anlg", "5.05", ["XIC(In)OTE(Out);"],
                        params=(("In", "BOOL", "Input"),)),
        plant_b=_export("AOI_Anlg", "5.05", ["XIC(In)OTE(Out);"],
                        params=(("In", "BOOL", "Input"), ("Scale", "REAL", "Input"))),
    )
    assert _verdicts(tmp_path) == {"AOI_Anlg": "SIGNATURE DRIFT"}


def test_comment_only_difference_is_reported_but_not_as_logic_drift(tmp_path):
    """Re-wording a comment is not an edit to the logic, but two people touched it."""
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"], comment="one"),
        plant_b=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"], comment="two"),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "COMMENT DRIFT"}


def test_xml_indentation_and_bom_are_not_drift(tmp_path):
    """Exports differ in indentation and BOM for reasons that are not edits.

    ADR-013 already records the BOM as a stable-ID hazard: inconsistent handling
    makes identical logic hash differently. A drift report that fired on either
    would be noise on the first real archive it met.
    """
    (tmp_path / "a.L5X").write_text(
        _export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]), encoding="utf-8")
    (tmp_path / "b.L5X").write_text(
        _export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"], indent="        "),
        encoding="utf-8-sig",
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "consistent"}


def test_interior_spacing_in_neutral_text_counts_as_a_difference(tmp_path):
    """Deliberate: whitespace is collapsed, not stripped.

    Stripping all whitespace would make `'HELLO WORLD'` and `'HELLOWORLD'` hash
    alike, and a drift detector that hides a real difference is worse than one
    that reports a cosmetic one. This does not fire in practice because neutral
    text is emitted by the export, not hand-formatted — the variation between
    real exports is in the XML around it, which the test above covers.
    """
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]),
        plant_b=_export("AOI_DigIn", "5.02", ["XIC(In)   OTE(Out);"]),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "DRIFT"}


def test_a_single_copy_is_not_a_finding(tmp_path):
    _corpus(tmp_path, only=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]))
    assert _verdicts(tmp_path) == {"AOI_DigIn": "single"}


def test_call_sites_are_counted_per_file(tmp_path):
    """Call-site counts are the only evidence offered for which copy is master."""
    _corpus(tmp_path, plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"]))
    rec = D.collect(tmp_path)["AOI_DigIn"][0]

    assert rec["calls_here"] == 1
    assert rec["rungs"] == 1
    assert rec["params"] == 1


def test_implicit_parameters_are_excluded_from_the_signature(tmp_path):
    """EnableIn/EnableOut ride on every AOI and carry no design intent."""
    _corpus(
        tmp_path,
        plant_a=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"],
                        params=(("In", "BOOL", "Input"),)),
        plant_b=_export("AOI_DigIn", "5.02", ["XIC(In)OTE(Out);"],
                        params=(("EnableIn", "BOOL", "Input"),
                                ("In", "BOOL", "Input"),
                                ("EnableOut", "BOOL", "Output"))),
    )
    assert _verdicts(tmp_path) == {"AOI_DigIn": "consistent"}


@pytest.mark.parametrize("verdict", ["SIGNATURE DRIFT", "DRIFT", "COMMENT DRIFT",
                                     "consistent", "single"])
def test_every_verdict_has_a_sort_rank(verdict):
    """A verdict with no rank would crash the report's ordering, not degrade it."""
    assert verdict in D.ORDER
