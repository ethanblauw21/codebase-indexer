"""type_resolver.py — receiver-type inference for receiver-typed languages (ADR-011).

ADR-021 (`call_resolver.py`) resolves the *unambiguous* call — a bare callee name with
exactly one in-repo target. ADR-011 adds the **hard, ambiguous** case that a bare name
cannot settle: ``recv.Method()`` in a receiver-typed language, where several classes define
``Method`` and only the *type of the receiver* says which one is called.

This module is the lightweight end of what an LSP does — the "hybrid type-resolution pass"
of ADR-011 §1: it combines the syntactic evidence tree-sitter already exposes (declaration
sites, parameter types, ``new T()`` initializers) with method-scoped name resolution to
infer the receiver's type. It does **not** stand up a language server or do full semantic
analysis.

The output is a *type hint* per call edge — the receiver's inferred type name, or ``None``.
It is deliberately conservative (ADR-011 §2, prefer-unknown): every case it is not sure of
returns ``None`` rather than a guess. The hint does not itself resolve anything — it travels
on ``Edge.receiver_type`` to ``call_resolver``, which turns a hint plus the whole-repo symbol
table into a provable unique target (or, failing that, leaves the edge unresolved). A wrong
type hint could only ever *fail to match* a real symbol; it can never manufacture a wrong
edge, because the resolver still requires a unique in-repo owner-type match.

Both passes implement only the **exact** strategies — the receiver is a simple identifier (or
``this``) whose declared type is read directly off a declaration:

    C# (Stage 1)                                                    →  inferred type
    * parameter type            void M(Foo x) { x.Bar(); }          →  Foo
    * explicit local type       Foo x = Make(); x.Bar();            →  Foo
    * `var` + object creation    var x = new Foo(); x.Bar();         →  Foo
    * field type                private Foo _x;  … _x.Bar();         →  Foo   (enclosing class)
    * `this`                     this.Bar();                         →  <enclosing type>

    C++ (Stage 2), receiver via `->` or `.`                         →  inferred type
    * parameter type            void m(Repo* r) { r->Save(); }      →  Repo
    * local declaration         Repo x; x.Save();                   →  Repo
    * field type                Repo* repo_;  … repo_->Save();      →  Repo   (enclosing class)
    * `this`                     this->Save();                       →  <enclosing type>

Heuristic member-chain inference (``a.b().c()``), `var`/`auto` from a method return type, and
C++ overload sets (a known type but several arity/signature matches) are deliberately out of
scope — they resolve to ``None`` (unknown), or, for an in-repo type whose method name is
overloaded, land as a non-unique match that ``call_resolver`` leaves unresolved. Those are the
graded, lower-confidence strategies a follow-up adds (ADR-011 §3).
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

from adapters._treesitter import node_text

try:  # adapters import this as a sibling module; tests import it top-level
    from tree_sitter import Node
except Exception:  # pragma: no cover - tree_sitter always present at runtime
    Node = object  # type: ignore


class CallTarget(NamedTuple):
    """A call site's bare callee name plus, when inferable, the receiver's type name.

    ``receiver_type`` is ``None`` for a bare call (``Foo()``) and for any receiver the pass
    cannot resolve exactly — the honest unknown that keeps a wrong hint out of the graph.
    """
    name: str
    receiver_type: Optional[str]


# C# type nodes whose *name* we read as a plain single-segment identifier. Anything else
# (generic_name `List<T>`, array_type `Foo[]`, predefined_type `int`, tuple, function
# pointer) is deliberately NOT inferred — returning None keeps us on the prefer-unknown side.
_SIMPLE_TYPE_KINDS = frozenset({"identifier", "qualified_name"})


def _simple_type_name(type_node: "Node", src: bytes) -> Optional[str]:
    """Return the single-segment type name of a C# `type` node, or None if not simple.

    ``Foo`` → ``"Foo"``; ``Ns.Sub.Foo`` → ``"Foo"`` (last segment, matching the symbol
    ``name`` column); ``Foo?`` → ``"Foo"``. Generics, arrays, predefined types, and anything
    unexpected → ``None`` (unknown), because their receivers are external or unresolvable by
    the exact strategies.
    """
    if type_node is None:
        return None
    t = type_node.type

    # `Foo?` — unwrap one nullable layer to the underlying type.
    if t == "nullable_type":
        inner = type_node.child_by_field_name("type")
        if inner is None:
            inner = next((c for c in type_node.children if c.is_named), None)
        return _simple_type_name(inner, src)

    if t == "identifier":
        return node_text(type_node, src) or None

    if t == "qualified_name":
        # dotted `Ns.Sub.Foo` — the type name is the final segment, but ONLY if that
        # segment is a plain identifier. `Ns.List<int>` ends in a generic_name, not a
        # simple type, so it stays unknown rather than yielding a bogus "List<int>".
        named = [c for c in type_node.children if c.is_named]
        if named and named[-1].type == "identifier":
            return node_text(named[-1], src) or None
        return None

    return None


def _creation_type_name(init_node: "Node", src: bytes) -> Optional[str]:
    """Type of an initializer expression, only for the exact `new T()` case.

    ``var x = new Foo();`` → ``"Foo"``. Any other initializer (method call, cast, literal)
    → ``None``: inferring a receiver type from a method's return type is a Stage-2 heuristic,
    not an exact strategy, so we stay unknown here.
    """
    if init_node is None:
        return None
    if init_node.type == "object_creation_expression":
        return _simple_type_name(init_node.child_by_field_name("type"), src)
    return None


def _build_scope(
    method_node: "Node",
    src: bytes,
    field_types: dict[str, str],
    enclosing_type_name: Optional[str],
) -> dict[str, str]:
    """Map every in-method receiver identifier to its inferred type name.

    Precedence for a name declared more than once (rare): parameters and locals shadow
    fields, and a later local declaration wins — but any name that resolves to *different*
    types across declarations is dropped to unknown (removed from the map), because the pass
    must not pick one arbitrarily. Fields of the enclosing class seed the scope first, so a
    local of the same name overrides them.
    """
    scope: dict[str, str] = dict(field_types)
    conflicted: set[str] = set()

    def _set(name: str, type_name: Optional[str]) -> None:
        if not name or not type_name:
            return
        if name in scope and scope[name] != type_name:
            conflicted.add(name)
        scope[name] = type_name

    # Parameters: `void M(Foo x)` → x : Foo
    plist = next((c for c in method_node.children if c.type == "parameter_list"), None)
    if plist is not None:
        for p in plist.children:
            if p.type != "parameter":
                continue
            pname = p.child_by_field_name("name")
            ptype = p.child_by_field_name("type")
            if pname is not None:
                _set(node_text(pname, src), _simple_type_name(ptype, src))

    # Local declarations, anywhere in the body: `Foo x = …;` or `var x = new Foo();`
    def walk(n: "Node") -> None:
        if n.type == "variable_declaration":
            type_node = n.child_by_field_name("type")
            declared = _simple_type_name(type_node, src)
            is_var = type_node is not None and node_text(type_node, src) == "var"
            for decl in (c for c in n.children if c.type == "variable_declarator"):
                nm = decl.child_by_field_name("name")
                if nm is None:
                    continue
                name = node_text(nm, src)
                if declared is not None and not is_var:
                    _set(name, declared)          # explicit type: Foo x = …
                elif is_var:
                    init = decl.child_by_field_name("value")
                    if init is None:
                        init = next(
                            (c for c in decl.children
                             if c.is_named and c.type != "identifier"),
                            None,
                        )
                    _set(name, _creation_type_name(init, src))  # var x = new Foo()
        for c in n.children:
            walk(c)

    body = next((c for c in method_node.children if c.type == "block"), None)
    if body is not None:
        walk(body)

    for name in conflicted:
        scope.pop(name, None)   # ambiguous across declarations → unknown, never a guess

    # `this` resolves to the enclosing type. A user variable literally named `this` is not
    # legal C#, so this cannot clobber a real local.
    if enclosing_type_name:
        scope["this"] = enclosing_type_name
    return scope


def infer_csharp_call_targets(
    method_node: "Node",
    src: bytes,
    field_types: dict[str, str],
    enclosing_type_name: Optional[str],
) -> list[CallTarget]:
    """Return every call target in a C# method/constructor body, receiver type where known.

    Reproduces exactly the bare-name call edges the adapter emitted before (one per
    ``invocation_expression``, callee = final identifier) and *adds* ``receiver_type`` for
    the exact strategies above. Receivers that are not a simple identifier or ``this`` (a
    chained ``a.b().c()``, an indexer, a parenthesized expression) stay ``None``.
    """
    scope = _build_scope(method_node, src, field_types, enclosing_type_name)
    targets: list[CallTarget] = []

    def walk(n: "Node") -> None:
        if n.type == "invocation_expression":
            fn = n.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    name = node_text(fn, src)
                    if name:
                        targets.append(CallTarget(name, None))   # bare Foo()
                elif fn.type == "member_access_expression":
                    name_node = fn.child_by_field_name("name")
                    recv_node = fn.child_by_field_name("expression")
                    name = node_text(name_node, src) if name_node is not None else ""
                    if name:
                        targets.append(CallTarget(name, _receiver_type(recv_node, scope, src)))
        for c in n.children:
            walk(c)

    walk(method_node)
    return targets


def _receiver_type(
    recv_node: "Optional[Node]", scope: dict[str, str], src: bytes
) -> Optional[str]:
    """Infer the receiver's type name from an exact strategy, else None (unknown)."""
    if recv_node is None:
        return None
    # `this` is a bare keyword node in tree-sitter-c-sharp (type "this"), not "this_expression".
    if recv_node.type in ("this", "this_expression"):
        return scope.get("this")
    if recv_node.type == "identifier":
        return scope.get(node_text(recv_node, src))
    return None   # chains, casts, indexers, literals: Stage-2 heuristics — stay unknown


def csharp_field_types(type_node: "Node", src: bytes) -> dict[str, str]:
    """Map field name → declared type name for the direct fields of a C# type declaration.

    Only simple declared types are recorded (ADR-011 §2). Fed into every method's scope so
    ``_field.Method()`` resolves to the field's type.
    """
    out: dict[str, str] = {}
    decl_list = next(
        (c for c in type_node.children if c.type == "declaration_list"), None
    )
    if decl_list is None:
        return out
    for member in decl_list.children:
        if member.type != "field_declaration":
            continue
        var_decl = next(
            (c for c in member.children if c.type == "variable_declaration"), None
        )
        if var_decl is None:
            continue
        type_name = _simple_type_name(var_decl.child_by_field_name("type"), src)
        if type_name is None:
            continue
        for decl in (c for c in var_decl.children if c.type == "variable_declarator"):
            nm = decl.child_by_field_name("name")
            if nm is not None:
                fname = node_text(nm, src)
                if fname:
                    out[fname] = type_name
    return out


# =========================================================================== #
# C++ (Stage 2)
# =========================================================================== #

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

# Declarator wrappers a variable/parameter/field name can hide behind in C++.
_CPP_DECLARATOR_WRAPPERS = frozenset({
    "pointer_declarator", "reference_declarator", "rvalue_reference_declarator",
    "init_declarator", "array_declarator", "parenthesized_declarator",
})


def _as_plain_ident(name: Optional[str]) -> Optional[str]:
    """Return ``name`` iff it is a single plain identifier (no ``<>``, ``::``, ``*``); else None.

    Keeps a generic (``List<int>``), a still-qualified fragment, or any decorated type off the
    hint channel — the prefer-unknown guard for C++ type names.
    """
    return name if name and _IDENT_RE.match(name) else None


def _cpp_type_name_of(decl_node: "Node", src: bytes) -> Optional[str]:
    """The simple type name of a C++ parameter/field/local declaration, or None.

    Reads the first type child (``const Repo& r`` → ``Repo``; ``app::Repo x`` → ``Repo``).
    ``auto``, primitive types, templates, and function pointers → None (unknown).
    """
    for c in decl_node.children:
        if c.type == "type_identifier":
            return _as_plain_ident(node_text(c, src))
        if c.type == "qualified_identifier":
            return _as_plain_ident(node_text(c, src).rsplit("::", 1)[-1])
    return None


def _cpp_inner_id(node: "Node", src: bytes) -> Optional[str]:
    """Innermost declared name behind pointer/reference/init/array declarator wrappers.

    ``* r`` → ``r``; ``x = get()`` → ``x`` (the initializer is never descended into for a
    name, since the wanted identifier always precedes it).
    """
    t = node.type
    if t in ("identifier", "field_identifier"):
        return node_text(node, src)
    if t in _CPP_DECLARATOR_WRAPPERS:
        for c in node.children:
            r = _cpp_inner_id(c, src)
            if r:
                return r
    return None


def _cpp_decl_names(decl_node: "Node", src: bytes) -> list[str]:
    """Declared variable/field names of a C++ declaration (skipping the type child)."""
    names: list[str] = []
    for c in decl_node.children:
        nm = _cpp_inner_id(c, src)   # type_identifier/qualifiers return None here
        if nm:
            names.append(nm)
    return names


def _cpp_has_function_declarator(field_decl: "Node") -> bool:
    """True if a field_declaration is actually a method declaration (`void Save();`)."""
    for c in field_decl.children:
        if c.type == "function_declarator":
            return True
        if c.type in _CPP_DECLARATOR_WRAPPERS:
            if any(g.type == "function_declarator" for g in c.children):
                return True
    return False


def cpp_field_types(type_node: "Node", src: bytes) -> dict[str, str]:
    """Map field name → declared type name for the data members of a C++ class/struct.

    Method declarations inside the class body are skipped — only real fields seed the
    receiver scope (so ``repo_->Save()`` resolves to ``repo_``'s type).
    """
    out: dict[str, str] = {}
    body = next(
        (c for c in type_node.children if c.type == "field_declaration_list"), None
    )
    if body is None:
        return out
    for member in body.children:
        if member.type != "field_declaration" or _cpp_has_function_declarator(member):
            continue
        type_name = _cpp_type_name_of(member, src)
        if type_name is None:
            continue
        for name in _cpp_decl_names(member, src):
            out[name] = type_name
    return out


def _build_cpp_scope(
    method_node: "Node",
    src: bytes,
    field_types: dict[str, str],
    enclosing_type_name: Optional[str],
) -> dict[str, str]:
    """Map every in-method receiver identifier to its inferred type name (C++).

    Same precedence and conflict rule as the C# scope: fields seed it, parameters and locals
    override, and a name that resolves to two different types is dropped to unknown.
    """
    scope: dict[str, str] = dict(field_types)
    conflicted: set[str] = set()

    def _set(name: Optional[str], type_name: Optional[str]) -> None:
        if not name or not type_name:
            return
        if name in scope and scope[name] != type_name:
            conflicted.add(name)
        scope[name] = type_name

    # Parameters: void m(Repo* r) → r : Repo
    fn_decl = next(
        (c for c in method_node.children if c.type == "function_declarator"), None
    )
    if fn_decl is not None:
        plist = next(
            (c for c in fn_decl.children if c.type == "parameter_list"), None
        )
        if plist is not None:
            for p in plist.children:
                if p.type != "parameter_declaration":
                    continue
                type_name = _cpp_type_name_of(p, src)
                for name in _cpp_decl_names(p, src):
                    _set(name, type_name)

    # Local declarations anywhere in the body: Repo x;  Repo* p = get();
    def walk(n: "Node") -> None:
        if n.type == "declaration":
            type_name = _cpp_type_name_of(n, src)
            if type_name is not None:
                for name in _cpp_decl_names(n, src):
                    _set(name, type_name)
        for c in n.children:
            walk(c)

    body = next(
        (c for c in method_node.children if c.type == "compound_statement"), None
    )
    if body is not None:
        walk(body)

    for name in conflicted:
        scope.pop(name, None)

    if enclosing_type_name:
        scope["this"] = enclosing_type_name
    return scope


def infer_cpp_call_targets(
    method_node: "Node",
    src: bytes,
    field_types: dict[str, str],
    enclosing_type_name: Optional[str],
) -> list[CallTarget]:
    """Return every call target in a C++ function/method body, receiver type where known.

    Reproduces exactly the bare-name call edges the adapter emitted before (callee = the
    final identifier / field_identifier) and adds ``receiver_type`` for the exact strategies.
    ``a->b()->c()`` chains, indexers, and casts stay ``None``.
    """
    scope = _build_cpp_scope(method_node, src, field_types, enclosing_type_name)
    targets: list[CallTarget] = []

    def walk(n: "Node") -> None:
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None:
                if fn.type in ("identifier", "qualified_identifier", "template_function"):
                    name = _cpp_callee_name(fn, src)
                    if name:
                        targets.append(CallTarget(name, None))     # bare foo(), ns::foo()
                elif fn.type == "field_expression":
                    field = fn.child_by_field_name("field")
                    recv = fn.child_by_field_name("argument")
                    name = node_text(field, src) if field is not None else ""
                    if name and len(name) > 1:
                        targets.append(CallTarget(name, _cpp_receiver_type(recv, scope, src)))
        for c in n.children:
            walk(c)

    walk(method_node)
    return targets


def _cpp_callee_name(fn: "Node", src: bytes) -> Optional[str]:
    """Bare callee identifier for a non-receiver C++ call (matches the old _CALL_QUERY).

    Unwraps ``ns::foo`` and the explicit-template forms ``foo<T>()`` / ``ns::foo<T>()`` /
    ``std::make_shared<T>()`` to the final bare identifier, exactly as the query did.
    """
    if fn.type == "identifier":
        return node_text(fn, src) or None
    if fn.type == "qualified_identifier":
        return _cpp_callee_name(fn.child_by_field_name("name"), src) \
            if fn.child_by_field_name("name") is not None else None
    if fn.type == "template_function":
        name = fn.child_by_field_name("name")
        return _cpp_callee_name(name, src) if name is not None else None
    return None


def _cpp_receiver_type(
    recv_node: "Optional[Node]", scope: dict[str, str], src: bytes
) -> Optional[str]:
    """Infer a C++ receiver's type name from an exact strategy, else None (unknown)."""
    if recv_node is None:
        return None
    if recv_node.type == "this":
        return scope.get("this")
    if recv_node.type == "identifier":
        return scope.get(node_text(recv_node, src))
    return None   # call_expression, nested field_expression, subscript: stay unknown
