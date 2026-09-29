"""utf8_stdio.py — make print() safe wherever the indexer's output goes (ADR-043, B-008).

On Windows, a pipe or a redirected file gets the ANSI code page (cp1252) as its text
encoding; only a real console window gets UTF-8. The indexer prints "━━", "✓" and "Δ",
which cp1252 cannot encode, so every captured run (`code-indexer > log`, a scheduled task,
an MCP client's stdio pipes) died on its first line with UnicodeEncodeError.

A leaf on purpose: it imports only ``sys``, so an entry point can call it before loading
anything heavy.
"""
import sys


def utf8_stdio() -> None:
    """Reconfigure stdout and stderr to UTF-8. Call it first in every entry point."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass    # not a TextIOWrapper (already replaced by a harness); leave it
