"""ADR-047: one IndexState per tool call (#52), tools off the event loop (#53),
one in-memory copy of the index (#63)."""
import inspect
import os
import sys
import threading
import time
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import MCPServer as M  # noqa: E402


class _StubRetriever:
    """Stands in for HybridRetriever: each construction is a new generation."""
    def __init__(self):
        self._doc_store = SimpleNamespace(docs={})
        self._db = object()
        self._tier1 = self._tier2 = self._tier3 = SimpleNamespace(ntotal=0)


@pytest.fixture
def stub_loading(monkeypatch):
    monkeypatch.setattr(M, "HybridRetriever", _StubRetriever)
    monkeypatch.setattr(M, "_state", None)
    monkeypatch.setattr(M, "_faiss_stamp", lambda index_dir=None: ())


def test_a_swap_mid_call_does_not_change_what_the_call_reads(stub_loading):
    """#52: a watchdog swap between two reads of one call used to mix generations."""
    seen = []

    @M._bind_index
    def tool():
        seen.append(M._index())
        M._reload_indexes()                 # the watchdog swaps mid-call
        seen.append(M._index())
        seen.append(M._get_hybrid_retriever())
        return "done"

    tool()
    first, second, retriever = seen
    assert first is second and retriever is first.retriever
    assert M._index() is not first          # the next call sees the new generation
    assert M._index().generation == first.generation + 1


def test_a_tool_called_from_a_tool_reads_the_same_state(stub_loading):
    inner_seen = []

    @M._bind_index
    def inner():
        inner_seen.append(M._index())

    @M._bind_index
    def outer():
        before = M._index()
        M._reload_indexes()
        inner()
        return before

    assert outer() is inner_seen[0]


def test_the_server_holds_one_copy_of_the_index(stub_loading):
    """#63: the chunks and vectors the tools read are the retriever's own."""
    st = M._ensure_indexes()
    assert st.doc_store is st.retriever._doc_store
    assert st.db is st.retriever._db
    assert st.tiers == (st.retriever._tier1, st.retriever._tier2, st.retriever._tier3)
    for gone in ("doc_store", "t1_index", "t2_index", "t3_index", "index_manager",
                 "_hybrid_retriever", "_iterative_retriever"):
        assert not hasattr(M, gone)


def test_every_registered_tool_is_async_with_the_same_arguments():
    """#53: FastMCP runs a sync tool on the event loop; an async one it awaits."""
    tools = M.mcp._tool_manager.list_tools()
    assert len(tools) == 14
    for t in tools:
        assert t.is_async, t.name
        plain = getattr(M, t.name)
        assert list(inspect.signature(t.fn).parameters) == \
            list(inspect.signature(plain).parameters), t.name


def test_a_slow_tool_leaves_the_event_loop_free(monkeypatch):
    """While a tool call is busy, the loop keeps serving (pings, other requests)."""
    def slow_load():
        time.sleep(0.6)
        raise RuntimeError("stub: no index")
    monkeypatch.setattr(M, "_ensure_indexes", slow_load)

    ticks = []

    async def main():
        async def ticker():
            for _ in range(5):
                await anyio.sleep(0.05)
                ticks.append(time.monotonic())

        async def call():
            with pytest.raises(Exception):
                await M.mcp.call_tool("index_status", {})

        start = time.monotonic()
        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            tg.start_soon(ticker)
        return start

    start = anyio.run(main)
    assert len(ticks) == 5 and ticks[-1] - start < 0.5    # all ticked during the 0.6 s call


def test_reindex_is_not_held_up_by_a_busy_read_tool(monkeypatch, tmp_path):
    """A read tool holds the read limiter; `reindex` must still run (it would
    otherwise wait out a search, and a rebuild would block every search)."""
    release = threading.Event()

    def busy_load():
        release.wait(5)
        raise RuntimeError("stub: no index")

    def fake_reindex(changed_files_only):
        release.set()                      # only reachable if reindex isn't queued behind the read
        return "reindexed"

    monkeypatch.setattr(M, "_ensure_indexes", busy_load)
    monkeypatch.setattr(M, "_reindex", fake_reindex)
    import incremental_indexer
    import index_lock
    monkeypatch.setattr(index_lock, "worktree_refusal", lambda path: None)   # tests run in worktrees
    monkeypatch.setattr(incremental_indexer, "INDEX_DIR", str(tmp_path))
    results = []

    async def main():
        async def read():
            with pytest.raises(Exception):
                await M.mcp.call_tool("index_status", {})

        async def rebuild():
            await anyio.sleep(0.1)         # the read takes the limiter first
            results.append(await M.mcp.call_tool("reindex", {"changed_files_only": True}))

        with anyio.fail_after(4):
            async with anyio.create_task_group() as tg:
                tg.start_soon(read)
                tg.start_soon(rebuild)

    anyio.run(main)
    assert release.is_set() and results
