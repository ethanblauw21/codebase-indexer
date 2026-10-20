"""ADR-051 (B-054): a line-ending-only difference is not a change.

The file key is an MD5 of the bytes with line endings normalized, as both readers
already normalize the text they chunk. An index built before it is re-keyed in place,
once. Fake embedder, summarizer off: no model loads.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np
import pytest

import core
import incremental_indexer as ii
import source as source_mod
from core import embed_dimension
from db import CodeDB
from source import GitCommitSource, WorkingTreeSource, md5_file, md5_file_raw

SAMPLE = (
    "import os\n\n\n"
    "def alpha(x):\n    return x + 1\n\n\n"
    "class Beta:\n    def gamma(self, y):\n        return alpha(y) * 2\n"
)


@pytest.fixture(autouse=True)
def _offline_tokens(monkeypatch):
    """Token counts by characters, so chunking needs no tokenizer download."""
    monkeypatch.setattr(core.jina_tokenizer, "count_tokens", lambda s: max(1, len(s) // 4))


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


# ── the file key ─────────────────────────────────────────────────────────────

def test_lf_and_crlf_have_one_file_hash(tmp_path):
    _write(tmp_path / "lf.py", SAMPLE)
    _write(tmp_path / "crlf.py", _crlf(SAMPLE))
    _write(tmp_path / "cr.py", SAMPLE.replace("\n", "\r"))
    assert md5_file(str(tmp_path / "lf.py")) == md5_file(str(tmp_path / "crlf.py"))
    assert md5_file(str(tmp_path / "lf.py")) == md5_file(str(tmp_path / "cr.py"))


def test_an_lf_file_hashes_as_before(tmp_path):
    """Only files with a \\r get a new key, so an LF repo is not re-keyed at all."""
    _write(tmp_path / "lf.py", SAMPLE)
    assert md5_file(str(tmp_path / "lf.py")) == hashlib.md5(SAMPLE.encode()).hexdigest()
    assert md5_file(str(tmp_path / "lf.py")) == md5_file_raw(str(tmp_path / "lf.py"))


@pytest.mark.parametrize("pad", [65535, 65536, 65534])
def test_a_crlf_split_across_blocks_hashes_as_one_newline(tmp_path, pad):
    """The \\r of a \\r\\n can end one 64 KiB block and its \\n start the next."""
    body = "x" * pad + "\n" + "tail\n" + "\n" * 3
    _write(tmp_path / "lf.txt", body)
    _write(tmp_path / "crlf.txt", _crlf(body))
    assert md5_file(str(tmp_path / "crlf.txt")) == md5_file(str(tmp_path / "lf.txt"))
    _write(tmp_path / "end.txt", "x" * 65535 + "\r")         # a lone \r at the very end
    _write(tmp_path / "end_lf.txt", "x" * 65535 + "\n")
    assert md5_file(str(tmp_path / "end.txt")) == md5_file(str(tmp_path / "end_lf.txt"))


def test_a_real_change_still_changes_the_hash(tmp_path):
    _write(tmp_path / "a.py", _crlf(SAMPLE))
    _write(tmp_path / "b.py", _crlf(SAMPLE.replace("x + 1", "x + 2")))
    assert md5_file(str(tmp_path / "a.py")) != md5_file(str(tmp_path / "b.py"))


# ── chunks and summary keys: already EOL-free before ADR-051 ─────────────────

def _chunk_keys(rel: str, content: str) -> dict[str, list[tuple[str, str, str]]]:
    return {tier: [(c.scope, c.text, ii.chunk_text_hash(c.text)) for c in chunks]
            for tier, chunks in ii.chunk_all_tiers(rel, content).items()}


def test_chunk_text_never_keeps_a_carriage_return(tmp_path):
    """The backlog left this unverified: the folder reader opens in text mode, whose
    universal newlines drop every \\r, so chunks and summary keys never differed."""
    _write(tmp_path / "lf.py", SAMPLE)
    _write(tmp_path / "crlf.py", _crlf(SAMPLE))
    src = WorkingTreeSource(str(tmp_path))
    crlf = src.read("crlf.py")
    assert "\r" not in crlf
    assert _chunk_keys("m.py", crlf) == _chunk_keys("m.py", src.read("lf.py"))


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_git_mode_reads_a_crlf_blob_into_the_same_chunks(tmp_path):
    """Git mode keys files on blob ids (ADR-042), which ADR-051 leaves alone; what it
    chunks and keys summaries on is EOL-free."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo / "m.py", SAMPLE)
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", "lf")
    _write(repo / "m.py", _crlf(SAMPLE))
    _git(repo, "commit", "-q", "-am", "crlf")
    lf, crlf = GitCommitSource(str(repo), "main~1"), GitCommitSource(str(repo), "main")
    try:
        assert lf.list(quiet=True)["m.py"] != crlf.list(quiet=True)["m.py"]   # blob ids
        assert _chunk_keys("m.py", crlf.read("m.py")) == _chunk_keys("m.py", lf.read("m.py"))
    finally:
        lf.close()
        crlf.close()


# ── an index ─────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _write(repo / "src" / "a.py", SAMPLE)
    _write(repo / "src" / "b.py", SAMPLE.replace("alpha", "omega"))
    index = tmp_path / "index"
    index.mkdir()
    embedded: list[str] = []

    def fake_embed(texts, batch_size=32):
        embedded.extend(texts)
        rng = np.random.default_rng(len(texts))
        return rng.standard_normal((len(texts), embed_dimension())).astype(np.float32)

    monkeypatch.setattr(core, "embed_batch", fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))
    return repo, str(index / "graph.db"), embedded


def _to_crlf(repo: Path) -> None:
    for path in (repo / "src").glob("*.py"):
        _write(path, _crlf(path.read_bytes().decode("utf-8")))


def test_an_index_built_from_lf_does_not_reprocess_crlf(env, capsys):
    repo, db_path, embedded = env
    ii.run_incremental(str(repo), interactive=False)
    assert embedded
    embedded.clear()
    _to_crlf(repo)
    ii.run_incremental(str(repo), interactive=False)
    assert embedded == []
    assert "Nothing changed" in capsys.readouterr().out


def _as_pre_adr051(db_path: str, repo: Path) -> None:
    """What an index built before ADR-051 holds: raw-byte MD5s, and no marker."""
    with CodeDB(db_path) as db:
        for (path,) in db._conn.execute("SELECT path FROM files").fetchall():
            db._conn.execute("UPDATE files SET content_hash = ? WHERE path = ?",
                             (md5_file_raw(str(repo / path)), path))
        db._conn.execute("DELETE FROM index_meta WHERE key = ?", (ii.FILE_KEY_KEY,))


def _stored(db_path: str) -> dict[str, str]:
    with CodeDB(db_path) as db:
        return dict(db._conn.execute("SELECT path, content_hash FROM files"))


def test_an_older_index_rekeys_once_and_not_again(env, monkeypatch, capsys):
    repo, db_path, embedded = env
    _to_crlf(repo)
    ii.run_incremental(str(repo), interactive=False)
    _as_pre_adr051(db_path, repo)
    assert _stored(db_path)["src/a.py"] != md5_file(str(repo / "src" / "a.py"))
    embedded.clear()
    capsys.readouterr()

    ii.run_incremental(str(repo), interactive=False)          # the upgrade run
    out = capsys.readouterr().out
    assert embedded == []                                     # re-keyed, not re-indexed
    assert "2 file(s) differed only in how line endings were hashed" in out
    assert _stored(db_path) == {p: md5_file(str(repo / p)) for p in ("src/a.py", "src/b.py")}
    with CodeDB(db_path) as db:
        assert db.meta_get(ii.FILE_KEY_KEY) == ii.FILE_KEY_VERSION

    calls = []
    monkeypatch.setattr(source_mod, "md5_file_raw", lambda p: calls.append(p) or "")
    _write(repo / "src" / "a.py", _crlf(SAMPLE.replace("x + 1", "x + 9")))
    ii.run_incremental(str(repo), interactive=False)          # the next run
    assert calls == []                                        # the old hash is never computed again
    assert any("x + 9" in t for t in embedded)                # a real edit is still indexed


def test_an_older_index_still_reindexes_a_real_edit(env):
    repo, db_path, embedded = env
    ii.run_incremental(str(repo), interactive=False)
    _as_pre_adr051(db_path, repo)
    _write(repo / "src" / "a.py", _crlf(SAMPLE.replace("x + 1", "x + 5")))
    embedded.clear()
    ii.run_incremental(str(repo), interactive=False)
    assert any("x + 5" in t for t in embedded)
    assert not any("omega" in t for t in embedded)            # b.py was LF: same key
    assert _stored(db_path)["src/a.py"] == md5_file(str(repo / "src" / "a.py"))
