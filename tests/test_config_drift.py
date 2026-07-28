"""
test_config_drift.py — indexer.toml and the code's defaults must agree (ADR-026 §8).

**The defect this exists to prevent.** ADR-026 asserts "a default that appears in two
places is a defect." That was a principle with no mechanism, and it had already failed
in the file the ADR held up as its model: `core.py` defaulted to
`jinaai/jina-embeddings-v2-base-code` / 768 dims for twenty days after ADR-009 §P1
promoted `BAAI/bge-code-v1` / 1536 in `indexer.toml`. Nothing noticed, because nothing
compared them. A repo running without an `indexer.toml` would have silently built a
768-dim index that a configured repo could not load.

**Two obligations, tested separately:**

1. Every key documented in the shipped `indexer.toml` is either wired to an accessor
   or explicitly listed as inert. The inert list may shrink, never grow.
2. For every wired key, the code's default equals the shipped value.

Cheap and offline: this parses TOML and reads module constants. No model load.
"""
from __future__ import annotations

import os
import tomllib

import pytest

import config
import core
import hybrid_retriever


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHIPPED_TOML = os.path.join(_REPO_ROOT, "indexer.toml")


@pytest.fixture(scope="module")
def shipped() -> dict:
    with open(_SHIPPED_TOML, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# The registry: shipped key -> the module constant that must equal it
# ---------------------------------------------------------------------------

def _wired() -> dict[tuple[str, str], object]:
    """(block, key) -> the single code default for that knob.

    Add an entry here when you wire a key. If a key is documented in indexer.toml
    and appears in neither this map nor KNOWN_INERT, test_every_shipped_key_is_
    accounted_for fails — which is the whole point.
    """
    return {
        ("embeddings", "model_id"):        core._DEFAULT_MODEL_ID,
        ("embeddings", "max_seq_length"):  core._DEFAULT_MAX_SEQ_LENGTH,
        ("embeddings", "dimension"):       core._DEFAULT_DIMENSION,
        ("embeddings", "query_instruct"):  core._DEFAULT_QUERY_INSTRUCT,

        ("reranker", "model_id"):          hybrid_retriever._DEFAULT_RERANKER_MODEL_ID,
        ("reranker", "enabled"):           hybrid_retriever._DEFAULT_RERANKER_ENABLED,

        ("retrieval", "fusion_mode"):      hybrid_retriever._DEFAULT_FUSION_MODE,
        ("retrieval", "dense_weight"):     hybrid_retriever._DEFAULT_DENSE_WEIGHT,
        ("retrieval", "sparse_weight"):    hybrid_retriever._DEFAULT_SPARSE_WEIGHT,

        # ADR-026 commit 1 wired these two — they were inert before.
        ("summarization", "enabled"):      config.DEFAULT_SUMMARIZATION_ENABLED,
        ("summarization", "model_id"):     config.DEFAULT_SUMMARIZER_MODEL_ID,
    }


# Wired, but with no single code default to compare against: `extra_*` keys are
# per-repo additions on top of the defaults, so "the shipped value" is this repo's
# own layout, not a default anything else should inherit. Accounted for, not compared.
WIRED_NO_DEFAULT: dict[tuple[str, str], str] = {
    ("ignore", "extra_dirs"):        "scan_policy._resolve — extends DEFAULT_IGNORE_DIRS",
    ("ignore", "extra_root_dirs"):   "scan_policy._resolve — extends DEFAULT_IGNORE_ROOT_DIRS",
    ("ignore", "extra_extensions"):  "scan_policy._resolve_exts — extends DEFAULT_INDEXABLE_EXTS",
}


# Documented in indexer.toml, read by nothing in src/. Each entry needs a reason and
# an owner. THIS SET MUST ONLY SHRINK.
#
# ADR-026 commit 2 removed the three [ignore] entries: `dirs`/`root_dirs`/`extensions`
# are live knobs now, and the shipped file no longer sets them — a bare key *replaces*
# the defaults, so a copy kept here "for reference" would freeze this repo's gate and
# silently drop every future default.
KNOWN_INERT: dict[tuple[str, str], str] = {
    ("indexer", "repo_root"):  "inert — incremental_indexer uses os.getcwd(); ADR-026 §6 anchors the scan, not this key",
    ("indexer", "index_dir"):  "inert — INDEX_DIR is a module constant; scan_policy hardcodes '.code-index' rather than half-wire it",
}

# Read by tools/coir_eval.py rather than src/. Out of scope for the src-side registry,
# but not inert — the harness genuinely consumes them.
_EVAL_KEYS_OWNED_BY_TOOLS = {
    "subtasks", "tier_projection", "configs", "budget_tokens", "rerank_depth",
    "rerank_sample_queries", "sparse_sample_queries", "sample_seed",
    "ci_subtasks", "ci_limit_queries", "baseline_path",
}


# ---------------------------------------------------------------------------
# 1 — accounting: nothing documented may be silently unread
# ---------------------------------------------------------------------------

def test_every_shipped_key_is_accounted_for(shipped):
    wired = _wired()
    unaccounted = []
    for block, body in shipped.items():
        if not isinstance(body, dict):
            continue
        for key in body:
            if block == "eval" and key in _EVAL_KEYS_OWNED_BY_TOOLS:
                continue
            if (block, key) in wired or (block, key) in KNOWN_INERT:
                continue
            if (block, key) in WIRED_NO_DEFAULT:
                continue
            unaccounted.append(f"[{block}].{key}")

    assert not unaccounted, (
        "indexer.toml documents keys that no accessor reads and that are not listed "
        f"in KNOWN_INERT: {unaccounted}. Wire them, or add them to KNOWN_INERT with a "
        "reason. A documented knob that does nothing is the defect ADR-026 exists to remove."
    )


def test_known_inert_set_has_not_grown():
    """A tripwire on the inert list itself.

    Ratcheted down from 5 to 2 by ADR-026 commit 2, which made the [ignore] block
    live. If you find yourself raising this number, you are adding a
    documented-but-dead knob — which is the defect, not the fix.
    """
    assert len(KNOWN_INERT) <= 2, (
        f"KNOWN_INERT grew to {len(KNOWN_INERT)}. It may only shrink — see ADR-026 §8."
    )


def test_wired_no_default_keys_are_really_read(shipped):
    """Each `extra_*` key named as wired must actually reach the resolver.

    Guards the loophole in WIRED_NO_DEFAULT: an entry there is exempt from the
    value-comparison, so without this it would be a place to park a dead key.
    """
    import scan_policy

    scan_policy.reset()
    try:
        policy = scan_policy.scan_policy(_REPO_ROOT)
        block = shipped.get("ignore", {})
        for extra_key, target in (
            ("extra_dirs", policy.ignore_dirs),
            ("extra_root_dirs", policy.ignore_root_dirs),
            ("extra_extensions", policy.extensions),
        ):
            for value in block.get(extra_key, []):
                assert value.lower() in {t.lower() for t in target}, (
                    f"[ignore].{extra_key} lists {value!r} and the resolved policy "
                    "does not contain it"
                )
    finally:
        scan_policy.reset()


def test_inert_keys_still_exist_in_the_shipped_config(shipped):
    """Stops KNOWN_INERT from rotting after a key is renamed or removed."""
    stale = [
        f"[{block}].{key}"
        for (block, key) in KNOWN_INERT
        if key not in shipped.get(block, {})
    ]
    assert not stale, f"KNOWN_INERT names keys no longer in indexer.toml: {stale}"


# ---------------------------------------------------------------------------
# 2 — agreement: a wired key's code default must equal the shipped value
# ---------------------------------------------------------------------------

def test_code_defaults_equal_shipped_values(shipped):
    """The check that would have caught the jina/bge drift on the day it happened."""
    mismatches = []
    for (block, key), code_default in _wired().items():
        if key not in shipped.get(block, {}):
            continue                          # covered by the accounting test above
        shipped_value = shipped[block][key]
        if isinstance(code_default, float) or isinstance(shipped_value, float):
            same = abs(float(code_default) - float(shipped_value)) < 1e-9
        else:
            same = code_default == shipped_value
        if not same:
            mismatches.append(
                f"[{block}].{key}: code default {code_default!r} != shipped {shipped_value!r}"
            )

    assert not mismatches, (
        "Code defaults have drifted from indexer.toml:\n  " + "\n  ".join(mismatches)
        + "\n\nA repo with no indexer.toml would behave differently from this one. "
          "Fix the code default, not this test."
    )


def test_embedder_dimension_default_matches_the_model(shipped):
    """Dimension and model id must move together or an index becomes unloadable."""
    assert core._DEFAULT_MODEL_ID == shipped["embeddings"]["model_id"]
    assert core._DEFAULT_DIMENSION == shipped["embeddings"]["dimension"]


# ---------------------------------------------------------------------------
# 3 — the summarization knobs are actually reachable (ADR-026 commit 1)
# ---------------------------------------------------------------------------

def test_summarization_accessors_read_the_shipped_config(shipped):
    config.reset_config_cache()
    assert config.summarization_enabled() == shipped["summarization"]["enabled"]
    assert config.summarizer_model_id() == shipped["summarization"]["model_id"]


def test_summarization_enabled_is_honoured(tmp_path, monkeypatch):
    """`enabled = false` must actually turn summarization off.

    Before ADR-026 this key was decorative: the real gate was a module constant, so
    the only way to skip summarization was to edit source — which on CPU is the
    difference between an index that completes and one that does not.
    """
    (tmp_path / "indexer.toml").write_text(
        "[summarization]\nenabled = false\nmodel_id = \"Qwen/Qwen2.5-Coder-7B-Instruct\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config.reset_config_cache()
    try:
        assert config.summarization_enabled() is False
        assert config.summarizer_model_id() == "Qwen/Qwen2.5-Coder-7B-Instruct"
    finally:
        config.reset_config_cache()


def test_missing_config_falls_back_to_the_single_default(tmp_path, monkeypatch):
    """No indexer.toml anywhere up the tree — accessors return the code defaults."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    monkeypatch.setattr(config, "find_config_path", lambda *_a, **_k: None)
    config.reset_config_cache()
    try:
        assert config.summarization_enabled() is config.DEFAULT_SUMMARIZATION_ENABLED
        assert config.summarizer_model_id() == config.DEFAULT_SUMMARIZER_MODEL_ID
    finally:
        config.reset_config_cache()


# ---------------------------------------------------------------------------
# 4 — one default per knob: the summarizer classes must not carry their own
# ---------------------------------------------------------------------------

def test_neither_summarizer_class_hardcodes_a_model_id():
    """Both classes resolve through config; neither bakes an id into its signature.

    ADR-020 found exactly this shape for `device`: one class honoured the override
    and the other ignored it. Two defaults for one knob is how that happens.
    """
    import inspect

    import summarizer

    for cls in (summarizer.ChunkSummarizer, summarizer.IsolatedChunkSummarizer):
        default = inspect.signature(cls.__init__).parameters["model_id"].default
        assert default is None, (
            f"{cls.__name__}.__init__ carries its own model_id default ({default!r}). "
            "The default belongs in config.py, once."
        )
