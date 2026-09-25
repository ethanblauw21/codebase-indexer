"""
summarizer.py — Optional LLM-based chunk summarization for embedding augmentation.

At indexing time an instruct-tuned LLM generates a structured extraction for each
code chunk.  The extraction is appended (not prepended) to the chunk text before the
Jina embedding is computed.  Appending preserves the code's lexical tokens at the
front of the embedding context so exact-match recall is unaffected; the extraction
adds semantic signal for intent-based queries ("where is auth validated?") without
diluting keyword precision.

  Embedded text format:   "<original code>\\n\\n# Summary\\n<extraction>"

Prompt design — Extraction over Synthesis
------------------------------------------
The prompt asks the model to LIST explicit facts from the code (inputs, outputs,
side-effects, key operations) rather than to EXPLAIN or INFER business logic.
This "strangle the prompt" approach cuts hallucinations dramatically: a model
asked to extract what it can see makes far fewer errors than one asked to explain
what the code means. temperature=0 (greedy) ensures deterministic, non-creative
output.

Model size tradeoff
--------------------
1.5B (default) — Qwen2.5-Coder-1.5B-Instruct
    Fast enough to complete a 10 000-chunk initial index in ~20 minutes on CPU,
    ~3 minutes on a mid-range GPU.  Reliable for straightforward CRUD, event
    handlers, utility functions.  Misses subtle architectural nuances and complex
    cross-cutting logic.  Good default for developer laptops.

3B — Qwen2.5-Coder-3B-Instruct
    3×–5× slower than 1.5B on CPU.  Noticeably better on domain-specific
    terminology and multi-step logic.  Practical choice with a GPU.

7B — Qwen2.5-Coder-7B-Instruct
    The quality floor for reliable summarization across all tier granularities,
    especially tier-3 architectural chunks (4 000 tokens).  Initial index on CPU
    can take several hours — only recommended when CUDA is available.

The model downloads automatically from HuggingFace Hub on first use (~3 GB for 1.5B).
It is not an Ollama model and does not require Ollama to be running.

SQLite summary cache
---------------------
Summaries are cached in the `chunk_summaries` table keyed by MD5(chunk_text).
On every incremental run only new or modified chunks are sent to the LLM.
Unchanged code (even if the file was re-scanned) reuses the cached summary at
zero LLM cost, making repeated runs nearly instant regardless of model size.

CPU vs GPU
----------
With CUDA:  float16, fast batch inference.
Without:    float32 — much slower.  For CPU-only machines install llama-cpp-python
            and use a GGUF Q4_K_M build (~4 GB RAM for 7B, much faster CPU throughput).
"""
from __future__ import annotations

import atexit
import logging
import time
from dataclasses import asdict, dataclass, fields
from typing import Callable, Sequence

from config import (
    summarizer_batch_token_budget,
    summarizer_max_batch_size,
    summarizer_model_id,
    summarizer_vram_reserve_mb,
)
from device import resolve_device

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Adaptive batching (ADR-027) — pure Python, no torch, so it can be tested with
# fakes. The worker supplies the model call and the memory probe.
# ─────────────────────────────────────────────────────────────────────────────

_TOKEN_BUDGET     = 16_000  # prompt tokens per batch; [summarization].batch_token_budget
_GROW_AFTER       = 8       # clean batches in a row before the budget grows by one chunk
_STEP_BACK_TRIES  = 2       # OOMs in a row answered by one chunk fewer, before halving
_PAUSE_POLL_S     = 5.0     # how often a paused worker re-checks free memory
_PAUSE_TIMEOUT_S  = 120.0   # after this, stop waiting and carry on at batch size 1


@dataclass
class BatchStats:
    """What happened to the chunks a summarizer was given. Summed across groups."""
    summarized:     int = 0   # chunks that came back with a non-empty summary
    empty_output:   int = 0   # the model ran but produced nothing usable
    empty_oom:      int = 0   # did not fit even at batch size 1
    empty_error:    int = 0   # generation raised something other than OOM, or the worker died
    oom_backoffs:   int = 0   # times a batch was halved and retried
    pauses:         int = 0   # times the worker waited for free memory
    pause_timeouts: int = 0   # pauses that gave up and dropped to batch size 1
    largest_batch:  int = 0
    peak_mb:        int = 0   # peak memory the worker's allocator reserved
    end_budget:     int = 0   # token budget and clean-batch streak when the call ended;
    end_streak:     int = 0   # the worker's next call picks up from here

    def merge(self, other: "BatchStats | dict") -> None:
        other = other if isinstance(other, dict) else asdict(other)
        for f in fields(self):
            value = other.get(f.name, 0)
            if f.name in ("largest_batch", "peak_mb"):
                setattr(self, f.name, max(getattr(self, f.name), value))
            elif f.name in ("end_budget", "end_streak"):
                if other.get("end_budget"):     # latest wins; a failed group reports none
                    setattr(self, f.name, value)
            else:
                setattr(self, f.name, getattr(self, f.name) + value)

    @property
    def empty(self) -> int:
        return self.empty_output + self.empty_oom + self.empty_error

    def line(self) -> str:
        return (
            f"{self.summarized} summarized, {self.empty} empty "
            f"(oom {self.empty_oom}, error {self.empty_error}, blank {self.empty_output}) | "
            f"largest batch {self.largest_batch}, {self.oom_backoffs} OOM backoffs, "
            f"{self.pauses} pauses ({self.pause_timeouts} timed out), peak {self.peak_mb} MiB"
        )


def run_adaptive_batches(
    lengths:      Sequence[int],
    run_batch:    Callable[[list[int]], list[str]],
    *,
    is_oom:       Callable[[BaseException], bool],
    max_batch:    int,
    free_mb:      Callable[[], float] | None = None,
    reserve_mb:   float = 0,
    on_oom:       Callable[[], None] = lambda: None,
    start_budget: int = _TOKEN_BUDGET,
    start_streak: int = 0,
    grow_after:   int = _GROW_AFTER,
    step_back_tries: int = _STEP_BACK_TRIES,
    pause_poll_s: float = _PAUSE_POLL_S,
    pause_timeout_s: float = _PAUSE_TIMEOUT_S,
    sleep:        Callable[[float], None] = time.sleep,
    clock:        Callable[[], float] = time.monotonic,
) -> tuple[list[str], BatchStats]:
    """Summarize ``len(lengths)`` items in batches sized by a token budget.

    ``lengths`` are prompt lengths in tokens. ``run_batch`` gets a list of item
    indices and returns one string per index. Items go longest first, so the
    first item of a batch sets its padded length, and a batch holds
    ``budget // that length`` items, at most ``max_batch``. Memory grows with
    batch size times padded length, so one budget fits every length: long chunks
    get small batches and short chunks get large ones.

    On OOM the same items are retried one smaller, and after ``step_back_tries``
    OOMs in a row, at half the size. The budget drops to what was tried, so later
    batches do not walk back into the same wall. Nothing is dropped until a single
    item fails alone. After ``grow_after`` clean batches the budget grows by one
    item at the current length. Before each batch, if ``free_mb`` reports less than
    ``reserve_mb``, another process has taken the memory: wait for it to come
    back, and if it does not, carry on one item at a time.

    Returns results in the caller's original order, and what happened.
    """
    n = len(lengths)
    results = [""] * n
    stats = BatchStats()
    order = sorted(range(n), key=lambda i: lengths[i], reverse=True)
    max_batch = max(1, max_batch)
    budget = max(1, start_budget)
    streak = max(0, start_streak)
    ooms_in_row = 0
    pos = 0

    while pos < n:
        length = max(1, lengths[order[pos]])
        if free_mb is not None and free_mb() < reserve_mb:
            stats.pauses += 1
            started = clock()
            while free_mb() < reserve_mb and clock() - started < pause_timeout_s:
                sleep(pause_poll_s)
            if free_mb() < reserve_mb:
                stats.pause_timeouts += 1
                budget, streak = length, 0

        size = max(1, min(max_batch, budget // length))
        batch = order[pos:pos + size]
        try:
            out = run_batch(batch)
        except Exception as exc:  # noqa: BLE001 — sorted into OOM vs everything else below
            streak = 0
            if is_oom(exc):
                on_oom()
                stats.oom_backoffs += 1
                if len(batch) > 1:
                    ooms_in_row += 1
                    smaller = (len(batch) - 1 if ooms_in_row <= step_back_tries
                               else max(1, len(batch) // 2))
                    budget = smaller * length
                    continue                    # retry the same items, smaller
                stats.empty_oom += len(batch)
            else:
                logger.warning("summarizer batch failed: %s", exc)
                stats.empty_error += len(batch)
            ooms_in_row = 0
            pos += len(batch)
            continue

        ooms_in_row = 0
        for i, text in zip(batch, out):
            results[i] = text
        stats.summarized   += sum(1 for t in out if t)
        stats.empty_output += sum(1 for t in out if not t)
        stats.largest_batch = max(stats.largest_batch, len(batch))
        pos += len(batch)
        streak += 1
        if streak >= grow_after:
            streak = 0
            if size < max_batch:
                budget = max(budget, size * length) + length

    stats.end_budget, stats.end_streak = budget, streak
    return results, stats


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess worker — module-level so ProcessPoolExecutor can pickle them on
# Windows (spawn start method requires top-level callables).
# These functions run ONLY inside the worker process, never in the parent.
# ─────────────────────────────────────────────────────────────────────────────

_w_model  = None   # resident in the worker process after _worker_init runs
_w_tok    = None
_w_device = None
# ADR-027: the token budget and clean-batch streak a call ended at, or None before
# the first call. Each call is one group of at most _GROUP_SIZE chunks; starting
# every call over would throw away what earlier calls learned about the card.
_w_next_budget: int | None = None
_w_next_streak = 0


def _worker_init(model_id: str, device: str, dtype_str: str) -> None:
    """
    ProcessPoolExecutor initializer — called once when the worker process
    starts.  Loads the model into the worker so it stays resident for the
    lifetime of the worker (no per-batch reload cost).

    ADR-027: the model and tokenizer are loaded directly rather than through the
    text-generation pipeline, because batched decoding needs left padding and the
    pipeline does not make that visible. Right padding does not raise; it just
    produces worse summaries.
    """
    global _w_model, _w_tok, _w_device
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype = torch.float16 if dtype_str == "float16" else torch.float32
    print(
        f"  [Summarizer] Loading {model_id} "
        f"(device={device}, dtype={dtype}) — first run only ...",
        flush=True,
    )
    _w_tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    _w_tok.padding_side = "left"
    if _w_tok.pad_token_id is None:
        _w_tok.pad_token = _w_tok.eos_token
    _w_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        device_map=device,
        torch_dtype=dtype,
        trust_remote_code=True,
    )
    _w_model.eval()
    _w_device = device
    print("  [Summarizer] Ready.", flush=True)


def _messages(code: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": _USER_TEMPLATE.format(code=code[:_MAX_CODE_CHARS])},
    ]


def _clean(text: str) -> str:
    # Trim at the first blank line — prevents the model spilling into follow-on
    # commentary or code examples beyond the extraction.
    return text.strip().split("\n\n")[0].strip()


def _is_cuda_oom(exc: BaseException) -> bool:
    import torch
    return isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in str(exc).lower()


def _free_mb() -> float:
    import torch
    free, _total = torch.cuda.mem_get_info()
    return free / 2**20


def _apply_memory_cap(reserve_mb: int) -> None:
    """Cap this process at what it holds plus what is free, minus the reserve.

    On Windows the display driver pages GPU memory into system RAM instead of
    failing an allocation, so CUDA never reports out-of-memory and throughput
    drops ~50x with nothing in the log. Past this cap PyTorch raises
    OutOfMemoryError itself, which run_adaptive_batches can back off from.
    Recomputed per batch, because other processes grow and shrink.
    """
    import torch
    free, total = torch.cuda.mem_get_info()
    held = torch.cuda.memory_reserved()
    budget = max(held, held + free - reserve_mb * 2**20)
    torch.cuda.set_per_process_memory_fraction(min(1.0, budget / total))


def _generate(prompts: list[str], max_new_tokens: int) -> list[str]:
    import torch
    enc = _w_tok(prompts, return_tensors="pt", padding=True).to(_w_model.device)
    with torch.inference_mode():
        out = _w_model.generate(
            **enc,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=None,   # must be None when do_sample=False in transformers ≥ 4.40
            top_p=None,
            top_k=None,
            pad_token_id=_w_tok.pad_token_id,
        )
    new_tokens = out[:, enc["input_ids"].shape[1]:]
    return [_clean(t) for t in _w_tok.batch_decode(new_tokens, skip_special_tokens=True)]


def _worker_summarize(
    codes:          list[str],
    max_new_tokens: int,
    max_batch_size: int,
    reserve_mb:     int,
    token_budget:   int = _TOKEN_BUDGET,
) -> tuple[list[str], dict]:
    """Summarize one group of chunks inside the worker. Returns (summaries, stats).

    ``token_budget`` is where the first call starts; later calls carry on from
    the budget the previous call ended with.
    """
    global _w_next_budget, _w_next_streak
    if _w_model is None:
        return [""] * len(codes), asdict(BatchStats(empty_error=len(codes)))

    prompts = [
        _w_tok.apply_chat_template(_messages(c), tokenize=False, add_generation_prompt=True)
        for c in codes
    ]
    lengths = [len(ids) for ids in _w_tok(prompts)["input_ids"]]
    on_cuda = _w_device == "cuda"

    def run_batch(idx: list[int]) -> list[str]:
        if on_cuda:
            _apply_memory_cap(reserve_mb)
        return _generate([prompts[i] for i in idx], max_new_tokens)

    if on_cuda:
        import torch
        results, stats = run_adaptive_batches(
            lengths, run_batch,
            is_oom=_is_cuda_oom,
            max_batch=max_batch_size,
            free_mb=_free_mb,
            reserve_mb=reserve_mb,
            on_oom=torch.cuda.empty_cache,
            start_budget=_w_next_budget if _w_next_budget is not None else token_budget,
            start_streak=_w_next_streak,
        )
        _w_next_budget, _w_next_streak = stats.end_budget, stats.end_streak
        stats.peak_mb = int(torch.cuda.max_memory_reserved() / 2**20)
    else:
        # Batching buys little on CPU and the memory cap means nothing there.
        results, stats = run_adaptive_batches(
            lengths, run_batch, is_oom=lambda _e: False, max_batch=1,
        )
    return results, asdict(stats)

_SYSTEM_PROMPT = (
    "You are a code extraction assistant. "
    "Extract only what is explicitly present in the provided code. "
    "Do not infer business logic or guess at intent beyond what the code states. "
    "Respond with a concise structured extraction — 2 to 4 sentences maximum."
)

_USER_TEMPLATE = """\
Extract the following from this code chunk:
1. Purpose: one sentence on what this function or module does.
2. Inputs: parameter names and types if visible.
3. Outputs: return value, mutations, or I/O side-effects.
4. Key operations: the main transformations or calls made.

Code:
{code}"""

# ~3 500 tokens — covers tier-3 architectural chunks (≤4 000 tokens ≈ 16 000 chars)
_MAX_CODE_CHARS = 14_000
# 4 bullet points × ~20 tokens each + overhead → hard ceiling prevents rambling
_MAX_NEW_TOKENS = 160

# ADR-027: chunks per worker job, and how long one job may take. A job of n chunks
# at batch size 1 runs ~3 s a chunk on an 8 GB card, longer for tier-3 chunks, and
# may first spend up to _PAUSE_TIMEOUT_S waiting for memory. A group also caps the
# batch, so it is kept at twice max_batch_size's default of 48.
_GROUP_SIZE = 96


def _group_timeout_s(n: int) -> float:
    return _PAUSE_TIMEOUT_S + 60.0 + 15.0 * n


class ChunkSummarizer:
    """
    Lazy-loaded instruct LLM that augments chunk text with factual extractions
    before embedding, improving recall for intent-based queries while preserving
    lexical precision for exact-name lookups.

    The model is downloaded from HuggingFace Hub on first use and cached locally.
    Requires: pip install transformers torch

    Usage
    -----
    summarizer = ChunkSummarizer()                        # no I/O yet
    summaries  = summarizer.summarize_batch(code_strings) # model loads here, once
    """

    def __init__(
        self,
        model_id: str | None = None,
        device:   str | None = None,
    ) -> None:
        # ADR-020: device defaults to resolve_device() (auto-CUDA unless
        # CODE_INDEXER_DEVICE forces a value), so the one override governs the
        # summarizer too. An explicit device= still wins for callers that pass one.
        #
        # ADR-026: model_id defaults to None and resolves through config, so
        # [summarization].model_id reaches BOTH summarizer classes. Baking the id
        # into each signature made it two defaults for one knob — the same split
        # ADR-020 found for device, which is why both are resolved the same way now.
        self._model_id = model_id if model_id is not None else summarizer_model_id()
        self._device   = device if device is not None else resolve_device()
        self._pipe     = None
        self._failed   = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._pipe is not None or self._failed:
            return
        try:
            import torch
            from transformers import pipeline

            # ADR-020: key dtype off the RESOLVED device, not raw cuda availability,
            # so CODE_INDEXER_DEVICE=cpu also forces float32 (avoiding float16-on-CPU
            # quirks) rather than picking float16 because a GPU merely exists.
            dtype = torch.float16 if self._device == "cuda" else torch.float32
            print(
                f"  [Summarizer] Loading {self._model_id} "
                f"(device={self._device}, dtype={dtype}) — first run only ..."
            )
            self._pipe = pipeline(
                "text-generation",
                model=self._model_id,
                device_map=self._device,
                torch_dtype=dtype,
                trust_remote_code=True,
            )
            print("  [Summarizer] Ready.")
            logger.info("ChunkSummarizer loaded: %s", self._model_id)
        except Exception as exc:
            self._failed = True
            print(
                f"  [Summarizer] Load failed ({type(exc).__name__}: {exc})"
                " — summarization disabled."
            )
            logger.warning("ChunkSummarizer load failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Release the model and its GPU memory.

        The in-process counterpart to IsolatedChunkSummarizer.shutdown(). This
        class holds the pipeline in the indexer's own process, so releasing it
        means dropping the reference and returning the cached blocks to the
        driver; without the empty_cache() call the allocator keeps them and the
        embedding model gains nothing.
        """
        self._pipe = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def summarize_batch(self, codes: list[str]) -> list[str]:
        """
        Return one extraction string per code chunk.

        Empty strings are returned for any chunk where inference fails, so the
        caller can safely skip augmentation for those chunks.  The original code
        text is never discarded — only the embedding is augmented.
        """
        self._load()
        if self._failed or self._pipe is None or not codes:
            return [""] * len(codes)

        messages_batch = [
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": _USER_TEMPLATE.format(code=code[:_MAX_CODE_CHARS])},
            ]
            for code in codes
        ]

        try:
            outputs = self._pipe(
                messages_batch,
                max_new_tokens=_MAX_NEW_TOKENS,
                do_sample=False,
                temperature=None,   # must be None when do_sample=False in transformers ≥ 4.40
                top_p=None,
                top_k=None,
                pad_token_id=self._pipe.tokenizer.eos_token_id,
                batch_size=min(4, len(messages_batch)),
            )
        except Exception as exc:
            logger.warning("ChunkSummarizer.summarize_batch failed: %s", exc)
            return [""] * len(codes)

        results: list[str] = []
        for out in outputs:
            msg_list = out[0]["generated_text"]
            if isinstance(msg_list, list):
                # Chat-template output: list of {"role": ..., "content": ...} dicts
                text = msg_list[-1].get("content", "").strip()
            else:
                text = str(msg_list).strip()
            # Trim at the first blank line — prevents the model spilling into
            # follow-on commentary or code examples beyond the extraction
            results.append(text.split("\n\n")[0].strip())

        return results


class IsolatedChunkSummarizer:
    """
    Drop-in replacement for ChunkSummarizer that runs the LLM in a dedicated
    child process via ProcessPoolExecutor(max_workers=1).

    WHY: on Windows (and CPU-only machines in general), loading both the Jina
    embedding model (core.py) and Qwen 1.5B-Instruct in the same process
    exhausts virtual memory during the first inference batch.  Windows OOM
    kills the process via a native SEH exception that Python's except-clauses
    cannot catch, so the indexer dies silently with no traceback.

    By running Qwen in a separate worker process:
      • The worker's memory is isolated from the main indexer process.
      • An OOM or segfault in the worker raises BrokenProcessPool (a Python
        exception) in the caller — caught here, summarization is disabled
        gracefully, and indexing continues for all remaining files.

    dtype defaults to "float16" to halve model RAM on CPU (~3 GB vs ~6 GB
    for 1.5B float32).  transformers supports float16 inference on CPU.
    """

    def __init__(
        self,
        model_id:       str | None = None,
        device:         str | None = None,
        dtype:          str = "float16",
        max_batch_size: int | None = None,
        vram_reserve_mb: int | None = None,
        token_budget:   int | None = None,
        executor_factory: Callable | None = None,
    ) -> None:
        # ADR-020: device resolves via resolve_device() (CODE_INDEXER_DEVICE-aware)
        # so the isolated-worker summarizer — the one the indexer actually uses —
        # is CPU-forceable too. dtype stays an explicit knob: float16 is the
        # deliberate CPU RAM-saving default (see class docstring), independent of device.
        #
        # ADR-026: this is the class incremental_indexer actually constructs, and it
        # constructed it with NO arguments — so [summarization].model_id was inert.
        # Resolving through config is what makes the documented knob real.
        self._model_id = model_id if model_id is not None else summarizer_model_id()
        self._device   = device if device is not None else resolve_device()
        self._dtype    = dtype
        # ADR-027: batching knobs, resolved through config like the model id.
        self._max_batch_size = max(1, max_batch_size if max_batch_size is not None
                                   else summarizer_max_batch_size())
        self._reserve_mb = max(0, vram_reserve_mb if vram_reserve_mb is not None
                               else summarizer_vram_reserve_mb())
        self._token_budget = max(1, token_budget if token_budget is not None
                                 else summarizer_batch_token_budget())
        self._executor_factory = executor_factory
        self._executor = None
        self._failed   = False
        self.stats     = BatchStats()
        atexit.register(self._shutdown)

    def _ensure_executor(self) -> None:
        if self._executor is not None or self._failed:
            return
        if self._executor_factory is not None:
            self._executor = self._executor_factory()
            return
        from concurrent.futures import ProcessPoolExecutor
        self._executor = ProcessPoolExecutor(
            max_workers=1,
            initializer=_worker_init,
            initargs=(self._model_id, self._device, self._dtype),
        )

    def summarize_batch(self, codes: list[str]) -> list[str]:
        """
        Return one extraction string per code chunk, same contract as
        ChunkSummarizer.summarize_batch.  Empty strings on any failure.

        ADR-027: chunks go to the worker in groups of at most _GROUP_SIZE, each
        with a timeout scaled to its size. The old single job per tier with a
        flat 300 s timeout turned summarization off for the rest of the run on
        any tier over ~100 chunks.
        """
        if self._failed or not codes:
            return [""] * len(codes)
        results: list[str] = []
        for start in range(0, len(codes), _GROUP_SIZE):
            results.extend(self._summarize_group(codes[start:start + _GROUP_SIZE]))
        return results

    def _summarize_group(self, group: list[str]) -> list[str]:
        timeout = _group_timeout_s(len(group))
        for attempt in (1, 2):
            if self._failed:
                break
            try:
                self._ensure_executor()
                future = self._executor.submit(
                    _worker_summarize, group, _MAX_NEW_TOKENS,
                    self._max_batch_size, self._reserve_mb, self._token_budget,
                )
                results, stats = future.result(timeout=timeout)
                self.stats.merge(stats)
                return results
            except Exception as exc:  # noqa: BLE001 — timeout, dead worker, anything
                # A worker that timed out is still generating and still holding its
                # GPU memory. It has to be killed, not just shut down, or the restart
                # loads a second copy of the model beside it and both spill.
                self._kill_worker()
                if attempt == 1:
                    print(
                        f"  [Summarizer] Worker failed on a group of {len(group)} "
                        f"({type(exc).__name__}: {exc}) — restarting it and retrying once.",
                        flush=True,
                    )
                    logger.warning("IsolatedChunkSummarizer worker failed, retrying: %s", exc)
                else:
                    print(
                        f"  [Summarizer] Worker failed again ({type(exc).__name__}: {exc})"
                        " — summarization disabled for remaining files.",
                        flush=True,
                    )
                    logger.warning("IsolatedChunkSummarizer worker failed twice: %s", exc)
                    self._failed = True
        self.stats.empty_error += len(group)
        return [""] * len(group)

    def stats_line(self) -> str:
        return self.stats.line()

    def shutdown(self) -> None:
        """Release the worker process and, with it, its GPU memory.

        Called by the indexer between the summarization and embedding passes.
        The model lives in a child process, so terminating it hands the memory
        back to the driver outright rather than relying on allocator reuse.
        Waits for the worker to exit, so the memory is back before the embedder
        loads. Safe to call more than once, and safe when no worker ever started.
        """
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
            self._executor = None

    def _kill_worker(self) -> None:
        """Stop the worker process now, even mid-generation."""
        if self._executor is None:
            return
        kill = getattr(self._executor, "kill_workers", None)   # Python 3.14+
        try:
            if kill is not None:
                kill()
            else:
                for proc in list(getattr(self._executor, "_processes", {}).values()):
                    proc.kill()
        except Exception:
            pass
        self._shutdown()

    def _shutdown(self) -> None:
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
