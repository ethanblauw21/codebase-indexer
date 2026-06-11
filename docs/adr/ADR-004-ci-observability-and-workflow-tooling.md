# ADR-004: CI Observability, PR Automation, and Commit Traceability

**Status:** proposed
**Date:** 2026-06-11
**Branch:** `chore/adr-004-ci-tooling`
**Reviewer:** @ethanblauw21

## Context

Three friction points in the current development loop:

1. **CI failures require log spelunking.** The `test` job doesn't exist yet in CI — only `lint` (flake8) runs. When tests and mutation testing are added, failed assertions and survived mutants would be buried in raw log lines. Diagnosing a broken build requires scrolling rather than reading a single summary.

2. **PR template is blank on every PR.** `.github/pull_request_template.md` has a well-structured checklist and ADR Implementation Log reference, but every PR starts from a blank form. The branch name, linked ADR, diff stats, and completed implementation log items are all knowable at PR-creation time — they just aren't surfaced.

3. **Commit messages don't carry their ADR reference.** Branch naming (`feature/adr-XXX-*`) already encodes the ADR number, but that context doesn't propagate into the commit messages themselves. Finding which commits belong to ADR-003, for example, requires cross-referencing branch names or grep-searching commit bodies — not `git log --grep`.

The homelab repo validated the commit-tagging technique (audit-id-in-commit-message, ADR-006 there). This ADR formalises the same pattern here, using ADR numbers as the durable identifier instead of FAISS vector IDs (which churn on reindex and are meaningless in `git log`).

## Decision

Ship three tooling changes as a single `chore/` branch:

**1. CI Failure Summary** — Add a `test` job to `.github/workflows/ci.yml` that runs pytest and mutmut, then writes a structured GitHub Step Summary (`$GITHUB_STEP_SUMMARY`) with failed assertions and survived mutants. Enforce mutation score thresholds: ≥ 90 = pass, 80–89 = warning, < 80 = fail.

**2. Human Review Pack** — Add `tools/gen_pr_body.py`: a script that reads the current branch name, resolves the linked ADR, and prints a pre-filled PR template body to stdout. Output follows the existing `.github/pull_request_template.md` structure exactly — it augments that seam, not a parallel artifact.

**3. Semantic Commit Tagging** — Add `.githooks/commit-msg`: a versioned hook that appends `[ADR-XXX src/foo.py]` to commit messages on ADR branches. ADR number from branch name; source-file paths from the staged diff. Activated per-clone with `git config core.hooksPath .githooks`.

## Design Details

### Mutation testing tool

The repo is Python; Stryker targets JavaScript/TypeScript. **`mutmut`** is the Python equivalent — identical semantics (generate mutations, run tests, report survivors). Initial scope: `src/stable_id.py`, `src/ast_chunker.py`, `src/incremental_indexer.py` — the three source modules that the current test suite meaningfully guards. The `--paths-to-mutate` list grows as test coverage expands to `hybrid_retriever.py`, `db.py`, and `core.py`.

### CI job structure

```yaml
test:
  name: Test + Mutate
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "3.12" }
    - run: pip install -e ".[dev]"
    - name: Run pytest
      run: pytest --tb=short -q 2>&1 | tee pytest_output.txt
      continue-on-error: true
    - name: Run mutmut
      run: |
        mutmut run \
          --paths-to-mutate src/stable_id.py,src/ast_chunker.py,src/incremental_indexer.py \
          --runner "python -m pytest tests/ -x -q --no-header --timeout=30"
      continue-on-error: true
    - name: Write CI summary and enforce thresholds
      if: always()
      run: python tools/ci_summary.py --threshold-high 90 --threshold-low 80
```

`continue-on-error: true` on each test step ensures the summary step always runs. The summary step sets exit code 1 if score < 80.

### Step Summary format (`tools/ci_summary.py`)

```markdown
## CI Failure Summary

### Test Results
> 3 failed, 47 passed

| Test | Failure |
|------|---------|
| test_stable_id.py::test_formula_matches_reference[...] | AssertionError: 12345 != 67890 |

### Mutation Score: 87% ⚠️ (threshold: 90 high / 80 low)

| File | Line | Survived Mutation |
|------|------|-------------------|
| src/stable_id.py | 45 | `return int(..., 16)` → `return int(..., 10)` |
```

Score badge: ✅ ≥ 90, ⚠️ 80–89, ❌ < 80. On local runs (no `$GITHUB_STEP_SUMMARY` env var), output goes to stdout.

### gen_pr_body.py logic

1. `git symbolic-ref --short HEAD` → branch name
2. Regex `adr-(\d+)` → ADR number; glob `docs/adr/ADR-XXX-*.md`
3. Parse ADR: Context first sentence → "What changed"; Decision section → "Implementation notes"; Implementation Log items (checked/unchecked) → "ADR Implementation Log delta" footer
4. `git diff --stat origin/master...HEAD` → diff stat
5. `git diff --name-only origin/master...HEAD` → detect Major (any `src/` file) for type-of-change checkbox
6. Heuristic: for each `[ ]` log item, check if any changed `src/` filename appears in the item text → mark as "likely closed by this diff"
7. Print filled template to stdout

Graceful fallback: branches with no ADR pattern emit a minimal template with just the diff stat.

### Commit-msg hook format

```
Add C++ adapter with header/impl unification [ADR-003 src/ast_chunker.py src/core.py]
```

Multi-line commits get the tag appended as a footer after a blank line. Single-line commits get it inline. Hook skips if `[ADR-` is already present (no double-tagging). Hook skips entirely on non-ADR branches (`chore/*`, `fix/short-name`, etc.).

`git log --oneline --grep='\[ADR-003\]'` becomes the canonical way to find all commits for a given ADR.

## Consequences

**Better:**
- Failed assertions and survived mutants are one paste from a GitHub build URL — no raw log scroll.
- PRs open with diff context, the linked ADR decision, and completed implementation log items already filled in.
- `git log` becomes self-documenting: every commit on a feature branch carries its ADR reference and the files it touched.
- `git log --grep='\[ADR-XXX\]'` gives an instant full commit trail for any ADR.

**Worse:**
- `pip install -e ".[dev]"` in CI adds `pytest` + `mutmut` + `pytest-timeout` to the install step (~30s cold, cached on warm runs).
- Mutmut adds significant CI wall-clock time (each mutation reruns the full test suite). Initial scope is narrow (3 files) to bound this; may need a scheduled/nightly variant if duration grows above ~10 min.
- Developers must run `git config core.hooksPath .githooks` once per clone to activate commit tagging. Not automatic.

**Neutral:**
- None of these changes touch `src/` — no behavioral change to the indexer or MCP server.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Stryker for mutation testing | Python-only codebase; Stryker targets JS/TS |
| FAISS vector IDs in commit tags | IDs churn on reindex; meaningless to a human reading `git log`; ADR numbers are durable and greppable |
| Separate PR summary artifact (parallel to PR template) | Competes with the template the team already maintains; augmenting the existing seam is lower friction |
| Pre-commit (framework) for hook management | Adds a dependency and config file for a single hook; `.githooks/` with `core.hooksPath` is simpler |

## Implementation Log

> Updated during development. Record deviations from the design, surprises, and decisions made in the moment.

- [ ] Add `[project.optional-dependencies].dev` to `pyproject.toml` (`pytest`, `mutmut`, `pytest-timeout`)
- [ ] Add `test` job to `.github/workflows/ci.yml` (pytest + mutmut + summary step)
- [ ] Write `tools/ci_summary.py` (parse pytest_output.txt + mutmut results, write Step Summary markdown, enforce thresholds)
- [ ] Write `tools/gen_pr_body.py` (branch → ADR → diff stat → filled PR template to stdout)
- [ ] Write `.githooks/commit-msg` (bash hook: ADR tag + staged src/ files)
- [ ] Add `git config core.hooksPath .githooks` setup instruction + section 7 to `CONTRIBUTING.md`
- [ ] Smoke-test `gen_pr_body.py` on `chore/adr-004-ci-tooling` branch (graceful fallback path)
- [ ] Smoke-test `commit-msg` hook: ADR branch → tag appended; chore branch → no tag; already-tagged → no double-tag

**Notes:**
<!-- 2026-06-11: Plan converted to ADR. All three features are chore-level (no src/ changes), but ADR documents the design rationale for the tooling choices (mutmut vs Stryker, ADR-number vs FAISS IDs, augment-template vs new-artifact). -->
