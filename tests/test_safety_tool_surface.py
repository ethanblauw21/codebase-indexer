"""ADR-049 §4–6: every read tool declares readOnlyHint (B-045), tool output is
redacted at the MCP boundary only (B-018), and the server instructions route
agents to the index before Grep (B-021).

The tool lists come from the FastMCP registry, not a hand-written list, so a tool
added later is covered without touching this file.
"""
import inspect
import os
import sys
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import MCPServer as M  # noqa: E402

FAKE_TOKEN = "ghp_" + "x" * 36
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----"


def _registered():
    return M.mcp._tool_manager.list_tools()


def test_every_tool_but_reindex_is_read_only():
    tools = _registered()
    assert any(t.name == "reindex" for t in tools)
    for t in tools:
        if t.name == "reindex":
            continue
        assert t.annotations is not None and t.annotations.readOnlyHint is True, t.name


def test_reindex_declares_it_writes_and_is_not_safe_to_retry():
    (t,) = [t for t in _registered() if t.name == "reindex"]
    assert t.annotations.readOnlyHint is False
    assert t.annotations.destructiveHint is True
    assert t.annotations.idempotentHint is False      # a client must not auto-retry a timed-out rebuild


def test_the_listing_carries_the_hints_to_clients():
    listed = anyio.run(M.mcp.list_tools)
    hints = {t.name: t.annotations.readOnlyHint for t in listed}
    assert hints.pop("reindex") is False
    assert hints and all(hints.values())


def test_the_listing_sends_dedented_descriptions_and_no_output_schema():
    """B-019: every session pays for the listing, so it carries no indentation and
    no generated ``{"result": string}`` schema, which tells an agent nothing."""
    for t in anyio.run(M.mcp.list_tools):
        assert t.outputSchema is None, t.name
        assert t.description and t.description == inspect.cleandoc(t.description), t.name


def test_read_only_cannot_be_overridden_by_hand():
    with pytest.raises(TypeError, match="derived from reads_index"):
        M._tool(readOnlyHint=False)


class _Chunk(SimpleNamespace):
    pass


@pytest.fixture
def search_returning_secrets(monkeypatch):
    """semantic_code_search over a stubbed index whose only chunk holds two fake secrets."""
    chunk = _Chunk(file="src/settings.py", scope="settings", tier="tier1_surgical",
                   text=f'GITHUB_TOKEN = "{FAKE_TOKEN}"\nKEY = """{FAKE_PEM}"""\n')
    monkeypatch.setattr(M, "_ensure_indexes", lambda: SimpleNamespace(db=None))
    monkeypatch.setattr(M, "_search", lambda query, top_n=10: [chunk])
    monkeypatch.setattr(M, "jina_tokenizer", SimpleNamespace(count_tokens=len))
    import incremental_indexer
    monkeypatch.setattr(incremental_indexer, "chunker_version_warning", lambda db: "")


def _text(result) -> str:
    content = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(block, "text", "") for block in content)


def test_tool_output_is_redacted_at_the_mcp_boundary(search_returning_secrets):
    out = _text(anyio.run(M.mcp.call_tool, "semantic_code_search", {"query": "settings"}))
    assert "src/settings.py" in out                   # the result itself still arrives
    assert FAKE_TOKEN not in out and "FAKE" not in out
    assert '"[REDACTED:github-token]"' in out and "[REDACTED:private-key]" in out


def test_the_plain_function_is_not_redacted(search_returning_secrets):
    """The TUI and the tests call the plain function; the index stays faithful."""
    out = M.semantic_code_search("settings")
    assert FAKE_TOKEN in out and FAKE_PEM in out and "[REDACTED" not in out


@pytest.mark.parametrize("git_mode", [False, True])
def test_both_instruction_branches_route_to_the_index_before_grep(tmp_path, monkeypatch, git_mode):
    import index_location
    monkeypatch.setattr(index_location, "git_ref", lambda path: "main" if git_mode else None)
    monkeypatch.setattr(index_location, "ref_display", lambda path, ref: ref, raising=False)
    text = M._server_instructions(str(tmp_path))
    assert ("read straight from git" in text) is git_mode
    assert "before Grep" in text and "exact strings" in text
    registered = {t.name for t in _registered()}
    for name in ("semantic_code_search", "trace_data_flow", "what_writes", "analyze_blast_radius"):
        assert f"`{name}`" in text and name in registered      # names only tools that exist
