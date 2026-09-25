"""model_host.py — one local process owns the GPU models (ADR-028).

Every project process (MCP server, watchdog, code-indexer) sends its embeds and
summaries here instead of loading its own copy of each model. The host keeps at
most one model on the card at a time:

- Embeds are small and urgent. They always go first, and the embedder stays
  loaded for ``embed_idle_s`` after the last one because searches come in bursts.
- Summaries are large and can wait. The summarizer loads once the embedder is
  gone, drains every project's queue in one longest-first run through ADR-027's
  batching loop, and stops at the next batch boundary when an embed arrives
  (``should_yield``). It stays loaded for ``_SUMMARY_LINGER_S`` after its queue
  empties, because the indexer sends pass 1 in slices, and unloading between
  slices would reload the model every slice.

The host never opens a project's index. It turns text into vectors and summaries,
and each project process still writes its own FAISS and SQLite files.

Three layers, so the policy can be tested without torch or a socket:

    HostScheduler   the queues and the one-model-at-a-time policy (pure Python)
    TorchBackend    the real models, loaded and unloaded in this process
    serve()         HTTP on 127.0.0.1, a per-user token, a lock file, host.json

Run it with ``python src/model_host.py``. Clients start it on demand
(model_client.ensure_host), so it normally does not need starting by hand.
"""
from __future__ import annotations

import base64
import heapq
import hmac
import itertools
import json
import logging
import os
import secrets
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol, Sequence

logger = logging.getLogger(__name__)

# PROVISIONAL: how long the summarizer stays loaded after its queue empties. It has
# to cover the gap between two of the indexer's pass-1 slices (a DB write and an
# HTTP round trip), and be well under a reload (~14 s measured by eye on 2026-09-24).
_SUMMARY_LINGER_S = 15.0
_IDLE_POLL_S = 0.5

EMBEDDER = "embedder"
SUMMARIZER = "summarizer"
KINDS = ("query", "index")     # queries go before save-time and indexing embeds


# ─────────────────────────────────────────────────────────────────────────────
# Files: where a client finds the host, and the token it needs.
# ─────────────────────────────────────────────────────────────────────────────

def host_dir() -> str:
    """Per-user directory for host.json, the token, the lock and the log.

    ``CODE_INDEXER_HOST_DIR`` overrides it, which is what the tests use. The
    default sits under %LOCALAPPDATA%, whose ACL already limits it to this user.
    """
    forced = os.environ.get("CODE_INDEXER_HOST_DIR")
    if forced:
        return forced
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "code-indexer", "model-host")


def host_file(name: str) -> str:
    return os.path.join(host_dir(), name)


def load_or_create_token() -> str:
    """The shared secret a request must carry. Created once, readable by this user only.

    Any local process can open a loopback port, so the port alone is not access
    control. Never log or print the value.
    """
    path = host_file("token")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_urlsafe(32))
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


# ─────────────────────────────────────────────────────────────────────────────
# Backend: the models. The scheduler calls these from its one thread only.
# ─────────────────────────────────────────────────────────────────────────────

class Backend(Protocol):
    def load(self, model: str) -> None: ...
    def unload(self, model: str) -> None: ...
    def embed(self, texts: list[str], kind: str): ...          # -> float32 array (n, dim)
    def summarize(self, codes: list[str],
                  should_yield: Callable[[], bool]) -> list[str | None]: ...
    def describe(self) -> dict: ...


class TorchBackend:
    """The real models, loaded into the host process through the existing loaders.

    The embedder is core's singleton and the summarizer is the ADR-027 worker's
    module state, so both behave exactly as they do in the indexer today. The
    embedder loads in whatever dtype core loads it in; see the ADR-028 log for
    why bf16 has to land before the host is turned on for real.
    """

    def __init__(self) -> None:
        import config
        from device import resolve_device
        self.device = resolve_device()
        self.summarizer_model_id = config.summarizer_model_id()
        self.max_batch_size = config.summarizer_max_batch_size()
        self.reserve_mb = config.summarizer_vram_reserve_mb()
        self.token_budget = config.summarizer_batch_token_budget()
        import summarizer as sm
        self.stats = sm.BatchStats()

    def describe(self) -> dict:
        import core
        return {"device": self.device, "embed_model_id": core.embed_model_id(),
                "embed_dimension": core.embed_dimension(),
                "summarizer_model_id": self.summarizer_model_id,
                "summary_stats": self.stats.line()}

    def load(self, model: str) -> None:
        if model == EMBEDDER:
            import core
            core._get_embed_model()
        else:
            import summarizer as sm
            sm._worker_init(self.summarizer_model_id, self.device, "float16")

    def unload(self, model: str) -> None:
        import gc
        if model == EMBEDDER:
            import core
            core._embed_model = None
        else:
            import summarizer as sm
            sm._w_model = sm._w_tok = None
        gc.collect()
        if self.device == "cuda":
            import torch
            torch.cuda.empty_cache()
            # The summarizer caps the process below the card (ADR-027 §2). The cap
            # was sized with the summarizer resident, so lift it for the next model.
            torch.cuda.set_per_process_memory_fraction(1.0)

    def embed(self, texts: list[str], kind: str):
        import numpy as np
        import core
        if kind == "query":
            return np.stack([core.embed(t) for t in texts]).astype(np.float32)
        return core.embed_batch(texts)

    def summarize(self, codes, should_yield):
        import summarizer as sm
        results, stats = sm._worker_summarize(
            codes, sm._MAX_NEW_TOKENS, self.max_batch_size, self.reserve_mb,
            self.token_budget, should_yield=should_yield)
        self.stats.merge(stats)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler: the queues and the swap policy.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _EmbedRequest:
    texts: list[str]
    kind: str
    project: str
    future: Future = field(default_factory=Future)


@dataclass
class _SummaryJob:
    codes: list[str]
    project: str
    results: list[str | None]
    future: Future = field(default_factory=Future)


class HostScheduler:
    """Decides, one step at a time, which model is on the card and what it runs.

    Clients call submit_embed / submit_summary from any thread and wait on the
    returned Future. Only the thread running ``run()`` (or a test calling
    ``step()``) touches the backend, so two models can never be loaded at once.
    """

    def __init__(self, backend: Backend, *, embed_idle_s: float, idle_exit_s: float,
                 summary_linger_s: float = _SUMMARY_LINGER_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._backend = backend
        self._embed_idle_s = embed_idle_s
        self._idle_exit_s = idle_exit_s
        self._summary_linger_s = summary_linger_s
        self._clock = clock
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self._embeds: list[tuple[int, int, _EmbedRequest]] = []    # heap
        self._jobs: list[_SummaryJob] = []
        self._stopping = False
        self.loaded: str | None = None
        self._last_embed = float("-inf")
        self._last_summary = float("-inf")
        self._last_work = clock()
        self.counters = {"embeds": 0, "embed_texts": 0, "summaries": 0,
                         "yields": 0, "loads": 0, "unloads": 0}
        self.load_seconds: dict[str, list[float]] = {EMBEDDER: [], SUMMARIZER: []}

    # ── client side ─────────────────────────────────────────────────────────
    def submit_embed(self, texts: Sequence[str], kind: str = "index", project: str = "") -> Future:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        req = _EmbedRequest(list(texts), kind, project)
        with self._cv:
            heapq.heappush(self._embeds, (KINDS.index(kind), next(self._seq), req))
            self._cv.notify_all()
        return req.future

    def submit_summary(self, codes: Sequence[str], project: str = "") -> Future:
        job = _SummaryJob(list(codes), project, [None] * len(codes))
        if not job.codes:
            job.future.set_result([])
            return job.future
        with self._cv:
            self._jobs.append(job)
            self._cv.notify_all()
        return job.future

    def embed_waiting(self) -> bool:
        with self._cv:
            return bool(self._embeds)

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            by_project: dict[str, dict] = {}
            for _p, _s, req in self._embeds:
                by_project.setdefault(req.project, {"embeds": 0, "summaries": 0})["embeds"] += len(req.texts)
            for job in self._jobs:
                pending = sum(1 for r in job.results if r is None)
                by_project.setdefault(job.project, {"embeds": 0, "summaries": 0})["summaries"] += pending
            return {"loaded": self.loaded, "queued": by_project,
                    "counters": dict(self.counters),
                    "load_seconds": {k: v[-5:] for k, v in self.load_seconds.items()}}

    # ── scheduler side ──────────────────────────────────────────────────────
    def _ensure(self, model: str) -> None:
        if self.loaded == model:
            return
        if self.loaded is not None:
            self._backend.unload(self.loaded)
            self.counters["unloads"] += 1
            self.loaded = None
        started = time.monotonic()
        self._backend.load(model)
        self.load_seconds[model].append(round(time.monotonic() - started, 2))
        self.counters["loads"] += 1
        self.loaded = model

    def _unload(self) -> None:
        if self.loaded is not None:
            self._backend.unload(self.loaded)
            self.counters["unloads"] += 1
            self.loaded = None

    def step(self) -> str:
        """Make one decision and carry it out. Returns what it did, for tests and logs."""
        now = self._clock()
        with self._cv:
            if self._stopping:
                return "stop"
            batch: list[_EmbedRequest] = []
            if self._embeds:
                kind = self._embeds[0][2].kind
                while self._embeds and self._embeds[0][2].kind == kind:
                    batch.append(heapq.heappop(self._embeds)[2])
            jobs = list(self._jobs)

        if batch:
            self._run_embeds(batch)
            return "embed"

        if jobs:
            if self.loaded == EMBEDDER and now - self._last_embed < self._embed_idle_s:
                return "hold"            # a search burst may not be over; summaries wait
            self._run_summaries(jobs)
            return "summarize"

        if self.loaded == EMBEDDER and now - self._last_embed >= self._embed_idle_s:
            self._unload()
            return "unload"
        if self.loaded == SUMMARIZER and now - self._last_summary >= self._summary_linger_s:
            self._unload()
            return "unload"
        if self.loaded is None and now - self._last_work >= self._idle_exit_s:
            return "exit"
        return "idle"

    def _run_embeds(self, batch: list[_EmbedRequest]) -> None:
        texts = [t for req in batch for t in req.texts]
        try:
            self._ensure(EMBEDDER)
            vectors = self._backend.embed(texts, batch[0].kind)
        except Exception as exc:  # noqa: BLE001 — every waiting client hears about it
            logger.warning("model host embed failed: %s", exc)
            for req in batch:
                req.future.set_exception(exc)
        else:
            pos = 0
            for req in batch:
                req.future.set_result(vectors[pos:pos + len(req.texts)])
                pos += len(req.texts)
        self.counters["embeds"] += len(batch)
        self.counters["embed_texts"] += len(texts)
        self._last_embed = self._last_work = self._clock()

    def _run_summaries(self, jobs: list[_SummaryJob]) -> None:
        # One run over every project's pending texts. A text repeated within or
        # across projects is summarized once.
        distinct: dict[str, None] = {}
        for job in jobs:
            for code, done in zip(job.codes, job.results):
                if done is None:
                    distinct.setdefault(code)
        codes = list(distinct)
        try:
            self._ensure(SUMMARIZER)
            out = self._backend.summarize(codes, self.embed_waiting)
        except Exception as exc:  # noqa: BLE001 — same contract as the summarizer: "" on failure
            logger.warning("model host summarize failed: %s", exc)
            out = [""] * len(codes)
        got = {c: s for c, s in zip(codes, out) if s is not None}
        if len(got) < len(codes):
            self.counters["yields"] += 1
        self.counters["summaries"] += len(got)

        with self._cv:
            for job in jobs:
                for i, code in enumerate(job.codes):
                    if job.results[i] is None and code in got:
                        job.results[i] = got[code]
                if all(r is not None for r in job.results):
                    self._jobs.remove(job)
                    job.future.set_result(list(job.results))
        self._last_summary = self._last_work = self._clock()

    def run(self) -> None:
        """Loop until idle for idle_exit_s or stopped. Fails every waiting client on the way out."""
        try:
            while True:
                did = self.step()
                if did in ("exit", "stop"):
                    break
                if did in ("idle", "hold"):
                    with self._cv:
                        self._cv.wait(timeout=_IDLE_POLL_S)
        finally:
            self._unload()
            self._fail_pending(RuntimeError("model host stopped"))

    def _fail_pending(self, exc: Exception) -> None:
        with self._cv:
            for _p, _s, req in self._embeds:
                req.future.set_exception(exc)
            for job in self._jobs:
                job.future.set_exception(exc)
            self._embeds.clear()
            self._jobs.clear()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: 127.0.0.1 only, token required on every request.
# ─────────────────────────────────────────────────────────────────────────────

def encode_vectors(vectors) -> dict:
    import numpy as np
    arr = np.ascontiguousarray(vectors, dtype=np.float32)
    return {"shape": list(arr.shape), "f32_b64": base64.b64encode(arr.tobytes()).decode("ascii")}


def decode_vectors(payload: dict):
    import numpy as np
    arr = np.frombuffer(base64.b64decode(payload["f32_b64"]), dtype=np.float32)
    return arr.reshape(payload["shape"]).copy()


class _HostServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, scheduler: HostScheduler, token: str, describe: Callable[[], dict]):
        super().__init__(addr, _Handler)
        self.scheduler = scheduler
        self.token = token
        self.describe = describe


class _Handler(BaseHTTPRequestHandler):
    server: _HostServer

    def log_message(self, fmt, *args):  # noqa: D401 — quiet; the host has its own log
        logger.debug("model host http: " + fmt, *args)

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "")
        if hmac.compare_digest(got.encode("utf-8"), f"Bearer {self.server.token}".encode("utf-8")):
            return True
        self._send(401, {"error": "missing or wrong token"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/v1/status":
            self._send(200, {"pid": os.getpid(), **self.server.describe(),
                             **self.server.scheduler.status()})
        else:
            self._send(404, {"error": f"no such path {self.path}"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        sched = self.server.scheduler
        try:
            body = self._body()
            if self.path == "/v1/embed":
                fut = sched.submit_embed(body["texts"], body.get("kind", "index"), body.get("project", ""))
                self._send(200, encode_vectors(fut.result()))
            elif self.path == "/v1/summarize":
                fut = sched.submit_summary(body["codes"], body.get("project", ""))
                self._send(200, {"summaries": fut.result()})
            elif self.path == "/v1/shutdown":
                sched.stop()
                self._send(200, {"stopping": True})
            else:
                self._send(404, {"error": f"no such path {self.path}"})
        except (KeyError, ValueError, TypeError) as exc:
            self._send(400, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def start_server(scheduler: HostScheduler, token: str,
                 describe: Callable[[], dict] = dict) -> _HostServer:
    """Bind 127.0.0.1 on a free port and serve in a background thread."""
    server = _HostServer(("127.0.0.1", 0), scheduler, token, describe)
    threading.Thread(target=server.serve_forever, name="model-host-http", daemon=True).start()
    return server


# ─────────────────────────────────────────────────────────────────────────────
# Process: one host per user, found through host.json.
# ─────────────────────────────────────────────────────────────────────────────

def _acquire_lock():
    """Hold host.lock for the life of the process, or return None if another host has it.

    The OS drops the lock when the process dies, so a crashed host leaves no stale lock.
    """
    path = host_file("host.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _write_host_json(port: int) -> None:
    path = host_file("host.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "pid": os.getpid(), "started_at": time.time()}, fh)
    os.replace(tmp, path)


def serve(backend: Backend | None = None) -> int:
    """Run the host until it has been idle for idle_exit_s, or a client asks it to stop."""
    import config
    lock = _acquire_lock()
    if lock is None:
        print("[model-host] another host holds the lock; exiting", flush=True)
        return 0
    backend = backend or TorchBackend()
    scheduler = HostScheduler(backend, embed_idle_s=config.model_host_embed_idle_s(),
                              idle_exit_s=config.model_host_idle_exit_s())
    server = start_server(scheduler, load_or_create_token(), backend.describe)
    port = server.server_address[1]
    _write_host_json(port)
    print(f"[model-host] pid {os.getpid()} listening on 127.0.0.1:{port}", flush=True)
    try:
        scheduler.run()
    finally:
        server.shutdown()
        try:
            os.remove(host_file("host.json"))
        except OSError:
            pass
        lock.close()
        print("[model-host] stopped", flush=True)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(serve())
