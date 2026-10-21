"""``python-ast`` code backend: symbols, imports and calls from the standard library parser.

Everything here is a deterministic function of one file's text. Calls inside the file are resolved when the
name is unambiguous, so they carry status ``resolved`` with a confidence below 1. A call into another
module becomes an ``xref`` only when the file's imports name it: ``from m import f`` then ``f()``,
``import m`` or ``import m as x`` then ``x.f()``, ``from pkg import mod`` then ``mod.f()``, relative
imports, imports inside a function body and star imports. Each xref lists ``via`` entries,
``["module", module, qualname]``, that the link stage resolves against the identifier index; a name the
imports do not bind gets none.

A class lists its ``bases`` as such ``via`` lists, ``["local", "", qualname]`` for a class of the same
file. ``self.m()`` or ``cls.m()`` to a method the class does not define, and ``super().m()``, become xrefs
``["self" | "super", class, "m"]`` that the link stage looks up along the class's chain.

A call through a receiver whose class the file states (``extract.pytypes``: a variable assigned an instance or a
call's result, an annotated parameter or variable, a ``self`` attribute, ``f().m()``, ``C.m()``) becomes an xref
``["typed", type, "m"]``, and a call of a class ``C(...)`` one to the ``__init__`` up its chain. A function
carries the class its return annotation names as ``returns`` (``items`` for a tuple), a ``via`` list the link
stage resolves once a build.
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath

from evo_agents.kg.extract.pytypes import FileTypes, dotted, super_member

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def module_name(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _resolve_relative(path: str, module: str | None, level: int) -> str | None:
    if level == 0:
        return module
    package = list(PurePosixPath(path).parent.parts)
    if level > 1:
        package = package[: len(package) - (level - 1)]
    base = ".".join(package)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


class _Env:
    """The names imports bind in one scope: ``names[local] = (module, member)``, member None for a module
    bound as a whole; ``stars`` are modules imported with ``*``."""

    def __init__(self, names: dict | None = None, stars: tuple = ()):
        self.names: dict[str, tuple[str, str | None]] = dict(names or {})
        self.stars: tuple[str, ...] = stars

    def child(self, func: ast.AST, path: str) -> _Env:
        """The scope of a function: its own imports added, names it assigns or takes as parameters removed."""
        env = _Env(self.names, self.stars)
        for name in _assigned(func):
            env.names.pop(name, None)
        for node in _imports_in(func.body):
            env.bind(node, path)
        return env

    def bind(self, node: ast.Import | ast.ImportFrom, path: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self.names[alias.asname] = (alias.name, None)
                else:  # `import a.b.c` binds `a`; `a.b.c.f()` walks down from it
                    top = alias.name.split(".")[0]
                    self.names[top] = (top, None)
            return
        base = _resolve_relative(path, node.module, node.level)
        if not base:
            return
        for alias in node.names:
            if alias.name == "*":
                self.stars = (*self.stars, base)
            else:
                self.names[alias.asname or alias.name] = (base, alias.name)

    def via(self, chain: list[str]) -> list[list[str]]:
        """``[module, qualname]`` pairs a call through the name chain ``a.b.f`` may reach, the longest module
        first: ``from m import C`` then ``C.f()`` gives (m, C.f); ``import a`` then ``a.b.f()`` gives
        (a.b, f) and (a, b.f)."""
        head, rest = chain[0], chain[1:]
        if head not in self.names:
            return [["module", m, head] for m in self.stars] if not rest else []
        module, member = self.names[head]
        out = []
        if member is not None:
            out.append(["module", module, ".".join([member, *rest])])
            module = f"{module}.{member}"  # `from pkg import mod`: the member may be a submodule
        for i in range(len(rest) - 1, -1, -1):
            out.append(["module", ".".join([module, *rest[:i]]), ".".join(rest[i:])])
        return out


def _imports_in(body: list) -> list:
    """Import statements of a block and of the blocks nested in it (if, try, with), not of nested defs."""
    out = []
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
        elif not isinstance(node, (*DEFS, ast.Lambda)):
            for name in ("body", "orelse", "finalbody", "handlers"):
                out += _imports_in(getattr(node, name, None) or [])
    return out


def _assigned(func: ast.AST) -> set[str]:
    """Names a function binds itself: parameters, assignment and loop targets. They hide an import."""
    args = func.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names |= {a.arg for a in (args.vararg, args.kwarg) if a}
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


class _Extractor:
    def __init__(self, tree: ast.Module, path: str):
        self.path = path
        self.symbols: list[dict] = []
        self.imports: list[dict] = []
        self.calls: list[dict] = []
        self.xrefs: list[dict] = []
        self.top_names = {n.name for n in tree.body if isinstance(n, DEFS)}
        self.quals: set[str] = set()
        self.module_env = _Env()
        for node in _imports_in(tree.body):
            self.module_env.bind(node, path)
        self.typing = FileTypes(self.top_names, {n.name for n in tree.body if isinstance(n, ast.ClassDef)})
        self.module_types = self.typing.scope(tree, self.module_env, None, {})
        self.envs: dict[int, _Env] = {}  # a method's import scope, made once for its class's attributes and itself

    def visit_body(self, body, top: str, parent: str | None, cls: str | None, env: _Env, types: dict) -> None:
        for node in body:
            if isinstance(node, DEFS):
                self.define(node, top, parent, cls, env, types)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.collect_import(node, top)

    def define(self, node, top: str, parent: str | None, cls: str | None, env: _Env, types: dict) -> None:
        qual = f"{parent}.{node.name}" if parent else node.name
        kind = "class" if isinstance(node, ast.ClassDef) else ("method" if cls else "function")
        anchor = top if parent else node.name
        sym = {"qualname": qual, "name": node.name, "kind": kind, "top": anchor, "line": node.lineno, "parent": parent}
        doc = ast.get_docstring(node)
        if doc:
            sym["doc"] = doc
        self.symbols.append(sym)
        self.quals.add(qual)
        if isinstance(node, ast.ClassDef):
            bases = [via for base in node.bases if (via := self.base_via(base, node.name, parent, env))]
            if bases:
                sym["bases"] = bases
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            self.envs.update((id(m), env.child(m, self.path)) for m in methods)
            self.typing.attrs[qual] = self.typing.class_attrs(node, qual, env, types, self.envs)
            self.visit_body(node.body, anchor, qual, qual, env, types)
            return
        sym.update(self.typing.returns(node, env, cls))
        inner = self.envs.pop(id(node), None) or env.child(node, self.path)
        scope = self.typing.scope(node, inner, cls, types)
        self.collect_calls(node, qual, anchor, cls, inner, scope)
        self.visit_body(node.body, anchor, qual, None, inner, scope)

    def base_via(self, base: ast.expr, name: str, parent: str | None, env: _Env) -> list[list[str]]:
        """``via`` entries of one base of a class: a class of this file (in the enclosing class's body, or
        at the top), else what the imports bind."""
        while isinstance(base, ast.Subscript):  # Generic[T], Base[int]
            base = base.value
        chain = dotted(base)
        if chain is None:
            return []
        if parent and f"{parent}.{chain[0]}" in self.quals:
            return [["local", "", ".".join([parent, *chain])]]
        if chain[0] in self.top_names and chain[0] != name:
            return [["local", "", ".".join(chain)]]
        return env.via(chain)

    def collect_import(self, node, top: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.imports.append({"module": alias.name, "top": top})
            return
        base = _resolve_relative(self.path, node.module, node.level)
        if base:
            self.imports.append({"module": base, "top": top})
            for alias in node.names:
                if alias.name != "*":
                    self.imports.append({"module": f"{base}.{alias.name}", "top": top, "optional": True})

    def collect_calls(self, func, qual: str, top: str, cls: str | None, env: _Env, types: dict) -> None:
        for node, here in self.typing.calls(func, types):
            call = {"caller": qual, "line": node.lineno, "top": top}
            member = super_member(node.func, cls) if cls else None
            if member:
                self.xrefs.append({**call, "via": [["super", cls, member]]})
                continue
            chain = dotted(node.func)
            if chain is None:  # f().m(): through what f returns
                self.add_xref(call, self.typing.callee(node.func, env, cls, here))
                continue
            self.named_call(call, chain, cls, env, here)
            self.constructor(call, chain, env, here)

    def named_call(self, call: dict, chain: list[str], cls: str | None, env: _Env, types: dict) -> None:
        head = chain[0]
        if len(chain) == 1 and head in self.top_names:
            self.calls.append({**call, "callee": head})
        elif len(chain) == 2 and head in ("self", "cls") and cls:
            self.calls.append({**call, "callee": f"{cls}.{chain[1]}", "member": [cls, chain[1]]})
        elif head in self.top_names or head in ("self", "cls"):
            if len(chain) > 1:  # C.m() for a class C of the file, self.attr.m()
                self.add_xref(call, self.typing.named_callee(chain, env, cls, types))
        else:
            self.add_xref(call, self.typing.named_callee(chain, env, cls, types))

    def constructor(self, call: dict, chain: list[str], env: _Env, types: dict) -> None:
        """A call of a class, ``C(...)``, runs the ``__init__`` nearest up the chain of ``C``. Only a name written
        as a class is tried; the link stage keeps it when the name is a class."""
        if not chain[-1][:1].isupper() or chain[0] in types or chain[0] in ("self", "cls"):
            return
        if chain[0] in self.top_names:
            owner = [["local", "", ".".join(chain)]] if chain[0] in self.typing.top_classes else []
        else:
            owner = env.via(chain)
        if owner:
            self.add_xref(call, [["typed", ["ann", owner], "__init__"]])

    def add_xref(self, call: dict, via: list | None) -> None:
        if via:
            self.xrefs.append({**call, "via": via})

    def result(self, doc: str | None = None) -> dict:
        known = {s["qualname"] for s in self.symbols}
        calls = [{k: v for k, v in c.items() if k != "member"} for c in self.calls if c["callee"] in known]
        inherited = [  # self.m() to a method the class does not define: up its chain, in the link stage
            {k: c[k] for k in ("caller", "line", "top")} | {"via": [["self", *c["member"]]]}
            for c in self.calls
            if c["callee"] not in known and "member" in c
        ]
        xrefs, seen = [], set()
        for x in self.xrefs + inherited:  # one per caller and target: the first line stands for the others
            key = (x["caller"], repr(x["via"]))
            if key not in seen:
                seen.add(key)
                xrefs.append(x)
        out = {"symbols": self.symbols, "imports": self.imports, "calls": calls, "xrefs": xrefs}
        return {**out, "doc": doc} if doc else out


def extract(text: str, path: str) -> dict | None:
    """Return {"symbols", "imports", "calls", "xrefs", ["doc"]} or None if the file does not parse; doc is the
    module's docstring.

    symbols: {qualname, name, kind (class|function|method), top (top-level anchor), line, parent, [bases],
    [returns], [items], [doc]}
    imports: {module, top}; calls: {caller, callee, line, top}; xrefs: {caller, line, top, via}
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    ex = _Extractor(tree, path)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            ex.collect_import(node, "_module")
    body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    ex.visit_body(body, "_module", None, None, ex.module_env, ex.module_types)
    return ex.result(ast.get_docstring(tree))
