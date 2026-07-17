"""device.py — shared torch device resolution (ADR-024, ADR-020).

Single source of truth for "which device should GPU-capable components run on":
CUDA if available, else CPU, overridable via ``CODE_INDEXER_DEVICE`` for forcing
a specific value (e.g. to compare "cpu" vs "cuda", or to force "mps"). No MPS
auto-detection tier — this project's actual dev/CI fleet is CUDA-or-CPU, so MPS
is only reachable via the explicit override.

ADR-024 wired this into the reranker + eval; ADR-020 extended it to the embedder
(``core.py``) and summarizer (``summarizer.py``), so the claim above now holds for
**all four** local model loads: ``CODE_INDEXER_DEVICE=cpu`` makes the whole stack —
embedder, reranker, summarizer, eval — run on CPU. It is the supported force-CPU
control (previously only ``CUDA_VISIBLE_DEVICES=""`` reliably did this, because the
embedder ignored the override).
"""
from __future__ import annotations

import os


def resolve_device() -> str:
    """Return "cuda" if available, else "cpu". ``CODE_INDEXER_DEVICE`` wins if set."""
    forced = os.environ.get("CODE_INDEXER_DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"
