from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Resolved at launch time by __main__.py; falls back to CWD at import time.
_PROJECT_ROOT: Path = Path.cwd().resolve()

_server = None
_lock = threading.Lock()
_load_error: str | None = None


def set_project_root(path: str | Path) -> None:
    global _PROJECT_ROOT, _server, _load_error
    _PROJECT_ROOT = Path(path).resolve()
    # Reset so a new project can be loaded if called before first use.
    _server = None
    _load_error = None


def _get_server():
    global _server, _load_error
    if _server is not None:
        return _server
    if _load_error is not None:
        raise RuntimeError(f"Index load failed: {_load_error}")
    try:
        import os
        os.chdir(str(_PROJECT_ROOT))
        import MCPServer as _m
        _server = _m
    except Exception as exc:
        _load_error = str(exc)
        raise
    return _server


def call_tool(tool_id: str, params: dict) -> str:
    srv = _get_server()
    fn = getattr(srv, tool_id)
    with _lock:
        return fn(**params)


def get_file_chunks(file_path: str) -> list[dict]:
    srv = _get_server()
    srv._ensure_indexes()
    search = file_path.replace("\\", "/")
    chunks: list[dict] = []
    for doc_id, doc in srv.doc_store.docs.items():
        doc_f = doc.get("file", "").replace("\\", "/")
        if search in doc_f or doc_f.endswith(search.split("/")[-1]):
            tier_raw = doc.get("tier", "")
            tier_label = (
                tier_raw
                .replace("tier1_surgical",    "T1")
                .replace("tier2_component",   "T2")
                .replace("tier3_architectural", "T3")
            )
            chunks.append({
                "id":      doc_id,
                "file":    doc.get("file", ""),
                "tier":    tier_label,
                "scope":   srv.get_clean_scope(doc),
                "text":    doc.get("text", ""),
                "summary": doc.get("summary", ""),
            })
    order = {"T1": 0, "T2": 1, "T3": 2}
    chunks.sort(key=lambda c: order.get(c["tier"], 9))
    return chunks


def extract_files(output: str) -> list[str]:
    return [r["file"] for r in extract_results(output)]


def extract_results(output: str) -> list[dict]:
    """Parse tool output into [{file, score}] preserving per-block score association."""
    file_pat = re.compile(
        r'([^\s\[\]\(\)\|\n]+\.(?:ts|tsx|js|jsx|py|json|md|css|scss|html))'
    )
    score_pat = re.compile(r'\[Score\]:\s*([\d.]+)')

    seen: set[str] = set()
    results: list[dict] = []

    for block in re.split(r'\n(?=- )', "\n" + output):
        fm = file_pat.search(block)
        if not fm:
            continue
        fp = fm.group(1).strip()
        if not fp or len(fp) <= 4 or fp in seen:
            continue
        seen.add(fp)
        sm = score_pat.search(block)
        results.append({
            "file": fp,
            "score": float(sm.group(1)) if sm else None,
        })

    return results[:60]
