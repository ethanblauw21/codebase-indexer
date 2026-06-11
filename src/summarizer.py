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

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Subprocess worker — module-level so ProcessPoolExecutor can pickle them on
# Windows (spawn start method requires top-level callables).
# These functions run ONLY inside the worker process, never in the parent.
# ─────────────────────────────────────────────────────────────────────────────

_w_pipe = None   # resident in the worker process after _worker_init runs


def _worker_init(model_id: str, device: str, dtype_str: str) -> None:
    """
    ProcessPoolExecutor initializer — called once when the worker process
    starts.  Loads the pipeline into _w_pipe so it stays resident for the
    lifetime of the worker (no per-batch reload cost).
    """
    global _w_pipe
    import torch
    from transformers import pipeline

    dtype = torch.float16 if dtype_str == "float16" else torch.float32
    print(
        f"  [Summarizer] Loading {model_id} "
        f"(device={device}, dtype={dtype}) — first run only ...",
        flush=True,
    )
    _w_pipe = pipeline(
        "text-generation",
        model=model_id,
        device_map=device,
        torch_dtype=dtype,
        trust_remote_code=True,
    )
    print("  [Summarizer] Ready.", flush=True)


def _worker_batch(messages_batch: list, max_new_tokens: int) -> list[str]:
    """
    Run one summarization batch inside the worker process.
    Returns a list of extraction strings (empty string on failure).
    """
    if _w_pipe is None:
        return [""] * len(messages_batch)
    try:
        outputs = _w_pipe(
            messages_batch,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=None,
            top_p=None,
            top_k=None,
            pad_token_id=_w_pipe.tokenizer.eos_token_id,
            batch_size=1,
        )
    except Exception:
        return [""] * len(messages_batch)

    results: list[str] = []
    for out in outputs:
        msg_list = out[0]["generated_text"]
        if isinstance(msg_list, list):
            text = msg_list[-1].get("content", "").strip()
        else:
            text = str(msg_list).strip()
        results.append(text.split("\n\n")[0].strip())
    return results

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
        model_id: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        device:   str = "auto",
    ) -> None:
        self._model_id = model_id
        self._device   = device
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

            dtype = torch.float16 if torch.cuda.is_available() else torch.float32
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
        model_id: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        device:   str = "auto",
        dtype:    str = "float16",
    ) -> None:
        self._model_id = model_id
        self._device   = device
        self._dtype    = dtype
        self._executor = None
        self._failed   = False
        atexit.register(self._shutdown)

    def _ensure_executor(self) -> None:
        if self._executor is not None or self._failed:
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
        """
        if self._failed or not codes:
            return [""] * len(codes)

        try:
            print(f"  [summarizer] ensuring worker process ({len(codes)} chunks)...", flush=True)
            self._ensure_executor()
            print("  [summarizer] worker process ready, submitting batch...", flush=True)
            messages_batch = [
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": _USER_TEMPLATE.format(code=c[:_MAX_CODE_CHARS])},
                ]
                for c in codes
            ]
            future = self._executor.submit(_worker_batch, messages_batch, _MAX_NEW_TOKENS)
            print("  [summarizer] batch submitted, waiting for result (timeout=300s)...", flush=True)
            result = future.result(timeout=300)
            print(f"  [summarizer] result received ({len(result)} summaries)", flush=True)
            return result
        except Exception as exc:
            print(
                f"  [Summarizer] Worker process failed ({type(exc).__name__}: {exc})"
                " — summarization disabled for remaining files.",
                flush=True,
            )
            logger.warning("IsolatedChunkSummarizer worker failed: %s", exc)
            self._failed = True
            self._shutdown()
            return [""] * len(codes)

    def _shutdown(self) -> None:
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
