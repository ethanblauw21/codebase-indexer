# ADR-038: One Writer and One Watchdog per Index, Across Processes

**Status:** accepted
**Date:** 2026-09-28
**Branch:** `feature/b033-b053-index-writer-lock`
**Reviewer:** @edb
**Backlog:** [B-033](../backlog.md#b-033) (two servers write one index with no lock) · [B-053](../backlog.md#b-053) (no server instructions; a watchdog in every session)
**Depends on:** [ADR-036](./ADR-036-one-reindex-at-a-time.md) (one run at a time inside a process)
**Depended on by:** none yet. Related: [ADR-028](./ADR-028-central-model-host.md), whose `host.lock` is the pattern reused here; [B-052](../backlog.md#b-052), the worktree setup that makes N sessions share one index.

## Context

ADR-036 serializes runs inside one MCP server with a `threading.Lock`. Nothing coordinates
processes, and go-live day (2026-09-28) hit that three ways:

1. **Two writers.** InventoryApp-V2 had two indexer registrations. A session opened at 09:28 started
   both servers. With no index yet, a watchdog event in each started `run_incremental()`, which on an
   empty index is a full build. Two full builds wrote one `.code-index/` until they were killed.
   #70 made `save_all` atomic, so a reader no longer sees a torn `.faiss`, but two writers still
   overwrite each other's saves (the ghost-vector class ADR-031 removed).
2. **A first build nobody sees.** The watchdog has no "no index yet" guard. A first save in a fresh
   project starts an hour of GPU work inside a server, with its progress on a stderr nobody reads.
3. **N watchdogs on one index.** B-052's guidance puts the index in a clean `origin/main` worktree
   that every InventoryApp session serves from. Each session's server runs its own watchdog on that
   folder. They are idle until the folder moves to a newer `main`; then every open session starts the
   same rebuild at once. @edb's requirement: the indexer must never be driven from Claude-made
   worktrees doing separate work at the same time. Prose alone is known not to hold, so it is enforced
   in code.

Also, the server declared no MCP `instructions`, so no agent was told what the index reflects.

## Decision

A new leaf module, `src/index_lock.py`, holds two OS file locks in the index directory. Like
ADR-028's `host.lock` (`msvcrt.locking` / `flock`), the kernel drops them when their process dies, so a
killed build or a closed session never leaves a stale lock. Each lock file records the holder's pid,
purpose and start time, for messages only.

1. **`write.lock`: one writer per index.** `run_incremental` holds it for the whole run, so the CLI,
   the watchdog and the `reindex` tool are all covered. A second writer does not wait, since a run
   can take an hour:
   - **CLI** (`code-indexer`): prints who holds it and exits 2.
   - **`reindex` tool**: raises, so the client gets `isError`, naming the holder.
   - **Watchdog**: logs the holder and retries in 60 s (`_BUSY_RETRY_S`).

   Re-acquiring inside the holding process is a no-op, so `reindex` holds the lock across its
   full-rebuild wipe as well as the run. ADR-036's `threading.Lock` still orders threads within a
   process.
2. **The watchdog only maintains a finished index.** `_fire` skips, with a log line, when
   `index_meta` has no `last_verified_at` (ADR-025 §4 writes it at the end of every completed run).
   First builds come from a terminal. A killed first build leaves no marker, so it is also skipped
   until a terminal build finishes.
3. **`watch.lock`: one watchdog per index.** The server that takes it runs the watchdog for its
   lifetime. The others log "Standby", serve the read tools, and retry every 60 s
   (`_WATCH_RETRY_S`); one of them takes over when the watching server exits.
4. **Linked worktrees don't write.** When the server's folder is a linked git worktree
   (`git rev-parse --git-dir` ≠ `--git-common-dir`), there is no watchdog, `reindex` raises, and
   `code-indexer` exits 2. A worktree that exists only to hold the index opts in with
   `[indexer] allow_linked_worktree = true` in its `indexer.toml` (default `false`, drift-tested).
   `code-indexer --allow-worktree` overrides it once.
5. **Server `instructions`.** Built at startup from `index_meta`. They name the folder and commit
   the index reflects, tell the agent to `Read` files its branch changed, and say never to call
   `reindex` from a parallel-work worktree. About 600 characters.

## Consequences

- Two servers, or a server plus a terminal build, can no longer write one index at once. The
  second one learns who holds it instead of silently racing.
- A first build never starts inside a server. A fresh project's sessions do nothing until
  `code-indexer` has run once in a terminal. That was already the guidance; now it is enforced.
- With N sessions on one index, one watchdog runs a folder move's rebuild once, not N times.
- A non-watching server still answers `reindex` (serialized by `write.lock`). An agent that calls
  it right after the watchdog's run gets a cheap no-op incremental.
- **Opt-in needed on InventoryApp-index:** it is a linked worktree, so it needs
  `allow_linked_worktree = true` in its `indexer.toml`. Without it, the index there is never
  updated.
- `.code-index/` gets two small lock files. They are never deleted; an unheld lock file is harmless.
- **Not solved here:** a server that started its watchdog keeps it even if another server later
  holds `write.lock`. It retries, which is the intent. B-033's "reload when another process
  saved" (fix 3) is also not done: a standby server keeps serving the vectors it loaded until its
  next reload. Its reads stay consistent, since saves are atomic (#70), but they are stale until a
  restart. Filed as the remaining part of B-033.

## Alternatives Considered

- **An `O_EXCL` lock file with a pid, and stale recovery by checking the pid.** This was B-033's
  first sketch. On Windows `os.kill(pid, 0)` is not a liveness check, and pid reuse makes recovery
  guessy. OS locks need no recovery.
- **Waiting for the lock instead of refusing.** A second `reindex` call, or a CLI run, would block
  for up to an hour with no progress shown. Failing at once with the holder's pid is more useful.
- **A non-watching server's `reindex` refuses outright** (B-053's sketch). Not needed once
  `write.lock` serializes writers, and it would surprise a user whose only open session is a
  standby that just became the only one.

## Implementation Log

- 2026-09-28: `src/index_lock.py`; `run_incremental` holds `write.lock`; `reindex`, the watchdog
  and the CLI handle `IndexBusy`; the watchdog skips an unbuilt index; `watch.lock` with standby and
  takeover; linked-worktree refusal plus `[indexer] allow_linked_worktree`; server `instructions`.
  Tests: `tests/test_index_writer_lock.py`. A child process really holds each lock, including a
  killed holder leaving no stale lock, a real `git worktree add`, and standby then takeover.
  `test_reindex_serial.py` now runs in a scratch directory. No GPU.
