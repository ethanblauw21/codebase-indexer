"""B-038: find_test_coverage must find pytest's `test_*.py` files.

The Python adapter declared "test_.py" as a file *suffix*, and the tool matches
suffixes with endswith, so `test_scan_policy.py` never matched and every Python
source reported "No test files found in the index". Model-free: the doc store is a
stub and the semantic tier (`_search`) returns nothing.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import MCPServer as M  # noqa: E402


def _docs(*paths):
    return {i: {"file": p, "tier": "tier1_surgical", "scope": "Full File_part_1", "text": f"# {p}"}
            for i, p in enumerate(paths)}


@pytest.fixture
def stub_index(monkeypatch):
    state = SimpleNamespace(doc_store=None)

    def install(*paths):
        state.doc_store = SimpleNamespace(docs=_docs(*paths))
    monkeypatch.setattr(M, "_ensure_indexes", lambda: state)
    monkeypatch.setattr(M, "_search", lambda q, top_n=10: [])
    return install


def test_pytest_prefix_file_is_a_direct_match(stub_index):
    stub_index("src/scan_policy.py", "tests/test_scan_policy.py", "tests/test_other.py")
    out = M.find_test_coverage("src/scan_policy.py")
    direct = out.split("2. SEMANTIC COVERAGE")[0]
    assert "tests/test_scan_policy.py" in direct
    assert "tests/test_other.py" not in direct
    assert "No test files found" not in out


def test_suffix_convention_still_matches(stub_index):
    stub_index("pkg/widget.py", "pkg/widget_test.py")
    out = M.find_test_coverage("widget.py")
    assert "pkg/widget_test.py" in out.split("2. SEMANTIC COVERAGE")[0]


def test_prefix_glob_does_not_claim_other_languages(stub_index):
    """`test_*.py` must not turn a JS file named test_x.js into a Python test."""
    stub_index("src/app.js", "src/test_app.js", "src/app.test.js")
    out = M.find_test_coverage("src/app.js")
    direct = out.split("2. SEMANTIC COVERAGE")[0]
    assert "src/app.test.js" in direct
    assert "src/test_app.js" not in direct


def test_no_tests_message_names_the_patterns(stub_index):
    stub_index("src/scan_policy.py")
    out = M.find_test_coverage("src/scan_policy.py")
    assert "No test files found" in out
    assert "test_*.py" in out
