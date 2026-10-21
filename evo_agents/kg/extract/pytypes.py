"""Receiver types for the ``python-ast`` code backend: the class a variable, a ``self`` attribute or the result of a
call is an instance of, read off one file without importing or running it.

A type is an expression the link stage evaluates (``pipeline.link``), a list so it can be stored as a fact:

    ["ann", via]          an instance of the class ``via`` names: an annotation such as X, "X", Optional[X],
                          X | None, type[X] or Self
    ["ret", via, item]    what a call of the callee ``via`` names returns: an instance of the callee when it is a
                          class, else the class its return annotation names; ``item`` picks one item of a tuple
    ["one", [type, ...]]  the class every one of these types names, when they all name the same one

``via`` is a list of ``[kind, value, qualname]`` entries, as a call carries them (``extract.python``). A call through
a typed receiver, ``x.m()``, becomes ``["typed", type, "m"]``: the link stage looks ``m`` up the chain of the class
the type names, so an inherited method is found too.

Types are flow-insensitive per scope. A name has a type when every binding of it in the scope gives one: an
annotated parameter, an annotated assignment, an assignment of a call, of another typed name, of ``a or b`` or
``a if c else b``, or one item of a call that returns a tuple. Bindings that give different types make a ``one``
type, so the link stage keeps the name only when they all name the same class. Assigning None neither gives a type
nor takes one away. Any other binding (an unannotated parameter, a loop, ``with ... as``, ``except ... as``, a walrus,
an augmented assignment, an import, an untyped value) leaves the name untyped. A function sees the typed names of
the scope around it that it does not bind itself. A class's attribute gets a type the same way from the annotations
and assignments of its body, from ``self.x = ...`` in its methods, and from a property with a return annotation.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterator

# Annotations that never name a class of the project.
BUILTINS = frozenset(
    "None bool int float complex str bytes bytearray object list dict set frozenset tuple type Any".split()
)
# Subscripted forms whose first argument is the class: Optional[X], type[X], Annotated[X, ...], ClassVar[X].
WRAPPERS = frozenset(
    {"Optional", "Annotated", "Final", "ClassVar", "Required", "NotRequired", "ReadOnly", "type", "Type"}
)
TUPLES = frozenset({"tuple", "Tuple"})
PROPERTIES = frozenset({"property", "cached_property"})
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
MAX_ONE = 4  # different types one name may take before it has none
# Nodes a scope's walk does not go into: nested scopes, and nodes with nothing that binds a name below them.
NESTED = frozenset({*SCOPES, *COMPREHENSIONS})
NO_DESCENT = NESTED | {ast.Name, ast.Constant}


def dotted(expr: ast.expr) -> list[str] | None:
    """``a.b.f`` -> [a, b, f]; None for anything that is not a dotted name."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    return [expr.id, *reversed(parts)]


def super_member(func: ast.expr, cls: str) -> str | None:
    """``super().m`` or ``super(C, self).m`` in a method of class ``C``: the member's name."""
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)):
        return None
    inner = func.value
    if not (isinstance(inner.func, ast.Name) and inner.func.id == "super"):
        return None
    first = inner.args[0] if inner.args else None
    if first is not None and not (isinstance(first, ast.Name) and first.id == cls.rpartition(".")[2]):
        return None  # super(Other, self): the chain of another class, not followed
    return func.attr


def _unquote(expr: ast.expr) -> ast.expr | None:
    """A string annotation, ``"Store"``, as the expression it holds."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            return ast.parse(expr.value.strip(), mode="eval").body
        except (SyntaxError, ValueError):
            return None
    return expr


def _is_none(expr: ast.expr | None) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


def _union(expr: ast.expr) -> list[ast.expr]:
    """The members of ``A | B | C``."""
    out, stack = [], [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            stack += [node.right, node.left]
        else:
            out.append(node)
    return out


def _args(subscript: ast.Subscript) -> list[ast.expr]:
    inner = subscript.slice
    return list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]


def _one(members: list[ast.expr]) -> ast.expr | None:
    """The one member of a union that is not None."""
    rest = [m for m in members if not _is_none(m)]
    return rest[0] if len(rest) == 1 else None


def annotation_class(expr: ast.expr | None) -> ast.expr | None:
    """The dotted name of the class an annotation names, or None when it names no one class."""
    expr = _unquote(expr) if expr is not None else None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return annotation_class(_one(_union(expr)))
    if isinstance(expr, ast.Subscript):
        name = (dotted(expr.value) or [""])[-1]
        args = _args(expr)
        if name in WRAPPERS:
            return annotation_class(args[0])
        return annotation_class(_one(args)) if name == "Union" else None
    return expr if expr is not None and dotted(expr) else None


def annotation_items(expr: ast.expr | None) -> list[ast.expr] | None:
    """The items of ``tuple[A, B]`` (``Optional`` of one too), or None for any other annotation."""
    expr = _unquote(expr) if expr is not None else None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return annotation_items(_one(_union(expr)))
    if not isinstance(expr, ast.Subscript):
        return None
    name = (dotted(expr.value) or [""])[-1]
    args = _args(expr)
    if name == "Optional":
        return annotation_items(args[0])
    if name in TUPLES and not any(isinstance(a, ast.Constant) and a.value is Ellipsis for a in args):
        return args
    return None


def _branches(node: ast.AST) -> list[ast.AST]:
    """The child nodes of ``node`` that have children or a name of their own: not Load, Store or an operator."""
    return [child for child in ast.iter_child_nodes(node) if child._fields]


def _children(node: ast.AST) -> Iterator[ast.AST]:
    """The nodes under ``node`` in source order, without entering a nested scope; contexts and operators left out."""
    stack = _branches(node)[::-1]
    while stack:
        child = stack.pop()
        yield child
        if type(child) not in NO_DESCENT:
            stack += _branches(child)[::-1]


def _arguments(func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[ast.arg]:
    args = func.args
    return [
        *args.posonlyargs,
        *args.args,
        *(a for a in (args.vararg,) if a),
        *args.kwonlyargs,
        *(a for a in (args.kwarg,) if a),
    ]


def _named(node: ast.AST) -> list[str]:
    """Names a statement binds without a Name node: a def, a class, an import, global, except and match captures."""
    if isinstance(node, DEFS):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names if a.name != "*"]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def bound_names(scope: ast.AST) -> set[str]:
    """Every name a scope binds itself: parameters, assignment targets and the names of ``_named``."""
    names = {a.arg for a in _arguments(scope)} if isinstance(scope, (*FUNCS, ast.Lambda)) else set()
    if isinstance(scope, COMPREHENSIONS):
        return {n.id for g in scope.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)}
    for node in _children(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        else:
            names.update(_named(node))
    return names


def _targets(stmt: ast.stmt) -> Iterator[tuple[ast.expr, ast.expr | None, int | None, ast.expr | None]]:
    """(target, value, item, annotation) for each target an assignment binds: ``a, b = f()`` binds ``a`` to item 0
    of ``f()``, ``a, b = x, y`` binds ``a`` to ``x``."""
    if isinstance(stmt, ast.AnnAssign):
        yield stmt.target, stmt.value, None, stmt.annotation
        return
    for target in stmt.targets:
        if not isinstance(target, (ast.Tuple, ast.List)):
            yield target, stmt.value, None, None
            continue
        pairs = isinstance(stmt.value, (ast.Tuple, ast.List)) and len(stmt.value.elts) == len(target.elts)
        for i, elt in enumerate(target.elts):
            if isinstance(elt, ast.Starred):
                break
            yield (elt, stmt.value.elts[i], None, None) if pairs else (elt, stmt.value, i, None)


def one_of(kinds: list[list]) -> list | None:
    """One type for several: the type itself when they are equal, else a ``one`` type of at most MAX_ONE."""
    flat: list[list] = []
    for kind in kinds:
        for k in kind[1] if kind[0] == "one" else [kind]:
            if k not in flat:
                flat.append(k)
    if not flat or len(flat) > MAX_ONE:
        return None
    return flat[0] if len(flat) == 1 else ["one", flat]


class Bindings:
    """The typed names of one scope as its bindings come, in source order, over ``base``, the typed names of the
    enclosing scope: the first binding of a name in the scope hides the enclosing one. A binding without a type
    takes the name's type away for good."""

    def __init__(self, base: dict | None = None) -> None:
        self.types: dict[str, list] = dict(base or {})
        self.local: set[str] = set()
        self.dead: set[str] = set()

    def bind(self, name: str, kind: list | None) -> None:
        if name not in self.local:
            self.local.add(name)
            self.types.pop(name, None)
        if name in self.dead:
            return
        merged = one_of([self.types[name], kind]) if kind is not None and name in self.types else kind
        if merged is None:
            self.dead.add(name)
            self.types.pop(name, None)
        else:
            self.types[name] = merged


class FileTypes:
    """The types of one file's scopes. ``top_names``: the file's top-level definitions; ``top_classes`` those that
    are classes. ``attrs``: class qualname -> attribute -> type, filled as the classes are defined."""

    def __init__(self, top_names: set[str], top_classes: set[str]):
        self.top_names = top_names
        self.top_classes = top_classes
        self.attrs: dict[str, dict[str, list]] = {}
        self._bound: dict[int, set[str]] = {}  # bound_names of the nested scopes, by node id

    def bound(self, node: ast.AST) -> set[str]:
        if id(node) not in self._bound:
            self._bound[id(node)] = bound_names(node)
        return self._bound[id(node)]

    # -- annotations and callees --------------------------------------------------------------------

    def class_via(self, annotation: ast.expr | None, env, cls: str | None) -> list[list] | None:
        """The ``via`` list of the class an annotation names, from the scope of ``env``; None for a builtin."""
        names = dotted(core) if (core := annotation_class(annotation)) is not None else None
        if not names or (len(names) == 1 and names[0] in BUILTINS):
            return None
        if names[-1] == "Self" and len(names) <= 2:
            return [["local", "", cls]] if cls else None
        if names[0] in self.top_names:
            return [["local", "", ".".join(names)]]
        return env.via(names) or None

    def annotation(self, annotation: ast.expr | None, env, cls: str | None) -> list | None:
        via = self.class_via(annotation, env, cls)
        return ["ann", via] if via else None

    def returns(self, func: ast.FunctionDef | ast.AsyncFunctionDef, env, cls: str | None) -> dict:
        """``returns``: the via list of the class a function's return annotation names; ``items``: one per item
        of a tuple it returns, [] for an item that names no class."""
        if func.returns is None:
            return {}
        items = annotation_items(func.returns)
        if items is not None:
            vias = [self.class_via(item, env, cls) or [] for item in items]
            return {"items": vias} if any(vias) else {}
        via = self.class_via(func.returns, env, cls)
        return {"returns": via} if via else {}

    def callee(self, func: ast.expr, env, cls: str | None, types: dict) -> list[list] | None:
        """The ``via`` list of what a call calls, as the call names it, or None when nothing in the file says."""
        member = super_member(func, cls) if cls else None
        if member:
            return [["super", cls, member]]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):  # f().m()
            inner = self.callee(func.value.func, env, cls, types)
            return [["typed", ["ret", inner, None], func.attr]] if inner else None
        names = dotted(func)
        return self.named_callee(names, env, cls, types) if names else None

    def named_callee(self, names: list[str], env, cls: str | None, types: dict) -> list[list] | None:
        head, rest = names[0], names[1:]
        if head in ("self", "cls") and cls:
            return self.self_callee(head, rest, cls)
        if head in types:
            return [["typed", types[head], rest[0]]] if len(rest) == 1 else None
        if head in self.top_names:
            local = [["local", "", ".".join(names)]]
            return local + ([self.class_member(head, rest[0])] if len(rest) == 1 and head in self.top_classes else [])
        via = env.via(names)
        owner = env.via(names[:-1]) if rest and names[-2][:1].isupper() else []  # C.m(), m maybe inherited
        return via + ([["typed", ["ann", owner], names[-1]]] if owner else []) or None

    def self_callee(self, head: str, rest: list[str], cls: str) -> list[list] | None:
        if len(rest) == 1:
            return [["self", cls, rest[0]]]
        attr = self.attrs.get(cls, {}).get(rest[0]) if head == "self" and len(rest) == 2 else None
        return [["typed", attr, rest[1]]] if attr else None

    @staticmethod
    def class_member(cls: str, name: str) -> list:
        """``C.m()`` for a class ``C`` of the file: ``m`` up the chain of ``C``, inherited or not."""
        return ["typed", ["ann", [["local", "", cls]]], name]

    # -- values -------------------------------------------------------------------------------------

    def value(self, value: ast.expr | None, item: int | None, env, cls: str | None, types: dict) -> list | None:
        """The type of an assigned value; None when it has none."""
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            callee = self.callee(value.func, env, cls, types)
            return ["ret", callee, item] if callee else None
        if item is not None:
            return None
        if isinstance(value, ast.Name):
            return types.get(value.id)
        if isinstance(value, (ast.BoolOp, ast.IfExp)):
            sides = value.values if isinstance(value, ast.BoolOp) else [value.body, value.orelse]
            kinds = [self.value(s, None, env, cls, types) for s in sides if not _is_none(s)]
            return one_of(kinds) if kinds and all(kinds) else None
        names = dotted(value) if isinstance(value, ast.Attribute) else None
        if names and len(names) == 2 and names[0] == "self" and cls:
            return self.attrs.get(cls, {}).get(names[1])
        return None

    def bind_target(self, out: Bindings, target, value, item, annotation, env, cls, types, owner=None) -> set[int]:
        """Record one assignment target in ``out``; ``owner`` names ``self`` for attribute targets. Returns the ids
        of the nodes it accounted for."""
        if owner is None and isinstance(target, ast.Name):
            name = target.id
        elif owner and isinstance(target, ast.Attribute) and dotted(target) == [owner, target.attr]:
            name = target.attr
        else:
            return set()
        if annotation is not None:
            out.bind(name, self.annotation(annotation, env, cls))
        elif value is not None and not _is_none(value):
            out.bind(name, self.value(value, item, env, cls, types))
        return {id(target)}

    # -- scopes -------------------------------------------------------------------------------------

    def scope(self, node: ast.AST, env, cls: str | None, outer: dict) -> dict[str, list]:
        """The typed names of a module or function body: its own bindings over the enclosing scope's typed names
        it does not bind."""
        out = Bindings(outer)
        if isinstance(node, FUNCS):
            single = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for arg in _arguments(node):  # *args and **kwargs hold many values: no type
                typed = arg in single and arg.annotation is not None
                out.bind(arg.arg, self.annotation(arg.annotation, env, cls) if typed else None)
        handled: set[int] = set()
        for child in _children(node):
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                for target, value, item, annotation in _targets(child):
                    handled |= self.bind_target(out, target, value, item, annotation, env, cls, out.types)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store) and id(child) not in handled:
                out.bind(child.id, None)
            else:
                for name in _named(child):
                    out.bind(name, None)
        return out.types

    def class_attrs(self, node: ast.ClassDef, cls: str, env, outer: dict, envs: dict) -> dict[str, list]:
        """The typed attributes of a class: annotations and assignments of its body, ``self.x = ...`` in its
        methods, properties with a return annotation. ``envs`` gives each method its import scope."""
        out = Bindings()
        for stmt in node.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                for target, value, item, annotation in _targets(stmt):
                    self.bind_target(out, target, value, item, annotation, env, cls, outer)
            elif isinstance(stmt, FUNCS):
                self._method_attrs(out, stmt, cls, envs[id(stmt)], outer)
        return out.types

    def _method_attrs(self, out: Bindings, func, cls: str, env, outer: dict) -> None:
        decorators = {(dotted(d) or [""])[-1] for d in func.decorator_list}
        if decorators & PROPERTIES:
            via = self.class_via(func.returns, env, cls) if func.returns is not None else None
            out.bind(func.name, ["ret", [["self", cls, func.name]], None] if via else None)
            return
        if decorators & {"staticmethod", "classmethod"} or not _arguments(func):
            return
        owner = _arguments(func)[0].arg
        assigns, stores = [], []
        for child in _children(func):
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                assigns.append(child)
            elif (
                isinstance(child, ast.Attribute)
                and isinstance(child.ctx, ast.Store)
                and dotted(child) == [owner, child.attr]
            ):
                stores.append(child)
        if not stores:
            return
        types = self.scope(func, env, cls, outer)  # only for a method that sets an attribute of its own
        handled: set[int] = set()
        for stmt in assigns:
            for target, value, item, annotation in _targets(stmt):
                handled |= self.bind_target(out, target, value, item, annotation, env, cls, types, owner)
        for store in stores:
            if id(store) not in handled:
                out.bind(store.attr, None)

    def calls(self, func: ast.AST, types: dict) -> Iterator[tuple[ast.Call, dict]]:
        """Each call under ``func``, in the order of ``ast.walk``, with the typed names in effect there: inside a
        nested function, lambda or comprehension, the names it binds itself are untyped."""
        todo = deque([(func, types)])
        while todo:
            node, current = todo.popleft()
            kind = type(node)
            if kind is ast.Call:
                yield node, current
            elif kind is ast.Name or kind is ast.Constant:
                continue
            elif current and node is not func and kind in NESTED:
                hidden = self.bound(node)
                if hidden & current.keys():
                    current = {k: v for k, v in current.items() if k not in hidden}
            todo.extend((child, current) for child in _branches(node))
