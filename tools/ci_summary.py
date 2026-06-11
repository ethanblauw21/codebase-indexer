"""
Parse pytest output and mutmut cache, write a GitHub Step Summary (or stdout).

Exit codes:
  0 — all thresholds met (or mutmut cache absent)
  1 — mutation score below --threshold-low
"""

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path


def parse_pytest_output(path: Path) -> tuple[list[dict], int, int]:
    """Return (failures, n_failed, n_passed) from pytest --tb=short -q output."""
    if not path.exists():
        return [], 0, 0

    text = path.read_text(encoding="utf-8", errors="replace")
    failures = []

    # Collect FAILED lines: "FAILED tests/foo.py::test_bar - SomeError: msg"
    for line in text.splitlines():
        m = re.match(r"^FAILED\s+(\S+)\s+-\s+(.+)$", line)
        if m:
            failures.append({"test": m.group(1), "reason": m.group(2)})

    # Summary line: "3 failed, 47 passed in 1.23s"
    n_failed = n_passed = 0
    for line in reversed(text.splitlines()):
        m = re.search(r"(\d+) failed", line)
        if m:
            n_failed = int(m.group(1))
        m2 = re.search(r"(\d+) passed", line)
        if m2:
            n_passed = int(m2.group(1))
        if n_failed or n_passed:
            break

    return failures, n_failed, n_passed


def read_mutmut_cache(cache_path: Path) -> tuple[list[dict], int, int]:
    """Return (survivors, n_killed, n_total) from .mutmut-cache SQLite DB."""
    if not cache_path.exists():
        return [], 0, 0

    try:
        con = sqlite3.connect(str(cache_path))
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        # mutmut stores results in a table called "mutants"
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        if "mutants" not in tables:
            return [], 0, 0

        cur.execute("SELECT filename, line_number, status, mutation FROM mutants")
        rows = cur.fetchall()
        con.close()
    except sqlite3.Error:
        return [], 0, 0

    survivors = []
    n_killed = 0
    n_total = 0

    for row in rows:
        status = (row["status"] or "").lower()
        if status == "untested":
            continue
        n_total += 1
        if status in ("survived", "bad_timeout", "suspicious"):
            survivors.append(
                {
                    "file": row["filename"],
                    "line": row["line_number"],
                    "mutation": row["mutation"] or "",
                }
            )
        else:
            n_killed += 1

    return survivors, n_killed, n_total


def score_badge(score: float, high: int, low: int) -> str:
    if score >= high:
        return "✅"
    if score >= low:
        return "⚠️"
    return "❌"


def build_markdown(
    failures: list[dict],
    n_failed: int,
    n_passed: int,
    survivors: list[dict],
    n_killed: int,
    n_total: int,
    threshold_high: int,
    threshold_low: int,
    has_pytest: bool,
    has_mutmut: bool,
) -> str:
    lines = ["## CI Summary", ""]

    if has_pytest:
        lines.append("### Test Results")
        lines.append(f"> {n_failed} failed, {n_passed} passed")
        lines.append("")
        if failures:
            lines.append("| Test | Failure |")
            lines.append("|------|---------|")
            for f in failures:
                test = f["test"].replace("|", "\\|")
                reason = f["reason"].replace("|", "\\|")
                lines.append(f"| `{test}` | {reason} |")
            lines.append("")

    if has_mutmut:
        if n_total > 0:
            score = (n_killed / n_total) * 100
        else:
            score = 100.0
        badge = score_badge(score, threshold_high, threshold_low)
        lines.append(
            f"### Mutation Score: {score:.0f}% {badge} "
            f"(threshold: {threshold_high} high / {threshold_low} low)"
        )
        lines.append("")
        if survivors:
            lines.append("| File | Line | Survived Mutation |")
            lines.append("|------|------|-------------------|")
            for s in survivors:
                fname = s["file"].replace("|", "\\|")
                mutation = s["mutation"].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{fname}` | {s['line']} | `{mutation}` |")
            lines.append("")
    else:
        score = 100.0  # no cache → no threshold failure

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold-high", type=int, default=90)
    parser.add_argument("--threshold-low", type=int, default=80)
    parser.add_argument("--pytest-output", default="pytest_output.txt")
    parser.add_argument("--mutmut-cache", default=".mutmut-cache")
    args = parser.parse_args()

    pytest_path = Path(args.pytest_output)
    cache_path = Path(args.mutmut_cache)

    has_pytest = pytest_path.exists()
    has_mutmut = cache_path.exists()

    failures, n_failed, n_passed = parse_pytest_output(pytest_path)
    survivors, n_killed, n_total = read_mutmut_cache(cache_path)

    md = build_markdown(
        failures, n_failed, n_passed,
        survivors, n_killed, n_total,
        args.threshold_high, args.threshold_low,
        has_pytest, has_mutmut,
    )

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    else:
        print(md)

    # Enforce threshold
    if has_mutmut and n_total > 0:
        score = (n_killed / n_total) * 100
        if score < args.threshold_low:
            print(
                f"Mutation score {score:.0f}% is below threshold {args.threshold_low}%",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
