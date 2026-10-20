"""ADR-054 (B-062): OPENBLAS_NUM_THREADS is set before numpy or faiss loads, in every entry point.

Each case imports one module in a fresh interpreter with a finder on sys.meta_path
that records the variable's value at the moment numpy, faiss or torch is first
looked up, which is when OpenBLAS would allocate its per-thread buffers.
"""
import json
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")

_PROBE = r"""
import json, os, sys
seen = {}

class Recorder:
    def find_spec(self, name, path=None, target=None):
        if name in ("numpy", "faiss", "torch") and name not in seen:
            seen[name] = os.environ.get("OPENBLAS_NUM_THREADS")
        return None

sys.meta_path.insert(0, Recorder())
sys.path.insert(0, sys.argv[1])
for statement in sys.argv[2:]:
    exec(statement)
print(json.dumps({"seen": seen, "after": os.environ.get("OPENBLAS_NUM_THREADS")}))
"""


def _probe(*statements: str, env_value: str | None = None) -> dict:
    env = dict(os.environ)
    env.pop("OPENBLAS_NUM_THREADS", None)
    env["CODE_INDEXER_DEVICE"] = "cpu"
    if env_value is not None:
        env["OPENBLAS_NUM_THREADS"] = env_value
    proc = subprocess.run([sys.executable, "-c", _PROBE, _SRC, *statements], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Every module a process can start from: the two console scripts, the model host
# (started as a subprocess), the retriever the eval tools import first, and core.
ENTRY_MODULES = ["core", "MCPServer", "incremental_indexer", "hybrid_retriever", "model_host"]


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_variable_is_set_before_numpy_and_faiss_load(module):
    out = _probe(f"import {module}")
    assert out["after"] == "1"
    for lib, value in out["seen"].items():
        assert value == "1", f"{lib} loaded with OPENBLAS_NUM_THREADS={value!r} via {module}"


@pytest.mark.parametrize("module", ["core", "MCPServer", "incremental_indexer", "hybrid_retriever"])
def test_entry_modules_do_load_numpy_and_faiss(module):
    """Keeps the test above honest: these modules really do pull both libraries in."""
    out = _probe(f"import {module}")
    assert {"numpy", "faiss"} <= set(out["seen"])


def test_model_host_sets_it_before_its_lazy_imports():
    out = _probe("import model_host", "import faiss, numpy")
    assert out["seen"] == {"faiss": "1", "numpy": "1"}


def test_tui_launches_through_mcpserver_with_it_set():
    out = _probe("import tui.backend", "import MCPServer")
    assert out["seen"].get("numpy") == "1"


@pytest.mark.parametrize("module", ["core", "MCPServer"])
def test_an_explicit_value_is_kept(module):
    out = _probe(f"import {module}", env_value="4")
    assert out["after"] == "4"
    assert set(out["seen"].values()) == {"4"}


def test_the_probe_catches_numpy_imported_first():
    """Negative control: importing numpy before core is the bug this guards against."""
    out = _probe("import numpy", "import core")
    assert out["seen"]["numpy"] is None
    assert out["after"] == "1"


def test_blas_threads_is_a_leaf():
    """It must not import anything that could load numpy before it sets the variable."""
    out = _probe("import blas_threads", "import sys; assert 'numpy' not in sys.modules")
    assert out["after"] == "1" and out["seen"] == {}
