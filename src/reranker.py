"""reranker.py — cross-encoder / causal-LM rescoring of dense candidates.

Canonical home for the reranker scorer and its loader. Both the production
retriever (``hybrid_retriever.py``) and the offline eval harness
(``tools/coir_eval.py``) import from here so there is exactly ONE scorer
implementation to reason about.

Two scorer shapes share a single ``.predict(pairs, batch_size)`` surface:

* ``Qwen3Reranker`` — Qwen3-Reranker is a causal LM, NOT a sentence-transformers
  CrossEncoder. It judges (query, doc) relevance by reading the logit of the
  "yes" vs "no" token after an instruction prompt (Qwen's documented interface).
  We wrap it so callers drive it exactly like a CrossEncoder.
* ``sentence_transformers.CrossEncoder`` — the classic single-logit scorer.

``load_reranker(model_id)`` picks the right one by id, so swapping models needs
no call-site changes.
"""
from __future__ import annotations

# Task instruction prepended to every (query, doc) judgment. Code-retrieval framed.
RERANK_INSTRUCTION = (
    "Given a code search query, retrieve the most relevant code snippet or answer "
    "that satisfies the query."
)
# Cap on query+doc tokens fed to the reranker. Mirrors the embedder's max_seq_length
# policy and bounds CPU cost (cost scales with queries x depth x sequence length).
RERANK_MAX_LENGTH = 512


class Qwen3Reranker:
    """Qwen3-Reranker (causal-LM yes/no scorer) with a CrossEncoder-compatible API."""

    def __init__(self, model_id, max_length=RERANK_MAX_LENGTH, instruction=RERANK_INSTRUCTION,
                 device="cpu"):
        import warnings

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        # Cosmetic only: encode-then-pad is the Qwen3-Reranker recipe (we must splice
        # fixed prompt token-ids around the body), but it trips a "use __call__" advisory.
        # Tokenization is <1% of per-pair cost, so this is noise, not a speed fix.
        warnings.filterwarnings("ignore", message=r".*__call__ method is faster.*")
        self.torch = torch
        self.instruction = instruction
        self.max_length = max_length
        self.device = device
        # Left-padding is required: we read the logits at the final position, so the
        # real last token must sit at the right edge of every padded row.
        self.tok = AutoTokenizer.from_pretrained(model_id, padding_side="left")
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.float32,
        ).to(device).eval()
        self.true_id = self.tok.convert_tokens_to_ids("yes")
        self.false_id = self.tok.convert_tokens_to_ids("no")
        prefix = (
            "<|im_start|>system\nJudge whether the Document meets the requirements "
            "based on the Query and the Instruct provided. Note that the answer can "
            'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.prefix_ids = self.tok.encode(prefix, add_special_tokens=False)
        self.suffix_ids = self.tok.encode(suffix, add_special_tokens=False)

    def _format(self, query, doc):
        return (f"<Instruct>: {self.instruction}\n"
                f"<Query>: {query}\n"
                f"<Document>: {doc}")

    def predict(self, pairs, batch_size=32, **_ignored):
        """Score (query, doc) pairs in [0, 1] (P(yes)). Extra kwargs (e.g.
        ``convert_to_numpy`` passed by CrossEncoder call sites) are ignored."""
        torch = self.torch
        # Budget for the (query, doc) body once the fixed prompt scaffolding is reserved.
        body_budget = self.max_length - len(self.prefix_ids) - len(self.suffix_ids)
        scores = []
        for start in range(0, len(pairs), batch_size):
            chunk = pairs[start:start + batch_size]
            texts = [self._format(q, d) for q, d in chunk]
            enc = self.tok(texts, padding=False, truncation="longest_first",
                           return_attention_mask=False, max_length=body_budget)
            # Splice the fixed prompt scaffolding around each body, then build a
            # matching attention mask. The mask is REQUIRED: we left-pad (so the real
            # last token sits at index -1), and without masking the causal model would
            # attend to pad tokens as real text — corrupting logits badly once batches
            # contain long/short docs together. tok.pad left-pads ids AND mask in step.
            full = {"input_ids": [], "attention_mask": []}
            for ids in enc["input_ids"]:
                row = self.prefix_ids + ids + self.suffix_ids
                full["input_ids"].append(row)
                full["attention_mask"].append([1] * len(row))
            batch = self.tok.pad(full, padding=True, return_tensors="pt").to(self.device)
            with torch.no_grad():
                last = self.model(**batch).logits[:, -1, :]
                pair_logits = torch.stack([last[:, self.false_id], last[:, self.true_id]], dim=1)
                p_yes = torch.nn.functional.log_softmax(pair_logits, dim=1)[:, 1].exp()
            scores.extend(p_yes.tolist())
        return scores


def load_reranker(model_id, device="cpu"):
    """Return a reranker exposing .predict(pairs, batch_size). Qwen3 -> logit scorer;
    anything else -> sentence-transformers CrossEncoder."""
    if "qwen3-reranker" in model_id.lower():
        print(f"Loading reranker (Qwen3 logit-scorer): {model_id}", flush=True)
        return Qwen3Reranker(model_id, device=device)
    from sentence_transformers import CrossEncoder
    print(f"Loading reranker (CrossEncoder): {model_id}", flush=True)
    return CrossEncoder(model_id, device=device, trust_remote_code=True)
