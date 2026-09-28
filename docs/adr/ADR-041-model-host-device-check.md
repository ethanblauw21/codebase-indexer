# ADR-041: The Model Host Records Its Device, and a CUDA Client Corrects a CPU One

**Status:** accepted
**Date:** 2026-09-28
**Branch:** `feature/b051-host-device-check`
**Reviewer:** @edb
**Backlog:** [B-051](../backlog.md#b-051) — the shared model host runs on whatever interpreter
launched it, so a CPU-only env can put every project's models on the CPU
**Depends on:** [ADR-028](./ADR-028-central-model-host.md) — the host process, `host.json`, the
lock file and the client's spawn/fallback path this builds on.
**Depended on by:** none yet.

## Context

ADR-028's host is started by whichever client asks first, with **that client's Python**
(`model_client.py:89`, now `_spawn`). Nothing about the host depends on which project started
it except the device its interpreter resolves.

**Happened on 2026-09-28 (InventoryApp-V2 go-live).** InventoryApp's old `repo-indexer`
registration runs under VectorEnv (Python 3.12, CPU-only `torch 2.11.0+cpu`). It started the host
at 09:31 (pid 18656), which logged `[Summarizer] Loading ... (device=cpu ...)`. It died when
duplicate servers were killed for an unrelated reason, so the 09:38 build started a fresh host on
the GPU. That was luck, not a fix: SOPCentral and GanttWebApp still register `repo-indexer` under
the same CPU-only VectorEnv. If either starts the host first, every project sharing it runs on the
CPU — far slower, unmeasured on this machine — until the host idles out (`idle_exit_s`, 30 min
default). The build still prints "Done successfully"; nothing today would tell anyone.

The host has no idea what device it is running on beyond `TorchBackend.describe()["device"]`,
which only a client that already suspects trouble would think to fetch and compare by hand.
`host.json` (`_write_host_json`) records `port`, `pid` and `started_at` — enough to reach the
host, nothing about whether it is worth reaching.

## Decision

### 1. The host records its resolved device

`model_host._write_host_json` takes an optional `device` and writes it into `host.json` when
given. `serve()` passes `backend.describe().get("device")` — for `TorchBackend` this is
`device.resolve_device()`, already computed once at construction. The startup log line
(`[model-host] pid … listening on … device=<device or "unknown">`) carries the same value, so it
shows up in `host.log` without anyone querying `/v1/status`.

An old host that predates this change, or a backend that doesn't report a device, simply omits
the key — `host.json` stays valid, and a client sees "no field" rather than a wrong value.

### 2. The client compares the host's device with its own before trusting it

`model_client.ensure_host()` reads `host.json`'s `device` field (`_host_device()`) and compares it
with `device.resolve_device()` for **this** process, after confirming the host answers and before
the existing model-id check (§2 of ADR-028).

- **No `device` field (old host):** unknown, not CPU. Warn once (`_say_once`, stdout — this is
  informational, not an emergency) and use the host as-is. Never inferred as CPU, because an old
  host is just as likely to be on CUDA.
- **Host already on CUDA, or this client is not CUDA-capable:** nothing to correct. A CPU-only
  client finding a CUDA host is the good case ADR-028 was built for — it just uses it.
- **This client is CUDA-capable and the host is on CPU:** this is the bug. Warn loudly, to
  **stderr** (`_check_device`), naming both devices, then:
  - **the host is idle** (`_is_idle`: nothing loaded, nothing queued in `/v1/status`) — stop it
    (`POST /v1/shutdown`), wait for `host.json` to disappear, and start a fresh one from *this*
    interpreter (`_restart_host`, reusing `_spawn`). The new host resolves its own device the same
    way `serve()` always has, so a CUDA client restarting it gets a CUDA host.
  - **the host is busy** — restarting would cut off whatever it is doing for another project.
    Raise `HostUnavailable` instead, which the existing per-call `try/except` in `embed`,
    `embed_batch` and `HostSummarizer` turns into the same in-process fallback ADR-028 already
    uses when the host can't be reached at all. Nothing new to build there.

`_wait_for_status`, factored out of `ensure_host`'s existing spawn-and-poll loop, is reused by
both the original start path and the new restart path — one poll loop, not two.

### 3. What stays out of scope

- No new `indexer.toml` knob. The check runs whenever `[model_host].enabled` does; there is
  nothing to tune yet.
- No change to the reranker or to which project's config a host serves (ADR-028 §2 already owns
  that check).
- The ops half of B-051 — moving SOPCentral's and GanttWebApp's registrations off VectorEnv — is
  explicitly not this ADR's job; it is process, not code.
- The token is never logged, printed, or included in any of this — same rule as ADR-028.

## Consequences

**Better:**
- A CPU-only client can no longer silently strand every project on a CPU host. It gets fixed
  automatically when nothing else is using the host, and refuses to make things worse when
  something is.
- The device shows up in `host.log` on startup, so a human scanning the log sees it without
  querying `/v1/status`.

**Worse:**
- A CUDA client that finds a busy CPU host now falls back to in-process models for that call
  instead of using the (wrong-but-working) host. That is the intended trade — correctness over
  convenience — but it means a transient CPU host under load costs one call's speed twice: once
  for running on the CPU, once for the client giving up on it.
- One more startup/shutdown cycle is possible in the host's lifetime (the restart), which is a few
  seconds of the load times already measured in ADR-028's gaps table.

**Neutral:** an old host with no `device` field behaves exactly as before this ADR — nothing
changes for a fleet that hasn't restarted its host yet.

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Always restart on any mismatch, busy or not | Cuts off another project's in-flight work for a fix that a later idle window would do for free. |
| Never restart, always just warn and fall back | Leaves the fleet on the CPU host until someone notices the idle timeout, which is the exact silent-slowdown this ADR exists to stop. |
| Read the device from `/v1/status` (live) instead of `host.json` | `host.json` is already what the client opens first to find the host at all (`_endpoint`); adding one field to a file already being read is smaller than a second network round trip's worth of trust. |
| A config knob to disable the check | Nothing to tune yet — the check is cheap (one file read) and has no false-positive mode to escape from. Add a knob if one shows up. |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in
> the moment.

- [x] `model_host._write_host_json(port, device=None)`; `serve()` passes
  `backend.describe().get("device")` and logs it on the startup line
- [x] `model_client._host_device()`, `_is_idle()`, `_check_device()`, `_restart_host()`,
  `_wait_for_status()` (factored out of `ensure_host`'s existing poll loop)
- [x] `ensure_host()` calls `_check_device` before `_check_models`
- [x] `_say_once` takes a `stream` so the device-mismatch warning can go to stderr while the
  existing fallback messages stay on stdout
- [x] Tests (`tests/test_model_host_device_check.py`): mocked device resolution and a fake
  `host.json`, no real host process or model load
- [ ] Ops: move SOPCentral's and GanttWebApp's `repo-indexer` registrations off VectorEnv
  (B-051 part 3 — tracked in the backlog item, not here)

**Notes:**

- 2026-09-28: scoped to code parts 1 and 2 of B-051 only; the ops migration is explicitly a
  separate, non-code follow-up.
