"""ADR-034: class members own their docs, and every member gets a chunk."""
from ast_chunker import parse_file

TS = '''export class Q {
  /** the field doc */
  count = 0;

  /** Run the queue. */
  run(a: number): void { go(); }

  /** Decorated. */
  @bound
  tick(): void { return; }

  /** Too far away. */


  far(): void { return; }

  // a line comment
  plain(): void { return; }
}
'''


def _syms(src, path="q.ts"):
    return {s.fqn: s for s in parse_file(path, src).symbols}


def test_method_doc_moves_into_the_method():
    run = _syms(TS)["q.ts::Q.run"]
    assert run.text.startswith("/** Run the queue. */")
    assert run.start_line == 5


def test_moved_doc_leaves_the_skeleton_and_field_doc_stays():
    skel = _syms(TS)["q.ts::Q"].text
    assert "Run the queue" not in skel
    assert "/** the field doc */" in skel
    assert "run(a: number): void  ..." in skel


def test_decorated_method_takes_its_doc_across_the_decorator():
    tick = _syms(TS)["q.ts::Q.tick"]
    assert tick.text.startswith("/** Decorated. */")
    assert "Decorated" not in _syms(TS)["q.ts::Q"].text


def test_doc_separated_by_two_blank_lines_does_not_move():
    syms = _syms(TS)
    assert not syms["q.ts::Q.far"].text.startswith("/**")
    assert "Too far away" in syms["q.ts::Q"].text


def test_line_comment_does_not_move():
    syms = _syms(TS)
    assert syms["q.ts::Q.plain"].text.startswith("plain()")
    assert "// a line comment" in syms["q.ts::Q"].text


def test_top_level_function_doc_is_untouched():
    syms = _syms("/** Top. */\nexport function f(): void { return; }\n")
    assert syms["q.ts::f"].text.startswith("function f")
