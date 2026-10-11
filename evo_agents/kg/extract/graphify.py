"""``graphify-ast`` code backend: graphify's tree-sitter extractors (Apache-2.0), called per file.

Optional: ``pip install 'evo-ak[graphify]'``. graphify extracts one file at a time; this module
maps its nodes to symbols (qualified names built from its contains and method edges) and keeps its
same-file calls. Relative JS and TS imports are resolved here from the import specifiers, because a
single-file run of graphify cannot see the target file. The calls graphify left unresolved become
``xrefs``: each with the ``via`` entries ``xref.call_via`` derives from the file's own imports, which the
link stage resolves against the identifier index.
"""

from __future__ import annotations

import posixpath
import re
import tempfile
from functools import cache
from pathlib import Path, PurePosixPath

from evo_agents.kg.extract import xref

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["'](\.{1,2}/[^"']+)["']""")
# Extractors of their own that mark no node ``_callable``: their definitions are found by shape instead.
STRUCTURAL = {"extract_go", "extract_rust", "extract_bash"}


@cache
def available() -> bool:
    try:
        import graphify.extract  # noqa: F401
    except ImportError:
        return False
    return True


@cache
def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        return pkg_version("graphifyy")
    except PackageNotFoundError:
        return "unknown"


def supports(path: str) -> bool:
    if not available():
        return False
    from graphify import extract as gx

    return gx._get_extractor(Path(path)) is not None


def _clean(label: str) -> str:
    return label.strip().lstrip(".").removesuffix("()").strip() or label


def _line(location: str | None) -> int:
    m = re.match(r"L(\d+)", location or "")
    return int(m.group(1)) if m else 0


def import_candidates(text: str, path: str) -> list[list[str]]:
    """Possible target paths for each relative import of a JS or TS file."""
    if PurePosixPath(path).suffix not in JS_SUFFIXES:
        return []
    base = posixpath.dirname(path)
    out = []
    for spec in dict.fromkeys(IMPORT.findall(text)):
        target = posixpath.normpath(posixpath.join(base, spec))
        stem = re.sub(r"\.(js|jsx|mjs|cjs)$", "", target)
        candidates = [target] if PurePosixPath(target).suffix in JS_SUFFIXES else []
        candidates += [stem + s for s in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")]
        candidates += [f"{stem}/index{s}" for s in (".ts", ".tsx", ".js")]
        out.append(list(dict.fromkeys(candidates)))
    return out


def _run(text: str, path: str) -> tuple[dict, Path, bool] | None:
    """graphify's result for one file written alone to a temporary directory, the path it saw, and whether
    its extractor is one that marks no ``_callable`` nodes."""
    from graphify import extract as gx

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        fn = gx._get_extractor(target)
        if fn is None:
            return None
        try:
            result = fn(target)
        except Exception:
            return None
    return result, target, getattr(fn, "__name__", "") in STRUCTURAL


def _defines(node: dict, path: str, structural: bool) -> bool:
    """A definition of this file: marked ``_callable`` by graphify's engine, or, for an extractor that marks
    nothing, a node of this file with a location that is neither its file nor its script entry point."""
    if node.get("_callable"):
        return True
    if not structural or not node.get("source_location") or not str(node.get("source_file")).endswith("/" + path):
        return False
    return (node.get("metadata") or {}).get("kind") != "bash_entrypoint"


def _symbols(result: dict, target: Path, path: str, structural: bool) -> tuple[list[dict], dict[str, str]]:
    nodes = {n["id"]: n for n in result.get("nodes", [])}
    file_ids = {
        n["id"] for n in nodes.values() if n.get("label") == target.name and _line(n.get("source_location")) <= 1
    }
    parent: dict[str, str] = {}
    for e in result.get("edges", []):
        if e.get("relation") in ("contains", "method") and e["target"] in nodes:
            parent.setdefault(e["target"], e["source"])

    def qualname(nid: str) -> str:
        parts, seen = [], set()
        while nid in nodes and nid not in file_ids and nid not in seen:
            seen.add(nid)
            parts.append(_clean(nodes[nid]["label"]))
            nid = parent.get(nid, "")
        return ".".join(reversed(parts))

    symbols, quals = [], {}
    for nid, n in sorted(nodes.items(), key=lambda kv: (_line(kv[1].get("source_location")), kv[0])):
        if nid in file_ids or not _defines(n, path, structural):
            continue
        qual = qualname(nid)
        if not qual:
            continue
        quals[nid] = qual
        up = parent.get(nid)
        member = up in nodes and up not in file_ids
        is_class = n.get("_callable_class") or (structural and not n["label"].endswith("()"))
        symbols.append(
            {
                "qualname": qual,
                "name": _clean(n["label"]),
                "kind": "class" if is_class else ("method" if member else "function"),
                "line": _line(n.get("source_location")),
                "parent": quals.get(up) if member else None,
            }
        )
    return symbols, quals


def _xrefs(result: dict, quals: dict[str, str], scopes: xref.Scopes) -> list[dict]:
    """The calls graphify could not resolve in the file, each with the ``via`` its imports give it."""
    out, seen = [], set()
    for call in result.get("raw_calls") or []:
        caller = quals.get(call.get("caller_nid"))
        via = xref.call_via(call, scopes) if caller else []
        key = (caller, repr(via))
        if via and key not in seen:
            seen.add(key)
            out.append({"caller": caller, "line": _line(call.get("source_location")), "via": via})
    return out


def extract(text: str, path: str) -> dict | None:
    """Return {"symbols", "calls", "imports": [[candidate paths]], "xrefs"} or None.

    xrefs: {caller, line, via}, a call into another file with the evidence ``xref.call_via`` found for it;
    A symbol of a file that declares a package or namespace carries it as ``package``, an index key.
    """
    ran = _run(text, path)
    if ran is None:
        return None
    result, target, structural = ran
    symbols, quals = _symbols(result, target, path, structural)
    calls = []
    for e in result.get("edges", []):
        if e.get("relation") == "calls" and e["source"] in quals and e["target"] in quals:
            calls.append(
                {
                    "caller": quals[e["source"]],
                    "callee": quals[e["target"]],
                    "line": _line(e.get("source_location")),
                    "status": "parsed" if e.get("confidence") == "EXTRACTED" else "resolved",
                }
            )
    scopes = xref.scopes_of(text, path, result)
    for sym in symbols:
        package = scopes.package_at(sym["line"])
        if package:
            sym["package"] = package
    return {
        "symbols": symbols,
        "calls": calls,
        "imports": import_candidates(text, path),
        "xrefs": _xrefs(result, quals, scopes),
    }
