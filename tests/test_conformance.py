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


# ---------------------------------------------------------------------------
# Key-distinctness invariant — makes the normalize_fqn /arity defect (every C# method
# collapsing to its arity digit) structurally impossible to reintroduce silently.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ce.discover_fixtures(), ids=lambda f: f["stem"])
def test_key_distinctness_per_fixture(fixture):
    """For every fixture, no two distinct ground-truth keys — and no two distinct adapter-
    output keys — may collapse onto the same normalized key. A collision means normalization
    is lossy and the scorer can silently undercount or mask a recall miss."""
    fr = ce.score_fixture(fixture)
    for side in ("expected", "actual"):
        for cat in ("symbols", "edges"):
            collisions = fr["collisions"][side][cat]
            assert not collisions, (
                f"{fr['feature']} {side}/{cat} normalized-key collision(s): {collisions}"
            )


def test_key_collisions_detects_a_real_merge():
    """The detector itself must flag two distinct raw keys that normalize identically —
    guards against the invariant test passing because the detector is a no-op."""
    syms = [{"fqn": "a.py::Foo", "kind": "class"}, {"fqn": "b.py::Foo", "kind": "class"}]
    coll = ce.key_collisions(syms, [])
    assert coll["symbols"], "collision detector failed to catch a genuine key merge"


def test_normalize_fqn_csharp_arity_preserved():
    """C# member FQNs (Namespace.Type.Member/arity) survive normalization intact; distinct
    methods must not collapse to their arity digit."""
    compute = ce.normalize_fqn("Shop.Core.Order.Compute/0")
    build = ce.normalize_fqn("Shop.Core.Order+Builder.Build/0")
    assert compute == "Shop.Core.Order.Compute/0"
    assert build == "Shop.Core.Order+Builder.Build/0"
    assert compute != build
    # Same name, different arity stay distinct (arity is part of identity).
    assert ce.normalize_fqn("A.B.M/1") != ce.normalize_fqn("A.B.M/2")


def test_normalize_fqn_paths_and_bare_identifiers_unchanged():
    """The `file::symbol` and bare-path cases the harness has always relied on are pinned,
    so the /arity fix cannot regress py/ts normalization."""
    assert ce.normalize_fqn("src/api/auth.ts::AuthService.login") == "AuthService.login"
    assert ce.normalize_fqn(r"C:\repo\pkg\async_gen.py") == "async_gen.py"
    assert ce.normalize_fqn("pkg/mod/async_gen.py") == "async_gen.py"
    assert ce.normalize_fqn("dumps") == "dumps"                       # bare call target
    assert ce.normalize_fqn("System.Collections.Generic") == "System.Collections.Generic"


def test_normalize_fqn_cpp_namespace_preserved():
    """C++ uses `::` as its namespace separator and carries no path prefix. The `::`
    path-stripping must NOT fire on a bare-identifier left side, or every C++ namespace
    would be stripped, collapsing distinct symbols and masking recall misses (the C++
    analog of the C# /arity collapse)."""
    order = ce.normalize_fqn("shop::Order")
    method = ce.normalize_fqn("shop::Order::compute(int)")
    inner = ce.normalize_fqn("a::b::Inner")
    assert order == "shop::Order"                       # namespace qualifier kept
    assert method == "shop::Order::compute(int)"        # ns + params intact
    assert inner == "a::b::Inner"                       # nested namespace kept
    assert order != method and method != inner
    # A genuine path prefix on a C++ FQN (separator or source ext on the left side) is
    # still stripped down to the namespaced symbol — first `::` only.
    assert ce.normalize_fqn(r"pkg\shop.cpp::shop::Order") == "shop::Order"
    assert ce.normalize_fqn("compute(int)") == "compute(int)"   # global-scope free fn


# ---------------------------------------------------------------------------
# Known-gap semantics — validation, honest reporting, and the unexpected-pass alert.
# ---------------------------------------------------------------------------


def test_known_gap_requires_reason_and_ref():
    assert ce.validate_known_gap({}, "feat") is None
    with pytest.raises(ValueError):
        ce.validate_known_gap({"known_gap": {"reason": "x"}}, "feat")   # no ref
    with pytest.raises(ValueError):
        ce.validate_known_gap({"known_gap": {"ref": "docs#y"}}, "feat")  # no reason
    ok = ce.validate_known_gap({"known_gap": {"reason": "r", "ref": "docs#x"}}, "feat")
    assert ok["ref"] == "docs#x"


def _synthetic_fr(feature, known_gap, perfect):
    """A minimal fixture-result with the structure render/aggregate/alert helpers read."""
    cat = {"tp": 1, "emitted": 1, "gt": 1, "missed": [], "spurious": []}
    empty = {"symbols": {}, "edges": {}}
    return {
        "feature": feature, "language": "csharp", "stem": feature,
        "categories": {c: dict(cat) for c in ce._CATEGORIES},
        "known_gap": known_gap, "perfect": perfect,
        "collisions": {"expected": dict(empty), "actual": dict(empty)},
    }


def test_unexpected_pass_is_detected_and_alerted():
    """A known_gap fixture that scores perfectly (gap closed) must be flagged by the detector
    AND surfaced in the human scorecard — the CI gate keys off exactly this signal."""
    gap_fr = _synthetic_fr("csharp/closed_gap", {"reason": "r", "ref": "d"}, perfect=True)
    clean_fr = _synthetic_fr("csharp/clean", None, perfect=True)
    results = {
        "fixtures": [gap_fr, clean_fr],
        "by_language": ce.aggregate([clean_fr]),
        "by_language_known_gap": ce.aggregate([gap_fr]),
    }
    assert ce.unexpected_passes(results) == ["csharp/closed_gap"]
    assert "UNEXPECTED PASS" in ce.render_scorecard(results)


def test_real_suite_gaps_manifest_and_no_unexpected_pass():
    """On the committed suite: known-gap fixtures really do score sub-1.0 (the gap shows up),
    there are no unexpected passes, and no key collisions anywhere."""
    results = ce.run()
    gaps = [fr for fr in results["fixtures"] if fr["known_gap"]]
    assert gaps, "expected known-gap fixtures in the suite"
    assert all(not fr["perfect"] for fr in gaps), "a known-gap fixture unexpectedly scores 1.0"
    assert ce.unexpected_passes(results) == []
    assert ce.collision_report(results) == []


def test_known_gap_fixtures_excluded_from_clean_gate():
    """Known-gap fixtures must not appear in the clean aggregate that the baseline gates on,
    so a documented sub-1.0 gap can never dilute or fail the gated number."""
    results = ce.run()
    # Clean csharp stays perfect even though two sub-1.0 csharp gap fixtures exist.
    assert results["by_language"]["csharp"]["edges_all"]["precision"] == 1.0
    assert results["by_language_known_gap"]["csharp"]["edges_all"]["precision"] < 1.0
