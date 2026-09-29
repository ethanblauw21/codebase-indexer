# ADR-043: `code-indexer` Writes UTF-8, Wherever Its Output Goes

**Status:** proposed
**Date:** 2026-09-29
**Branch:** `feature/adr-043-utf8-cli-output`
**Reviewer:** @edb
**Backlog:** [B-008](../backlog.md#b-008) — the indexer crashes on a Windows `cp1252` stream
before indexing a single file
**Depends on:** [ADR-036](./ADR-036-one-reindex-at-a-time.md) — its `MCPServer._utf8_stdio`,
the fix this generalizes.
**Depended on by:** none yet.

## Context

On Windows, Python gives stdout the ANSI code page (`cp1252` here) whenever the stream is a pipe or
a redirected file. A real console window gets UTF-8 (PEP 528), which is why nobody sees this when
running `code-indexer` by hand. `cp1252` has no `━`, `✓` or `Δ`. The stdout error handler is not
`backslashreplace` (stderr's is), so the first `print` of any of them raises `UnicodeEncodeError`.

`run_incremental()` opens with a `━━ Incremental Indexer: … ━━` banner, and
`incremental_indexer.py` has 55 `print` lines with non-ASCII characters. So any unattended run whose
output is captured dies on its first line: `code-indexer > build.log`, a pipe through `tee`, a
scheduled task, `subprocess` with `capture_output`, or an agent's shell tool. The wrapper exits 0.
**Reproduced 2026-09-29** while refreshing this repo's index for the B-056 A/B:
`python src/incremental_indexer.py > log` died at `incremental_indexer.py:1121`, the banner.

It dies before scanning, so today it writes nothing and the index is untouched. That holds only
while the banner stays first. The first non-ASCII `print` after the stale-vector purge would leave
the half-written state that B-035 / ADR-037 had to heal.

The MCP server hit the same crash through its stdio pipes, and ADR-036 fixed it there with
`MCPServer._utf8_stdio()`, which reconfigures both streams to UTF-8 at startup. The CLI entry point
`incremental_indexer.main()` never got it. CI can't see the gap: it runs on Ubuntu, where every
stream is already UTF-8.

## Decision

1. **One helper, in a leaf module.** A new `src/utf8_stdio.py` holds `utf8_stdio()`: the existing
   body, which reconfigures `sys.stdout` and `sys.stderr` to `encoding="utf-8", errors="replace",
   line_buffering=True` and skips a stream that is not a `TextIOWrapper`. It imports only `sys`, so
   any entry point can call it before loading anything heavy.
2. **Every entry point calls it first.** `incremental_indexer.main()` (the `code-indexer` script)
   calls it before parsing arguments. `MCPServer._utf8_stdio()` stays as a one-line delegate,
   because two tests and the server's `main()` call it by that name.
3. **The test forces the failure on any OS.** A child process started with `PYTHONIOENCODING=cp1252`
   has a cp1252 stdout on Linux too. The test asserts that the banner crashes such a child without
   the helper and prints as UTF-8 with it, so Ubuntu CI now guards the fix.

## Consequences

**Better:** unattended and captured runs work on Windows without anyone knowing to set
`PYTHONIOENCODING`. The fix lives in one place, and a future entry point has a helper to call.

**Worse:** captured output is now UTF-8 on Windows. A consumer that reads the log as `cp1252` (a
plain `type build.log` in an old console) shows mojibake for `━`. It no longer crashes, and UTF-8 is
what every other part of the tooling already writes.

**Neutral:** ADR-026 §4's rule that new stdout strings stay ASCII-only because of B-008 is no longer
needed. It stays, because ASCII output is harmless. `errors="replace"` means a character UTF-8
cannot encode (lone surrogates only) prints as `?` instead of raising.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Strip every non-ASCII character from the indexer's output (B-008's second option) | 55 lines in `incremental_indexer.py`, plus whatever the next change adds. It can't be enforced short of a lint rule, and the server already chose to reconfigure. |
| Set `PYTHONUTF8=1` in the console-script wrapper | setuptools generates the wrapper, and it wouldn't cover `python src/incremental_indexer.py`. |
| Call `utf8_stdio()` at import time of `incremental_indexer` | Importing a module shouldn't change the host process's streams. Tests and the server import it as a library. |
| Leave the helper in `MCPServer` and import it from the CLI | That imports FastMCP, the retriever and `core` before the CLI can print its first line. |

## Implementation Log

- [x] `src/utf8_stdio.py` + `pyproject.toml` `py-modules` entry (7aa1e81)
- [x] `incremental_indexer.main()` calls it first; `MCPServer._utf8_stdio()` delegates
- [x] Test (`tests/test_utf8_stdio.py`): a cp1252 child crashes on the banner without the helper,
  prints UTF-8 with it, and `main()` survives a cp1252 stdout. The `main()` test **fails** with
  the call removed. Full suite: 645 passed, 1 skipped.
- [x] Verified on Windows: `python src/incremental_indexer.py > log` with no `PYTHONIOENCODING`
  exits 0 on this repo, and the log is UTF-8 with the banner intact.
- [x] B-008 → done in `docs/backlog.md`

**Notes:**
- 2026-09-29, MCP Inspector 2.8.0: `tools/list --strict` exits 0 with all 14 tools. **Correction:**
  I first wrote here that a `tools/call reindex` "times out because a no-change reindex takes
  over a minute". That was wrong. `reindex()` defaults to a **full rebuild**, and both calls
  (this branch's server and the unchanged one) were full rebuilds of the live index. Inspector
  killed them at its fixed 60 s, which wiped the index and its backup (B-057). A full rebuild
  restored it the same morning, identical to before. Nothing in ADR-043 is involved. Outputs are
  in `gpu-crash-repro/telemetry/adr043/` (gitignored).
- 2026-09-29: `master`'s `pyproject.toml` `py-modules` list is the old one. `chore/cleanup-and-docs`
  (aa4212f) rewrites it, so whichever merges second resolves a one-line conflict: keep
  `"utf8_stdio"`.
