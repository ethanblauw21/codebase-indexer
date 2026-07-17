"""ADR-011 §4 — call-edge resolution conformance as a regression gate.

The *resolution* sibling of test_conformance.py (which gates *extraction*). Runs the
resolution scorecard (tools/resolution_eval.py) over the hand-authored ground-truth fixtures
and asserts the ADR-011 §4 contract on the live numbers:

    receiver-type inference RAISES the call-edge resolution rate WHILE precision is held.

The lift must come entirely from resolving *more* ambiguous calls correctly — never from
guessing: both regimes keep precision at 1.0, so the delta is pure recall on the resolvable
universe. CPU-only (tree-sitter + SQLite, no embedding, no GPU).

Regenerate the baseline after an intentional, reviewed change:
    python tools/resolution_eval.py --write-baseline
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import resolution_eval as re


def test_fixtures_present():
    """The resolution corpus is non-empty — catches an accidental fixture wipe."""
    assert re.discover_fixtures(), "no resolution fixtures under tests/fixtures/resolution/"


@pytest.mark.parametrize("fixture", re.discover_fixtures(), ids=lambda f: f["stem"])
def test_no_orphaned_ground_truth(fixture):
    """Every .resolution.json has a sibling source file (an orphan is an authoring bug)."""
    assert fixture["source_path"] is not None, (
        f"{fixture['expected_path']} has no sibling source file"
    )


def test_ground_truth_is_complete():
    """Ground truth must cover exactly the emitted call edges — no declared-but-unemitted
    call, no emitted-but-undeclared call. Either means the number is measured over the wrong
    set; the harness surfaces both structurally."""
    results = re.run()
    problems = re.integrity_report(results)
    assert not problems, "resolution ground-truth integrity:\n  " + "\n  ".join(problems)


def test_no_regression_and_lift_holds_vs_baseline():
    """The full §4 contract via the harness gate: typed precision 1.0, baseline precision 1.0,
    typed rate strictly above baseline, and no regression vs the committed baseline."""
    results = re.run()
    problems = re.check_baseline(results)
    assert not problems, "resolution conformance:\n  " + "\n  ".join(problems)


def test_receiver_typing_lifts_rate_with_precision_held():
    """Per language, spelled out independently of the committed baseline file: the receiver-
    type hint resolves strictly MORE of the resolvable universe, and neither regime ever
    mis-resolves (the correctness gate, §2)."""
    by_lang = re.run()["by_language"]
    assert by_lang, "no languages scored"
    for lang, regimes in by_lang.items():
        typed, base = regimes["typed"], regimes["baseline"]
        assert typed["precision"] == 1.0, f"{lang}: typed precision below 1.0 — a wrong edge"
        assert base["precision"] == 1.0, f"{lang}: baseline mis-resolved (should prefer-unknown)"
        assert typed["rate"] > base["rate"], (
            f"{lang}: no lift — typed rate {typed['rate']} <= baseline {base['rate']}"
        )
        assert typed["wrong"] == 0, f"{lang}: typed regime emitted {typed['wrong']} wrong edge(s)"


def test_baseline_leaves_ambiguous_names_unresolved():
    """The lift is real, not an artifact: on the SAME parse, stripping the receiver-type hint
    drops the resolution rate (name-only resolution cannot settle a shared method name)."""
    by_lang = re.run()["by_language"]
    for lang, regimes in by_lang.items():
        assert regimes["baseline"]["rate"] < 1.0, (
            f"{lang}: baseline already resolves everything — fixture lacks a genuinely "
            f"ambiguous (shared-name) call, so it cannot demonstrate the lift"
        )


def test_classify_rules():
    """The per-site verdict rules, pinned directly (the harness's load-bearing logic)."""
    assert re._classify("Foo.Save/0", "Foo.Save/0") == "hit"
    assert re._classify("Foo.Save/0", "Bar.Save/0") == "wrong"   # resolved, but wrong target
    assert re._classify(None, "Foo.Save/0") == "wrong"           # expected unknown, got a target
    assert re._classify(None, None) == "unknown_ok"              # correct prefer-unknown
    assert re._classify("Foo.Save/0", None) == "miss"            # unresolved (recall gap, not wrong)
