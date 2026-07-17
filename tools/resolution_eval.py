"""ADR-011 §4 — call-edge RESOLUTION conformance scorecard (the lift the ADR promises).

The *resolution* sibling of ADR-008's *extraction* scorecard (`tools/conformance_eval.py`).
Where that harness scores what the adapters *emit* (bare call targets, one pure tree-sitter
parse), this scores what `call_resolver` can *resolve* those calls to — the number ADR-011
stands on: with receiver-type inference, the **call-edge resolution rate rises while precision
is held** (ADR-011 §4). It is the referee that ADR promised; the extraction harness never runs
the resolver, so it cannot see this.

Each fixture is scored TWICE through the real pipeline (`parse_file` → `db.upsert_file` →
`resolve_call_edges`), on the same source:

    typed    — edges keep their `receiver_type` hint     (ADR-011)
    baseline — the hint is stripped before indexing       (ADR-021, name-only resolution)

The delta between the two is the lift *attributable to receiver typing* — same parser, same
resolver, the hint the only difference. Ground truth lives beside the source at
`tests/fixtures/resolution/<language>/<feature>.resolution.json`:

    {"calls": [{"source": <caller fqn>, "target": <bare callee>, "expected": <fqn|null>}, ...]}

`expected` is the single correct in-repo target, or `null` where the call is *correctly
unresolvable* (prefer-unknown, §2 — external receiver type, chained receiver, overload set).

AUTHORING RULE (inherited from ADR-008): `expected` is authored from the SOURCE SEMANTICS —
the target a correct resolver *should* pick — never copied from resolver output. Echoing the
resolver guarantees a meaningless pass and measures nothing. `null` is a first-class expected
value: a resolver that resolves a null-expected site has manufactured a wrong edge.

Metrics, per language, over the declared call sites:
    resolvable = sites with a non-null expected      (the resolvable universe)
    hits       = resolved to EXACTLY the expected fqn
    wrong      = resolved to a non-null target that is NOT the expected  (a wrong edge —
                 covers both "expected a target, got the wrong one" and "expected null, got one")
    resolution rate = hits / resolvable              (coverage of the resolvable universe)
    precision       = hits / (hits + wrong)          (are the resolutions we assert correct?)

§4 pass condition (encoded in `check_baseline` and gated by tests/test_resolution_conformance.py):
    rate(typed) > rate(baseline)  AND  precision(typed) == 1.0  AND  precision(baseline) == 1.0
    (the baseline is prefer-unknown too — it fails to resolve ambiguous names, it never
     mis-resolves; the lift must come entirely from resolving MORE, never from guessing.)

CPU-only: `upsert_file` is called with no chunks, so nothing is embedded — pure tree-sitter +
SQLite, no model load, no GPU (consistent with the extraction harness).

CLI:
    python tools/resolution_eval.py                  # print the scorecard
    python tools/resolution_eval.py --json out.json  # machine-readable results
    python tools/resolution_eval.py --write-baseline # commit the current numbers
    python tools/resolution_eval.py --check-baseline # gate: lift holds, precision 1.0, no regression
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conformance_eval import normalize_fqn  # reuse the path-prefix normalizer  # noqa: E402

_FIXTURE_ROOT = os.path.join(_ROOT, "tests", "fixtures", "resolution")
_BASELINE_PATH = os.path.join(_ROOT, "benchmarks", "resolution", "baseline.json")

_LANG_BY_EXT = {".cs": "csharp", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp"}
_SOURCE_EXTS = tuple(_LANG_BY_EXT)

_REGIMES = ("typed", "baseline")


def discover_fixtures() -> list[dict]:
    """Every `<feature>.resolution.json` with a sibling source file, sorted by path."""
    fixtures = []
    if not os.path.isdir(_FIXTURE_ROOT):
        return fixtures
    for dirpath, _dirs, files in os.walk(_FIXTURE_ROOT):
        for fn in sorted(files):
            if not fn.endswith(".resolution.json"):
                continue
            stem = fn[: -len(".resolution.json")]
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


def _resolved_map(source_path: str, content: str, strip_hints: bool) -> dict:
    """Index one source through the real pipeline and return {(norm_source, target): resolved}.

    `strip_hints=True` nulls every edge's `receiver_type` before indexing, reproducing the
    ADR-021 (name-only) regime on the identical parse — the hint is the only variable.
    """
    from ast_chunker import parse_file
    from db import CodeDB
    from call_resolver import resolve_call_edges

    result = parse_file(source_path, content)
    edges = result.edges
    if strip_hints:
        for e in edges:
            e.receiver_type = None

    tmpdir = tempfile.mkdtemp(prefix="resoleval_")
    db = CodeDB(os.path.join(tmpdir, "graph.db"))
    try:
        db.upsert_file(source_path, "hash:" + source_path, result.symbols, edges)
        resolve_call_edges(db)
        rows = db._conn.execute(
            "SELECT source_fqn, target, resolved_target FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    finally:
        db.close()

    out: dict = {}
    for src, tgt, resolved in rows:
        out[(normalize_fqn(src), tgt)] = resolved
    return out


def _classify(expected, actual) -> str:
    """One declared call site → 'hit' | 'wrong' | 'unknown_ok' | 'miss'.

    hit         expected a target, got exactly it
    wrong       got a non-null target that is not the expected one (a WRONG edge — the
                precision violation the correctness gate forbids; includes expected==null)
    unknown_ok  expected null, got null (correct prefer-unknown)
    miss        expected a target, got null (recall gap — NOT a precision violation)
    """
    if actual is not None and actual != expected:
        return "wrong"
    if expected is None:
        return "unknown_ok"          # actual is None here (else caught by 'wrong' above)
    return "hit" if actual == expected else "miss"


def score_fixture(fixture: dict) -> dict:
    """Score one fixture in both regimes against its ground truth.

    Returns per-regime counts, the derived rate/precision, and structural-integrity lists
    (`missed_edges` = declared but not emitted; `undeclared_edges` = emitted but not in ground
    truth) — either is an authoring/extraction defect that makes the number untrustworthy.
    """
    if fixture["source_path"] is None:
        raise FileNotFoundError(
            f"resolution.json without a sibling source file: {fixture['expected_path']}"
        )
    with open(fixture["expected_path"], encoding="utf-8") as fh:
        spec = json.load(fh)
    with open(fixture["source_path"], encoding="utf-8") as fh:
        content = fh.read()

    ext = os.path.splitext(fixture["source_path"])[1].lower()
    language = spec.get("language") or _LANG_BY_EXT.get(ext, "unknown")
    feature = spec.get("feature", fixture["stem"])
    calls = spec.get("calls", [])
    declared = {(normalize_fqn(c["source"]), c["target"]): c.get("expected") for c in calls}

    regimes: dict = {}
    emitted_keys: set = set()
    for regime in _REGIMES:
        actual = _resolved_map(fixture["source_path"], content, strip_hints=(regime == "baseline"))
        emitted_keys = set(actual)  # identical across regimes (same parse); captured once
        counts = {"hit": 0, "wrong": 0, "unknown_ok": 0, "miss": 0}
        details = []
        for key, expected in declared.items():
            got = actual.get(key)               # None if edge absent (surfaced as missed below)
            verdict = _classify(expected, got)
            counts[verdict] += 1
            if verdict in ("wrong", "miss"):
                details.append((key, expected, got, verdict))
        resolvable = counts["hit"] + counts["miss"]
        resolved = counts["hit"] + counts["wrong"]
        regimes[regime] = {
            "counts": counts,
            "resolvable": resolvable,
            "resolved": resolved,
            "rate": round(counts["hit"] / resolvable, 4) if resolvable else 1.0,
            "precision": round(counts["hit"] / resolved, 4) if resolved else 1.0,
            "details": details,
        }

    declared_keys = set(declared)
    return {
        "feature": feature,
        "language": language,
        "stem": fixture["stem"],
        "n_calls": len(declared),
        "regimes": regimes,
        "missed_edges": sorted("|".join(k) for k in (declared_keys - emitted_keys)),
        "undeclared_edges": sorted("|".join(k) for k in (emitted_keys - declared_keys)),
    }


def aggregate(fixture_results: list[dict]) -> dict:
    """Micro-average per (language, regime): pool hit/wrong/resolvable, then derive rate/prec."""
    langs: dict = {}
    for fr in fixture_results:
        bucket = langs.setdefault(
            fr["language"],
            {r: {"hit": 0, "wrong": 0, "resolvable": 0, "resolved": 0} for r in _REGIMES},
        )
        for regime in _REGIMES:
            rg = fr["regimes"][regime]
            bucket[regime]["hit"] += rg["counts"]["hit"]
            bucket[regime]["wrong"] += rg["counts"]["wrong"]
            bucket[regime]["resolvable"] += rg["resolvable"]
            bucket[regime]["resolved"] += rg["resolved"]

    report: dict = {}
    for lang, regimes in sorted(langs.items()):
        report[lang] = {}
        for regime in _REGIMES:
            b = regimes[regime]
            report[lang][regime] = {
                "rate": round(b["hit"] / b["resolvable"], 4) if b["resolvable"] else 1.0,
                "precision": round(b["hit"] / b["resolved"], 4) if b["resolved"] else 1.0,
                "hit": b["hit"], "wrong": b["wrong"],
                "resolvable": b["resolvable"], "resolved": b["resolved"],
            }
    return report


def run() -> dict:
    fixture_results = [score_fixture(f) for f in discover_fixtures()]
    return {"fixtures": fixture_results, "by_language": aggregate(fixture_results)}


def integrity_report(results: dict) -> list[str]:
    """Structural defects: a declared call not emitted, or an emitted call not declared.

    Either means the ground truth and the adapter output disagree about which calls EXIST, so
    the resolution number is measured over the wrong set. Empty = ground truth is complete.
    """
    msgs: list[str] = []
    for fr in results["fixtures"]:
        for m in fr["missed_edges"]:
            msgs.append(f"{fr['feature']}: declared call not emitted by adapter — {m}")
        for u in fr["undeclared_edges"]:
            msgs.append(f"{fr['feature']}: emitted call missing from ground truth — {u}")
    return msgs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scorecard(results: dict) -> str:
    lines = ["", "ADR-011 §4 — Call-Edge Resolution Conformance Scorecard", "=" * 66]
    by_lang = results["by_language"]
    if not by_lang:
        lines.append("(no fixtures found under tests/fixtures/resolution/)")
        return "\n".join(lines)

    n_fix = len(results["fixtures"])
    n_lang = len(by_lang)
    lines.append(
        f"{n_fix} fixture(s) across {n_lang} language(s)  —  "
        f"baseline = ADR-021 (name-only), typed = ADR-011 (receiver-type hint)\n"
    )
    for lang, regimes in by_lang.items():
        b, t = regimes["baseline"], regimes["typed"]
        lift = round(t["rate"] - b["rate"], 4)
        lines.append(f"[{lang}]")
        lines.append(
            f"  baseline   rate={b['rate']:.3f}  precision={b['precision']:.3f}"
            f"   (hit={b['hit']}/{b['resolvable']} resolvable, wrong={b['wrong']})"
        )
        lines.append(
            f"  typed      rate={t['rate']:.3f}  precision={t['precision']:.3f}"
            f"   (hit={t['hit']}/{t['resolvable']} resolvable, wrong={t['wrong']})"
        )
        lines.append(f"  LIFT       +{lift:.3f} resolution rate, precision held\n")

    # Per-fixture misses/wrongs in the typed regime — the honest gaps.
    detail = []
    for fr in results["fixtures"]:
        for key, expected, got, verdict in fr["regimes"]["typed"]["details"]:
            detail.append((fr["feature"], "|".join(key), expected, got, verdict))
    if detail:
        lines.append("Typed-regime detail (miss = unresolved; WRONG = mis-resolved edge):")
        for feat, key, expected, got, verdict in detail:
            tag = "WRONG" if verdict == "wrong" else "miss "
            lines.append(f"  {tag} {feat}  {key}  expected={expected}  got={got}")
        lines.append("")

    integ = integrity_report(results)
    if integ:
        lines.append("!! INTEGRITY — ground truth and adapter output disagree on which calls exist:")
        for msg in integ:
            lines.append(f"     {msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baseline / gate
# ---------------------------------------------------------------------------


def _baseline_view(results: dict) -> dict:
    """The committed view: per-language typed & baseline rate + precision."""
    return {
        lang: {regime: {"rate": regimes[regime]["rate"], "precision": regimes[regime]["precision"]}
               for regime in _REGIMES}
        for lang, regimes in results["by_language"].items()
    }


def check_baseline(results: dict, tol: float = 1e-4) -> list[str]:
    """Return regression/invariant-violation messages (empty = pass).

    Enforces the ADR-011 §4 contract on the live numbers AND guards the committed baseline:
      1. typed precision == 1.0            (the correctness gate — no wrong edges)
      2. baseline precision == 1.0         (the lift is more-resolved, never guessed)
      3. typed rate > baseline rate        (there IS a lift)
      4. no regression vs committed typed rate/precision
      5. structural integrity holds        (ground truth complete)
    """
    problems: list[str] = []
    cur = _baseline_view(results)

    for lang, regimes in cur.items():
        t, b = regimes["typed"], regimes["baseline"]
        if t["precision"] + tol < 1.0:
            problems.append(f"{lang}: typed precision {t['precision']:.4f} < 1.0 — a wrong edge (§2 gate)")
        if b["precision"] + tol < 1.0:
            problems.append(f"{lang}: baseline precision {b['precision']:.4f} < 1.0 — baseline mis-resolved")
        if t["rate"] <= b["rate"] + tol:
            problems.append(
                f"{lang}: no resolution lift — typed rate {t['rate']:.4f} "
                f"not above baseline {b['rate']:.4f} (§4)"
            )

    if os.path.isfile(_BASELINE_PATH):
        with open(_BASELINE_PATH, encoding="utf-8") as fh:
            base = json.load(fh)
        for lang, regimes in base.items():
            if lang not in cur:
                problems.append(f"{lang}: present in baseline, absent now")
                continue
            for metric in ("rate", "precision"):
                base_val = regimes["typed"][metric]
                cur_val = cur[lang]["typed"][metric]
                if cur_val + tol < base_val:
                    problems.append(
                        f"{lang}/typed/{metric} regressed: {cur_val:.4f} < {base_val:.4f}"
                    )
    else:
        problems.append(f"no baseline at {_BASELINE_PATH} — run --write-baseline first")

    problems.extend(integrity_report(results))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="ADR-011 §4 call-edge resolution conformance scorecard")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--write-baseline", action="store_true", help="commit current numbers as baseline")
    ap.add_argument("--check-baseline", action="store_true", help="gate: lift holds, precision 1.0, no regression")
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

    if args.check_baseline:
        problems = check_baseline(results)
        if problems:
            print("\nRESOLUTION CONFORMANCE FAILURE:")
            for p in problems:
                print(f"  {p}")
            return 1
        print("\n[resolution lift holds; typed precision 1.0; ground truth complete]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
