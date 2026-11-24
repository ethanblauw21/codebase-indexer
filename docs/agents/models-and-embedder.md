# Models, downloads, config-vs-code rules, reranker/embedder changes

Moved from src/CLAUDE.md, verbatim (source lines 78-90, 111-115). Read when working on this area.

## Models

All models are downloaded automatically by HuggingFace on first use. To pre-download before going offline (recommended for CI or air-gapped environments), use `huggingface-cli` (ships with `huggingface_hub`, a transitive dep of `sentence-transformers`):

```bash
huggingface-cli download BAAI/bge-code-v1                      # embedder (~5.8 GiB download, fp32; 2.9 GB loaded in bf16)
huggingface-cli download Qwen/Qwen3-Reranker-0.6B              # reranker (~1.2 GB, optional)
huggingface-cli download Qwen/Qwen2.5-Coder-1.5B-Instruct      # summarizer (~3 GB, optional)
```

Model IDs live in `indexer.toml` (`[embeddings]`, `[reranker]`, `[summarization]`) and **all three are read from config at runtime**: the **embedder** (`src/core.py`, ADR-009 §P1 — `model_id` + `max_seq_length` + `dimension` + `query_instruct`), the **reranker** (`src/config.py` → `HybridRetriever`), and the **summarizer** (ADR-026 — `config.summarizer_model_id()`, resolved by both `ChunkSummarizer` and `IsolatedChunkSummarizer`). `[summarization].enabled` is the real on/off gate (ADR-026); it used to be decorative, with a module constant deciding instead.

**A code default must equal the value shipped in `indexer.toml`** — `tests/test_config_drift.py` enforces it (ADR-026 §8). This is not pedantry: `core.py` defaulted to jina/768 for twenty days after config moved to bge/1536, so an unconfigured repo would have built an index a configured repo could not load.
The reranker and summarizer are both optional — the indexer degrades gracefully without them. **Reranking is off by default** (`[reranker].enabled = false`): the retriever returns the RRF-ranked top-10, which is the measured Wave-0 baseline (ADR-007), not a fallback.

**Changing the embedder is a one-time reindex** (ADR-009 §P1): a new model usually has a different vector `dimension`, so update `model_id` + `dimension` together, delete `.code-index`, and rerun `code-indexer`. `stable_id`s are model-independent, so it is recompute-vectors-only; `core.py` refuses to load an index whose dimension no longer matches the configured embedder.

The pre-download `huggingface-cli` line for the reranker above lists the configured model; the prior `jinaai/jina-reranker-v2-base-code` id was a non-existent model and has been replaced by `Qwen/Qwen3-Reranker-0.6B`.
