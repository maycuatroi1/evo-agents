"""``python-ast`` code backend: symbols, imports and same-file calls from the standard library parser.

Everything here is a deterministic function of one file's text. Calls are resolved only inside the
file and only when the name is unambiguous, so they carry status ``resolved`` with a confidence below 1.
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath


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


def extract(text: str, path: str) -> dict | None:
    """Return {"symbols": [...], "imports": [...], "calls": [...]} or None if the file does not parse.

    symbols: {qualname, name, kind (class|function|method), top (top-level anchor), line, parent}
    imports: {module, top}; calls: {caller, callee, line}
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    symbols: list[dict] = []
    imports: list[dict] = []
    calls: list[dict] = []
    top_names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    def visit_body(body, top: str, parent: str | None, cls: str | None):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{parent}.{node.name}" if parent else node.name
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if cls else "function")
                anchor = top if parent else node.name
                symbols.append(
                    {
                        "qualname": qual,
                        "name": node.name,
                        "kind": kind,
                        "top": anchor,
                        "line": node.lineno,
                        "parent": parent,
                    }
                )
                if isinstance(node, ast.ClassDef):
                    visit_body(node.body, anchor, qual, qual)
                else:
                    collect_calls(node, qual, anchor, cls)
                    visit_body(node.body, anchor, qual, None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                collect_import(node, top)

    def collect_import(node, top: str):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"module": alias.name, "top": top})
        else:
            base = _resolve_relative(path, node.module, node.level)
            if base:
                imports.append({"module": base, "top": top})
                for alias in node.names:
                    if alias.name != "*":
                        imports.append({"module": f"{base}.{alias.name}", "top": top, "optional": True})

    def collect_calls(func, qual: str, top: str, cls: str | None):
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name) and node.func.id in top_names:
                target = node.func.id
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and cls
            ):
                target = f"{cls}.{node.func.attr}"
            if target:
                calls.append({"caller": qual, "callee": target, "line": node.lineno, "top": top})

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            collect_import(node, "_module")
    visit_body([n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))], "_module", None, None)
    known = {s["qualname"] for s in symbols}
    calls = [c for c in calls if c["callee"] in known]
    return {"symbols": symbols, "imports": imports, "calls": calls}
