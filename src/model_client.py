"""model_client.py — the indexer's side of the model host (ADR-028).

Drop-ins for the calls the indexer makes today:

    embed(text)            core.embed            query path (hybrid_retriever)
    embed_batch(texts)     core.embed_batch      indexing path (incremental_indexer)
    make_summarizer()      IsolatedChunkSummarizer()

With ``[model_host].enabled = false`` (the default) each one is exactly today's
in-process call. With it on, the call goes to the host, which is started if it is
not running. If the host cannot be reached or started, or serves a different
model than this project is configured for, the call falls back to in-process
loading and says so once in the log. It never fails an index or a search.

The reranker stays in-process for now; it is off by default (see ADR-028's log).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import config
import model_host

logger = logging.getLogger(__name__)

# After a failure, skip the host for this long before trying it again, so a dead
# host costs one timeout rather than one per file.
_RETRY_AFTER_S = 60.0
_STATUS_TIMEOUT_S = 2.0
_EMBED_TIMEOUT_S = 600.0          # covers a summary window's batch finishing first
_SUMMARY_TIMEOUT_PER_TEXT_S = 30.0

_skip_until = 0.0
_warned: set[str] = set()


class HostUnavailable(Exception):
    """The host could not be reached, started, or used for this project."""


def _say_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[model-client] {message}", flush=True)
        logger.warning(message)


# ── transport ───────────────────────────────────────────────────────────────

def _endpoint() -> tuple[str, str]:
    """(base url, token) from host.json and the token file, or HostUnavailable."""
    try:
        with open(model_host.host_file("host.json"), encoding="utf-8") as fh:
            port = int(json.load(fh)["port"])
        with open(model_host.host_file("token"), encoding="utf-8") as fh:
            token = fh.read().strip()
    except (OSError, ValueError, KeyError) as exc:
        raise HostUnavailable(f"no running host ({type(exc).__name__})") from exc
    return f"http://127.0.0.1:{port}", token


def _call(method: str, path: str, body: dict | None = None, timeout: float = _STATUS_TIMEOUT_S) -> dict:
    base, token = _endpoint()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise HostUnavailable(f"host answered {exc.code} on {path}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HostUnavailable(f"{type(exc).__name__} on {path}") from exc


def status() -> dict:
    return _call("GET", "/v1/status")


def _spawn() -> None:
    """Start a host detached from this process, logging to host.log."""
    os.makedirs(model_host.host_dir(), exist_ok=True)
    log = open(model_host.host_file("host.log"), "ab")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "close_fds": True, "cwd": os.getcwd()}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(model_host.__file__)], **kwargs)


def _check_models(info: dict) -> None:
    """Refuse a host that serves different models than this project is configured for.

    The host reads the config of whichever project started it. Vectors from another
    embedder would load into this project's index without error and be wrong.
    """
    import core
    want = {"embed_model_id": core.embed_model_id(),
            "embed_dimension": core.embed_dimension(),
            "summarizer_model_id": config.summarizer_model_id()}
    for key, value in want.items():
        if info.get(key) != value:
            raise HostUnavailable(f"host serves {key}={info.get(key)!r}, this project wants {value!r}")


def ensure_host() -> dict:
    """Return the running host's status, starting a host first if none answers."""
    global _skip_until
    if time.monotonic() < _skip_until:
        raise HostUnavailable("host failed recently; using in-process models")
    try:
        try:
            info = status()
        except HostUnavailable:
            _spawn()
            deadline = time.monotonic() + config.model_host_spawn_timeout_s()
            while True:
                time.sleep(0.25)
                try:
                    info = status()
                    break
                except HostUnavailable:
                    if time.monotonic() > deadline:
                        raise HostUnavailable("started a host but it never answered; see host.log")
        _check_models(info)
        return info
    except HostUnavailable:
        _skip_until = time.monotonic() + _RETRY_AFTER_S
        raise


def _project() -> str:
    return os.path.basename(os.getcwd())


# ── drop-ins ────────────────────────────────────────────────────────────────

def embed(text):
    """core.embed, through the host when [model_host].enabled."""
    if config.model_host_enabled():
        try:
            ensure_host()
            return model_host.decode_vectors(
                _call("POST", "/v1/embed", {"texts": [text], "kind": "query", "project": _project()},
                      timeout=_EMBED_TIMEOUT_S))[0]
        except HostUnavailable as exc:
            _say_once("embed", f"model host unavailable ({exc}); embedding in-process")
    import core
    return core.embed(text)


def embed_batch(texts: list[str], batch_size: int = 32):
    """core.embed_batch, through the host when [model_host].enabled."""
    if config.model_host_enabled() and texts:
        try:
            ensure_host()
            return model_host.decode_vectors(
                _call("POST", "/v1/embed", {"texts": list(texts), "kind": "index", "project": _project()},
                      timeout=_EMBED_TIMEOUT_S))
        except HostUnavailable as exc:
            _say_once("embed", f"model host unavailable ({exc}); embedding in-process")
    import core
    return core.embed_batch(texts, batch_size=batch_size)


class HostSummarizer:
    """IsolatedChunkSummarizer's interface, answered by the host.

    Falls back to a real IsolatedChunkSummarizer the first time the host fails,
    and stays on it for the rest of the run, so one index never mixes a
    half-finished host run with a second worker loading beside it.
    """

    def __init__(self) -> None:
        self._fallback = None
        self.summarized = 0
        self.empty = 0

    def summarize_batch(self, codes: list[str]) -> list[str]:
        if not codes:
            return []
        if self._fallback is None:
            try:
                ensure_host()
                out = _call("POST", "/v1/summarize", {"codes": list(codes), "project": _project()},
                            timeout=_EMBED_TIMEOUT_S + _SUMMARY_TIMEOUT_PER_TEXT_S * len(codes))["summaries"]
                self.summarized += sum(1 for s in out if s)
                self.empty += sum(1 for s in out if not s)
                return out
            except HostUnavailable as exc:
                _say_once("summarize", f"model host unavailable ({exc}); summarizing in-process")
                from summarizer import IsolatedChunkSummarizer
                self._fallback = IsolatedChunkSummarizer()
        return self._fallback.summarize_batch(codes)

    def stats_line(self) -> str:
        line = f"via model host: {self.summarized} summarized, {self.empty} empty"
        if self._fallback is not None:
            line += f" | in-process fallback: {self._fallback.stats_line()}"
        return line

    def shutdown(self) -> None:
        # The host unloads its own models. Only a fallback worker is ours to stop.
        if self._fallback is not None:
            self._fallback.shutdown()


def make_summarizer():
    """The summarizer the indexer should use: the host's, or today's worker."""
    if config.model_host_enabled():
        return HostSummarizer()
    from summarizer import IsolatedChunkSummarizer
    return IsolatedChunkSummarizer()
