"""ADR-043 (B-008): code-indexer's output survives a cp1252 stdout.

On Windows a redirected or piped stdout is cp1252, and the indexer's first print is a
"━━" banner. PYTHONIOENCODING=cp1252 gives a child process the same stream on any OS,
so these run (and guard the fix) on Ubuntu CI too.
"""
import io
import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
BANNER = "━━ Incremental Indexer: x ━━"


def _cp1252_child(code: str, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "cp1252"
    return subprocess.run([sys.executable, "-c", code, SRC, *args], capture_output=True,
                          env=env, stdin=subprocess.DEVNULL, timeout=300)


def test_a_cp1252_stdout_cannot_print_the_banner_without_the_helper():
    """The failure itself, so the next test proves a fix rather than a lucky platform."""
    out = _cp1252_child(f"print({BANNER!r})")
    assert out.returncode != 0
    assert b"UnicodeEncodeError" in out.stderr


def test_the_helper_makes_a_cp1252_stdout_print_utf8():
    out = _cp1252_child("import sys; sys.path.insert(0, sys.argv[1]); "
                        "from utf8_stdio import utf8_stdio; utf8_stdio(); "
                        f"print({BANNER!r}); print('✓ Δ', file=sys.stderr)")
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    assert out.stdout.decode("utf-8").strip() == BANNER
    assert out.stderr.decode("utf-8").strip() == "✓ Δ"


_CLI_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
import incremental_indexer as ii
ii.run_incremental = lambda **kw: print("━━ Incremental Indexer: x ━━")
sys.argv = ["code-indexer", "--allow-worktree"]
ii.main()
'''


def test_code_indexer_main_survives_a_cp1252_stdout():
    """The real entry point: main() must fix the streams before run_incremental prints."""
    out = _cp1252_child(_CLI_CHILD)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    assert out.stdout.decode("utf-8").strip().endswith(BANNER)


def test_a_stream_without_reconfigure_is_left_alone(monkeypatch):
    """A harness that swapped in a plain StringIO keeps working (no AttributeError)."""
    from utf8_stdio import utf8_stdio
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    utf8_stdio()
    print(BANNER)
    assert fake.getvalue().strip() == BANNER
