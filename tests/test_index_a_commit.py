"""ADR-042: index a commit, not a folder.

Every test builds a real temporary git repository. The build tests use the same
deterministic fake embedder as test_cross_file_embed_batching, so no model loads.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np
import pytest

import config
import core
import incremental_indexer as ii
import index_location
import index_lock
from core import embed_dimension
from db import CodeDB
from source import GitCommitSource, GitError, WorkingTreeSource


def git(repo, *args, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, text=True, input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}"
    return done.stdout.strip()


def _write(path, text: str, *, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main: two TS files (one CRLF), an LFS pointer, a symlink and a submodule entry,
    and an indexer.toml in git mode (untracked, as InventoryApp keeps it)."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    _write(r / "src" / "a.ts", "export function alpha(x: number) {\n  return x + 1;\n}\n")
    _write(r / "src" / "b.ts", "import { alpha } from './a';\nexport const beta = () => alpha(2);\n",
           newline="\r\n")
    _write(r / "assets" / "big.ts",
           "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12345\n")
    git(r, "add", "src", "assets")
    # A symlink's blob is its target. An empty target is invalid on Linux, where git
    # checks the link out for real, so point it at a sibling.
    blob = git(r, "hash-object", "-w", "--stdin", stdin="a.ts")
    git(r, "update-index", "--add", "--cacheinfo", f"120000,{blob},src/link.ts")
    head_like = "1" * 40
    git(r, "update-index", "--add", "--cacheinfo", f"160000,{head_like},vendor/sub")
    git(r, "commit", "-q", "-m", "init")
    (r / "indexer.toml").write_text('[indexer]\nsource = "git:main"\n', encoding="utf-8")
    monkeypatch.chdir(r)
    index_location.reset()
    config.reset_config_cache()
    yield r
    index_location.reset()
    config.reset_config_cache()


# ── the source ───────────────────────────────────────────────────────────────

def test_list_is_the_commits_tree_with_blob_shas(repo):
    src = GitCommitSource(str(repo), "main")
    listed = src.list(quiet=True)
    assert set(listed) == {"src/a.ts", "src/b.ts", "assets/big.ts"}   # no symlink, no submodule
    assert listed["src/a.ts"] == git(repo, "rev-parse", "main:src/a.ts")
    src.close()


def test_read_normalizes_line_endings_and_skips_lfs_pointers(repo):
    src = GitCommitSource(str(repo), "main")
    assert "\r" not in src.read("src/b.ts")
    assert src.read("src/b.ts").startswith("import { alpha }")
    assert src.read("assets/big.ts") == ""
    assert src.exists("src/a.ts") and not src.exists("src/nope.ts")
    with pytest.raises(FileNotFoundError):
        src.read("src/nope.ts")
    src.close()


def test_the_working_tree_is_ignored(repo):
    (repo / "src" / "a.ts").write_text("export const edited = 1;\n", encoding="utf-8")
    (repo / "src" / "new.ts").write_text("export const untracked = 1;\n", encoding="utf-8")
    src = GitCommitSource(str(repo), "main")
    assert "src/new.ts" not in src.list(quiet=True)
    assert "alpha" in src.read("src/a.ts")
    src.close()


def test_the_ref_is_resolved_once_per_source(repo):
    src = GitCommitSource(str(repo), "main")
    first = src.commit
    _write(repo / "src" / "c.ts", "export const c = 3;\n")
    git(repo, "add", "src/c.ts")
    git(repo, "commit", "-q", "-m", "c")
    assert src.commit == first and "src/c.ts" not in src.list(quiet=True)
    assert "src/c.ts" in GitCommitSource(str(repo), "main").list(quiet=True)
    src.close()


def test_an_unknown_ref_fails_clearly(repo):
    with pytest.raises(GitError, match="not a commit"):
        GitCommitSource(str(repo), "origin/nope")


# ── where the index lives, and linked worktrees ──────────────────────────────

def test_the_index_lives_in_the_git_common_dir_from_every_worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    expected = os.path.normcase(os.path.join(str(repo), ".git", "code-index"))
    assert os.path.normcase(index_location.resolve_index_dir(str(repo))) == expected
    # The worktree has no indexer.toml of its own; it reads the main worktree's.
    assert config.find_config_path(str(wt)) == str(repo / "indexer.toml")
    assert os.path.normcase(index_location.resolve_index_dir(str(wt))) == expected


def test_git_mode_worktrees_are_not_refused(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    assert index_lock.linked_worktree(str(wt))
    assert index_lock.worktree_refusal(str(wt)) is None


def test_worktree_mode_is_unchanged(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert index_location.resolve_index_dir(str(plain)) == ".code-index"


# ── a build ──────────────────────────────────────────────────────────────────

def _fake_embed(texts, batch_size=32):
    dim = embed_dimension()
    return np.array([[float((hash(t) >> (8 * i)) & 0xFF) for i in range(dim)] for t in texts],
                    dtype=np.float32)


@pytest.fixture
def build(repo, monkeypatch):
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)
    index = index_location.resolve_index_dir(str(repo))
    monkeypatch.setattr(ii, "INDEX_DIR", index)
    monkeypatch.setattr(ii, "DB_PATH", os.path.join(index, "graph.db"))

    def run():
        ii.run_incremental(str(repo), interactive=False)
        with CodeDB(os.path.join(index, "graph.db")) as db:
            files = dict(db._conn.execute("SELECT path, content_hash FROM files").fetchall())
            meta = {k: db.meta_get(k) for k in ("last_indexed_commit", "source")}
        return files, meta
    return run


def test_a_build_indexes_the_commit_into_the_common_dir(repo, build):
    files, meta = build()
    assert set(files) == {"src/a.ts", "src/b.ts", "assets/big.ts"}
    assert files["src/a.ts"] == git(repo, "rev-parse", "main:src/a.ts")
    assert meta == {"last_indexed_commit": git(repo, "rev-parse", "main"), "source": "git:main"}
    assert os.path.exists(repo / ".git" / "code-index" / "graph.db")
    assert not os.path.exists(repo / ".code-index")


def test_edits_and_checkouts_change_nothing_until_the_ref_moves(repo, build):
    build()
    (repo / "src" / "a.ts").write_text("export const edited = 1;\n", encoding="utf-8")
    git(repo, "checkout", "-q", "-b", "elsewhere")
    files, meta = build()
    assert files["src/a.ts"] == git(repo, "rev-parse", "main:src/a.ts")
    assert meta["last_indexed_commit"] == git(repo, "rev-parse", "main")


def test_moving_the_ref_reindexes_only_the_diff(repo, build, monkeypatch):
    build()
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "--", ".")
    _write(repo / "src" / "a.ts", "export function alpha(x: number) {\n  return x + 2;\n}\n")
    git(repo, "rm", "-q", "src/b.ts")
    git(repo, "add", "src/a.ts")
    git(repo, "commit", "-q", "-m", "change a, drop b")
    seen = []
    real = ii._prepare_file
    monkeypatch.setattr(ii, "_prepare_file", lambda **kw: seen.append(kw["rel_path"]) or real(**kw))
    files, meta = build()
    assert seen == ["src/a.ts"]
    assert set(files) == {"src/a.ts", "assets/big.ts"}
    assert meta["last_indexed_commit"] == git(repo, "rev-parse", "main")


def test_a_ref_poller_schedules_a_reindex_only_when_the_ref_moved(repo, build, monkeypatch):
    import MCPServer
    build()

    class Debouncer:
        def __init__(self):
            self.calls = []

        def schedule(self, delay=None):
            self.calls.append(delay)

    d = Debouncer()
    poller = MCPServer._RefPoller(str(repo), "main", 60.0, d)
    assert poller.check() is False and d.calls == []
    git(repo, "commit", "-q", "--allow-empty", "-m", "moves main")
    assert poller.check() is True and d.calls == [0.0]


def test_folder_source_reads_what_it_always_read(tmp_path):
    folder = tmp_path / "f"
    _write(folder / "x.py", "def x():\n    return 1\n", newline="\r\n")
    src = WorkingTreeSource(str(folder))
    listed = src.list(quiet=True)
    assert set(listed) == {"x.py"}
    assert src.read("x.py") == "def x():\n    return 1\n"


# ── the server in git mode ───────────────────────────────────────────────────

def test_instructions_name_the_ref_and_the_commit(repo, build):
    import MCPServer
    build()
    text = MCPServer._server_instructions(str(repo))
    commit = git(repo, "rev-parse", "main")
    assert "main" in text and commit[:10] in text
    assert f"git diff --name-only {commit[:10]}...HEAD" in text
    assert "working tree" in text and len(text) < 1000


@pytest.mark.skipif(not __import__("MCPServer")._WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_the_watchdog_follows_the_ref_instead_of_files(repo, build, monkeypatch):
    import MCPServer
    build()
    started = MCPServer.start_watchdog(str(repo))
    try:
        assert isinstance(started, MCPServer._RefPoller)
        assert started.ref == "main"
        assert os.path.exists(repo / ".git" / "code-index" / "watch.lock")
    finally:
        started.stop()
        for obj, lock in list(MCPServer._observers):
            if obj is started:
                lock.release()
                MCPServer._observers.remove((obj, lock))


def test_an_indexer_toml_saved_with_a_bom_still_loads(tmp_path):
    (tmp_path / "indexer.toml").write_bytes(b'\xef\xbb\xbf[indexer]\nsource = "git:main"\n')
    assert config.index_source(str(tmp_path)) == "git:main"


def test_switching_source_keeps_content_dates_from_git_history(repo, build, monkeypatch):
    """A worktree-mode index switched to git mode re-hashes every file (MD5 -> blob
    id). That is not a content change, so the dates come from git history, not now."""
    (repo / "indexer.toml").write_text("[indexer]\n", encoding="utf-8")
    config.reset_config_cache()
    index = str(repo / ".git" / "code-index")
    monkeypatch.setattr(ii, "INDEX_DIR", ".code-index")
    monkeypatch.setattr(ii, "DB_PATH", os.path.join(".code-index", "graph.db"))
    ii.run_incremental(str(repo), interactive=False)

    # Stale the folder index's dates, as if built long before, then switch.
    with CodeDB(os.path.join(".code-index", "graph.db")) as db:
        db._conn.execute("UPDATE files SET content_changed_at = '2000-01-01T00:00:00Z'")
        db._conn.commit()
    import shutil
    shutil.copytree(repo / ".code-index", index)
    (repo / "indexer.toml").write_text('[indexer]\nsource = "git:main"\n', encoding="utf-8")
    config.reset_config_cache()
    monkeypatch.setattr(ii, "INDEX_DIR", index)
    monkeypatch.setattr(ii, "DB_PATH", os.path.join(index, "graph.db"))
    ii.run_incremental(str(repo), interactive=False)

    history = ii.git_change_times(str(repo), git(repo, "rev-parse", "main"))
    with CodeDB(os.path.join(index, "graph.db")) as db:
        stamps = dict(db._conn.execute("SELECT path, content_changed_at FROM files"))
    assert stamps["src/a.ts"] == history["src/a.ts"][0]
    assert stamps["src/b.ts"] == history["src/b.ts"][0]


def test_an_alias_ref_is_followed_and_shown_with_its_target(repo, build):
    """GanttWebApp indexes refs/code-index/target, re-pointed at each staging branch."""
    import MCPServer
    git(repo, "branch", "staging-1")
    git(repo, "symbolic-ref", "refs/code-index/target", "refs/heads/staging-1")
    (repo / "indexer.toml").write_text('[indexer]\nsource = "git:refs/code-index/target"\n',
                                       encoding="utf-8")
    config.reset_config_cache()
    build()
    assert index_location.ref_display(str(repo), "refs/code-index/target") == \
        "refs/code-index/target (→ staging-1)"
    assert index_location.ref_display(str(repo), "main") == "main"
    assert "(→ staging-1)" in MCPServer._server_instructions(str(repo))
