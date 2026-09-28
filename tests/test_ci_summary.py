"""
tools/ci_summary.py is the step that fails the CI test job (ADR-004): pytest and
mutmut both run with continue-on-error, so a failing test only fails the job if
this script says so. Master went green over six failing snapshot tests because it
only enforced the mutation score, and scored a missing mutmut cache as 100.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import ci_summary  # noqa: E402


def _run(tmp_path, monkeypatch, pytest_text: str | None) -> int:
    out = tmp_path / "pytest_output.txt"
    if pytest_text is not None:
        out.write_text(pytest_text, encoding="utf-8")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "ci_summary.py",
        "--pytest-output", str(out),
        "--mutmut-cache", str(tmp_path / "no-cache"),
    ])
    return ci_summary.main()


def test_all_passed_is_success(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, "....\n544 passed, 2 skipped in 17.49s\n") == 0


@pytest.mark.parametrize("summary", [
    "6 failed, 538 passed, 2 skipped in 19.74s",
    "538 passed, 1 error in 3.1s",
    "1 failed, 2 errors in 0.5s",
])
def test_a_failed_or_errored_test_fails_the_job(tmp_path, monkeypatch, summary):
    text = "FAILED tests/test_x.py::test_y - AssertionError: boom\n" + summary + "\n"
    assert _run(tmp_path, monkeypatch, text) == 1


def test_missing_pytest_output_fails_the_job(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, None) == 1


def test_output_without_a_summary_line_fails_the_job(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, "Killed\n") == 1


def test_missing_mutmut_cache_is_reported_as_not_measured(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, "3 passed in 0.1s\n") == 0
    out = capsys.readouterr().out
    assert "not measured" in out
    assert "100%" not in out


def test_parse_counts_errors_separately(tmp_path):
    p = tmp_path / "o.txt"
    p.write_text("2 failed, 10 passed, 3 errors in 1s\n", encoding="utf-8")
    _, n_failed, n_errors, n_passed = ci_summary.parse_pytest_output(p)
    assert (n_failed, n_errors, n_passed) == (2, 3, 10)
