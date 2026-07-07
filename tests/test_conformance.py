"""ADR-008 §1 — extraction conformance as a regression gate.

Runs the precision/recall scorecard (tools/conformance_eval.py) over the hand-authored
ground-truth fixtures and fails if any per-language P/R drops below the committed
baseline (benchmarks/conformance/baseline.json). This is the *correctness* sibling of
test_adapter_snapshots.py, which guards *drift*; the two are deliberately separate.

Regenerate the baseline after an intentional, reviewed change:
    python tools/conformance_eval.py --write-baseline
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

import conformance_eval as ce


def test_fixtures_present():
    """The ground-truth corpus is non-empty — catches an accidental fixture wipe."""
    fixtures = ce.discover_fixtures()
    assert fixtures, "no conformance fixtures found under tests/fixtures/conformance/"


@pytest.mark.parametrize("fixture", ce.discover_fixtures(), ids=lambda f: f["stem"])
def test_no_orphaned_expected(fixture):
    """Every expected.json has a sibling source file (an orphan is an authoring bug)."""
    assert fixture["source_path"] is not None, (
        f"{fixture['expected_path']} has no sibling source file"
    )


def test_no_regression_vs_baseline():
    """Per-language precision/recall must not fall below the committed baseline."""
    results = ce.run()
    problems = ce.check_baseline(results)
    assert not problems, "conformance regression:\n  " + "\n  ".join(problems)
