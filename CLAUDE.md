# CLAUDE.md

Repo-wide guidance. Code-specific rules (config drift, scan gate, MCP tools) are in `src/CLAUDE.md`, which loads when you work under `src/`; reference detail is in `docs/agents/`.

## Reference Documents

- **Contribution Standards:** read `CONTRIBUTING.md` before starting any new work (minor vs major classification, branch naming, ADR requirements, bug/feature tracking, PR process).
- **Docs map:** `docs/README.md` (what every document in `docs/` is for).

## Working Lists

Three documents, three jobs; do not let one hold another's content (`CONTRIBUTING.md` section 4):

- `docs/backlog.md`: **wants.** Problems and requests, `B-NNN`, with a source and a date.
- `docs/roadmap.md`: **order.** Sequencing and dependency edges. Start here to find what's next.
- `docs/adr/`: **decisions.** One committed solution per file, plus its Implementation Log.

**A backlog item asserts a problem; an ADR asserts a solution you are building.** New wants go to the backlog, never to a new `proposed` ADR on `master`.

## Working Style

- For non-trivial new features or significant design decisions, use `/grill-plan` to produce an ADR before implementing.
- Major changes are any changes to `src/`. They require a branch, an ADR, and a PR. See `CONTRIBUTING.md`.
- **The ADR is born on its branch**: created in the branch's first commit, `accepted` when it reaches `master`. Nothing is ever proposed on `master`.

## Install, run, test

```bash
pip install -e .          # exposes code-indexer and code-indexer-serve (or: pip install -r requirements.txt)
code-indexer              # rebuild the index (incremental by default)
code-indexer-serve        # MCP server with watchdog auto-reindex
python -m pytest tests/ -v   # pyproject sets pythonpath = ["src"]
```

Direct entry points: `python src/MCPServer.py`, `python src/incremental_indexer.py`. `tests/test_stable_id.py` has the golden fixtures for the 60-bit ID formula.

Retrieval eval (needs a built index; run `code-indexer` first): `python tools/eval_retrieval.py [--index-dir .code-index] [--verbose]` compares tier-1-only against three-tier RRF fusion over a fixed 10-query set (MRR@5, Hit@{1,3,5}). It exits 1 if three-tier degrades quality: surface that, do not revert silently (ADR-002 H3).

