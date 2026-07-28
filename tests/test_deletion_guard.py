"""
test_deletion_guard.py — a bulk purge is confirmed, not performed silently (ADR-026 §5).

**The mechanism this guards.** `DiffResult.deleted` is "present in SQLite, absent from
disk", and it drives an irreversible purge: FAISS `remove_ids` physically compacts the
survivors (no tombstones) and `db.delete_file` cascades to symbols, chunks and
locations. That is *also* what makes ADR-026's migration free — newly-ignored files
land in `deleted` and clean themselves up with no reindex.

Both halves are the same mechanism, which is the whole problem: at this line, "the
ignore-set fix is removing 84% of an index that should never have held it" and "a
misconfiguration is destroying an index" are indistinguishable. The guard does not try
to tell them apart. It shows the damage and makes a human say yes.

Pure logic over a NamedTuple — no database, no FAISS, no model load.
"""
from __future__ import annotations

import incremental_indexer as ii


def _diff(deleted: list[str], new: list[str] | None = None) -> ii.DiffResult:
    return ii.DiffResult(new=new or [], modified=[], deleted=deleted)


# ---------------------------------------------------------------------------
# The threshold
# ---------------------------------------------------------------------------

def test_threshold_is_the_larger_of_fifty_and_a_fifth():
    """50 of 60 files is a catastrophe; 50 of 20,000 is a refactor."""
    assert ii._deletion_threshold(60) == 50
    assert ii._deletion_threshold(20_000) == 4_000
    assert ii._deletion_threshold(0) == 50


def test_an_ordinary_deletion_passes_straight_through():
    diff = _diff([f"src/gone_{i}.py" for i in range(5)])
    out, msg = ii.bulk_deletion_verdict(diff, 600)
    assert out is diff
    assert msg is None


def test_a_deletion_at_the_threshold_is_not_gated():
    """`>` not `>=` — the boundary case is the ordinary one."""
    n_indexed = 1000
    at_limit = ii._deletion_threshold(n_indexed)
    out, msg = ii.bulk_deletion_verdict(_diff([f"a/{i}.py" for i in range(at_limit)]), n_indexed)
    assert msg is None
    assert len(out.deleted) == at_limit


# ---------------------------------------------------------------------------
# Non-interactive callers log and skip
# ---------------------------------------------------------------------------

def test_non_interactive_runs_skip_the_purge_and_say_so():
    """The watchdog and the `reindex` MCP tool have nobody to ask.

    Skipping is the safe direction: the stale entries stay, and the next run asks
    again. Blocking a background process on a prompt nobody can see is not.
    """
    diff = _diff([f"benchmarks/corpus/f{i}.py" for i in range(503)])
    out, msg = ii.bulk_deletion_verdict(diff, 601, interactive=False)
    assert out.deleted == []
    assert "SKIPPED" in msg
    assert "--prune" in msg


def test_the_rest_of_the_run_still_proceeds():
    """Only deletions are withheld — new and modified files are indexed as usual."""
    diff = _diff([f"old/{i}.py" for i in range(200)], new=["src/fresh.py"])
    out, _ = ii.bulk_deletion_verdict(diff, 300, interactive=False)
    assert out.deleted == []
    assert out.new == ["src/fresh.py"]


def test_the_report_names_the_directories_responsible():
    """"Which directory did this?" is the question a user actually has."""
    diff = _diff(
        [f"benchmarks/corpus/f{i}.py" for i in range(500)]
        + [f"venv/site-packages/g{i}.py" for i in range(60)]
    )
    _, msg = ii.bulk_deletion_verdict(diff, 601, interactive=False)
    assert "benchmarks/" in msg
    assert "venv/" in msg
    assert "500" in msg
    assert "93%" in msg or "93 %" in msg   # 560 of 601


# ---------------------------------------------------------------------------
# --prune
# ---------------------------------------------------------------------------

def test_prune_answers_yes_in_advance():
    diff = _diff([f"benchmarks/f{i}.py" for i in range(503)])
    out, msg = ii.bulk_deletion_verdict(diff, 601, prune=True, interactive=False)
    assert out is diff
    assert msg is None


def test_prune_is_exposed_on_the_cli():
    """The message tells the user to run `--prune`; the flag has to exist."""
    import inspect

    source = inspect.getsource(ii.main)
    assert "--prune" in source


# ---------------------------------------------------------------------------
# The interactive path
# ---------------------------------------------------------------------------

def _answer(monkeypatch, text: str) -> None:
    monkeypatch.setattr(ii.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a: text)


def test_yes_applies_the_deletions(monkeypatch):
    _answer(monkeypatch, "y")
    diff = _diff([f"a/{i}.py" for i in range(100)])
    out, msg = ii.bulk_deletion_verdict(diff, 200)
    assert out.deleted == diff.deleted
    assert "confirmed" in msg.lower()


def test_anything_other_than_yes_skips(monkeypatch):
    """Default-no: the prompt is `[y/N]`, and a bare Enter must not destroy an index."""
    for reply in ("", "n", "no", "maybe"):
        _answer(monkeypatch, reply)
        out, msg = ii.bulk_deletion_verdict(_diff([f"a/{i}.py" for i in range(100)]), 200)
        assert out.deleted == [], f"reply {reply!r} applied the deletions"
        assert "SKIPPED" in msg


def test_an_interrupted_prompt_skips(monkeypatch):
    """Ctrl-C / EOF at the prompt must not be read as consent."""
    monkeypatch.setattr(ii.sys.stdin, "isatty", lambda: True, raising=False)

    def _raise(*_a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)
    out, msg = ii.bulk_deletion_verdict(_diff([f"a/{i}.py" for i in range(100)]), 200)
    assert out.deleted == []
    assert "SKIPPED" in msg


# ---------------------------------------------------------------------------
# The callers
# ---------------------------------------------------------------------------

def test_both_unattended_callers_pass_interactive_false():
    """MCPServer's watchdog and `reindex` tool must not be able to block on input.

    Parsed rather than grepped: "run_incremental()" appears in prose in this file
    too, and a text scan reads those as call sites.
    """
    import ast
    import inspect

    import MCPServer

    tree = ast.parse(inspect.getsource(MCPServer))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_incremental"
    ]
    assert len(calls) == 2, f"expected 2 run_incremental call sites, found {len(calls)}"
    for call in calls:
        passed = {
            kw.arg: getattr(kw.value, "value", None) for kw in call.keywords
        }
        assert passed.get("interactive") is False, (
            f"unattended call at line {call.lineno} does not pass interactive=False"
        )
