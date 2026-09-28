"""tools/pass2_bench.py: every arm embeds the same texts; only the calls differ."""
from __future__ import annotations

import importlib.util
import os

import numpy as np

import core
import incremental_indexer as ii
from core import embed_dimension

_BENCH = os.path.join(os.path.dirname(__file__), "..", "tools", "pass2_bench.py")


def _load_bench():
    spec = importlib.util.spec_from_file_location("pass2_bench", _BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_embed(texts, batch_size=32):
    dim = embed_dimension()
    return np.array([[float((hash(t) >> (8 * i)) & 0xFF) for i in range(dim)] for t in texts],
                    dtype=np.float32)


def test_the_arms_embed_the_same_texts_in_different_calls(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"m{i}.py").write_text(
            f"def f{i}(x):\n    return x + {i}\n\n\ndef g{i}(y):\n    return y * {i}\n",
            encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(real))
    monkeypatch.setattr(ii, "DB_PATH", str(real / "graph.db"))
    ii.run_incremental(str(repo), interactive=False)

    bench = _load_bench()
    results = {a: bench.run_arm(a, str(repo), str(real / "graph.db")) for a in bench.ARMS}

    texts = {r["texts"] for r in results.values()}
    assert len(texts) == 1 and texts.pop() > 0
    assert results["old"]["calls"] > results["b050"]["calls"] == results["b055"]["calls"]
    # The real index is untouched and the module is restored.
    assert ii.INDEX_DIR == str(real) and ii._EMBED_WINDOW_TEXTS == 256
