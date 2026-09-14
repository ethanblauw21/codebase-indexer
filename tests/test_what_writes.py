"""The one-hop write lookup, end to end and model-free.

Parses the `rung_addressing` conformance fixture with the real adapter, persists
it through the real `CodeDB.upsert_file`, and drives `MCPServer.what_writes`
against it with a monkeypatched `_db` — so this covers the whole path the fault
-tracing question actually travels: adapter reference emission → SQLite →
tool output. No embedder, no FAISS, no GPU.

Why the tool does not go through retrieval at all: "what writes this tag" is a
deterministic filter over a relation the extractor already resolved (writes are
11,379/11,379 on the survey corpus). Routing it through the RRF surface would
re-introduce the hop-decay that stopped ADR-019's graph-only fixtures from ever
registering a lift, and would let a ranked cut-off silently drop a writer.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapters.l5x_adapter import L5xAdapter  # noqa: E402
from db import CodeDB  # noqa: E402
import MCPServer as M  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "conformance" / "l5x"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """`rung_addressing` parsed, persisted, and wired in as MCPServer._db()."""
    src = FIXTURES / "rung_addressing.L5X"
    result = L5xAdapter().parse(str(src), src.read_bytes())

    db = CodeDB(str(tmp_path / "graph.db"))
    db.upsert_file(
        path="Filler.L5X",
        content_hash="h:rung_addressing",
        symbols=result.symbols,
        edges=result.edges,
        references=result.references,
    )
    monkeypatch.setattr(M, "_db", lambda: db)
    monkeypatch.setattr(M, "_ensure_indexes", lambda: None)
    yield db
    db.close()


# ----------------------------------------------------------------- db primitives

def test_edges_to_returns_every_writer_and_nothing_else(wired):
    assert wired.get_edges_to("Valve_Open", "writes") == ["Filler.Sequence"]
    assert wired.get_edges_to("Valve_Open", "reads") == []
    assert wired.get_edges_to("Step_Last", "reads") == []


def test_references_recover_the_rungs_the_edge_collapsed(wired):
    """One `writes` edge, two rungs. This is the whole point of item 1."""
    rows = wired.get_references_to("Valve_Open", "WRITE")
    assert [r["line"] for r in rows] == [1, 3]
    assert {r["context_fqn"] for r in rows} == {"Filler.Sequence"}


def test_reference_kind_filter_separates_set_from_used(wired):
    assert [r["line"] for r in wired.get_references_to("Seq_Active", "WRITE")] == [0]
    assert [r["line"] for r in wired.get_references_to("Seq_Active", "READ")] == [1]
    assert len(wired.get_references_to("Seq_Active")) == 2


# ------------------------------------------------------------------- the tool

def test_what_writes_names_the_routine_and_both_rungs(wired):
    out = M.what_writes("Valve_Open")

    assert "Filler.Sequence" in out
    assert "rung 1, 3" in out
    assert "1 routine(s) write it, at 2 rung(s)" in out


def test_what_writes_reports_readers_only_when_asked(wired):
    """Widely-read tags produce long lists, so reads are opt-in."""
    quiet = M.what_writes("Seq_Active")
    assert "READ BY" not in quiet
    assert "include_readers=True" in quiet

    loud = M.what_writes("Seq_Active", include_readers=True)
    assert "READ BY 1 routine(s), at 1 rung(s)" in loud


def test_what_writes_says_so_when_nothing_writes_the_tag(wired):
    """A read-only tag is a real answer, not an error.

    `Start_PB` is read on rung 0 and written nowhere — the honest response names
    what could be setting it from outside the program rather than reporting an
    empty result the caller has to interpret.
    """
    out = M.what_writes("Start_PB")

    assert "No routine writes it" in out
    assert "HMI write" in out


def test_unknown_tag_suggests_the_abbreviation_problem(wired):
    """The miss mode an engineer actually hits.

    Equipment appears under a long name, a short name and an initialism, and the
    tag list is filtered three times before it is found. An empty result should
    say that rather than imply the controller has no such logic.
    """
    out = M.what_writes("Weighscale_Total")

    assert "Nothing in the index carries that name" in out
    assert "spelled differently" in out


def test_multiple_writers_warn_about_scan_order(wired, monkeypatch):
    """Last write in scan order wins, and that is not visible in the rung.

    Raw INSERTs here spell the kind the way it is STORED (`WRITES`), because
    they bypass `_normalise_edge_kind`. Going through the adapter path would
    use the lowercase spelling.
    """
    with wired._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind) "
            "VALUES ('Filler.Override', 'Valve_Open', 'WRITES')"
        )
    wired.invalidate_graph_cache()

    out = M.what_writes("Valve_Open")
    assert "2 routine(s) write it" in out
    assert "LAST write in scan order wins" in out
    assert "rung unknown" in out


def test_alias_is_reported_as_a_hardware_endpoint(wired):
    """An alias tag is driven by the module, not only by ladder."""
    with wired._tx() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO edges(source_fqn, target, kind) "
            "VALUES ('Fill_Done', 'Local:1:I.Data.3', 'ALIAS_OF')"
        )
    wired.invalidate_graph_cache()

    out = M.what_writes("Fill_Done")
    assert "ALIAS" in out
    assert "Local:1:I.Data.3" in out
    assert "driven by the I/O module" in out
