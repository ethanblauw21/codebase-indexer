#!/usr/bin/env python3
"""
Capture golden snapshots of parse_file() and chunk_file_ast() output.

Run BEFORE the adapter refactor (ADR-003 Phase 1) to record pre-refactor behavior.
After the refactor, run tests/test_adapter_snapshots.py to verify byte-identical output.

A changed FQN or chunk boundary would orphan every existing index.
The snapshot gate makes that a CI failure rather than a silent corruption.

Usage:
    python tools/capture_snapshots.py [--fixtures-dir tests/fixtures]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

from ast_chunker import chunk_file_ast, parse_file  # noqa: E402

FIXTURE_NAMES = ["sample.py", "sample.ts", "sample.js", "sample.tsx", "sample.cs", "sample.cpp"]


def _serialize_parse(result) -> dict:
    symbols = sorted(
        [
            {
                "fqn": s.fqn,
                "kind": s.kind,
                "name": s.name,
                "class_context": s.class_context,
                "start_line": s.start_line,
                "end_line": s.end_line,
            }
            for s in result.symbols
        ],
        key=lambda s: s["fqn"],
    )
    edges = sorted(
        [
            {
                "source_fqn": e.source_fqn,
                "target": e.target,
                "kind": e.kind,
                "resolved_target": e.resolved_target,
            }
            for e in result.edges
        ],
        key=lambda e: (e["source_fqn"], e["target"], e["kind"]),
    )
    symbol_types = sorted(
        [
            {
                "fqn": st.fqn,
                "return_type": st.return_type,
                "params": st.params,
                "type_params": st.type_params,
                "is_async": st.is_async,
                "is_generator": st.is_generator,
            }
            for st in result.symbol_types
        ],
        key=lambda st: st["fqn"],
    )
    return {"symbols": symbols, "edges": edges, "symbol_types": symbol_types}


def _serialize_chunks(chunks) -> list:
    return [
        {
            "scope": c.scope,
            "file": c.file,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "tags": sorted(c.tags),
            "text": c.text,
        }
        for c in chunks
    ]


def _portable(value, fixture_path: str, name: str):
    """Replace the fixture's absolute path with its bare name, in every string.

    The adapters put the absolute file path into FQNs, scopes and chunk text, so a
    snapshot taken as-is only matches on the machine and checkout it was taken in.
    Both separator spellings are replaced, so a Windows-captured snapshot and a
    Linux run agree.
    """
    if isinstance(value, str):
        return value.replace(fixture_path, name).replace(fixture_path.replace("\\", "/"), name)
    if isinstance(value, list):
        return [_portable(v, fixture_path, name) for v in value]
    if isinstance(value, dict):
        return {k: _portable(v, fixture_path, name) for k, v in value.items()}
    return value


def snapshot(fixture_path: str, name: str) -> dict:
    """parse_file() and chunk_file_ast() output for one fixture, with portable paths."""
    with open(fixture_path, encoding="utf-8") as fh:
        content = fh.read()
    parse_result = parse_file(fixture_path, content)
    chunks       = chunk_file_ast(fixture_path, content)
    return _portable(
        {
            "fixture": name,
            "parse":   _serialize_parse(parse_result),
            "chunks":  _serialize_chunks(chunks),
        },
        fixture_path,
        name,
    )


def capture(fixtures_dir: str) -> None:
    src_dir  = os.path.join(fixtures_dir, "src")
    snap_dir = os.path.join(fixtures_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)

    for name in FIXTURE_NAMES:
        fixture_path = os.path.abspath(os.path.join(src_dir, name))
        if not os.path.exists(fixture_path):
            print(f"  SKIP  {name} — not found at {fixture_path}")
            continue

        snap = snapshot(fixture_path, name)

        out_path = os.path.join(snap_dir, f"{name}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        print(
            f"  WROTE {os.path.relpath(out_path)}"
            f"  ({len(snap['parse']['symbols'])} symbols,"
            f" {len(snap['parse']['edges'])} edges,"
            f" {len(snap['chunks'])} chunks)"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--fixtures-dir",
        default=os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures"),
        metavar="DIR",
        help="Directory containing src/ fixtures and where snapshots/ will be written",
    )
    args = ap.parse_args()
    capture(args.fixtures_dir)


if __name__ == "__main__":
    main()
