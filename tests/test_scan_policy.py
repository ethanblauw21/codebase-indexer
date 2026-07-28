"""
test_scan_policy.py — the `[ignore]` config contract (ADR-026 §1, §2, §6, commit 2).

`indexer.toml` shipped an `[ignore]` block that nothing read, for long enough that its
`extensions` list had rotted to 5 entries against a real set of 11 — so wiring it
naively would have silently killed C#/C++ Tier-A indexing. This file pins the contract
that makes it live: what extends, what replaces, what raises, and where the scan is
anchored.

TOML parsing and string comparison only — no model load, no embedding, no GPU.
"""
from __future__ import annotations

import os

import pytest

import scan_policy as sp

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_cache():
    """The policy is cached per root; every case here writes its own config."""
    sp.reset()
    yield
    sp.reset()


def _repo(tmp_path, toml_body: str | None = None) -> str:
    """A repo root, optionally carrying an `indexer.toml`."""
    if toml_body is not None:
        (tmp_path / "indexer.toml").write_text(toml_body, encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_no_config_yields_the_built_in_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.ignore_dirs == sp.DEFAULT_IGNORE_DIRS
    assert policy.ignore_root_dirs == sp.DEFAULT_IGNORE_ROOT_DIRS
    assert policy.extensions == sp.DEFAULT_INDEXABLE_EXTS
    assert policy.config_path is None


def test_git_and_index_dir_are_excluded_unconditionally(tmp_path):
    """Not knobs: indexing `.git` embeds packfiles; indexing the index recurses."""
    policy = sp.scan_policy(_repo(tmp_path, "[ignore]\ndirs = []\n"))
    assert policy.ignore_dirs == frozenset()          # the config was honored...
    assert policy.is_ignored_dir(".git")              # ...and these still hold
    assert policy.is_ignored_dir(".code-index")


# ---------------------------------------------------------------------------
# Precedence: extra_* extends, the bare key replaces
# ---------------------------------------------------------------------------

def test_extra_key_extends_the_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_dirs = ["vendor"]\n'))
    assert "vendor" in policy.ignore_dirs
    assert "node_modules" in policy.ignore_dirs        # defaults survive


def test_bare_key_replaces_the_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\ndirs = ["vendor"]\n'))
    assert policy.ignore_dirs == frozenset({"vendor"})
    assert "node_modules" not in policy.ignore_dirs    # deliberately discarded


def test_setting_both_spellings_raises_and_names_both_keys(tmp_path):
    body = '[ignore]\ndirs = ["a"]\nextra_dirs = ["b"]\n'
    with pytest.raises(ValueError) as exc:
        sp.scan_policy(_repo(tmp_path, body))
    assert "dirs" in str(exc.value) and "extra_dirs" in str(exc.value)


def test_empty_extra_list_is_a_no_op(tmp_path):
    """Honored as written — and "written" here means "change nothing"."""
    policy = sp.scan_policy(_repo(tmp_path, "[ignore]\nextra_dirs = []\n"))
    assert policy.ignore_dirs == sp.DEFAULT_IGNORE_DIRS


def test_empty_bare_list_excludes_nothing(tmp_path):
    """Legal, destructive, and exactly what someone asking for it asked for."""
    policy = sp.scan_policy(_repo(tmp_path, "[ignore]\ndirs = []\n"))
    assert policy.ignore_dirs == frozenset()
    assert not policy.is_ignored_dir("node_modules")


@pytest.mark.parametrize("key,attr", [
    ("dirs", "ignore_dirs"),
    ("root_dirs", "ignore_root_dirs"),
    ("extensions", "extensions"),
])
def test_precedence_applies_to_every_knob(tmp_path, key, attr):
    """All three knobs share one resolver — none of them is special-cased."""
    policy = sp.scan_policy(_repo(tmp_path, f"[ignore]\n{key} = []\n"))
    assert getattr(policy, attr) == frozenset()


# ---------------------------------------------------------------------------
# Type validation — a config error must fail at load, not at scan time
# ---------------------------------------------------------------------------

def test_a_bare_string_raises_instead_of_iterating_characters(tmp_path):
    """`dirs = "foo"` would otherwise resolve to {"f", "o"} and exclude nothing."""
    with pytest.raises(ValueError, match="list of strings"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\ndirs = "foo"\n'))


def test_non_string_list_entries_raise(tmp_path):
    with pytest.raises(ValueError, match="list of strings"):
        sp.scan_policy(_repo(tmp_path, "[ignore]\ndirs = [1, 2]\n"))


def test_extensions_must_carry_a_leading_dot(tmp_path):
    """`extensions = ["rs"]` matches nothing; failing loudly beats indexing zero files."""
    with pytest.raises(ValueError, match="start with a dot"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_extensions = ["rs"]\n'))


def test_extensions_are_lower_cased(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_extensions = [".RS"]\n'))
    assert ".rs" in policy.extensions
    assert policy.is_indexable("src/main.rs")


# ---------------------------------------------------------------------------
# Root-only vs any-depth
# ---------------------------------------------------------------------------

def test_root_only_exclusions_do_not_match_at_depth(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_root_dirs = ["benchmarks"]\n'))
    assert not policy.is_indexable("benchmarks/corpus/click/api.py")
    assert policy.is_indexable("src/benchmarks/timing.py")
    assert policy.is_ignored_dir("benchmarks", at_root=True)
    assert not policy.is_ignored_dir("benchmarks", at_root=False)


def test_any_depth_exclusions_match_anywhere(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert not policy.is_indexable("node_modules/pkg/index.js")
    assert not policy.is_indexable("src/deep/node_modules/pkg/index.js")


# ---------------------------------------------------------------------------
# is_indexable vs is_scannable
# ---------------------------------------------------------------------------

def test_project_descriptors_are_scannable_but_not_indexable(tmp_path):
    """`.csproj` is parsed for edges and never chunked — one predicate each."""
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.is_scannable("src/App.csproj")
    assert not policy.is_indexable("src/App.csproj")
    assert policy.is_scannable("compile_commands.json")
    assert not policy.is_indexable("compile_commands.json")


def test_non_source_files_are_neither(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert not policy.is_scannable("README.md")
    assert not policy.is_indexable("README.md")


# ---------------------------------------------------------------------------
# §6 — anchoring
# ---------------------------------------------------------------------------

def test_the_policy_is_anchored_to_the_config_directory(tmp_path):
    """Resolved from `src/`, the policy still reports the repo root as its root."""
    root = _repo(tmp_path, '[ignore]\nextra_root_dirs = ["benchmarks"]\n')
    src = os.path.join(root, "src")
    os.makedirs(src)
    policy = sp.scan_policy(src)
    assert policy.root == root
    assert "benchmarks" in policy.ignore_root_dirs


def test_a_scan_rooted_below_the_config_is_refused(tmp_path):
    """§6: silently resolving root-only exclusions against `src/` is the failure."""
    import incremental_indexer as ii

    root = _repo(tmp_path, '[ignore]\nextra_root_dirs = ["benchmarks"]\n')
    src = os.path.join(root, "src")
    os.makedirs(src)
    with pytest.raises(ValueError, match="not the directory holding"):
        ii.scan_disk(src)


def test_the_config_walk_stops_at_a_git_boundary(tmp_path):
    """A stray `indexer.toml` above the repo must not decide what gets deleted."""
    outer = tmp_path / "outer"
    inner = outer / "repo"
    inner.mkdir(parents=True)
    (outer / "indexer.toml").write_text('[ignore]\ndirs = ["should-not-leak"]\n', encoding="utf-8")
    (inner / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")   # worktree-style file

    policy = sp.scan_policy(str(inner))
    assert policy.config_path is None
    assert policy.ignore_dirs == sp.DEFAULT_IGNORE_DIRS


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Verification against this repository — the incident, as a test
# ---------------------------------------------------------------------------

def test_this_repo_scans_to_source_only():
    """The motivating incident, pinned (ADR-026 §Context).

    A CPU reindex on 2026-07-27 immediately began embedding
    `benchmarks/real_repo/corpus/click/…` — the cloned CoIR eval corpora, 510 of this
    tree's 613 indexable files. An unpatched run produces an index that is ~83%
    third-party corpus.

    Asserted structurally rather than as a magic total: the numbers move every time
    someone adds a file, and a test that has to be edited on every commit stops being
    read. What must not move is *which trees are in scope*.
    """
    import incremental_indexer as ii

    sp.reset()
    try:
        scanned = ii.scan_disk(_REPO_ROOT, quiet=True)
    finally:
        sp.reset()

    tops = {p.split("/")[0] for p in scanned}
    assert tops == {"src", "tests", "tools"}, f"unexpected trees in scope: {tops - {'src', 'tests', 'tools'}}"
    assert not any(p.startswith("benchmarks/") for p in scanned)
    assert "src/scan_policy.py" in scanned
    assert len(scanned) < 200, (
        f"{len(scanned)} files in scope — the corpus is back in the index"
    )


def test_the_shipped_config_is_loadable_and_anchored():
    """The file this repo ships must itself satisfy the contract it documents."""
    sp.reset()
    try:
        policy = sp.scan_policy(_REPO_ROOT)
        assert policy.root == _REPO_ROOT
        assert policy.config_path == os.path.join(_REPO_ROOT, "indexer.toml")
        assert {"benchmarks", "gpu-crash-repro", "graphify-out"} <= policy.ignore_root_dirs
        assert policy.ignore_dirs == sp.DEFAULT_IGNORE_DIRS      # defaults not replaced
        assert policy.extensions == sp.DEFAULT_INDEXABLE_EXTS
    finally:
        sp.reset()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_reset_drops_the_cache(tmp_path):
    root = _repo(tmp_path, '[ignore]\nextra_dirs = ["first"]\n')
    assert "first" in sp.scan_policy(root).ignore_dirs

    (tmp_path / "indexer.toml").write_text('[ignore]\nextra_dirs = ["second"]\n', encoding="utf-8")
    assert "first" in sp.scan_policy(root).ignore_dirs      # still cached
    sp.reset()
    assert "second" in sp.scan_policy(root).ignore_dirs
