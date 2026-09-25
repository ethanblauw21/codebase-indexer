# ADR-036: One Reindex at a Time, and a Watchdog Reindex That Can Print

**Status:** proposed
**Date:** 2026-09-25
**Branch:** `feature/adr-036-one-reindex-at-a-time`
**Reviewer:** @edb
**Backlog:** [B-032](../backlog.md#b-032) — a save during a running watchdog reindex starts a second reindex in parallel
**Depends on:** none
**Depended on by:** none yet. Related: [ADR-028](./ADR-028-central-model-host.md), the model host the live watchdog daemon will embed and summarize through; B-033, the same race across processes.

## Context

The watchdog daemon is the top priority once the GPU works again: an MCP server that keeps its
index current as files are saved. Two things stand between today's code and that.

**1. Overlapping runs (B-032).** `_ReindexDebouncer` collapses a burst of events into one
`run_incremental` after 3 s of quiet. But `_fire` runs the reindex in the timer's own thread, and an
event during that run starts a new timer. That timer fires 3 s later and starts a second
`run_incremental` beside the first. The `reindex` MCP tool is a third way in, with no guard either.
Two runs race on SQLite writes, on `MultiIndexManager.save_all`, and, with summaries on, for the GPU.

**2. Found while testing 1: the watchdog reindex never ran under an MCP client on Windows.**
- A client that launches the server over stdio gives it pipes. On Windows a pipe's text encoding is
  the ANSI code page, cp1252 here (`sys.stdout.encoding` = `cp1252`, UTF-8 mode off, Python 3.14).
- `run_incremental`'s first line is `print("━━ Incremental Indexer: …")`, and cp1252 has no `━`.
- So every watchdog reindex raised `UnicodeEncodeError` on its first print. `_fire` caught it and
  printed `[Watchdog] Reindex failed: 'charmap' codec can't encode characters in position 0-1`,
  to a stdout nobody reads.
- The server's registration in `~/.claude.json` sets neither `PYTHONUTF8` nor `PYTHONIOENCODING`.
- The `reindex` tool was unaffected, because it swaps `sys.stdout` for a `StringIO` while it runs.
  The eval tools were unaffected because they already `reconfigure` their streams (HANDOFF.md noted
  the crash for hand-run kit scripts).
- Seven `print` calls in `incremental_indexer.py` hold characters outside cp1252 (`━`, `→`, `✓`, `✗`).

## Decision

1. **`_reindex_lock`**, a module-level `threading.Lock` in `MCPServer.py`. The watchdog's run and the
   `reindex` tool both hold it around `run_incremental` plus the index reload. The tool's body moves
   unchanged into `_reindex`; the decorated `reindex` keeps its signature and docstring, so the tool
   schema does not change.
2. **At most one waiting run.** `_ReindexDebouncer` gets a `_queued` flag. A timer that fires while
   another fired run is still waiting for the lock returns at once. The waiting run reads the disk
   only when it starts, so it covers that change too. It clears `_queued` as soon as it holds the
   lock, so a save after that point gets a run of its own.
3. **`_utf8_stdio()`**, called first in `main()`: `reconfigure` stdout and stderr to UTF-8 with
   `errors="replace"` and line buffering. The protocol is untouched, because FastMCP writes it through
   its own UTF-8 wrapper over `sys.stdout.buffer`. Line buffering flushes each of our lines whole, so
   none can land in the middle of a protocol message.

Out of scope: two server processes on one index (B-033), and re-embedding only changed chunks
(B-034).

## Consequences

**Better:**
- A save during a reindex queues exactly one follow-up. It no longer starts a second writer.
- A `reindex` tool call waits for a watchdog run in flight instead of wiping the index under it.
- The watchdog reindex actually runs when the server is launched by an MCP client on Windows.

**Worse:**
- A `reindex` call can now block for as long as the watchdog run ahead of it takes. With summaries on,
  that can be minutes. The client sees a slow tool call, not a second writer.
- The server's own stdout lines are now UTF-8 on the wire. A client that decoded them as cp1252 would
  show `━` as mojibake. No client reads them: they are not protocol messages.

**Neutral:** the event-storm half (debounce) is unchanged, and so is the 3 s delay.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| A running flag with a follow-up scheduled when the run ends (the fix B-032 sketched) | The lock gives the same one-at-a-time behaviour, and it also covers the `reindex` tool, which a flag inside the debouncer can't see. |
| The lock alone, with no `_queued` flag | Each save more than 3 s apart during a long run would leave another thread waiting on the lock, and each of them would run a reindex afterwards. |
| Strip the non-ASCII characters from the indexer's prints | Seven lines today, and the next `✓` brings the bug back. Fixing the stream fixes every print. |
| Set `PYTHONUTF8=1` in the MCP registration | It fixes this machine only. Anyone who registers the server from the README would hit the bug again. |
| Move the indexer's prints to stderr | Better hygiene for a stdio server, but FastMCP takes `sys.stdout.buffer` when it starts, so swapping `sys.stdout` would move the protocol too. It needs its own change. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] `_reindex_lock`, held by the watchdog and the `reindex` tool; the tool body moved to `_reindex`
- [x] `_queued`: at most one fired run waits for the lock
- [x] `_utf8_stdio()` in `main()`
- [x] Tests (`tests/test_reindex_serial.py`, 4). A fake `run_incremental` blocks until the test lets it
  finish:
  - Five saves during one run give two runs, never two at once.
  - A save after the follow-up started gets a third run.
  - The `reindex` tool waits for a watchdog run in flight.
  - A subprocess with a cp1252 pipe for stdout prints the banner.
  - All four fail on `master`'s `MCPServer.py`.
- [ ] End to end on the GPU: a server launched over stdio with pipes, a file saved, and the watchdog's
  reindex finishing
- [ ] MCP Inspector: `tools/list --strict`, and `reindex` called once

**Notes:**
<!-- 2026-09-25: branch cut from master (0f39e44). -->

**2026-09-25, how the encoding bug surfaced.** Running the new tests against `master`'s `MCPServer.py`
left extra watchdog timers alive after pytest had restored the real `run_incremental`. One fired and
printed `[Watchdog] Reindex failed: 'charmap' codec can't encode characters in position 0-1`.
Position 0-1 is the `━━` of the banner. Reproduced directly: importing `MCPServer` and printing the
banner with stdout redirected exits 1 with that error. With `_utf8_stdio()` first, it exits 0.

**Suite:** 388 passed, 1 skipped. The 6 failures are `test_adapter_snapshots`. It fails in every
worktree outside the main checkout (a path difference, seen since ADR-034) and passes in the main
checkout and in CI.
