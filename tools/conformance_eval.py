"""ADR-008 §1–§3 — extraction conformance scorecard (precision/recall).

The *extraction* sibling of ADR-007's *retrieval* harness. Where the snapshot tests
(`tests/test_adapter_snapshots.py`) guard against extraction *drift* (golden == current
output), this measures extraction *correctness*: it scores what the adapters actually
emit against independently hand-authored **ground truth**, and reports per-language
precision/recall for symbols, edges, and — the headline metric — call edges.

    precision = correct emitted ÷ all emitted     (are the edges we assert real?)
    recall    = correct emitted ÷ ground truth    (did we find the edges that exist?)

Ground-truth fixtures live in `tests/fixtures/conformance/<language>/<feature>.<ext>`
with a sibling `<feature>.expected.json` declaring the expected symbols and edges.

CRITICAL AUTHORING RULE: expected.json is authored from the *source semantics* — what a
correct extractor SHOULD produce — never by copying parser output. Echoing the parser
guarantees a meaningless 1.0 and measures nothing. A fixture that legitimately scores
below 1.0 is a real, honest signal of an adapter gap.

FQN normalization: symbol/edge identifiers are stored path-prefixed
(`C:\\…\\sample.py::Foo.bar`). We strip the path prefix so fixtures are portable across
checkouts — expected.json uses the normalized form (`Foo.bar`, `format_output`, bare
call targets like `dumps`, module names like `json`).

CLI:
    python tools/conformance_eval.py                 # print the scorecard
    python tools/conformance_eval.py --json out.json # machine-readable results
    python tools/conformance_eval.py --write-baseline # commit the current numbers
    python tools/conformance_eval.py --check-baseline # regression gate (exit 1 on drop)
    python tools/conformance_eval.py --write-readme   # regenerate the README table
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src")
sys.path.insert(0, _SRC)

_FIXTURE_ROOT = os.path.join(_ROOT, "tests", "fixtures", "conformance")
_BASELINE_PATH = os.path.join(_ROOT, "benchmarks", "conformance", "baseline.json")
_README_PATH = os.path.join(_ROOT, "README.md")

_README_START = "<!-- CONFORMANCE:START -->"
_README_END = "<!-- CONFORMANCE:END -->"

# Extension → language label (also the fixture subdirectory name).
_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
}

_SOURCE_EXTS = tuple(_LANG_BY_EXT)

# Categories scored. "edges_call" is a subset of "edges_all" surfaced on its own because
# call-edge precision/recall is the number the depth-over-breadth thesis stands on.
_CATEGORIES = ("symbols", "edges_all", "edges_call")


def normalize_fqn(s: str) -> str:
    """Strip the checkout-specific path prefix so identifiers are portable.

    `C:\\…\\sample.py::Foo.bar` -> `Foo.bar`; a bare source-file path -> its basename
    (`C:\\…\\async_gen.py` -> `async_gen.py`). A bare call target (`dumps`), module
    (`json`), or namespaced FQN is returned UNCHANGED — including C# member FQNs of the
    form `Namespace.Type.Member/arity`, whose trailing `/arity` is part of symbol identity,
    not a path separator. (A naive `os.path.basename` splits on `/` and would collapse
    every C# method to its arity digit, silently merging distinct methods and masking
    real recall misses. C# is the first `/arity` language, so this only surfaces here.)

    C++ uses `::` as its *namespace* separator (`shop::Order`, `shop::Order::compute(int)`),
    NOT a path separator — and its symbols carry no path prefix. So `::` is only treated as
    the path-prefix delimiter when its left side actually looks like a filesystem path
    (has a separator or a source-file extension). A bare identifier left side (`shop`) is a
    C++ namespace and is preserved. Without this guard, `normalize_fqn` would strip the
    namespace off every C++ FQN, collapsing distinct symbols and masking recall misses —
    the exact failure mode the C# `/arity` fix above guards against.
    """
    if "::" in s:
        prefix = s.split("::", 1)[0]
        if ("/" in prefix or "\\" in prefix) or os.path.splitext(prefix)[1]:
            return s.split("::", 1)[1]
        return s
    # Only basename strings that are genuinely filesystem paths: a path separator AND a
    # file extension on the final component. A C# FQN has a '/' before the arity, but its
    # final component is a bare integer with no extension, so it passes through untouched.
    base = os.path.basename(s)
    if ("/" in s or "\\" in s) and os.path.splitext(base)[1]:
        return base
    return s


def _symbol_keys(symbols) -> set:
    """{(normalized_fqn, kind)} for a list of Symbol objects OR expected dicts."""
    out = set()
    for s in symbols:
        fqn = s["fqn"] if isinstance(s, dict) else s.fqn
        kind = s["kind"] if isinstance(s, dict) else s.kind
        out.add((normalize_fqn(fqn), kind))
    return out


def _edge_keys(edges) -> set:
    """{(norm_source, norm_target, kind)} for Edge objects OR expected dicts.

    Expected dicts use `source`/`target`/`kind`; Edge objects use `source_fqn`/`target`.
    """
    out = set()
    for e in edges:
        if isinstance(e, dict):
            src, tgt, kind = e["source"], e["target"], e["kind"]
        else:
            src, tgt, kind = e.source_fqn, e.target, e.kind
        out.add((normalize_fqn(src), normalize_fqn(tgt), kind))
    return out


def _category_sets(symbol_keys: set, edge_keys: set) -> dict:
    return {
        "symbols": symbol_keys,
        "edges_all": edge_keys,
        "edges_call": {e for e in edge_keys if e[2] == "call"},
    }


# ---------------------------------------------------------------------------
# Key-distinctness invariant
#
# A normalized key must uniquely identify one raw fact. If two DISTINCT raw keys collapse
# onto the same normalized key, the scorer can no longer tell them apart: counts undercount
# and — worse — a real recall miss is masked by a surviving sibling of the same key. This is
# exactly the defect the old `normalize_fqn` had for C# `/arity` FQNs (every method → its
# arity digit). Surfacing collisions structurally makes that class of bug impossible to
# reintroduce silently. (Same-name, same-arity C# overloads legitimately share a key — an
# accepted coarseness, documented in the conventions doc — so we simply do not author them.)
# ---------------------------------------------------------------------------


def _collisions(raw_norm_pairs) -> dict:
    """{normalized_key: {distinct raw_keys}} for every normalized key ≥2 raw keys map to."""
    by_norm: dict = {}
    for raw, norm in raw_norm_pairs:
        by_norm.setdefault(norm, set()).add(raw)
    return {norm: raws for norm, raws in by_norm.items() if len(raws) > 1}


def _symbol_norm_pairs(symbols):
    out = []
    for s in symbols:
        fqn = s["fqn"] if isinstance(s, dict) else s.fqn
        kind = s["kind"] if isinstance(s, dict) else s.kind
        out.append(((fqn, kind), (normalize_fqn(fqn), kind)))
    return out


def _edge_norm_pairs(edges):
    out = []
    for e in edges:
        if isinstance(e, dict):
            src, tgt, kind = e["source"], e["target"], e["kind"]
        else:
            src, tgt, kind = e.source_fqn, e.target, e.kind
        out.append(((src, tgt, kind), (normalize_fqn(src), normalize_fqn(tgt), kind)))
    return out


def key_collisions(symbols, edges) -> dict:
    """Return {'symbols': {...}, 'edges': {...}} of normalized-key collisions (empty = clean).

    Accepts Symbol/Edge objects or expected-json dicts interchangeably, so the same check
    runs over both hand-authored ground truth and live adapter output.
    """
    return {
        "symbols": _collisions(_symbol_norm_pairs(symbols)),
        "edges": _collisions(_edge_norm_pairs(edges)),
    }


def validate_known_gap(expected: dict, feature: str) -> dict | None:
    """Return the known_gap block if present and well-formed, else None.

    A known_gap MUST carry a non-empty `reason` and a `ref` pointing at the documenting
    section/ADR — we refuse to record a gap nobody can look up (an unreferenced gap is an
    authoring bug, not a measurement).
    """
    kg = expected.get("known_gap")
    if kg is None:
        return None
    if not isinstance(kg, dict) or not kg.get("reason") or not kg.get("ref"):
        raise ValueError(
            f"{feature}: 'known_gap' must be an object with non-empty 'reason' and 'ref' "
            f"(a pointer to the documenting README section or ADR)"
        )
    return kg


def discover_fixtures() -> list[dict]:
    """Every `<feature>.expected.json` with a sibling source file, sorted by path."""
    fixtures = []
    if not os.path.isdir(_FIXTURE_ROOT):
        return fixtures
    for dirpath, _dirs, files in os.walk(_FIXTURE_ROOT):
        for fn in sorted(files):
            if not fn.endswith(".expected.json"):
                continue
            stem = fn[: -len(".expected.json")]
            src = None
            for ext in _SOURCE_EXTS:
                cand = os.path.join(dirpath, stem + ext)
                if os.path.isfile(cand):
                    src = cand
                    break
            fixtures.append({
                "expected_path": os.path.join(dirpath, fn),
                "source_path": src,
                "stem": stem,
            })
    return sorted(fixtures, key=lambda f: f["expected_path"])


def score_fixture(fixture: dict) -> dict:
    """Parse one fixture's source and score it against its expected.json.

    Returns per-category {tp, emitted, gt} plus feature/language metadata. Raises if the
    source file is missing (an orphaned expected.json is a fixture-authoring bug).
    """
    from ast_chunker import parse_file

    if fixture["source_path"] is None:
        raise FileNotFoundError(
            f"expected.json without a sibling source file: {fixture['expected_path']}"
        )

    with open(fixture["expected_path"], encoding="utf-8") as fh:
        expected = json.load(fh)
    with open(fixture["source_path"], encoding="utf-8") as fh:
        content = fh.read()

    ext = os.path.splitext(fixture["source_path"])[1].lower()
    language = expected.get("language") or _LANG_BY_EXT.get(ext, "unknown")
    feature = expected.get("feature", fixture["stem"])

    result = parse_file(fixture["source_path"], content)

    actual = _category_sets(_symbol_keys(result.symbols), _edge_keys(result.edges))
    gold = _category_sets(
        _symbol_keys(expected.get("symbols", [])),
        _edge_keys(expected.get("edges", [])),
    )

    per_cat = {}
    for cat in _CATEGORIES:
        exp_set, act_set = gold[cat], actual[cat]
        per_cat[cat] = {
            "tp": len(exp_set & act_set),
            "emitted": len(act_set),
            "gt": len(exp_set),
            "missed": sorted("|".join(map(str, k)) for k in (exp_set - act_set)),
            "spurious": sorted("|".join(map(str, k)) for k in (act_set - exp_set)),
        }

    perfect = all(
        not per_cat[cat]["missed"] and not per_cat[cat]["spurious"] for cat in _CATEGORIES
    )
    known_gap = validate_known_gap(expected, feature)

    return {
        "feature": feature,
        "language": language,
        "stem": fixture["stem"],
        "categories": per_cat,
        "known_gap": known_gap,
        "perfect": perfect,
        "collisions": {
            "expected": key_collisions(expected.get("symbols", []), expected.get("edges", [])),
            "actual": key_collisions(result.symbols, result.edges),
        },
    }


def _pr(tp: int, emitted: int, gt: int):
    """(precision, recall) with the standard empty-set conventions."""
    precision = tp / emitted if emitted else (1.0 if gt == 0 else 0.0)
    recall = tp / gt if gt else 1.0
    return precision, recall


def aggregate(fixture_results: list[dict]) -> dict:
    """Micro-average per (language, category): pool tp/emitted/gt, then compute P/R.

    Micro-averaging (pool counts, then divide) weights facts equally regardless of how
    they are split across fixtures, and handles empty categories cleanly.
    """
    langs: dict[str, dict] = {}
    for fr in fixture_results:
        lang = fr["language"]
        bucket = langs.setdefault(lang, {c: {"tp": 0, "emitted": 0, "gt": 0} for c in _CATEGORIES})
        for cat in _CATEGORIES:
            c = fr["categories"][cat]
            bucket[cat]["tp"] += c["tp"]
            bucket[cat]["emitted"] += c["emitted"]
            bucket[cat]["gt"] += c["gt"]

    report = {}
    for lang, cats in sorted(langs.items()):
        report[lang] = {}
        for cat in _CATEGORIES:
            tp, emitted, gt = cats[cat]["tp"], cats[cat]["emitted"], cats[cat]["gt"]
            p, r = _pr(tp, emitted, gt)
            report[lang][cat] = {
                "precision": round(p, 4), "recall": round(r, 4),
                "tp": tp, "emitted": emitted, "gt": gt,
            }
    return report


def run() -> dict:
    """Discover, score, aggregate into two disjoint sets.

    `by_language` is the CLEAN set (known_gap fixtures excluded) — this is what the committed
    baseline gates on, so a documented gap can never dilute or inflate the gated number.
    `by_language_known_gap` reports the gap fixtures honestly; it never gates on a sub-1.0
    score, but an *unexpected pass* there is surfaced separately (see unexpected_passes).
    """
    fixtures = discover_fixtures()
    fixture_results = [score_fixture(f) for f in fixtures]
    clean = [fr for fr in fixture_results if not fr["known_gap"]]
    gaps = [fr for fr in fixture_results if fr["known_gap"]]
    return {
        "fixtures": fixture_results,
        "by_language": aggregate(clean),
        "by_language_known_gap": aggregate(gaps),
    }


def unexpected_passes(results: dict) -> list[str]:
    """known_gap fixtures that scored perfectly — the documented gap has apparently closed.

    This is an ALERT, not a silent success: the adapter improved (or the fixture drifted),
    so the fixture must drop its known_gap marker and the docs must be updated. Returns the
    offending feature names (empty = all gaps still manifest as expected).
    """
    return [fr["feature"] for fr in results["fixtures"] if fr["known_gap"] and fr["perfect"]]


def collision_report(results: dict) -> list[str]:
    """Flat list of key-collision messages across every fixture (empty = invariant holds).

    Checks both hand-authored ground truth and live adapter output: a collision on either
    side means normalization is merging distinct facts and the scorecard cannot be trusted.
    """
    msgs: list[str] = []
    for fr in results["fixtures"]:
        for side in ("expected", "actual"):
            for cat in ("symbols", "edges"):
                for norm_key, raws in fr["collisions"][side][cat].items():
                    raw_list = ", ".join(sorted(str(r) for r in raws))
                    msgs.append(
                        f"{fr['feature']} [{side}/{cat}]: {len(raws)} distinct keys collapse "
                        f"to {norm_key!r} — {raw_list}"
                    )
    return msgs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CAT_LABEL = {"symbols": "Symbols", "edges_all": "Edges (all)", "edges_call": "Call edges"}


def _render_lang_block(lines: list, by_lang: dict) -> None:
    for lang, cats in by_lang.items():
        lines.append(f"[{lang}]")
        for cat in _CATEGORIES:
            d = cats[cat]
            lines.append(
                f"  {_CAT_LABEL[cat]:12}  P={d['precision']:.3f}  R={d['recall']:.3f}"
                f"   (tp={d['tp']} emitted={d['emitted']} gt={d['gt']})"
            )
        lines.append("")


def render_scorecard(results: dict) -> str:
    lines = ["", "ADR-008 — Extraction Conformance Scorecard", "=" * 62]
    by_lang = results["by_language"]
    gap_lang = results.get("by_language_known_gap", {})
    if not by_lang and not gap_lang:
        lines.append("(no fixtures found under tests/fixtures/conformance/)")
        return "\n".join(lines)

    n_fix = len(results["fixtures"])
    n_gap = sum(1 for fr in results["fixtures"] if fr["known_gap"])
    n_lang = len({fr["language"] for fr in results["fixtures"]})
    lines.append(
        f"{n_fix} fixture(s) across {n_lang} language(s)  —  "
        f"{n_fix - n_gap} clean (gate CI), {n_gap} known-gap (reported, never gate)\n"
    )

    lines.append("CLEAN SET — regression-gated:")
    lines.append("-" * 62)
    _render_lang_block(lines, by_lang)

    if gap_lang:
        lines.append("KNOWN-GAP SET — documented gaps, reported honestly, excluded from the gate:")
        lines.append("-" * 62)
        _render_lang_block(lines, gap_lang)

    # Per-fixture missed/spurious detail — the honest gaps. Tag known-gap fixtures so an
    # expected sub-1.0 is not mistaken for a regression.
    detail = []
    for fr in results["fixtures"]:
        tag = " (known gap)" if fr["known_gap"] else ""
        for cat in _CATEGORIES:
            c = fr["categories"][cat]
            if c["missed"] or c["spurious"]:
                detail.append((fr["feature"] + tag, cat, c["missed"], c["spurious"]))
    if detail:
        lines.append("Detail (missed = not extracted; spurious = extracted but not expected):")
        for feat, cat, missed, spurious in detail:
            lines.append(f"  {feat} / {_CAT_LABEL[cat]}")
            for m in missed:
                lines.append(f"      MISS {m}")
            for s in spurious:
                lines.append(f"      SPUR {s}")
        lines.append("")

    # Alerts — either condition means the harness or a gap needs attention.
    ups = unexpected_passes(results)
    if ups:
        lines.append("!! UNEXPECTED PASS — a known_gap fixture now scores 1.0; the gap has closed:")
        for feat in ups:
            lines.append(f"     {feat}  → drop its known_gap marker and update the docs.")
    collisions = collision_report(results)
    if collisions:
        lines.append("!! KEY COLLISION — normalization is merging distinct facts (normalize_fqn defect?):")
        for msg in collisions:
            lines.append(f"     {msg}")
    return "\n".join(lines)


def render_readme_table(results: dict) -> str:
    """The committed per-language precision/recall table (ADR-008 §3).

    The table reports the CLEAN set only (known_gap fixtures excluded), and the fixture
    count is the clean count so the P/R and the N refer to the same fixtures. Documented
    gaps are listed separately below so they are visible without contaminating the number.
    """
    by_lang = results["by_language"]
    rows = [
        "| Language | Symbols P/R | Edges P/R | Call edges P/R | Fixtures |",
        "|----------|-------------|-----------|----------------|----------|",
    ]
    counts: dict[str, int] = {}
    for fr in results["fixtures"]:
        if fr["known_gap"]:
            continue
        counts[fr["language"]] = counts.get(fr["language"], 0) + 1
    for lang, cats in by_lang.items():
        def pr(cat):
            d = cats[cat]
            return f"{d['precision']:.2f} / {d['recall']:.2f}"
        rows.append(
            f"| {lang} | {pr('symbols')} | {pr('edges_all')} | {pr('edges_call')} "
            f"| {counts.get(lang, 0)} |"
        )
    note = (
        "\n_Measured on hand-authored feature fixtures (Tier-A adapters), not an exhaustive "
        "corpus — a row is \"precision/recall on the fixtures we wrote,\" never a language's "
        "true precision (ADR-008 §7). Regenerate with `python tools/conformance_eval.py "
        "--write-readme`._"
    )

    # Known-gap fixtures: listed, never averaged into the table above.
    gap_lines = []
    for fr in results["fixtures"]:
        if fr["known_gap"]:
            gap_lines.append(f"- **{fr['feature']}** — {fr['known_gap']['reason']}")
    if gap_lines:
        note += (
            "\n\n**Known extraction gaps** (encoded as correct ground truth; reported, not "
            "gated — the ruler catching real, documented limitations):\n" + "\n".join(gap_lines)
        )
    return "\n".join(rows) + "\n" + note


def _write_readme(results: dict) -> bool:
    if not os.path.isfile(_README_PATH):
        return False
    with open(_README_PATH, encoding="utf-8") as fh:
        text = fh.read()
    table = render_readme_table(results)
    block = f"{_README_START}\n{table}\n{_README_END}"
    if _README_START in text and _README_END in text:
        pre = text[: text.index(_README_START)]
        post = text[text.index(_README_END) + len(_README_END):]
        new = pre + block + post
    else:
        new = text.rstrip() + "\n\n## Extraction Accuracy (ADR-008)\n\n" + block + "\n"
    if new != text:
        with open(_README_PATH, "w", encoding="utf-8") as fh:
            fh.write(new)
    return True


def _baseline_view(results: dict) -> dict:
    """The regression-gate view: per-language per-category precision/recall only."""
    return {
        lang: {cat: {"precision": cats[cat]["precision"], "recall": cats[cat]["recall"]}
               for cat in _CATEGORIES}
        for lang, cats in results["by_language"].items()
    }


def check_baseline(results: dict, tol: float = 1e-4) -> list[str]:
    """Return a list of regression messages (empty = pass) vs the committed baseline."""
    if not os.path.isfile(_BASELINE_PATH):
        return [f"no baseline at {_BASELINE_PATH} — run --write-baseline first"]
    with open(_BASELINE_PATH, encoding="utf-8") as fh:
        base = json.load(fh)
    cur = _baseline_view(results)
    problems = []
    for lang, cats in base.items():
        if lang not in cur:
            problems.append(f"{lang}: present in baseline, absent now")
            continue
        for cat, metrics in cats.items():
            for metric, base_val in metrics.items():
                cur_val = cur[lang][cat][metric]
                if cur_val + tol < base_val:
                    problems.append(
                        f"{lang}/{cat}/{metric} regressed: {cur_val:.4f} < {base_val:.4f}"
                    )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="ADR-008 extraction conformance scorecard")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--write-baseline", action="store_true", help="commit current P/R as baseline")
    ap.add_argument("--check-baseline", action="store_true", help="fail (exit 1) on any P/R regression")
    ap.add_argument("--write-readme", action="store_true", help="regenerate the README table")
    args = ap.parse_args()

    results = run()
    print(render_scorecard(results))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\n[wrote {args.json}]")

    if args.write_baseline:
        os.makedirs(os.path.dirname(_BASELINE_PATH), exist_ok=True)
        with open(_BASELINE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_baseline_view(results), fh, indent=2, sort_keys=True)
        print(f"\n[wrote baseline {_BASELINE_PATH}]")

    if args.write_readme:
        ok = _write_readme(results)
        print(f"\n[readme {'updated' if ok else 'NOT found'}]")

    if args.check_baseline:
        rc = 0
        problems = check_baseline(results)
        if problems:
            print("\nBASELINE REGRESSION (clean set):")
            for p in problems:
                print(f"  {p}")
            rc = 1
        # Harness-integrity gates — independent of the P/R baseline.
        collisions = collision_report(results)
        if collisions:
            print("\nKEY-COLLISION FAILURE (normalization is merging distinct facts):")
            for msg in collisions:
                print(f"  {msg}")
            rc = 1
        ups = unexpected_passes(results)
        if ups:
            print("\nUNEXPECTED PASS (a known_gap fixture now scores 1.0 — update fixture + docs):")
            for feat in ups:
                print(f"  {feat}")
            rc = 1
        if rc == 0:
            print("\n[baseline OK; key-distinctness holds; known gaps still manifest]")
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
