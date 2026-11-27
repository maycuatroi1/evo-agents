"""Code items: which extractor reads one, through the registry CODE_EXTRACTORS, and the facts each makes of it.

A source's code backend (``code.backend``: auto, python-ast, graphify-ast, scip or none) chooses for each code item an
extractor whose key (``python-ast``, ``graphify-ast@<version>``, ``none``, with ``+scip`` when it also reads the
item's SCIP data) enters the structure stage's memo key and the build's coverage.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import ClassVar

from evo_agents.kg.extract import graphify as gfy
from evo_agents.kg.extract import python as py_extract
from evo_agents.kg.pipeline.facts import edge, frag_unit, item_unit, node, ref

SCIP_MARK = "+scip"  # an extractor that also read the item's SCIP data (``pipeline.scip_facts``)
# The optional extra behind the graphify-ast code backend (symbols for about 40 languages).
GRAPHIFY_INSTALL = "pip install 'evo-ak[graphify]' or uv tool install 'evo-ak[graphify]'"


def symbol_key(record: dict, source_id: str, path: str, qualname: str) -> list[str]:
    """The PG-Keys key of a Symbol: one function of one file of one repo, whichever source read it. The
    repo is the item's ``props.repo``: the harness.yaml name for a ``repo:`` source or for the harness
    itself, the checkout's directory name for a git source given only a ``path:``. An item without one
    (another connector) falls back to its source id."""
    return [(record.get("props") or {}).get("repo") or source_id, path, qualname]


def symbol_facts(record: dict, source_id: str, sym: dict, unit: str, by_qual: dict[str, str]) -> list[dict]:
    """The Symbol node of one definition, its ``member_of`` or ``defines`` edge, and its ident. The ident
    names the file, and the package the definition is in when its language has packages: the link stage
    finds a symbol by file or package and qualified name. A class's ident also carries its ``bases``, the
    ``via`` lists the index resolves once every definition is known (``pipeline.hierarchy``), and a Python function
    the class its return annotation names, ``returns`` (``items`` for a tuple), resolved the same way."""
    item_id = record["id"]
    props = record.get("props") or {}
    path = props.get("path") or ""
    sid = f"symbol:{source_id}:{path}::{sym['qualname']}"
    by_qual[sym["qualname"]] = sid
    facts = [
        node(
            sid,
            "Symbol",
            sym["qualname"],
            [unit],
            props={
                "kind": sym["kind"],
                "path": path,
                "line": sym["line"],
                **({"end_line": sym["end"]} if sym.get("end") else {}),
                "file": item_id,
                "key": symbol_key(record, source_id, path, sym["qualname"]),
            },
            where=[[unit, f"line {sym['line']}"]],
        )
    ]
    if sym.get("doc"):
        facts.append({"t": "doc", "src": sid, "text": sym["doc"]})
    parent = by_qual.get(sym["parent"]) if sym["parent"] else None
    if parent:
        facts.append(edge(sid, "member_of", parent, [unit]))
    else:
        facts.append(edge(item_id, "defines", sid, [unit]))
    facts.append(
        {
            "t": "ident",
            "type": "symbol",
            "key": f"{path}::{sym['qualname']}",
            "name": sym["name"],
            "qualname": sym["qualname"],
            "source": source_id,
            "repo": props.get("repo"),
            "file": item_id,
            "path": path,
            "kind": sym["kind"],
            "node": sid,
            **({"package": sym["package"]} if sym.get("package") else {}),
            **({"bases": sym["bases"]} if sym.get("bases") else {}),
            **{k: sym[k] for k in ("returns", "items") if sym.get(k)},
        }
    )
    return facts


def _xref_facts(record: dict, source_id: str, xrefs: list[dict], by_qual: dict[str, str], unit_for) -> list[dict]:
    """A ``calls`` reference per call into another file, for the link stage to resolve through the index.
    ``via`` holds the evidence the file's imports give, most specific first (see ``extract.xref``)."""
    repo = (record.get("props") or {}).get("repo")
    facts = []
    for x in xrefs:
        caller = by_qual.get(x["caller"])
        if caller:
            unit = unit_for(x.get("top"))
            flags = {k: True for k in ("read", "dispatch") if x.get("ref" if k == "read" else k)}
            target = {"type": "call", "via": x["via"], "source": source_id, "repo": repo, **flags}
            facts.append(ref(caller, "calls", target, [unit], status="resolved", where=[[unit, f"line {x['line']}"]]))
    return facts


def missing_graphify(source_id: str) -> str:
    """The build error for a source that asks for ``graphify-ast`` where graphify cannot be imported."""
    return (
        f"source {source_id} declares code backend graphify-ast, but graphify is not installed;"
        f" install the extra: {GRAPHIFY_INSTALL}"
    )


SCIP_MISSING = (
    "code backend scip found no SCIP index data for any code item (no index file, or no scip-python or scip-typescript"
    " here), so the code was read with python-ast and graphify-ast; set code.scip_index or install the tools"
)


def code_coverage(backend: str, items: Sequence[tuple[dict, str]]) -> dict:
    """What the code backend of one source did with its code items, given as (record, extractor) pairs:
    items per extractor (``none``: no symbols), and, when ``auto`` ran without graphify, the non-Python
    items it left without symbols, by language, with a warning that names the extra to install. Coverage
    data only: the graph and the memo keys stay what ``code_extractor`` makes them."""
    extractors: dict[str, int] = {}
    typed = 0
    for _, extractor in items:
        typed += extractor.endswith(SCIP_MARK)
        name = extractor.removesuffix(SCIP_MARK).partition("@")[
            0
        ]  # the backend, without the version the memo key carries
        extractors[name] = extractors.get(name, 0) + 1
    fallback: dict[str, int] = {}
    if backend == "auto" and not gfy.available():
        for record, extractor in items:
            path = (record.get("props") or {}).get("path") or ""
            if extractor == "none" and PurePosixPath(path).suffix != ".py":
                language = (record.get("body") or {}).get("language") or PurePosixPath(path).suffix or "unknown"
                fallback[language] = fallback.get(language, 0) + 1
    skipped = sum(fallback.values())
    warning = None
    if skipped:
        languages = ", ".join(f"{lang} {n}" for lang, n in sorted(fallback.items(), key=lambda kv: (-kv[1], kv[0])))
        warning = (
            f"graphify is not installed, so code backend auto fell back to python-ast:"
            f" {skipped} non-Python code item(s) got no symbols ({languages}); install the extra: {GRAPHIFY_INSTALL}"
        )
    out = {
        "backend": backend,
        "items": len(items),
        "extractors": dict(sorted(extractors.items(), key=lambda kv: (kv[0] == "none", kv[0]))),
        "fallback": skipped,
        "fallback_languages": dict(sorted(fallback.items())),
        "warning": warning,
    }
    if typed or backend == "scip":
        out["scip"] = {"items": typed, "without": len(items) - typed}
        if backend == "scip" and not typed and items:
            out["warning"] = warning or SCIP_MISSING
    return out


COMMENT_LINE = re.compile(r"(?:///?|/\*+|\*+/?|#(?![!\[])|--)\s?")  # a line of a comment, by how it starts
ANNOTATION = re.compile(r"@|#\[")  # a decorator or an attribute between a comment and its definition


def leading_comment(lines: list[str], line: int | None) -> str | None:
    """The comment a definition at ``line`` (from 1) carries: the comment lines right above it, decorators and
    attributes skipped; with ``line`` None, the comment that opens the file (after a shebang). Markers dropped."""
    if line is None:
        start = 1 if lines and lines[0].startswith("#!") else 0
        block = []
        for raw in lines[start:]:
            if not COMMENT_LINE.match(raw.strip()):
                break
            block.append(raw.strip())
    else:
        i = min(line, len(lines) + 1) - 2
        while i >= 0 and ANNOTATION.match(lines[i].strip()):
            i -= 1
        block = []
        while i >= 0 and COMMENT_LINE.match(lines[i].strip()):
            block.append(lines[i].strip())
            i -= 1
        block.reverse()
    words = [re.sub(r"\*+/$", "", COMMENT_LINE.sub("", s, count=1)).strip() for s in block]
    joined = "\n".join(w for w in words if w)
    return joined or None


class CodeExtractor:
    """What reads the symbols, calls and imports of a code item. ``name`` is the first part of its key in the
    structure stage's memo and in the coverage of a build; ``version`` the part after ``@``, when it has one."""

    name: ClassVar[str] = "none"

    def wants(self, path: str, backend: str) -> bool:
        """Whether the choice of ``backend`` for a file at ``path`` falls to this extractor, which reads it or not."""
        return True

    def reads(self, path: str) -> bool:
        return False

    def key(self) -> str:
        return self.name

    def facts(self, record: dict, source_id: str, text: str) -> list[dict]:
        return []


class PythonAst(CodeExtractor):
    """``python-ast``: Python's own parser (``extract.python``), for the Python files of every backend but graphify-ast."""

    name = "python-ast"

    def wants(self, path: str, backend: str) -> bool:
        return backend == "python-ast" or (backend in ("auto", "scip") and _is_python(path))

    def reads(self, path: str) -> bool:
        return _is_python(path)

    def facts(self, record: dict, source_id: str, text: str) -> list[dict]:
        item_id = record["id"]
        path = (record.get("props") or {}).get("path") or ""
        result = py_extract.extract(text, path)
        if result is None:
            return [{"t": "issue", "message": f"{path}: does not parse", "unit": item_unit(item_id)}]
        anchors = {f["anchor"] for f in record.get("fragments", [])}
        facts = [{"t": "doc", "src": item_id, "text": result["doc"]}] if result.get("doc") else []

        def unit_for(top: str) -> str:
            return frag_unit(item_id, top) if top in anchors else item_unit(item_id)

        by_qual: dict[str, str] = {}
        for sym in result["symbols"]:
            facts += symbol_facts(record, source_id, sym, unit_for(sym["top"]), by_qual)
        for name, qual in (result.get("aliases") or {}).items():
            if qual in by_qual:  # `print_json = _print_json`: an import of `print_json` reaches `_print_json`
                facts.append({"t": "ident", "type": "alias", "key": f"{item_id}::{name}", "node": by_qual[qual]})
        for imp in result["imports"]:
            facts.append(
                ref(
                    item_id,
                    "imports",
                    {
                        "type": "module",
                        "value": imp["module"],
                        "source": source_id,
                        "optional": bool(imp.get("optional")),
                    },
                    [unit_for(imp["top"])],
                )
            )
        for call in result["calls"]:
            caller, callee = by_qual.get(call["caller"]), by_qual.get(call["callee"])
            if caller and callee:
                unit = unit_for(call["top"])
                facts.append(
                    edge(
                        caller,
                        "calls",
                        callee,
                        [unit],
                        status="resolved",
                        conf=0.8,
                        where=[[unit, f"line {call['line']}"]],
                    )
                )
        return facts + _xref_facts(record, source_id, result["xrefs"], by_qual, unit_for)


class GraphifyAst(CodeExtractor):
    """``graphify-ast``: graphify's tree-sitter extractors (``extract.graphify``), when the extra is installed."""

    name = "graphify-ast"

    def reads(self, path: str) -> bool:
        return gfy.available() and gfy.supports(path)

    def key(self) -> str:
        return f"{self.name}@{gfy.version()}"

    def facts(self, record: dict, source_id: str, text: str) -> list[dict]:
        item_id = record["id"]
        props = record.get("props") or {}
        path = props.get("path") or ""
        result = gfy.extract(text, path)
        unit = item_unit(item_id)
        if result is None:
            return [{"t": "issue", "message": f"{path}: graphify could not extract it", "unit": unit}]
        lines = text.splitlines()
        head = leading_comment(lines, None)
        facts: list[dict] = [{"t": "doc", "src": item_id, "text": head}] if head else []
        by_qual: dict[str, str] = {}
        for sym in result["symbols"]:
            doc = leading_comment(lines, sym["line"])
            facts += symbol_facts(record, source_id, {**sym, "doc": doc} if doc else sym, unit, by_qual)
        for call in result["calls"]:
            caller, callee = by_qual.get(call["caller"]), by_qual.get(call["callee"])
            if caller and callee and caller != callee:
                facts.append(
                    edge(
                        caller,
                        "calls",
                        callee,
                        [unit],
                        status=call["status"],
                        conf=1.0 if call["status"] == "parsed" else 0.8,
                        where=[[unit, f"line {call['line']}"]],
                    )
                )
        for candidates in result["imports"]:
            facts.append(
                ref(item_id, "imports", {"type": "paths", "values": candidates, "repo": props.get("repo")}, [unit])
            )
        return facts + _xref_facts(record, source_id, result["xrefs"], by_qual, lambda _top: unit)


@dataclass(frozen=True)
class Choice:
    """The extractor of one code item and whether it also reads the item's SCIP data (``pipeline.scip_facts``)."""

    extractor: CodeExtractor
    scip: bool = False

    @property
    def key(self) -> str:
        return f"{self.extractor.key()}{SCIP_MARK}" if self.scip else self.extractor.key()


class CodeExtractors:
    """The registry of code extractors, in the order a backend's choice goes through them: the first that ``wants`` an
    item reads it when it ``reads`` its path, and otherwise leaves it to ``none``."""

    def __init__(self, extractors: Sequence[CodeExtractor]):
        self.extractors = tuple(extractors)
        self.named = {e.name: e for e in (NONE, *self.extractors)}

    def choose(self, record: dict, backend: str) -> Choice:
        props = record.get("props") or {}
        path = props.get("path") or ""
        if record.get("kind") != "code" or backend == "none":
            return Choice(NONE)
        chosen = next((e for e in self.extractors if e.wants(path, backend)), NONE)
        base = chosen if chosen.reads(path) else NONE
        # The index data the connector read from a SCIP file rides on the item: the build reads that, never a compiler.
        return Choice(base, scip=backend in ("auto", "scip") and bool(props.get("scip")))

    def facts(self, record: dict, source_id: str, text: str, backend: str) -> list[dict]:
        """The facts the extractor ``backend`` chooses makes of a code item, with its SCIP data's."""
        choice = self.choose(record, backend)
        facts = choice.extractor.facts(record, source_id, text)
        if choice.scip:
            from evo_agents.kg.pipeline.scip_facts import scip_facts

            facts += scip_facts(record, source_id, facts)
        return facts


NONE = CodeExtractor()
CODE_EXTRACTORS = CodeExtractors([PythonAst(), GraphifyAst()])


def _is_python(path: str) -> bool:
    return PurePosixPath(path).suffix == ".py"


def code_extractor(record: dict, backend: str) -> str:
    """Which extractor a code item gets, with its version: part of the structure task's memo key. ``+scip`` follows the
    base extractor of an item that carries SCIP data (``props.scip``) under the ``scip`` and ``auto`` backends."""
    return CODE_EXTRACTORS.choose(record, backend).key
