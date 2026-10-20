"""
test_scan_defaults_adr052.py — the ADR-052 scan defaults.

Four changes to what the gate admits, each pinned with the cases that must still pass
through it:

- `.gs` (Apps Script) is JavaScript, and its cross-file calls resolve under one global scope;
- `obj/` and MSBuild's `bin/<Configuration>/` are build output, `bin/*.py` is not;
- `.cshtml` / `.razor` are indexed as text, through `fallback_token_chunker`, end to end;
- credential files are never scanned, whatever `extensions` says.

No model load: the tokenizer and the embedder are fakes.
"""
from __future__ import annotations

import os
import re

import numpy as np
import pytest

import scan_policy as sp


@pytest.fixture(autouse=True)
def _clean_cache():
    sp.reset()
    yield
    sp.reset()


def _repo(tmp_path, toml_body: str | None = None) -> str:
    if toml_body is not None:
        (tmp_path / "indexer.toml").write_text(toml_body, encoding="utf-8")
    return str(tmp_path)


def _write(root, rel: str, text: str = "") -> None:
    path = os.path.join(str(root), *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# §1 — Apps Script `.gs`
# ---------------------------------------------------------------------------

_GS_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "conformance", "apps_script")


def test_gs_is_indexable_and_scannable(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.is_indexable("src/Code.gs")
    assert policy.is_scannable("src/Code.gs")
    assert policy.is_indexable("Utils.GS")             # the suffix is lower-cased


def test_gs_parses_with_the_javascript_grammar():
    """A bare `.gs -> JavaScriptAdapter` line raised KeyError: the grammar is per extension."""
    from adapters import get_adapter
    from adapters.ts_adapter import JavaScriptAdapter

    adapter = get_adapter(".gs")
    assert isinstance(adapter, JavaScriptAdapter)
    src = b"function a() { return b(); }\nfunction b() { return 1; }\n"
    result = adapter.parse("Code.gs", src)
    assert {s.fqn for s in result.symbols} == {"Code.gs::a", "Code.gs::b"}
    assert adapter.analyze_tags("Code.gs", src, result.symbols) is not None


def _resolve(files: dict[str, str], tmp_path) -> dict[tuple[str, str], str | None]:
    """Index `files` through the real parse → upsert → resolve path; {(source, callee): resolved}."""
    from ast_chunker import parse_file
    from call_resolver import resolve_call_edges
    from db import CodeDB

    db = CodeDB(str(tmp_path / "graph.db"))
    try:
        for rel, text in files.items():
            result = parse_file(rel, text)
            db.upsert_file(rel, "hash:" + rel, result.symbols, result.edges)
        resolve_call_edges(db)
        rows = db._conn.execute(
            "SELECT source_fqn, target, resolved_target FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    finally:
        db.close()
    return {(src, tgt): resolved for src, tgt, resolved in rows}


def _fixture_sources() -> dict[str, str]:
    out = {}
    for name in ("menu.gs", "helpers.gs"):
        with open(os.path.join(_GS_DIR, name), encoding="utf-8") as fh:
            out[name] = fh.read()
    return out


def test_gs_cross_file_calls_resolve_under_one_global_scope(tmp_path):
    """Apps Script has no imports; every top-level function is one global. A name defined
    once in the project is exactly the callee, which is the ADR-021 unique-repo-wide rule."""
    got = _resolve(_fixture_sources(), tmp_path)

    # Bare calls across files, both directions.
    assert got[("menu.gs::recalculate", "getEstimateSheet")] == "helpers.gs::getEstimateSheet"
    assert got[("menu.gs::recalculate", "sumColumn")] == "helpers.gs::sumColumn"
    assert got[("menu.gs::recalculate", "formatCurrency")] == "helpers.gs::formatCurrency"
    assert got[("helpers.gs::formatCurrency", "currencySymbol")] == "menu.gs::currencySymbol"

    # Google services and built-ins are defined nowhere in the project: unresolved.
    for key in [("menu.gs::onOpen", "getUi"), ("menu.gs::onOpen", "addToUi"),
                ("helpers.gs::getEstimateSheet", "getActive"),
                ("helpers.gs::sumColumn", "getRange"), ("helpers.gs::formatCurrency", "toFixed")]:
        assert got[key] is None, key

    resolved = {k: v for k, v in got.items() if v is not None}
    assert len(resolved) == 4, resolved                # nothing else bound


def test_gs_resolved_edges_read_as_verified(tmp_path):
    """The resolved edges clear the ADR-008 verdict floor, as any unique-name resolution."""
    from ast_chunker import parse_file
    from call_resolver import resolve_call_edges
    from db import CodeDB, EDGE_CONFIDENCE_FLOOR, effective_confidence

    db = CodeDB(str(tmp_path / "graph.db"))
    try:
        for rel, text in _fixture_sources().items():
            result = parse_file(rel, text)
            db.upsert_file(rel, "hash:" + rel, result.symbols, result.edges)
        resolve_call_edges(db)
        rows = db._conn.execute(
            "SELECT candidate, confidence FROM edges "
            "WHERE kind = 'CALLS' AND resolved_target IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 4
    assert all(effective_confidence(bool(c), conf) >= EDGE_CONFIDENCE_FLOOR for c, conf in rows)


def test_gs_a_name_defined_in_two_files_is_ambiguous_without_a_same_file_copy(tmp_path):
    """Two definitions and a caller in a third file: no rule can pick one, so none is bound."""
    got = _resolve({
        "a.gs": "function log(x) { return x; }\n",
        "b.gs": "function log(x) { return x + 1; }\n",
        "c.gs": "function run() { return log(1); }\n",
    }, tmp_path)
    assert got[("c.gs::run", "log")] is None


def test_gs_a_duplicate_definition_is_not_bound_to_the_callers_own_file(tmp_path):
    # Apps Script runs whichever duplicate loads last, everywhere, so neither copy is
    # provably the callee (ADR-052 §1, call_resolver's Apps Script rule).
    got = _resolve({
        "a.gs": "function log(x) { return x; }\nfunction run() { return log(1); }\n",
        "b.gs": "function log(x) { return x + 1; }\n",
    }, tmp_path)
    assert got[("a.gs::run", "log")] is None


def test_js_keeps_the_same_file_step_for_a_duplicate(tmp_path):
    # The rule is Apps Script's: a module-scoped .js caller still binds to its own copy.
    got = _resolve({
        "a.js": "function log(x) { return x; }\nfunction run() { return log(1); }\n",
        "b.js": "function log(x) { return x + 1; }\n",
    }, tmp_path)
    assert got[("a.js::run", "log")] == "a.js::log"


def test_gs_member_call_keeps_the_same_file_step(tmp_path):
    # this.total() names a method of the caller's own class, which the shared global
    # scope does not change.
    got = _resolve({
        "a.gs": ("class Sheet {\n  total() { return 1; }\n"
                 "  show() { return this.total(); }\n}\n"),
        "b.gs": "class Other {\n  total() { return 2; }\n}\n",
    }, tmp_path)
    assert got[("a.gs::Sheet.show", "total")] == "a.gs::Sheet.total"


# ---------------------------------------------------------------------------
# §2 — .NET build output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "App/obj/Debug/net8.0/App.AssemblyInfo.cs",
    "App/obj/Debug/net48/MainWindow.g.cs",
    "obj/Release/App.g.i.cs",
    "App/bin/Debug/net8.0/App.cs",
    "App/bin/Release/net8.0/publish/wwwroot/js/site.js",
    "App/bin/x64/Release/Native.cs",
    "App/bin/x86/Debug/Native.cs",
    "App/Bin/DEBUG/Generated.cs",                       # case-folded, like every dir rule
    "Obj/Foo.cs",
])
def test_dotnet_build_output_is_excluded(tmp_path, path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert not policy.is_indexable(path)
    assert not policy.is_scannable(path)


@pytest.mark.parametrize("path", [
    "bin/cli.py",                                       # Python console scripts
    "tools/bin/run.py",
    "bin/www.js",                                       # npm package CLI
    "src/bin/main.py",
    "bin/debug_helpers.py",                             # a file, not a Debug directory
    "Debug/Tracing.cs",                                 # Debug without bin above it
    "App/Views/Home/Release.cshtml",
    "App/Form1.Designer.cs",                            # checked-in generated code stays (§2)
    "App/Properties/Resources.Designer.cs",
    "App/Generated/Client.g.cs",
    "App/Migrations/20241001_Init.cs",                  # EF migrations are real source
    "App/bin/App.csproj",                               # bin/ itself is not excluded
])
def test_real_source_near_build_output_is_kept(tmp_path, path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.is_scannable(path)


def test_the_walk_skips_build_output_and_keeps_bin_scripts(tmp_path):
    """Through `scan_disk`, not just the predicate: prune cannot see `bin/Debug` as one
    name, so the walk descends and the path check must catch every file under it."""
    import incremental_indexer as ii

    root = _repo(tmp_path)
    for rel in ["bin/cli.py", "App/Program.cs", "App/obj/Debug/net8.0/App.g.cs",
                "App/bin/Debug/net8.0/App.cs", "App/bin/Release/net8.0/publish/wwwroot/js/site.js",
                "App/Migrations/20241001_Init.cs"]:
        _write(root, rel, "// placeholder\n")
    assert set(ii.scan_disk(root, quiet=True)) == {
        "bin/cli.py", "App/Program.cs", "App/Migrations/20241001_Init.cs"}


def test_a_custom_configuration_is_one_extra_dirs_line(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_dirs = ["bin/Staging", "Migrations"]\n'))
    assert not policy.is_indexable("App/bin/Staging/net8.0/App.cs")
    assert not policy.is_indexable("App/Migrations/20241001_Init.cs")
    assert not policy.is_indexable("App/bin/Debug/App.cs")    # defaults survive
    assert policy.is_indexable("bin/cli.py")


def test_bare_dirs_still_means_exclude_nothing(tmp_path):
    """The multi-segment defaults live in the same set, so `dirs = []` drops them too."""
    policy = sp.scan_policy(_repo(tmp_path, "[ignore]\ndirs = []\n"))
    assert policy.is_indexable("App/bin/Debug/App.cs")
    assert policy.is_indexable("App/obj/App.g.cs")


def test_a_multi_segment_entry_matches_consecutive_names_only(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.is_indexable("bin/tools/Debug/x.py")       # not consecutive
    assert policy.is_indexable("Debug/bin/x.py")             # wrong order


# ---------------------------------------------------------------------------
# §3 — Razor and Blazor as text
# ---------------------------------------------------------------------------

def test_razor_and_blazor_are_indexable_xaml_is_opt_in(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.is_indexable("Views/Home/Index.cshtml")
    assert policy.is_scannable("Views/Home/Index.cshtml")    # the watchdog's predicate
    assert policy.is_indexable("Components/OrderGrid.razor")
    assert not policy.is_scannable("MainWindow.xaml")

    sp.reset()
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_extensions = [".xaml"]\n'))
    assert policy.is_indexable("MainWindow.xaml")
    assert policy.is_indexable("Views/Home/Index.cshtml")    # defaults survive


class _FakeTokenizer:
    """Words and whitespace runs as tokens; decode is concatenation. Enough for the
    chunker's budget arithmetic, with no download."""

    def encode(self, text, add_special_tokens=False):
        return re.findall(r"\S+|\s+", text)

    def decode(self, tokens):
        return "".join(tokens)


_CSHTML = """@model EganEstimator.Models.Estimate
@{
    ViewData["Title"] = "Estimate";
}
<h1>@Model.Name</h1>
<table class="table">
@foreach (var line in Model.Lines)
{
    <tr><td>@line.Description</td><td>@line.Cost.ToString("C")</td></tr>
}
</table>
<p class="total">Estimate total: @Model.Total.ToString("C")</p>
<a asp-controller="Estimates" asp-action="Edit" asp-route-id="@Model.Id">Edit</a>
"""

_RAZOR = """@page "/orders"
@inject IOrderService Orders

<OrderGrid Items="@items" />

@code {
    private List<Order> items = new();

    protected override async Task OnInitializedAsync()
    {
        items = await Orders.ListAsync();
    }
}
"""


def test_razor_files_go_through_the_fallback_path_end_to_end(tmp_path, monkeypatch):
    """B-064 level 1: scanned, chunked into every tier, embedded, written — no symbols,
    no edges, no crash. `run_incremental` is the real build, with a fake embedder."""
    import core
    import incremental_indexer as ii
    from ast_chunker import parse_file
    from core import embed_dimension
    from db import CodeDB
    from stable_id import TIER_CONFIGS

    monkeypatch.setattr(core.jina_tokenizer, "_tokenizer", _FakeTokenizer())
    dim = embed_dimension()
    monkeypatch.setattr(core, "embed_batch", lambda texts, batch_size=32: np.array(
        [[float((hash(t) >> (8 * i)) & 0xFF) + 1.0 for i in range(dim)] for t in texts],
        dtype=np.float32))
    monkeypatch.setattr(ii, "summarization_enabled", lambda: False)

    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "Views/Estimates/Details.cshtml", _CSHTML)
    _write(repo, "Components/Orders.razor", _RAZOR)
    _write(repo, "MainWindow.xaml", "<Window x:Class=\"App.MainWindow\" />\n")   # not a default
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(ii, "INDEX_DIR", str(index))
    monkeypatch.setattr(ii, "DB_PATH", str(index / "graph.db"))

    scanned = ii.scan_disk(str(repo), quiet=True)
    assert set(scanned) == {"Views/Estimates/Details.cshtml", "Components/Orders.razor"}

    for rel, text in [("Views/Estimates/Details.cshtml", _CSHTML), ("Components/Orders.razor", _RAZOR)]:
        result = parse_file(rel, text)
        assert result.symbols == [] and result.edges == []
        tiers = ii.chunk_all_tiers(rel, text)
        assert set(tiers) == {name for name, _, _ in TIER_CONFIGS}
        assert all(tiers[name] for name in tiers), {n: len(c) for n, c in tiers.items()}

    ii.run_incremental(str(repo), interactive=False)

    with CodeDB(str(index / "graph.db")) as db:
        rows = db._conn.execute(
            "SELECT f.path, c.tier, c.text FROM chunks c JOIN files f ON f.id = c.file_id"
        ).fetchall()
        n_symbols = db._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        n_edges = db._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert n_symbols == 0 and n_edges == 0
    by_file: dict[str, set[int]] = {}
    for path, tier, _ in rows:
        by_file.setdefault(path, set()).add(tier)
    assert set(by_file) == {"Views/Estimates/Details.cshtml", "Components/Orders.razor"}
    assert all(len(tiers) == len(TIER_CONFIGS) for tiers in by_file.values()), by_file
    assert any("Estimate total" in content for _, _, content in rows)


# ---------------------------------------------------------------------------
# §4 — credential files
# ---------------------------------------------------------------------------

# Widened so the exclusion is what decides, not the extension list.
_WIDE = '[ignore]\nextra_extensions = [".json", ".pem", ".key", ".p12", ".pfx", ".local", ".production"]\n'


@pytest.mark.parametrize("path", [
    ".env.local",
    "config/.env.production",
    "credentials.json",
    "secrets/application_default_credentials.json",
    "client_secret_1234.apps.googleusercontent.com.json",
    "service-account.json",
    "deploy/my_service_account.json",
    "serviceAccountKey.json",
    "gantt-app-firebase-adminsdk-abcde-0123456789.json",
    "egan-estimator-0123456789ab.json",
    "secrets.json",
    "certs/server.pem",
    "certs/server.key",
    "certs/Signing.PFX",
    "certs/client.p12",
    "Credentials.JSON",
])
def test_credential_files_are_never_scanned(tmp_path, path):
    policy = sp.scan_policy(_repo(tmp_path, _WIDE))
    assert not policy.is_indexable(path)
    assert not policy.is_scannable(path)


@pytest.mark.parametrize("path", [
    "keyboard.json",                    # why `*key*.json` was rejected
    "monkey.json",
    "keymap.json",
    "package-lock.json",
    "config.json",
    "src/auth/credentials.py",          # `*credentials*` is .json only
    "src/secrets_manager.py",
    "src/service_account.py",
    "src/keys.ts",
    "fixtures/build-12345.json",        # not 12 hex digits
])
def test_files_that_only_look_like_credentials_are_kept(tmp_path, path):
    policy = sp.scan_policy(_repo(tmp_path, _WIDE))
    assert policy.is_indexable(path)


def test_env_templates_are_re_included(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    for name in (".env.example", ".env.sample", ".env.template"):
        assert not policy.is_ignored_file(name)
    assert policy.is_ignored_file(".env")
    assert policy.is_ignored_file("app/.env.local")


def test_the_walk_skips_credential_files(tmp_path):
    import incremental_indexer as ii

    root = _repo(tmp_path, _WIDE)
    _write(root, "config.json", "{}\n")
    _write(root, "service-account.json", '{"private_key": "FAKE"}\n')
    _write(root, "certs/dev.pem",
           "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n")
    assert set(ii.scan_disk(root, quiet=True)) == {"config.json"}


def test_extra_files_extends_the_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_files = ["local_settings.py"]\n'))
    assert not policy.is_indexable("app/local_settings.py")
    assert policy.is_ignored_file("credentials.json")         # defaults survive


def test_a_bang_pattern_re_includes_one_default(tmp_path):
    """An exception without replacing the defaults (and freezing them)."""
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_files = ["!secrets.json"]\n'))
    assert not policy.is_ignored_file("secrets.json")
    assert policy.is_ignored_file("credentials.json")


def test_bare_files_replaces_the_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nfiles = []\n'))
    assert policy.ignore_files == frozenset()
    assert not policy.is_ignored_file("credentials.json")


def test_files_follows_the_shared_precedence_rules(tmp_path):
    with pytest.raises(ValueError, match="extra_files"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\nfiles = ["a"]\nextra_files = ["b"]\n'))
    sp.reset()
    with pytest.raises(ValueError, match="list of strings"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_files = "secrets.json"\n'))


def test_a_file_pattern_with_a_path_raises(tmp_path):
    with pytest.raises(ValueError, match="file name, not a path"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_files = ["config/secret.json"]\n'))


def test_no_config_yields_the_file_defaults(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path))
    assert policy.ignore_files == sp.DEFAULT_IGNORE_FILES
    assert "ignore_files=" in policy.describe()


# ---------------------------------------------------------------------------
# `!` drops one default from dirs, root_dirs and extensions (ADR-052 §5)
# ---------------------------------------------------------------------------

def test_a_bang_dir_drops_one_default_and_keeps_the_rest(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_dirs = ["!obj", "Migrations"]\n'))
    assert policy.is_indexable("App/obj/Generated.cs")              # obj dropped
    assert not policy.is_indexable("App/bin/Debug/App.cs")          # other defaults stay
    assert not policy.is_indexable("node_modules/x/index.js")
    assert not policy.is_indexable("App/Migrations/0001_Init.cs")   # and the addition holds


def test_a_bang_drops_a_multi_segment_dir_folded(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_dirs = ["!BIN/debug"]\n'))
    assert policy.is_indexable("App/bin/Debug/App.cs")
    assert not policy.is_indexable("App/bin/Release/App.cs")


def test_a_bang_root_dir_drops_one_default(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_root_dirs = ["!functions"]\n'))
    assert policy.is_indexable("functions/src/index.ts")


def test_a_bang_extension_drops_one_default(tmp_path):
    policy = sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_extensions = ["!.cshtml"]\n'))
    assert not policy.is_indexable("Views/Home/Index.cshtml")
    assert policy.is_indexable("Pages/Counter.razor")


def test_a_bang_that_names_no_default_raises(tmp_path):
    with pytest.raises(ValueError, match="names no built-in default"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\nextra_dirs = ["!objj"]\n'))


def test_a_bang_in_the_bare_key_raises(tmp_path):
    with pytest.raises(ValueError, match="nothing to remove"):
        sp.scan_policy(_repo(tmp_path, '[ignore]\ndirs = ["!obj"]\n'))
