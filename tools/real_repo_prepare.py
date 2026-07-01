#!/usr/bin/env python3
"""ADR-019 — prepare the real-repo eval corpus (clone at SHA + build the real index).

For every repo in ``benchmarks/real_repo/repos.toml`` this:
  1. clones the repo at its **exact pinned SHA** into a git-ignored working dir
     (``benchmarks/real_repo/corpus/<name>``), and
  2. builds the **production** index (``src/incremental_indexer.py``) into a
     git-ignored per-repo index dir (``benchmarks/real_repo/index/<name>``), with
     chunk summarization OFF — the eval grades retrieval, not summaries.

Only the manifest, the hand-authored fixtures, and the baseline are committed; the
cloned source and built indexes are regenerable from the SHAs and thus git-ignored.
Pinning by SHA is what makes the eventual scorecard reproducible and auditable —
anyone can clone the same code and re-grade (ADR-019 §1, the moat's inspectable number).

Usage:
    python tools/real_repo_prepare.py                 # prepare every repo (skip cached)
    python tools/real_repo_prepare.py --only spdlog   # just one
    python tools/real_repo_prepare.py --force          # re-clone + re-index from scratch
    python tools/real_repo_prepare.py --list           # show manifest + prepared state, no work
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib

# The production indexer lives in src/; import it the same way coir_eval.py does.
_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_REAL = os.path.join(_ROOT, "benchmarks", "real_repo")
_MANIFEST = os.path.join(_REAL, "repos.toml")
_CORPUS = os.path.join(_REAL, "corpus")
_INDEX = os.path.join(_REAL, "index")
_PREPARED = os.path.join(_INDEX, "PREPARED.json")

sys.path.insert(0, os.path.join(_ROOT, "src"))

# The indexer prints box-drawing/arrow glyphs; force UTF-8 so a cp1252 console
# (Windows default) doesn't crash the run on the first '━'.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _on_rm_error(func, path, exc_info):
    """rmtree onerror: clear the read-only bit git sets on pack files, then retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path):
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_on_rm_error)


def _git(args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def load_manifest(path=_MANIFEST):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    repos = data.get("repos", [])
    if not repos:
        raise SystemExit(f"No [[repos]] entries in {path}")
    return repos


def _head_sha(repo_dir):
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None
    try:
        return _git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def ensure_clone(repo, force=False):
    """Clone ``repo`` at its pinned SHA into corpus/<name>; return (status, dir)."""
    dest = os.path.join(_CORPUS, repo["name"])
    sha = repo["sha"]

    if not force and _head_sha(dest) == sha:
        return "cached", dest

    _rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    # Fetch the exact commit directly — GitHub allows fetching a SHA that isn't a
    # ref tip (allowAnySHA1InWant), so a depth-1 fetch pins precisely and cheaply.
    _git(["init", "-q"], cwd=dest)
    _git(["remote", "add", "origin", repo["url"]], cwd=dest)
    fetched = _git(["fetch", "-q", "--depth", "1", "origin", sha], cwd=dest, check=False)
    if fetched.returncode != 0:
        # Fallback: some servers refuse arbitrary-SHA fetches — pull the tag's
        # history (full depth) and check the commit out from it.
        _git(["fetch", "-q", "--tags", "origin", repo.get("tag", "")], cwd=dest, check=False)
        _git(["fetch", "-q", "origin"], cwd=dest, check=False)
        _git(["checkout", "-q", sha], cwd=dest)
    else:
        _git(["checkout", "-q", "FETCH_HEAD"], cwd=dest)

    got = _head_sha(dest)
    if got != sha:
        raise SystemExit(
            f"[{repo['name']}] checkout SHA mismatch: wanted {sha}, got {got}"
        )
    return "cloned", dest


def build_index(repo, corpus_dir, force=False):
    """Index ``corpus_dir`` with the production indexer into index/<name>; return stats."""
    import incremental_indexer as ii
    from db import CodeDB

    index_dir = os.path.join(_INDEX, repo["name"])
    if force:
        _rmtree(index_dir)
    os.makedirs(index_dir, exist_ok=True)

    # The indexer reads INDEX_DIR/DB_PATH/ENABLE_SUMMARIZATION as module globals at
    # call time (run_incremental takes only repo_path). Point them at this repo's
    # git-ignored index dir and disable summarization for the eval build.
    ii.INDEX_DIR = index_dir
    ii.DB_PATH = os.path.join(index_dir, "graph.db")
    ii.ENABLE_SUMMARIZATION = False

    ii.run_incremental(repo_path=corpus_dir)

    with CodeDB(ii.DB_PATH) as db:
        return db.stats(), index_dir


def _load_prepared():
    if os.path.exists(_PREPARED):
        try:
            with open(_PREPARED, encoding="utf-8") as f:
                return {r["name"]: r for r in json.load(f)}
        except Exception:
            return {}
    return {}


def _save_prepared(records):
    os.makedirs(_INDEX, exist_ok=True)
    ordered = sorted(records.values(), key=lambda r: r["name"])
    with open(_PREPARED, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2)
        f.write("\n")


def cmd_symbols(repo, kind=None, grep=None):
    """Dump every indexed symbol FQN for one repo — the authoring aid for fixtures.

    The retriever returns chunks whose ``scope`` IS ``symbols.fqn`` (``<relpath>::<sym>``),
    so gold entries must be these exact strings. Grep/kind filters narrow the list while
    hunting for a specific symbol to author gold against.
    """
    from db import CodeDB

    index_dir = os.path.join(_INDEX, repo["name"])
    db_path = os.path.join(index_dir, "graph.db")
    if not os.path.exists(db_path):
        raise SystemExit(f"{repo['name']} not prepared yet — run prepare first ({db_path})")

    rows = []
    with CodeDB(db_path) as db:
        cur = db._conn.execute(
            "SELECT s.fqn, s.kind, f.path FROM symbols s "
            "JOIN files f ON f.id = s.file_id ORDER BY f.path, s.start_line"
        )
        for fqn, k, path in cur.fetchall():
            if kind and k != kind:
                continue
            if grep and grep.lower() not in fqn.lower():
                continue
            rows.append((fqn, k, path))

    for fqn, k, _ in rows:
        print(f"{k:<12} {fqn}")
    print(f"\n{len(rows)} symbol(s) in {repo['name']}"
          + (f" (kind={kind})" if kind else "")
          + (f" (grep={grep!r})" if grep else ""))


def cmd_list(repos):
    prepared = _load_prepared()
    print(f"{'repo':<12} {'lang':<12} {'sha':<12} {'indexed':<8} symbols/chunks/edges")
    print("-" * 68)
    for r in repos:
        p = prepared.get(r["name"])
        state = "yes" if p else "no"
        counts = (f"{p['symbols']}/{p['chunks']}/{p['edges']}" if p else "-")
        print(f"{r['name']:<12} {r['language']:<12} {r['sha'][:12]:<12} "
              f"{state:<8} {counts}")


def main():
    ap = argparse.ArgumentParser(description="Clone + index the ADR-019 real-repo corpus.")
    ap.add_argument("--only", metavar="NAME", help="prepare a single repo by name")
    ap.add_argument("--force", action="store_true",
                    help="re-clone and re-index from scratch (ignore cached state)")
    ap.add_argument("--manifest", default=_MANIFEST, help="path to repos.toml")
    ap.add_argument("--list", action="store_true",
                    help="print manifest + prepared state and exit (no work)")
    ap.add_argument("--symbols", metavar="NAME",
                    help="dump indexed symbol FQNs for one repo (fixture authoring aid)")
    ap.add_argument("--kind", help="with --symbols: filter by symbol kind (e.g. function, class)")
    ap.add_argument("--grep", help="with --symbols: filter FQNs containing this substring")
    args = ap.parse_args()

    all_repos = load_manifest(args.manifest)

    if args.list:
        cmd_list(all_repos)
        return

    if args.symbols:
        match = [r for r in all_repos if r["name"] == args.symbols]
        if not match:
            raise SystemExit(f"No repo named {args.symbols!r} in the manifest")
        cmd_symbols(match[0], kind=args.kind, grep=args.grep)
        return

    repos = all_repos
    if args.only:
        repos = [r for r in repos if r["name"] == args.only]
        if not repos:
            raise SystemExit(f"No repo named {args.only!r} in the manifest")

    prepared = _load_prepared()
    for repo in repos:
        name = repo["name"]
        print(f"\n══ {name} ({repo['language']}) @ {repo['sha'][:12]} ══")
        clone_status, corpus_dir = ensure_clone(repo, force=args.force)
        print(f"  clone: {clone_status} -> {os.path.relpath(corpus_dir, _ROOT)}")
        stats, index_dir = build_index(repo, corpus_dir, force=args.force)
        print(f"  index: files={stats['files']} symbols={stats['symbols']} "
              f"chunks={stats['chunks']} edges={stats['edges']}")
        prepared[name] = {
            "name": name,
            "language": repo["language"],
            "sha": repo["sha"],
            "corpus": os.path.relpath(corpus_dir, _ROOT).replace("\\", "/"),
            "index_dir": os.path.relpath(index_dir, _ROOT).replace("\\", "/"),
            "files": stats["files"],
            "symbols": stats["symbols"],
            "chunks": stats["chunks"],
            "edges": stats["edges"],
        }
        _save_prepared(prepared)

    print(f"\nPrepared {len(repos)} repo(s). Manifest of built indexes: "
          f"{os.path.relpath(_PREPARED, _ROOT)}")


if __name__ == "__main__":
    main()
