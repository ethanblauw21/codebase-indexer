# ADR-052: Scan Defaults — Apps Script, .NET Output, Razor as Text, Credential Files

**Status:** proposed
**Date:** 2026-10-20
**Branch:** `feature/adr-052-scan-defaults-and-languages`
**Reviewer:** @edb
**Backlog:** [B-015](../backlog.md#b-015) (Apps Script `.gs`), [B-063](../backlog.md#b-063) item 1 only
(`obj`/`bin` defaults), [B-064](../backlog.md#b-064) level 1 only (`.cshtml`/`.razor` as text),
[B-018](../backlog.md#b-018) the scan-policy half only (the output-redaction half is ADR-049)
**Depends on:** ADR-026 (the scan gate and its `extra_*`/bare-key precedence, which this extends)
**Depended on by:** none yet

## Context

The scan gate (`src/scan_policy.py`, ADR-026) decides what the indexer looks at. Four gaps in its
defaults were filed separately:

- **B-015.** Apps Script projects whose clasp `fileExtension` is `gs` are invisible: `.gs` is not an
  indexable extension and has no REGISTRY entry. Apps Script is plain JavaScript run in **one shared
  global scope**: files have no imports and call each other's top-level functions by name.
- **B-063.** `DEFAULT_IGNORE_DIRS` has no `obj` or `bin`. On AcadeDesignTools, 56 of 147 `.cs` files
  (38%) are generated `obj/` output (`*.g.cs`, `*.g.i.cs`, `*AssemblyAttributes.cs`); EganEstimator
  has 8. `bin` is a common name outside .NET: Python projects keep real scripts in `bin/*.py`, npm
  packages keep their CLI in `bin/*.js`, and Rust keeps binaries in `src/bin/`.
- **B-064.** `.cshtml` (MVC / Razor Pages) and `.razor` (Blazor) are not indexed at all, so "which
  view shows the estimate total" has no answer. EganEstimator has 17 views. With no adapter they
  would go through `fallback_token_chunker`, a path no test had run end to end.
- **B-018.** Nothing stops a credential file from being chunked, embedded and pasted into an agent's
  context. Today's default extensions happen to exclude `.json`, `.env` and `.pem`, so the defaults
  are safe by accident; the moment a repo adds `extra_extensions = [".json"]` to index its config,
  a committed `service-account.json` goes into the index.

## Decision

### 1. Apps Script `.gs` is JavaScript (B-015)

- `.gs` joins `DEFAULT_INDEXABLE_EXTS` and the REGISTRY.
- **One REGISTRY line is not enough.** `_WebAdapter.parse` looks its grammar up by extension
  (`self._grammars[ext]`), so `.gs → JavaScriptAdapter` raises `KeyError` on the first parse.
  `adapters/__init__.py` defines a three-line `AppsScriptAdapter(JavaScriptAdapter)` that maps `.gs`
  to the JavaScript grammar. The adapter class in `ts_adapter.py` is unchanged.
- **Call resolution under one global scope is left as the ADR-021 order.** Measured on a two-file
  fixture (Implementation Log): a call to a top-level function defined once anywhere in the project
  resolves through the *unique repo-wide* rule, which is exactly Apps Script's semantics; a call to
  a Google service (`SpreadsheetApp.getActive()`) stays unresolved; a name defined in no file stays
  unresolved. The one wrong binding is a function name defined in **two** `.gs` files: Apps Script
  runs whichever file loads last, everywhere, while the resolver's *same-file* step binds each
  caller to its own file's copy. That is a duplicate definition — almost always a bug in the script
  — and fixing it needs `call_resolver.py` to know a file's language. It is recorded as a known gap
  (an `xfail(strict=True)` test asserting the correct answer), not fixed here.

### 2. `obj` everywhere; `bin` only under MSBuild's configuration folders (B-063 item 1)

- `obj` joins `DEFAULT_IGNORE_DIRS` at any depth. It is MSBuild's intermediate directory and the
  C/C++ object-file directory of countless Makefiles; it is source in nothing we could find.
- **`bin` does not.** Instead, `ignore_dirs` entries may now be a **multi-segment path**, matched as
  consecutive directory names at any depth, and the defaults gain `bin/Debug`, `bin/Release`,
  `bin/x64` and `bin/x86`. MSBuild writes `bin/<Configuration>/` or `bin/<Platform>/<Configuration>/`,
  so this catches its output (including `dotnet publish`'s `bin/Release/net8.0/publish/wwwroot/js/`,
  which carries copies of every vendored script) and leaves `bin/cli.py`, `bin/run.js` and
  `src/bin/main.rs` alone. A custom configuration is one line: `extra_dirs = ["bin/Staging"]`.
- **No generated-file name patterns** (`*.g.cs`, `*.g.i.cs`, `*.Designer.cs`, `*AssemblyAttributes.cs`).
  Every generated file in the B-063 sample lives under `obj/`, which now covers it. Outside `obj/` a
  generated file is checked in on purpose: `Form1.Designer.cs` carries the WinForms event wiring
  (`button1.Click += button1_Click`), and a checked-in `*.g.cs` is often the only definition of types
  the rest of the code calls, so dropping it costs the resolver its targets.
- EF Core `Migrations/` stays indexed: it is real source. A repo that finds it noisy adds
  `extra_dirs = ["Migrations"]`.

### 3. `.cshtml` and `.razor` are indexed as text; `.xaml` is not a default (B-064 level 1)

- `.cshtml` and `.razor` join `DEFAULT_INDEXABLE_EXTS`. They have no adapter, so `chunk_file_ast`
  falls back to `fallback_token_chunker` for tier 1 and tiers 2–3 use it as for every file: three
  tiers of text chunks, no symbols, no edges. A test drives this end to end (scan → chunk → ingest
  with a fake embedder).
- **`.xaml` stays opt-in** (`extra_extensions = [".xaml"]`, covered by the same end-to-end test).
  In Razor the behavior lives in the view (`@code` blocks, tag helpers, `@Model.Total`); in WPF it
  lives in the code-behind `.cs`, which is already indexed, while the `.xaml` is layout, styles and
  resource dictionaries that can run to thousands of lines of noise. And taking a default extension
  back out costs a user a full `extensions = [...]` replacement, which freezes every later default,
  so the bar for adding one is evidence, not parity. The 11 AcadeDesignTools files are not that yet.
- **Level 2 (a Razor adapter) — grammar status.** The original `tree-sitter-razor` is archived and
  does not build with current tree-sitter. A maintained fork by `tris203` is the grammar
  nvim-treesitter and nixpkgs use (nixpkgs pinned a 2025-02-17 snapshot). There is **no PyPI wheel**
  (`pip download tree-sitter-razor` finds nothing, 2026-10-20), so using it here means vendoring and
  building a C grammar, which `CONTRIBUTING`'s no-new-runtime-dependency rule would have to allow.

### 4. Credential files are never scanned (B-018, scan half)

A fourth `[ignore]` knob, **`files` / `extra_files`**: `fnmatch` patterns matched against the file
**name** (case-folded, like directory names). A path matching one is neither indexable nor
scannable, whatever its extension. A pattern starting with `!` re-includes names it matches, so an
exception does not require replacing the defaults. A pattern containing `/` raises: these match
names, and `config/secret.json` would silently match nothing.

Defaults (`DEFAULT_IGNORE_FILES`):

| Pattern | Catches |
|---|---|
| `.env`, `.env.*` but `!.env.example`, `!.env.sample`, `!.env.template` | dotenv files; the documented templates stay |
| `*credentials*.json` | gcloud `application_default_credentials.json`, the Google API quickstart's `credentials.json` |
| `client_secret*.json` | Google OAuth client downloads (`client_secret_<id>.apps.googleusercontent.com.json`) |
| `*service[-_]account*.json`, `serviceaccount*.json` | `service-account.json`, `my_service_account.json`, Firebase's `serviceAccountKey.json` |
| `*firebase-adminsdk*.json` | Firebase Admin SDK key downloads |
| `*-` + 12 × `[0-9a-f]` + `.json` | the gcloud console's key download, `<project>-<12 hex>.json` |
| `secrets.json` | ASP.NET user-secrets copied into a repo |
| `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` | private keys and keystores |

Rules kept narrow on purpose:

- **`*key*.json` is rejected** (the backlog's suggestion): it matches `keyboard.json`, `monkey.json`,
  `keymap.json`. The key-download names above are the ones Google and Firebase actually write.
- **`*credentials*` is restricted to `.json`**: `credentials.py` is ordinary source in every auth
  library (`google/auth/credentials.py`).
- No content check: the scan gate sees only paths (the watchdog never opens the file). Content is
  the output-redaction pass's job (ADR-049).

## Consequences

**Better:**
- Apps Script projects with `.gs` files are indexed, with symbols and cross-file call edges.
- .NET repos stop indexing their own build output; on AcadeDesignTools that is 38% of the `.cs`.
- Razor views and Blazor components are searchable as text.
- A credential file stays out of the index even after a repo widens `extensions`.
- `ignore_dirs` can name a directory by its parent, which is how `bin` is handled without touching
  `bin/*.py`.

**Worse:**
- A repo that already has an index will **drop** its `obj/` and MSBuild `bin/` files on the next
  incremental run, and gain `.gs`/`.cshtml`/`.razor` files. Both are the intent, but a large `obj/`
  can trip the bulk-deletion prompt (`max(50, 20%)`) once; `--prune` accepts it.
- WPF code-behind loses the `InitializeComponent` and named-element fields generated into
  `obj/*.g.cs`, so a reference to `OkButton` no longer has an in-index declaration.
- The Apps Script duplicate-definition case binds to the wrong copy (§1).
- A multi-segment `ignore_dirs` entry cannot be pruned during the walk (prune sees one name at a
  time); the walk descends into `bin/Debug/` and the path check rejects every file under it. Cheap
  for MSBuild output; it would not be for a `node_modules`-sized tree.
- One more knob to document.

**Neutral:**
- `dirs = []` still means "exclude nothing": the multi-segment defaults live in the same set.
- `.env` files have no indexable suffix (`_suffix(".env") == ""`), so the `.env` patterns guard
  `.env.local`-style names under a widened `extensions`, and document intent.

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| Point `.gs` at the existing `_js` instance | `KeyError` on `_grammars[".gs"]`; mutating the shared class dict from `__init__` would hide the dependency |
| Make the resolver treat all `.gs` files as one module | Needs `call_resolver.py` to know languages, for a case (duplicate top-level names) that is a bug in the script |
| `bin` at any depth | Drops `bin/*.py` scripts and npm CLIs |
| `bin` only when a `*.csproj` sits beside it | The watchdog sees a path and never the disk; the two consumers would disagree, which is what ADR-026 §2 removed |
| Generated-file name patterns | Redundant with `obj`, and wrong for the checked-in generated files outside it (§2) |
| `.xaml` as a default | §3 |
| `*key*.json`, `*credentials*`, `secrets.py` | Too broad; each matches real source or data |
| Content sniffing for keys in the scan gate | The gate is path-only by contract; redaction is ADR-049 |

## Implementation Log

- [x] `scan_policy.py`: `.gs`, `.cshtml`, `.razor`; `obj`, `bin/Debug`, `bin/Release`, `bin/x64`, `bin/x86`; multi-segment `ignore_dirs`; `files`/`extra_files` with `!` exceptions. Still a leaf module: the one new import is stdlib `fnmatch`.
- [x] `adapters/__init__.py`: `AppsScriptAdapter` and the `.gs` REGISTRY entry
- [x] Conformance fixture `tests/fixtures/conformance/apps_script/{menu,helpers}.gs`: 1.0/1.0 on symbols (6), all edges (16) and call edges (16); new `apps_script` row in `benchmarks/conformance/baseline.json`
- [x] Tests: `tests/test_scan_defaults_adr052.py` (67 cases, 1 of them a strict xfail) — `.gs` scannable, parsed, and resolved across files; `obj`/`bin` including Python `bin/*.py` and npm `bin/*.js`, through `scan_disk` too; Razor end to end through `run_incremental` with a fake tokenizer and embedder; credential files and the look-alikes that must still be indexed; `extra_files`/`files`/`!`/both-keys/bad-value behavior
- [x] `src/CLAUDE.md` "The Scan Gate"

**Notes:**

- 2026-10-20 — **`.gs` needed an adapter subclass, not a REGISTRY line.** Found before writing
  it: `_WebAdapter.parse` and `analyze_tags` index `self._grammars[ext]`. The subclass lives in
  `adapters/__init__.py`, so `ts_adapter.py` (graph session) is untouched.
- 2026-10-20 — **Apps Script resolution, measured** (fixture: `menu.gs` + `helpers.gs`, real
  `parse_file` → `CodeDB.upsert_file` → `resolve_call_edges`, CPU, no model): 16 call edges,
  **4 resolved, all correct** — `recalculate → getEstimateSheet / sumColumn / formatCurrency`
  (menu → helpers) and `formatCurrency → currencySymbol` (helpers → menu), each by the
  unique-repo-wide rule; the 12 service and built-in calls (`getUi`, `getActive`, `getRange`,
  `toFixed`, …) stay unresolved; **0 wrong**. The resolved edges carry no producer confidence
  (`confidence` NULL, `candidate` 0), so they read as verified (effective ≥ the 0.5 floor), the same
  grade as any unique-name resolution in JS. A name defined in two files with the caller in a third
  stays unresolved; with the caller in one of the two, the same-file step binds it to its own copy,
  which Apps Script would not do — pinned as `xfail(strict=True)`.
- 2026-10-20 — **Resolution harness not used for `.gs`.** `tools/resolution_eval.py` gates each
  language on the typed regime *beating* the hint-stripped baseline. Apps Script has no imports or
  receiver types, so the two regimes are identical by construction and a `.gs` row would fail that
  gate for a reason that says nothing about correctness. The cross-file measurement is a pytest
  instead.
- 2026-10-20 — **Razor end to end.** `.cshtml` and `.razor` through `scan_disk` → `run_incremental`
  with a fake tokenizer (words and whitespace) and a fake embedder: both files get chunks in all
  three tiers, zero symbols, zero edges, no crash; `.xaml` in the same tree is skipped by default.
  `parse_file` returns an empty result for an extension with no adapter, and `chunk_file_ast`
  falls straight to `fallback_token_chunker`.
- 2026-10-20 — **Outside this ADR's file list:** `tools/conformance_eval.py`, one line (`".gs":
  "apps_script"` in `_LANG_BY_EXT`). Fixture discovery matches `stem + ext` against that table,
  so without it the `.gs` fixture is an orphan `expected.json` and the eval raises.
- 2026-10-20 — **Not regenerated:** the README conformance table (`--write-readme`) now lacks the
  `apps_script` row. `README.md` belongs to the symbols session (ADR-050) on this round; run
  `python tools/conformance_eval.py --write-readme` after both merge. The "Defaults as of this
  writing" comment in `indexer.toml`'s `[ignore]` block was already stale (it lists `indexer`,
  `public`, `mocks`, which are not defaults) and now also misses this ADR's additions; that file
  is not edited this round.

- 2026-10-20 — **Gates** (Linux cloud container, Python 3.12, CPU, PyPI `torch` 2.14.1 run on CPU,
  no model, huggingface.co unreachable so `HF_HUB_OFFLINE=1`): flake8 clean; `pytest --co` 775
  collected, no errors (704 on `871b55c`); `pytest` **724 passed, 48 failed, 2 skipped, 1 xfailed**
  against `871b55c`'s 654 / 48 / 2 / 0. The 48 failures are the **same 48 test ids on both**, all
  from tests that load the embedder's tokenizer from HuggingFace. The +70 passed are this ADR's 66
  passing tests and 4 conformance parametrizations over the new fixtures.
  `conformance_eval --check-baseline`, `resolution_eval --check-baseline` and
  `l5x_fixture_teeth --check` exit 0. No MCP tool changed, so no Inspector run.

**Open questions for @edb:**

1. **Removing one default.** `files` has `!` exceptions, but `dirs` and `extensions` still need a
   full replacement to drop a single default (e.g. a repo that wants `obj/` indexed). Worth a
   `!` form for those knobs too?
2. **`.xaml`.** Left opt-in (§3). If the B-063 build on AcadeDesignTools shows XAML answers
   questions the code-behind can't, flip it.
3. **Apps Script duplicates.** Is the duplicate-definition wrong binding worth a language-aware
   rule in `call_resolver.py` (no same-file step for `.gs`), or is it fine as a known gap?
4. **gcloud key-name pattern.** `*-<12 hex>.json` also matches any hyphenated name ending in 12 hex
   digits (a content-hashed build manifest). Acceptable loss, or drop it and rely on the other
   service-account patterns?
