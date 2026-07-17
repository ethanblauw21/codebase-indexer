# Conformance Fixture Conventions (ADR-008)

How the extraction conformance suite is authored and scored. The suite lives in
`tests/fixtures/conformance/<language>/<feature>.{ext,expected.json}`, is scored by
`tools/conformance_eval.py`, and is gated by `tests/test_conformance.py`. It is the
*extraction*-accuracy sibling of the *retrieval* eval (ADR-007): pure tree-sitter parse,
no embedder/GPU — the whole suite runs in seconds on CPU. The real cost is fixture-authoring
labor, not compute.

## The authoring integrity rule (non-negotiable)

`expected.json` ground truth is authored **from source semantics — what a correct extractor
*should* emit — never by copying parser output.** Echoing the adapter guarantees a
meaningless 1.00 that measures nothing. The workflow:

1. Write a small source file exercising one feature.
2. **Calibrate** (learn *notation*, not correctness): dump the adapter's output to learn its
   conventions — edge kinds, FQN shape, how a call target is spelled.
3. Author `expected.json` in that notation but reflecting *correct* semantics.
4. Run the scorer. Anything below 1.00 is either an authoring error (fix the ground truth)
   or a **real adapter gap** (leave it; see [Known gaps](#known-gaps)).

## Symbol model — what counts as a symbol

A fixture measures the adapter against **its declared extraction contract** (the README /
adapter docstrings), not against an idealized universal model. The current symbol model is:

> **namespace-scoped types** (class, interface, struct, record, enum) **+ their members**
> (method, constructor, property).

**Fields are NOT symbols.** A C# field (`private readonly List<int> _lines`), like a Python
instance attribute or a TS class field, is data, not a named code entity in the model. This
is deliberate and consistent across all languages in the suite. Expanding the model to
include fields is a **feature ADR**, not a fixture decision — do not quietly add field
symbols to ground truth to chase coverage. The same holds for enum *members* (the values
`X`/`Y`/`Z` of an enum are not symbols) and events.

## FQN and edge notation

Identifiers are **path-normalized** before comparison (`normalize_fqn`) so fixtures are
checkout-portable:

- `…/sample.py::Foo.bar` → `Foo.bar` (strip the `<path>::` prefix).
- A genuine source-file path (`C:\…\async_gen.py`) → its basename (`async_gen.py`). "Genuine
  path" = has a separator **and** a file extension on the final component.
- Everything else is returned **unchanged**: bare call targets (`dumps`), modules (`json`),
  and namespaced FQNs — **including C# member FQNs of the form `Namespace.Type.Member/arity`**.

### C# FQNs (per ADR-003 D3)

- **Types:** `Namespace.Type` — namespace-qualified, **no file-path prefix** (C# namespaces
  are globally unique within a project, so file path is not needed for identity; this also
  lets partial classes across files share one FQN).
- **Nested types:** `Namespace.Outer+Inner` (CLR `+` separator).
- **Methods & constructors:** `Namespace.Type.Member/arity` — the `/arity` suffix is always
  applied. **Properties carry no arity.**
- **Edges:** `import` (using directive / project ref, sourced at the file), `owns`
  (type → each member), `extends` / `implements` (base-list), `call` (member → callee's
  final identifier). `new Foo()` is object creation, **not** a call edge.

### The `/arity` caveat and the key-distinctness invariant

The `/arity` suffix is **part of symbol identity**, not a path separator. A naive
`os.path.basename` splits on `/` and would collapse every C# method to its arity digit
(`Compute/0` and `Build/0` both → `0`) — silently merging distinct methods and, worse,
**masking real recall misses** (a dropped method hides behind a surviving sibling of the
same arity). C# is the first `/arity` language, so this only surfaced with the C# batch; it
was a **harness** defect in `normalize_fqn`, now fixed, and pinned by
`tests/test_conformance.py`.

To make this class of bug impossible to reintroduce silently, the suite enforces a
**key-distinctness invariant**: for every fixture, no two distinct ground-truth keys — and
no two distinct adapter-output keys — may normalize to the same key. A collision fails CI.

**Accepted coarseness:** two C# overloads with the *same name and same arity* legitimately
share a key (`Foo(int)` and `Foo(string)` are both `…Foo/1`). This is a deliberate, documented
limitation of an arity-based (rather than full-signature) FQN — not a bug. We simply do not
author same-name/same-arity overloads into a single fixture, so the invariant holds.

## Known gaps

Some fixtures deliberately encode **correct** semantics that the current adapter cannot yet
produce. These are marked with a `known_gap` block and are **expected to score below 1.0** —
that sub-1.0 is the ruler honestly catching a real, documented limitation.

```json
"known_gap": {
  "reason": "one-sentence description of the gap and why the score is sub-1.0",
  "ref": "docs/conformance-fixture-conventions.md#known-gaps"
}
```

Both fields are **required** — a `known_gap` without a `reason` and a `ref` (a pointer to the
documenting section or ADR) is an authoring error and fails validation. We refuse to record a
gap nobody can look up.

### Scoring: two disjoint aggregates

The scorer partitions fixtures into two sets that **never mix**:

- **Clean set** — every non-`known_gap` fixture. This is what the committed baseline
  (`benchmarks/conformance/baseline.json`) gates on. A documented gap can therefore never
  dilute or inflate the gated number.
- **Known-gap set** — reported honestly in the scorecard and README, but **never gates on a
  sub-1.0 score**.

### Alert on unexpected pass

A `known_gap` fixture that scores **perfectly** is an **alert**, not a silent success: the
adapter has improved (or the fixture drifted), the gap has closed, and the fixture must drop
its `known_gap` marker while its docs are updated. `--check-baseline` fails on this, on any
clean-set regression, and on any key collision.

### Current known gaps (C#)

- **`filescoped_namespace`** — under a file-scoped namespace (`namespace X;`, the .NET 6+
  default), types should be namespace-qualified (`Ledger.Account`). The adapter only
  propagates the namespace for **block-form** declarations; under a file-scoped namespace the
  type declarations are *siblings* of the `file_scoped_namespace_declaration` node in the
  tree-sitter grammar and are walked with no namespace, so every symbol comes out unqualified.
  Because the FQN is the symbol's identity, this zeroes out symbol/edge matching. **Undocumented
  adapter bug surfaced by this batch; adapter fix is a separate Major/ADR change (touches
  `src/`).**
- **`interface_impl_gap`** — a class implementing two interfaces with no base class: both are
  correctly `implements`, but the adapter's base-list heuristic (first entry → `extends`, rest
  → `implements`) mislabels the first as `extends`. **Documented limit** in
  `src/adapters/csharp_adapter.py` ("Base-list extends/implements … cannot distinguish base
  class vs interface without type resolution").

## Adding a language

1. Confirm the adapter + its extension are registered (`src/adapters/__init__.py`) and the
   extension maps to the language in `conformance_eval._LANG_BY_EXT`.
2. Author feature fixtures under `tests/fixtures/conformance/<language>/` following the rules
   above.
3. `python tools/conformance_eval.py` to score; iterate until clean fixtures are 1.00 and any
   real gaps are captured as `known_gap`.
4. `--write-baseline` and `--write-readme` to commit the numbers, then run `pytest
   tests/test_conformance.py`.
