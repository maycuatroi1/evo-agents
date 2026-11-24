"""Structure facts of the ``scip`` code backend: what ``props.scip`` of a code item (``extract.scip_index``) says,
as facts for the link stage.

The symbols of the file come from the base extractor (``python-ast``, ``graphify-ast``) when it made any; their
nodes are only named here, by an ``ident`` of type ``scip`` (SCIP symbol -> node), so a use in another file finds
them. A file the base extractor read as nothing (TypeScript without graphify) gets its Symbol nodes from the index's
definitions. Each use the index resolved is a ``ref`` of type ``scip`` that the link stage resolves through those
idents, ``introspected`` at confidence 1: a ``calls`` edge from a function to a function or method, a ``references``
edge for any other use (a type, a variable, a use at the top of the file, from the file), and an ``implements`` edge
from a symbol to each one the index says it implements.
"""

from __future__ import annotations

from evo_agents.kg.extract.scip_index import symbol_kind
from evo_agents.kg.pipeline.stages import frag_unit, item_unit, ref, symbol_facts

STATUS = "introspected"


def _known(facts: list[dict], path: str) -> dict[str, str]:
    """Qualified name -> Symbol node, of the symbols the base extractor made for ``path``."""
    prefix = f"{path}::"
    return {
        f["key"][len(prefix) :]: f["node"]
        for f in facts
        if f.get("t") == "ident" and f.get("type") == "symbol" and f["key"].startswith(prefix)
    }


def _own_symbols(record: dict, source_id: str, defs: list, unit_of, known: dict[str, str]) -> list[dict]:
    """The Symbol nodes of the index's functions, methods and classes when the base extractor made none."""
    facts: list[dict] = []
    for symbol, qual, line, end in defs:
        kind = symbol_kind(symbol)
        if kind == "variable" or qual in known:
            continue
        parent, _, name = qual.rpartition(".")
        sym = {
            "qualname": qual,
            "name": name,
            "kind": "class" if kind == "class" else ("method" if parent else "function"),
            "top": qual.split(".")[0],
            "line": line,
            "end": end,
            "parent": parent if parent in known else None,
        }
        facts += symbol_facts(record, source_id, sym, unit_of(sym["top"]), known)
    return facts


def scip_facts(record: dict, source_id: str, base: list[dict]) -> list[dict]:
    """The facts ``props.scip`` adds to what the base extractor found in the same file (``base``)."""
    data = (record.get("props") or {}).get("scip") or {}
    path = (record.get("props") or {}).get("path") or ""
    item_id = record["id"]
    anchors = {f["anchor"] for f in record.get("fragments", [])}

    def unit_of(top: str | None) -> str:
        return frag_unit(item_id, top) if top and top in anchors else item_unit(item_id)

    known = _known(base, path)
    facts: list[dict] = [] if known else _own_symbols(record, source_id, data.get("defs") or [], unit_of, known)
    node_of, qual_of = {}, {}
    for symbol, qual, _, _ in data.get("defs") or []:
        if qual in known:
            node_of[symbol], qual_of[symbol] = known[qual], qual
            facts.append({"t": "ident", "type": "scip", "key": symbol, "node": known[qual]})
    facts += _implements(data.get("impl") or [], node_of, qual_of, unit_of)
    return facts + _uses(data.get("refs") or [], item_id, node_of, qual_of, unit_of)


def _implements(pairs: list, node_of: dict, qual_of: dict, unit_of) -> list[dict]:
    return [
        ref(
            node_of[child],
            "implements",
            {"type": "scip", "value": parent},
            [unit_of(qual_of[child].split(".")[0])],
            status=STATUS,
        )
        for child, parent in pairs
        if child in node_of
    ]


def _uses(refs: list, item_id: str, node_of: dict, qual_of: dict, unit_of) -> list[dict]:
    facts = []
    for caller, target, line in refs:
        if caller == target:
            continue
        src = node_of.get(caller, item_id)
        function_caller = src != item_id and symbol_kind(caller) == "function"
        rel = "calls" if function_caller and symbol_kind(target) == "function" else "references"
        unit = unit_of(qual_of[caller].split(".")[0]) if src != item_id else unit_of(None)
        facts.append(
            ref(src, rel, {"type": "scip", "value": target}, [unit], status=STATUS, where=[[unit, f"line {line}"]])
        )
    return facts
