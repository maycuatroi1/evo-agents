"""The module-level API of a Python file, and which of it a change breaks for a file that uses it: what the tier rule of
a protected file the knowledge graph says a change reaches reads (``evo_agents.hub.tiers.tier_of``, ``api``). Pure
functions, standard library only.

A module's API (``module_api``) is each name it binds at its top level, with what a caller depends on: a function's or a
class's signature (its parameters and their defaults, a class's bases and the signatures of its public methods), and
for any other binding only that the name is there. A change breaks the names ``changed_api`` gives: those it removes
and those whose signature it changes; a new name or a body that changes alone breaks nothing. A file uses the names
``used_names`` gives of a module: those it imports from it, and those it reads as attributes of the module it imports.
``api_hits`` is what both name: the names a change breaks for that file.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath


def module_of(path: str) -> str | None:
    """The dotted name of the module of the Python file ``path`` (within its repo; a leading ``src/`` left out, a
    package's ``__init__.py`` its package); None for a file that is not Python."""
    parts = list(PurePosixPath(path).parts)
    if not parts or not parts[-1].endswith(".py"):
        return None
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _signature(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{kind}({ast.unparse(node.args)})"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in [*node.bases, *node.keywords])
        methods = [
            f"{item.name}{_signature(item)}"
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (not item.name.startswith("_") or item.name == "__init__")
        ]
        return f"class({bases}): " + "; ".join(sorted(methods))
    return None


def _bound(node: ast.AST) -> list[str]:
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    names = []
    for target in targets:
        names += [item.id for item in ast.walk(target) if isinstance(item, ast.Name)]
    return names


def module_api(source: str) -> dict[str, str]:
    """name -> signature of each name ``source`` binds at its top level (see the module's docstring); SyntaxError
    when it is no Python."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        signature = _signature(node)
        if signature is not None:
            found[node.name] = signature  # type: ignore[attr-defined]
            continue
        for name in _bound(node):
            found.setdefault(name, "value")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    found.setdefault((alias.asname or alias.name).split(".")[0], "import")
    return found


def changed_api(before: str | None, after: str | None) -> list[str]:
    """The top-level names of a module that a change from ``before`` to ``after`` (its source at each end, None for no
    file) removes or whose signature it changes, sorted."""
    old = module_api(before) if before else {}
    new = module_api(after) if after else {}
    return sorted(name for name, signature in old.items() if new.get(name) != signature)


def used_names(source: str, module: str) -> set[str]:
    """The names of ``module`` (dotted) the Python ``source`` uses: those it imports from it, and those it reads as
    attributes of it when it imports the module itself."""
    package, _, leaf = module.rpartition(".")
    found: set[str] = set()
    aliases: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == module:
                    found.add(alias.name)
                elif node.module == package and alias.name == leaf:
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    aliases.add(alias.asname or module)
    if aliases:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases:
                found.add(node.attr)
    return found


def api_hits(changed: Iterable[str], used: Iterable[str]) -> list[str]:
    """The names both ``changed`` and ``used`` name, sorted: what a change breaks for the file that uses them."""
    return sorted(set(changed) & set(used))


def reached_api(changes: Mapping[str, tuple[str | None, str | None]], source: str) -> list[str]:
    """The names a change breaks for the Python file ``source``: of each module the change edits (``changes``, path ->
    (its source before, after)), those ``changed_api`` gives that ``source`` uses (``used_names``), as
    ``module.name``, sorted. A file the change edits that is no Python, or does not parse, breaks nothing here."""
    hits = []
    for path, (before, after) in changes.items():
        module = module_of(path)
        if module is None:
            continue
        try:
            names = api_hits(changed_api(before, after), used_names(source, module))
        except SyntaxError:
            continue
        hits += [f"{module}.{name}" for name in names]
    return sorted(hits)
