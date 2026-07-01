"""Regression: the C# adapter must emit CALLS edges (fix/csharp-call-query-field).

`_CALL_QUERY` used the wrong tree-sitter field (`member:` instead of `name:` on
member_access_expression), which silently matched nothing — so C# produced zero call
edges (serilog: 0 CALLS across 221 files). Without call edges the graph Traverse step
is dead for C# regardless of ADR-021. These tests lock in call extraction for both
free calls (`Foo()`) and receiver calls (`recv.Method()`).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ast_chunker import parse_file  # noqa: E402

_CS = """
namespace Demo
{
    public class Foo
    {
        public int Bar()
        {
            return Baz() + Helper.Compute(3);
        }

        private int Baz() { return 1; }
    }
}
"""


def _call_targets(path, code):
    pr = parse_file(path, code)
    return {e.target for e in pr.edges if e.kind == "call"}


def test_csharp_emits_call_edges():
    targets = _call_targets("Demo.cs", _CS)
    assert targets, "C# adapter emitted no call edges (the member:/name: query bug)"


def test_csharp_free_and_receiver_calls_captured():
    targets = _call_targets("Demo.cs", _CS)
    assert "Baz" in targets          # free function call: Baz()
    assert "Compute" in targets      # receiver call: Helper.Compute(3)
