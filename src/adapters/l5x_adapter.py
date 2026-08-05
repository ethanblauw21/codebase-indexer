"""
L5X adapter — Rockwell Automation Logix Designer XML export (ADR-013).

L5X has no tree-sitter grammar, so this is a hand-written adapter over the
export XML plus the Rockwell "neutral text" instruction language carried inside
each rung. It uses `xml.etree.ElementTree`; the survey work that preceded it ran
fine on stdlib XML, so `lxml` is not pulled in without a concrete need.

## What a file is

One L5X file is one entire controller, not one module. The largest file in the
survey corpus was 7.9 MB and 131,940 XML elements. That is a real mismatch with
file-granularity incremental indexing — any edit anywhere invalidates the whole
controller — and it is recorded as an open architectural question rather than
solved here.

## Chunking

The **routine** is the chunk unit and the **rung** is the addressable sub-unit.
Rungs are far too small to chunk on (median well under 130 characters); a
routine is a reasonable tier-1 chunk, mirroring the class-and-method shape the
other adapters already use.

## Embedded text

This adapter departs from every other one in the project. Elsewhere the source
text IS the embedding payload. Here neutral text like `XIC(a)XIO(b)OTE(c)`
carries almost no natural language, so the embedding payload is assembled from
rung comments, tag descriptions, routine and program names, and AOI parameter
descriptions. The neutral text is preserved verbatim in the chunk so lexical
search still finds what someone actually typed, but it is supporting structure
rather than the thing embedded.

## Scope

RLL (ladder) is extracted. **ST and FBD are explicitly unsupported**: their
routines are emitted as symbols so the structure is not silently missing, but
no body extraction is attempted. FBD needs a block-and-wire graph rather than
text extraction. ST extraction is a scoped follow-on (ADR-013 §3 pairs L5X with
IEC 61131-3 Structured Text).

See `l5x_instructions.py` for the mnemonic and operand-role tables, and
docs/adr/ADR-013 for the measured findings behind them.
"""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from adapters.base import Edge, ParseResult, Reference, Symbol
from adapters.l5x_instructions import (
    EXPR,
    KEYWORD,
    LABEL,
    LITERAL,
    ROUTINE,
    TAG,
    canonical_mnemonic,
    signature,
)

log = logging.getLogger(__name__)

# The instruction token immediately preceding an open paren.
_MNEMONIC_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,31})\(")

# Identifiers inside an array subscript, which are themselves tag reads.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_LITERAL_START = frozenset("0123456789-+.$'\"")

# Emitted on every AOI by the export; they carry no design intent and would add
# two noise symbols per definition.
_IMPLICIT_AOI_PARAMS = frozenset({"EnableIn", "EnableOut"})


# ------------------------------------------------------------------ helpers

def _text_of(elem) -> str:
    return "".join(elem.itertext()) if elem is not None else ""


def _describe(elem) -> str:
    """The Description child's text, flattened, or ''."""
    if elem is None:
        return ""
    node = elem.find("Description")
    return " ".join(_text_of(node).split()) if node is not None else ""


def scan_instructions(text: str):
    """Yield `(mnemonic, raw_operands, start, end)` from neutral text.

    A comma separates operands only where paren depth is 1 and bracket depth is
    0. Tracking parens alone splits a two-dimensional subscript like
    `Recipe[Row,Col]` at the comma inside the brackets and yields a phantom
    extra operand — measured at 36 operands across the survey corpus, and the
    cause of a matching count of unresolvable tag references.

    **Empty operand slots are preserved.** `GSV(Class,,Attr,Dest)` omits the
    instance name and is a four-operand call with a hole at position 1, not a
    three-operand call. Dropping empty slots renumbers every operand after the
    hole, and since roles are assigned positionally that silently re-types the
    rest of the instruction. Measured at 7 sites (6 `GSV`, 1 `SSV`).

    This is defensive, and honestly it currently changes nothing: reverting it
    reproduces the corpus edge counts exactly. `GSV` and `SSV` are the only
    instructions here that carry holes, and their role tuples are uniform
    keywords followed by a last-position destination, so a shift happens to
    re-type each operand to what it already was. It is kept because the parse
    should not depend on that coincidence, and flagged in ADR-013 §5.2 as a
    correction no fixture can currently give teeth to.

    The one case where an empty slot is genuinely not an operand is a call
    written with empty parens — `NOP()`, `TND()`, `AFI()`, 639 occurrences —
    which yields a single empty slot and is normalised to zero operands.
    """
    for m in _MNEMONIC_RE.finditer(text):
        i = m.end()
        start, paren, brack, args = i, 1, 0, []
        closed = False
        while i < len(text):
            c = text[i]
            if c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
                if paren == 0:
                    args.append(text[start:i])
                    closed = True
                    break
            elif c == "[":
                brack += 1
            elif c == "]":
                brack = max(0, brack - 1)
            elif c == "," and paren == 1 and brack == 0:
                args.append(text[start:i])
                start = i + 1
            i += 1
        if not closed:
            args.append(text[start:i])
        if len(args) == 1 and not args[0].strip():
            args = []          # `NOP()` — empty parens, not one empty operand
        yield m.group(1), args, m.start(), i + 1


def split_operand(operand: str) -> tuple[str | None, list[str]]:
    """Return `(base_name, index_identifiers)` for a tag-role operand.

    `Recipe[Row,Col].Member` yields `("Recipe", ["Row", "Col"])`. Literals,
    unbound `?` placeholders and empty operands yield `(None, [])`. Module I/O
    addresses keep their full text as the base, since they name hardware
    endpoints rather than declared symbols.
    """
    s = operand.strip()
    if not s or s[0] in _LITERAL_START or s.startswith("?"):
        return None, []

    indices: list[str] = []
    for chunk in re.findall(r"\[([^\]]*)\]", s):
        indices.extend(_IDENT_RE.findall(chunk))

    stripped = re.sub(r"\[[^\]]*\]", "", s)
    if ":" in stripped:
        # Module I/O: `DI_01:I.Data.0`. Hardware, not a declared symbol.
        return stripped, indices

    base = stripped.split(".")[0]
    return (base or None), indices


def is_module_io(name: str) -> bool:
    return ":" in name


class _Lines:
    """Maps a character offset in the source to a 1-based line number."""

    def __init__(self, text: str):
        self._starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._starts.append(i + 1)
        self._text = text

    def of_offset(self, offset: int) -> int:
        lo, hi = 0, len(self._starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def of_element(self, tag: str, name: str, start: int = 0) -> tuple[int, int]:
        """Best-effort line span for `<tag Name="name" ...>`.

        ElementTree does not record source positions, so the element is located
        by searching the raw text. A miss returns (1, 1) rather than raising —
        a wrong line number must not cost a symbol.
        """
        needle = f'<{tag} Name="{name}"'
        at = self._text.find(needle, start)
        if at < 0:
            return 1, 1
        close = self._text.find(f"</{tag}>", at)
        end = close if close >= 0 else at + len(needle)
        return self.of_offset(at), self.of_offset(end)


# ------------------------------------------------------------------ adapter

class L5xAdapter:
    language_id = "l5x"
    extensions  = frozenset({".L5X", ".l5x"})

    # Which symbol kinds become tier-1 chunks. `parameter` and `local_tag` are
    # deliberately absent: they are declarations internal to an AOI definition,
    # and there is no query whose best answer is one of them in isolation. On
    # the survey corpus they were 3,227 of 6,011 tier-1 chunks — more than half
    # the index — for symbols that only mean anything as part of the AOI whose
    # signature they belong to, which is chunked and carries their descriptions.
    #
    # This is a category error being corrected, not a granularity choice being
    # made. Whether a controller-scoped `tag` declaration should be a tier-1
    # chunk is a genuine question that could go either way, so `tag` stays in
    # and waits for gold queries and a harness run (ADR-013 §8).
    #
    # Symbols and edges are unaffected: all 6,011 symbols still reach the graph.
    chunkable_kinds = frozenset({
        "module", "tag", "aoi", "routine", "program",
    })

    def parse(self, path: str, src: bytes) -> ParseResult:
        try:
            text = src.decode("utf-8-sig")
        except UnicodeDecodeError:
            log.warning("l5x: %s is not UTF-8; skipping", path)
            return ParseResult([], [], [], [])

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            log.warning("l5x: %s failed to parse: %s", path, exc)
            return ParseResult([], [], [], [])

        # Every file in the survey corpus was a whole-controller export. A
        # routine- or program-level fragment export has different scoping, so
        # refuse it loudly rather than mis-attribute its symbols. Returning
        # empty rather than raising keeps the Protocol contract.
        target_type = root.attrib.get("TargetType")
        contains_context = root.attrib.get("ContainsContext", "false")
        if target_type != "Controller" or contains_context == "true":
            log.warning(
                "l5x: %s is a %s fragment export (ContainsContext=%s), not a "
                "whole-controller export; extraction is not supported",
                path, target_type or "unknown", contains_context,
            )
            return ParseResult([], [], [], [])

        return _Extractor(path, text, root).run()


    def analyze_tags(
        self,
        path: str,
        src: bytes,
        symbols: list[Symbol],
    ) -> tuple[list[str], dict[str, list[str]]]:
        return [], {}

    def test_conventions(self):
        return None

    def project_resolver(self):
        return None


class _Extractor:
    """One parse. Builds name registries first, then walks logic against them."""

    def __init__(self, path: str, text: str, root):
        self.path = path
        self.lines = _Lines(text)
        self.root = root
        self.symbols: list[Symbol] = []
        self.edges: list[Edge] = []
        self.references: list[Reference] = []
        self._seen_edges: set[tuple[str, str, str]] = set()

        # Registries, all filled before any logic is walked.
        self.controller_tags: dict[str, str] = {}          # name -> fqn
        self.program_tags: dict[str, dict[str, str]] = {}  # prog -> name -> fqn
        self.routines_by_program: dict[str, set[str]] = {}
        self.aoi_scope: dict[str, dict[str, str]] = {}     # aoi -> name -> fqn
        self.aoi_params: dict[str, list[dict]] = {}        # aoi -> ordered params
        self.aoi_names: set[str] = set()
        self.unknown_mnemonics: set[str] = set()
        self.unnamed_unkeyable = 0
        # Element kind -> how many were skipped for want of an expected
        # attribute. See `_skip`.
        self.skipped: dict[str, int] = {}

        # AOI positional binding, counted by the adapter rather than inferred
        # from the survey. The 418/418 arity rule was measured by a different
        # implementation from the one that ships, and 786 write edges depend on
        # it, so it is the one unverified figure with consequences attached.
        self.aoi_calls_bound = 0
        self.aoi_calls_arity_mismatch = 0

    # -- skipped elements ----------------------------------------------------

    def _skip(self, kind: str) -> None:
        """Record an element dropped because an expected attribute was absent.

        Every drop goes through here so that none of them is silent. A bare
        `if not name: continue` is what discarded 22 of 83 modules — a quarter
        of the hardware tree — while the parse reported success. The corpus
        that surfaced it happens to be clean for the other five element kinds,
        which is a fact about that corpus and not a property of the adapter.

        Counting rather than raising is deliberate: one malformed element
        should not cost an entire controller. The count is reported at the end
        of the parse, so the failure is loud without being fatal.
        """
        self.skipped[kind] = self.skipped.get(kind, 0) + 1

    # -- emit ---------------------------------------------------------------

    def _sym(self, fqn, kind, name, span, text="", class_context=None):
        self.symbols.append(Symbol(
            fqn=fqn, kind=kind, name=name, class_context=class_context,
            start_line=span[0], end_line=span[1], text=text,
        ))

    def _edge(self, source, target, kind, resolved=None):
        key = (source, target, kind)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append(Edge(
            source_fqn=source, target=target, kind=kind,
            resolved_target=resolved,
        ))

    # -- run ----------------------------------------------------------------

    def run(self) -> ParseResult:
        controller = self.root.find("Controller")
        if controller is None:
            log.warning("l5x: %s has no Controller element", self.path)
            return ParseResult([], [], [], [])

        self._collect_modules(controller)
        self._collect_controller_tags(controller)
        self._collect_aois(controller)
        self._collect_programs(controller)

        self._walk_aois(controller)
        self._walk_programs(controller)

        if self.skipped:
            log.warning(
                "l5x: %s discarded %d element(s) missing an expected "
                "attribute: %s. These are absent from the index; a parse that "
                "drops structure must not report success quietly",
                self.path, sum(self.skipped.values()),
                ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items())),
            )

        if self.aoi_calls_arity_mismatch:
            total = self.aoi_calls_bound + self.aoi_calls_arity_mismatch
            log.warning(
                "l5x: %s bound %d of %d AOI call sites positionally; %d had an "
                "operand count the arity rule (1 + required non-Enable "
                "parameters) does not predict, and their parameter edges are "
                "absent",
                self.path, self.aoi_calls_bound, total,
                self.aoi_calls_arity_mismatch,
            )

        if self.unknown_mnemonics:
            log.info(
                "l5x: %s used %d instruction(s) with no operand-role "
                "signature; reads emitted, writes suppressed: %s",
                self.path, len(self.unknown_mnemonics),
                ", ".join(sorted(self.unknown_mnemonics)),
            )

        return ParseResult(self.symbols, self.edges, self.references, [])

    # -- registries ----------------------------------------------------------

    def _module_fqn(self, mod) -> str | None:
        """Identity for one Module element, named or not.

        A `Name` attribute is NOT guaranteed. In the survey corpus 22 of 83
        modules (26.5%) carry none — they are sub-modules hanging off a parent
        device (drive peripherals, embedded drive ports) identified by their
        position in the hardware tree rather than by a name. Skipping them
        drops a quarter of the module hierarchy and every `owns` edge into it.

        Nameless modules are addressed by parent + port id + port address.
        Parent and port id alone are not enough: that pair collides 6 times in
        the corpus, while adding the port address is unique within every file
        and never collides with a named module's fqn.

        `Name` stays the key when present, and that is load-bearing rather than
        incidental: `ParentModule` refers to its parent *by name*, and in all 83
        corpus modules the parent named is one that carries a `Name`. Keying
        named modules on anything else would break every ownership edge.

        The address is taken from the **upstream** port — the module's address
        on its parent's bus. 18 modules expose two addressed ports, and in all
        18 the upstream one is second in document order, so taking the first
        addressed port keyed them on the downstream bus they provide instead of
        their own position in the tree.
        """
        name = mod.attrib.get("Name")
        if name:
            return name
        parent = mod.attrib.get("ParentModule")
        port_id = mod.attrib.get("ParentModPortId")
        addressed = [p for p in mod.iter("Port") if p.attrib.get("Address")]
        upstream = [p for p in addressed if p.attrib.get("Upstream") == "true"]
        address = next(
            (p.attrib["Address"] for p in (upstream or addressed)),
            None,
        )
        if not parent or port_id is None or address is None:
            # Nothing stable to key on. Counted rather than silently dropped.
            self.unnamed_unkeyable += 1
            self._skip("Module")
            return None
        return f"{parent}:{port_id}:{address}"

    def _collect_modules(self, controller) -> None:
        for mod in controller.iter("Module"):
            fqn = self._module_fqn(mod)
            if fqn is None:
                continue
            name = mod.attrib.get("Name") or fqn
            span = (self.lines.of_element("Module", name)
                    if mod.attrib.get("Name") else (1, 1))
            self._sym(fqn, "module", name, span, _describe(mod))
        # Parent edges in a second pass so both endpoints exist as symbols.
        for mod in controller.iter("Module"):
            fqn = self._module_fqn(mod)
            parent = mod.attrib.get("ParentModule")
            if fqn and parent:
                self._edge(parent, fqn, "owns")

    def _tags_in(self, container, scope_fqn: str | None, owner: str | None):
        """Emit tag symbols for one Tags container; return name -> fqn."""
        found: dict[str, str] = {}
        for tags_elem in container.findall("Tags"):
            for tag in tags_elem.findall("Tag"):
                name = tag.attrib.get("Name")
                if not name:
                    self._skip("Tag")
                    continue
                fqn = f"{scope_fqn}.{name}" if scope_fqn else name
                found[name] = fqn
                span = self.lines.of_element("Tag", name)
                self._sym(fqn, "tag", name, span, _describe(tag), scope_fqn)
                if owner:
                    self._edge(owner, fqn, "owns")
                # An alias is a symbol-to-hardware mapping in this corpus: all
                # 412 aliases pointed at module I/O and none at another tag.
                alias_for = tag.attrib.get("AliasFor")
                if alias_for:
                    self._edge(fqn, alias_for, "alias_of")
        return found

    def _collect_controller_tags(self, controller) -> None:
        self.controller_tags = self._tags_in(controller, None, None)

    def _collect_programs(self, controller) -> None:
        for prog in controller.iter("Program"):
            pname = prog.attrib.get("Name")
            if not pname:
                self._skip("Program")
                continue
            span = self.lines.of_element("Program", pname)
            self._sym(pname, "program", pname, span, _describe(prog))
            self.program_tags[pname] = self._tags_in(prog, pname, pname)

            owned = self.routines_by_program.setdefault(pname, set())
            for routine in prog.iter("Routine"):
                rname = routine.attrib.get("Name")
                if not rname:
                    self._skip("Routine")
                    continue
                owned.add(rname)
                fqn = f"{pname}.{rname}"
                rspan = self.lines.of_element("Routine", rname)
                self._sym(fqn, "routine", rname,
                          rspan, self._routine_text(routine, prog), pname)
                self._edge(pname, fqn, "owns")

    def _collect_aois(self, controller) -> None:
        for aoi in controller.iter("AddOnInstructionDefinition"):
            aname = aoi.attrib.get("Name")
            if not aname:
                self._skip("AddOnInstructionDefinition")
                continue
            self.aoi_names.add(aname)
            span = self.lines.of_element("AddOnInstructionDefinition", aname)
            self._sym(aname, "aoi", aname, span, _describe(aoi))

            scope: dict[str, str] = {}
            ordered: list[dict] = []
            for param in aoi.iter("Parameter"):
                pname = param.attrib.get("Name")
                if not pname:
                    self._skip("Parameter")
                    continue
                fqn = f"{aname}.{pname}"
                scope[pname] = fqn
                if pname in _IMPLICIT_AOI_PARAMS:
                    continue
                ordered.append({
                    "name": pname,
                    "usage": param.attrib.get("Usage", "Input"),
                    "required": param.attrib.get("Required") == "true",
                })
                pspan = self.lines.of_element("Parameter", pname)
                self._sym(fqn, "parameter", pname, pspan, _describe(param), aname)
                self._edge(aname, fqn, "owns")

            for local in aoi.iter("LocalTag"):
                lname = local.attrib.get("Name")
                if not lname:
                    self._skip("LocalTag")
                    continue
                fqn = f"{aname}.{lname}"
                scope[lname] = fqn
                lspan = self.lines.of_element("LocalTag", lname)
                self._sym(fqn, "local_tag", lname, lspan, _describe(local), aname)
                self._edge(aname, fqn, "owns")

            for routine in aoi.iter("Routine"):
                rname = routine.attrib.get("Name")
                if not rname:
                    self._skip("Routine")
                    continue
                fqn = f"{aname}.{rname}"
                rspan = self.lines.of_element("Routine", rname)
                self._sym(fqn, "routine", rname,
                          rspan, self._routine_text(routine, aoi), aname)
                self._edge(aname, fqn, "owns")

            self.aoi_scope[aname] = scope
            self.aoi_params[aname] = ordered

    # -- embedded text --------------------------------------------------------

    def _routine_text(self, routine, container) -> str:
        """Chunk body: assembled prose AND the neutral text verbatim.

        Both, not one or the other. The prose is what carries natural language
        and is what makes the chunk embeddable — neutral text like
        `XIC(a)XIO(b)OTE(c)` has almost none. But only about 30% of rungs carry
        a comment, so a prose-only chunk leaves roughly 70% of the ladder
        unreachable by any lexical query, and lexical matching is half of the
        hybrid retrieval this index is built on. An engineer searching for the
        instruction or tag they actually typed must find it.

        Rung numbers are kept inline so a hit can be traced back to a specific
        rung — the rung is the addressable sub-unit even though the routine is
        the chunk.
        """
        parts = [
            container.attrib.get("Name", ""),
            routine.attrib.get("Name", ""),
            _describe(routine),
        ]
        comments: list[str] = []
        logic: list[str] = []
        for rung in routine.iter("Rung"):
            number = rung.attrib.get("Number", "")
            comment = rung.find("Comment")
            if comment is not None:
                text = " ".join(_text_of(comment).split())
                if text:
                    comments.append(f"[{number}] {text}")
            body = " ".join(_text_of(rung.find("Text")).split())
            if body:
                logic.append(f"[{number}] {body}")
        parts.extend(comments)
        parts.extend(logic)
        return "\n".join(p for p in parts if p)

    # -- logic walk ------------------------------------------------------------

    def _walk_programs(self, controller) -> None:
        for prog in controller.iter("Program"):
            pname = prog.attrib.get("Name")
            if not pname:
                continue
            scope = dict(self.controller_tags)
            scope.update(self.program_tags.get(pname, {}))
            for routine in prog.iter("Routine"):
                rname = routine.attrib.get("Name")
                if rname:
                    self._walk_routine(routine, f"{pname}.{rname}", scope,
                                       owner_program=pname)

    def _walk_aois(self, controller) -> None:
        for aoi in controller.iter("AddOnInstructionDefinition"):
            aname = aoi.attrib.get("Name")
            if not aname:
                continue
            # AOI logic is hermetically encapsulated: across 2,156 AOI rungs in
            # the survey corpus it produced zero references outside its own
            # parameters and local tags. Resolve against AOI scope only, so a
            # name collision with a controller tag cannot leak an edge outward.
            scope = self.aoi_scope.get(aname, {})
            for routine in aoi.iter("Routine"):
                rname = routine.attrib.get("Name")
                if rname:
                    self._walk_routine(routine, f"{aname}.{rname}", scope,
                                       owner_program=None)

    def _walk_routine(self, routine, routine_fqn, scope, owner_program) -> None:
        rtype = routine.attrib.get("Type", "RLL")
        if rtype != "RLL":
            # ST and FBD are declared unsupported rather than left ambiguous.
            # The routine symbol is still emitted so the structure is visible.
            return
        for rung in routine.iter("Rung"):
            body = _text_of(rung.find("Text"))
            if body:
                self._scan(body, routine_fqn, scope, owner_program)

    def _scan(self, body, routine_fqn, scope, owner_program) -> None:
        for raw_mnemonic, operands, _s, _e in scan_instructions(body):
            # Positions are preserved, empties included: an omitted operand is
            # a hole, not an absent slot, and roles are assigned by position.
            operands = [o.strip() for o in operands]
            mnemonic = canonical_mnemonic(raw_mnemonic)

            if mnemonic in self.aoi_names:
                self._bind_aoi_call(mnemonic, operands, routine_fqn, scope)
                continue

            sig = signature(mnemonic)
            if sig is None:
                self.unknown_mnemonics.add(mnemonic)
            arity = len(operands)

            for i, operand in enumerate(operands):
                if not operand:
                    continue
                role = sig.role_at(i, arity) if sig else TAG
                if role in (LITERAL, KEYWORD, LABEL):
                    continue
                if role == ROUTINE:
                    self._bind_call(operand, routine_fqn, owner_program)
                    continue
                if role == EXPR:
                    # Expression operands are free-form; every identifier in
                    # them is a read. Nothing in an expression is written.
                    for ident in _IDENT_RE.findall(operand):
                        self._read(ident, routine_fqn, scope)
                    continue

                base, indices = split_operand(operand)
                # An index expression is a genuine read: the controller reads
                # Row and Col to compute the address of Recipe[Row,Col].
                for ident in indices:
                    self._read(ident, routine_fqn, scope)
                if base is None:
                    continue
                writes = bool(sig) and sig.writes_at(i, arity)
                both = bool(sig) and sig.both_at(i, arity)
                if writes or both:
                    self._write(base, routine_fqn, scope)
                if both or not writes:
                    self._read(base, routine_fqn, scope)

    def _resolve(self, name, scope) -> str | None:
        if is_module_io(name):
            return name          # hardware endpoint, not a declared symbol
        return scope.get(name)

    def _read(self, name, routine_fqn, scope) -> None:
        target = self._resolve(name, scope)
        if target:
            self._edge(routine_fqn, target, "reads")

    def _write(self, name, routine_fqn, scope) -> None:
        target = self._resolve(name, scope)
        if target:
            self._edge(routine_fqn, target, "writes")

    def _bind_call(self, operand, routine_fqn, owner_program) -> None:
        """A JSR target, resolved against the OWNING program's routines only.

        JSR is program-local. Resolving against a flat whole-file set of
        routine names makes two programs that each declare a routine of the
        same name resolve each other's calls; the survey corpus had 12 such
        names across its programs.
        """
        base, _ = split_operand(operand)
        if not base or owner_program is None:
            return
        if base in self.routines_by_program.get(owner_program, ()):
            self._edge(routine_fqn, f"{owner_program}.{base}", "call")

    def _bind_aoi_call(self, aoi_name, operands, routine_fqn, scope) -> None:
        """Bind an AOI invocation positionally.

        The arity rule is `1 + count(Required parameters excluding EnableIn and
        EnableOut)`, which held at 418 of 418 call sites in the survey corpus.
        Operand 0 is the backing instance tag, always written; operands 1..n
        bind in declaration order, with direction from Usage.
        """
        self._edge(routine_fqn, aoi_name, "call")
        if not operands:
            return

        instance, _ = split_operand(operands[0])
        if instance:
            self._write(instance, routine_fqn, scope)

        required = [p for p in self.aoi_params.get(aoi_name, []) if p["required"]]
        actuals = operands[1:]
        if len(actuals) != len(required):
            # Arity disagreement means positional binding is not safe here.
            # Emit the call edge and stop rather than bind to the wrong slots.
            self.aoi_calls_arity_mismatch += 1
            log.info(
                "l5x: %s call site passed %d operand(s) for %d required "
                "parameter(s); positional binding skipped",
                aoi_name, len(actuals), len(required),
            )
            return

        self.aoi_calls_bound += 1

        for param, actual in zip(required, actuals):
            base, indices = split_operand(actual)
            for ident in indices:
                self._read(ident, routine_fqn, scope)
            if not base:
                continue
            usage = param["usage"]
            if usage == "Input":
                self._read(base, routine_fqn, scope)
            elif usage == "Output":
                self._write(base, routine_fqn, scope)
            else:                      # InOut is passed by reference: both
                self._read(base, routine_fqn, scope)
                self._write(base, routine_fqn, scope)
