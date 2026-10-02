"""``graphify-ast`` code backend: graphify's tree-sitter extractors (Apache-2.0), called per file.

Optional: ``pip install 'evo-agents[graphify]'``. graphify extracts one file at a time; this module
maps its nodes to symbols (qualified names built from its contains and method edges) and keeps its
same-file calls. Relative JS and TS imports are resolved here from the import specifiers, because a
single-file run of graphify cannot see the target file.
"""

from __future__ import annotations

import posixpath
import re
import tempfile
from functools import cache
from pathlib import Path, PurePosixPath

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["'](\.{1,2}/[^"']+)["']""")


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


def extract(text: str, path: str) -> dict | None:
    """Return {"symbols": [...], "calls": [...], "imports": [[candidate paths]]} or None."""
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
    nodes = {n["id"]: n for n in result.get("nodes", [])}
    edges = result.get("edges", [])
    file_ids = {
        n["id"] for n in nodes.values() if n.get("label") == target.name and _line(n.get("source_location")) <= 1
    }
    parent: dict[str, str] = {}
    for e in edges:
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
        if nid in file_ids or not n.get("_callable"):
            continue
        qual = qualname(nid)
        if not qual:
            continue
        quals[nid] = qual
        up = parent.get(nid)
        kind = "class" if n.get("_callable_class") else ("method" if up in nodes and up not in file_ids else "function")
        symbols.append(
            {
                "qualname": qual,
                "name": _clean(n["label"]),
                "kind": kind,
                "line": _line(n.get("source_location")),
                "parent": quals.get(up) if up in nodes and up not in file_ids else None,
            }
        )
    calls = []
    for e in edges:
        if e.get("relation") == "calls" and e["source"] in quals and e["target"] in quals:
            calls.append(
                {
                    "caller": quals[e["source"]],
                    "callee": quals[e["target"]],
                    "line": _line(e.get("source_location")),
                    "status": "parsed" if e.get("confidence") == "EXTRACTED" else "resolved",
                }
            )
    return {"symbols": symbols, "calls": calls, "imports": import_candidates(text, path)}
