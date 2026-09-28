# ADR-039: Timestamps on the Model Host Log and the Build's Progress Lines

**Status:** accepted
**Date:** 2026-09-28
**Branch:** `feature/b049-log-timestamps`
**Reviewer:** @edb
**Backlog:** [B-049](../backlog.md#b-049) — the model-host log and the build's `[summarize N/M]`
lines carry no timestamps, so rates can't be tracked
**Depends on:** none
**Depended on by:** [B-048](../backlog.md#b-048) (re-summarization cost), which reads per-save
timing off these logs; related to [ADR-028](./ADR-028-central-model-host.md) (the host) and
[ADR-036](./ADR-036-one-reindex-at-a-time.md) (the watchdog, and the non-ASCII-in-prints question
this ADR revisits).

## Context

`host.log` has no times. Dating a host's block on InventoryApp-V2's first index meant lining up
process start times with `host.json`'s `started_at`; the file's modified time didn't help either,
because Windows doesn't update it while the host holds the file open — it read 09:31:55 during a
run that had started at 09:39.

The build's `[summarize N/M]` lines have no times either. They print every `_SUMMARY_SLICE` (192)
texts, so the rate had to be worked out by hand from readings @edb took live. That produced two bad
ETAs, because pass 1 sorts texts longest-first (`incremental_indexer.py:891`) and throughput climbs
roughly 3-11x over a run — an average-since-start rate reads as much slower than the run actually is
once it is past the long texts.

A smaller finding on the way: a status request the client dropped mid-reply (a poll it gave up
waiting on) logs a full `ConnectionAbortedError` traceback in `host.log`. It's harmless — the client
retries — but it buries anything that matters under noise from something that happens on every
dropped poll.

**Non-ASCII separators.** The obvious format for the progress line uses `·` between fields. ADR-036
already hit this question for this exact file: seven of `incremental_indexer.py`'s prints hold
characters outside cp1252 (`━`, `→`, `✓`, `✗`), and a raw Windows console or pipe without UTF-8
raises `UnicodeEncodeError` on the first one. ADR-036 rejected stripping the non-ASCII characters
("the next `✓` brings the bug back") and fixed the stream instead — `MCPServer.main()` now calls
`_utf8_stdio()` before the watchdog can print anything. Since the file already commits to non-ASCII
output under that fix, and the watchdog is the path that runs unattended and is what this ADR is
for, the progress line keeps `·` rather than introducing an ASCII-only line that would look
inconsistent next to `━━ Pass 1 of 2 ━━` one line above it. The banners this ADR touches (`[HH:MM:SS]`
prefixes) use only ASCII brackets and colons regardless.

## Decision

1. **`model_host.py` logging.** `_configure_logging()` (called once, from `if __name__ ==
   "__main__"`) sets `logging.basicConfig(format="%(asctime)s [model-host] %(message)s",
   datefmt="%Y-%m-%dT%H:%M:%S", force=True)`. The three `print()` calls that announced lock
   contention, the listening port, and shutdown become `logger.info()` calls, so every line in
   `host.log` — those three, the existing `logger.warning`/`logger.debug` calls, and the HTTP
   access log — gets the same local ISO timestamp, seconds precision.
2. **`_HostServer.handle_error()`** (overriding `socketserver.BaseServer.handle_error`) catches
   `ConnectionAbortedError` / `ConnectionResetError` / `BrokenPipeError` and logs one `logger.info`
   line naming the client address instead of letting the default handler print a full traceback to
   stderr. Any other exception still falls through to `super().handle_error()` — a real bug keeps
   its traceback.
3. **`incremental_indexer._format_summary_progress()`** renders
   `[summarize {done}/{total} · {HH:MM:SS} · {rate}/min last slice · ETA ~{HH:MM}]`. The rate is
   `slice_count / slice_seconds`, timed around the one `summarize_batch` call that just returned —
   not an average since the run started — so it tracks the longest-first climb in throughput. `now`
   and the elapsed slice time are parameters, not `datetime.now()` / `time.monotonic()` calls inside
   the function, so the ETA arithmetic is tested without a real or mocked clock.
4. **Phase banners get the same `[HH:MM:SS]` prefix**, via a shared `_hms()` helper: `Pass 1 of 2`,
   `Pass 2 of 2`, `Saving indexes...`, and the final `Done …` line.
5. **The watchdog's start/complete lines** in `MCPServer._ReindexDebouncer._fire` get an inline
   `[HH:MM:SS]` prefix. Only those two `print()` calls change; the failure-path print and everything
   else in `_fire` is untouched, since another branch is editing `MCPServer.py` more broadly.

## Consequences

**Better:**
- A host.log block can be dated without cross-referencing `host.json` or trusting Windows's mtime
  on a file the process still holds open.
- A run's ETA is readable at a glance mid-run, and tracks the actual longest-first speedup instead
  of lagging behind it.
- A dropped status poll is one line, not a traceback, so a real host error is easier to spot in the
  log.

**Worse:**
- `_format_summary_progress` takes a `now` and an elapsed-seconds argument instead of reading the
  clock itself, which is slightly more ceremony at the one call site for the sake of testability
  elsewhere.
- The progress line still holds a non-ASCII separator (`·`), so a caller that reads `host.log` (not
  build stdout — that is unaffected by this ADR's stream question, only the character choice) on a
  strict-ASCII pipe would still need the ADR-036 stream fix. Nothing new: the line one above it
  (`━━ Pass 1 of 2 ━━`) already required it.

**Neutral:** the host's HTTP access log (`log_message`) already routed through `logger.debug`; it
picks up the new format for free.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Average-since-start rate for the ETA | Measured wrong on this exact repo (B-049): longest-first ordering means the average lags well behind the true remaining time until the run is nearly done. |
| ASCII-only progress line (`\|` instead of `·`) | Inconsistent with the banner one line above it in the same file, which ADR-036 already committed to non-ASCII plus a stream fix rather than stripping characters. |
| Swallow every `OSError` in `handle_error`, not just the three connection variants | Too broad — a real `OSError` (disk full writing a response, say) would also be silenced to one line. |
| Read the clock inside `_format_summary_progress` | Makes the ETA math untestable without monkeypatching `datetime.now`/`time.monotonic`; passing them in is a pure function. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [x] `model_host.py`: `_configure_logging()`, three `print()` → `logger.info()`, `_HostServer.handle_error()`
- [x] `incremental_indexer.py`: `_hms()`, `_format_summary_progress()`, call site with a
  `time.monotonic()`-timed slice, four banner prefixes
- [x] `MCPServer.py`: two `print()` lines in `_ReindexDebouncer._fire` get a `[HH:MM:SS]` prefix
- [x] Tests (`tests/test_progress_timestamps.py`, 12): progress-line formatting and the
  last-slice-rate ETA with an injected `now`/elapsed-seconds pair (no real clock), the Pass 1
  banner's stamp, `_configure_logging`'s timestamp format on a swapped-in stream, `handle_error`
  logging one line for `ConnectionAbortedError`/`ConnectionResetError`/`BrokenPipeError` and
  falling through to a real traceback for anything else, and the watchdog's two stamped lines with
  `run_incremental` faked out.
- [x] Suite: 569 passed, 1 skipped (was 558 passed, 1 skipped before this branch — net +12 tests,
  no regressions). CPU only, `CUDA_VISIBLE_DEVICES=-1`, no model host process started.

**Notes:**
<!-- 2026-09-28: branch cut from master (cacaebb). -->
