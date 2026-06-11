## What changed

<!-- One or two sentences. What does this PR do and why? -->

## Type of change

- [ ] Minor — docs/comments/README only, no `src/` changes (no ADR needed)
- [ ] Major — any `src/` change: new or modified MCP tool, retrieval logic, indexing pipeline, graph schema, TUI (ADR required)
- [ ] Bug fix — minor (isolated, no shared code, no issue needed)
- [ ] Bug fix — non-trivial (touches shared indexing/retrieval code, GitHub issue required)

## Links

<!-- Delete rows that don't apply -->

| Type | Link |
|------|------|
| ADR | `docs/adr/ADR-XXX-...` |
| Issue | #XXX |
| Related PR | #XXX |

## Implementation notes

<!-- Reviewer context, deviations from the ADR design, tradeoffs made. -->

## Checklist

- [ ] MCP server starts cleanly (`python src/MCPServer.py`)
- [ ] `reindex` completes without error on a test target
- [ ] Golden path manually tested for any changed tool or pipeline stage
- [ ] ADR Implementation Log updated (Major changes only)
- [ ] No leftover `print()` / debug output
- [ ] No `TODO` / `FIXME` without a linked issue
