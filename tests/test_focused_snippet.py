"""investigate_architecture's evidence snippets: the lines that match the query, numbered
as in the source file, instead of the first 300 characters of the chunk."""
from MCPServer import _focused_snippet, _query_terms


def _tier1(first: int, body: list[str]) -> str:
    return ("File: src/move.ts\nEntity: src/move.ts::moveOut (function)\n"
            f"Lines: {first}-{first + len(body) - 1}\nCode:\n" + "\n".join(body))


BODY = (["export async function moveOut(req: MoveRequest) {"]
        + [f"  const step{i} = await load(req, {i});" for i in range(30)]
        + ["  // take the WIP off, item by item",
           "  for (const item of req.items) {",
           "    wip.decrement(item.partNumber, item.qty);",
           "  }"]
        + [f"  audit{i}();" for i in range(20)]
        + ["}"])


def test_shows_the_signature_and_the_matching_lines_with_file_line_numbers():
    snippet, span = _focused_snippet(_tier1(360, BODY), "move out decrements WIP item by item")
    assert span == (360, 360 + len(BODY) - 1)
    lines = snippet.splitlines()
    assert lines[0].split() [:2] == ["360", "export"]          # the signature, numbered
    assert any("wip.decrement" in ln and ln.split()[0] == "393" for ln in lines)
    assert "  ⋮" in lines                                          # the skipped middle
    assert "File:" not in snippet and "Entity:" not in snippet     # no header


def test_short_chunks_are_shown_whole():
    body = ["function f() {", "  return 1;", "}"]
    snippet, span = _focused_snippet(_tier1(10, body), "anything")
    assert [ln.split()[0] for ln in snippet.splitlines()] == ["10", "11", "12"]
    assert span == (10, 12)


def test_window_chunks_have_no_line_numbers():
    text = "\n".join(f"line {i} of a window" for i in range(40))
    snippet, span = _focused_snippet(text, "window")
    assert span is None
    assert snippet.splitlines()[0] == "  line 0 of a window"


def test_an_outline_with_elided_bodies_is_not_numbered():
    body = ["class Box {", "  open()  ...", "  close()  ...", "}"]
    text = ("File: a.ts\nEntity: a.ts::Box (class)\nLines: 5-40\nCode:\n" + "\n".join(body))
    snippet, span = _focused_snippet(text, "open")
    assert span == (5, 40)
    assert snippet.splitlines()[0] == "  class Box {"


def test_no_matching_term_shows_the_opening_lines():
    snippet, _ = _focused_snippet(_tier1(1, BODY), "zebra")
    assert snippet.splitlines()[1].split()[0] == "2"


def test_query_terms_split_identifiers_and_drop_filler():
    assert _query_terms("moveItemsToProduction takes WIP off") == {
        "move", "items", "production", "takes", "wip"}
