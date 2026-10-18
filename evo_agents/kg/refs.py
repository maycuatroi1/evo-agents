"""What the text of the kg tools calls a node, and the argument names the tools take besides their own.

The stored id of a file, a section of one and a symbol repeats a word the reader does not need:
``evo-agents:file:evo_agents/kg/store.py``, ``harness:file:docs/x.md#setup``,
``symbol:evo-agents:evo_agents/kg/store.py::Store.search``. The text shows them as ``evo-agents:evo_agents/kg/store.py``,
``harness:docs/x.md#setup`` and ``evo-agents:evo_agents/kg/store.py::Store.search``: source, path, then the symbol or
the section, the form ``bindings/*.yaml`` already uses. Every other id (``plan:x/step:2``, ``repo:x``, ``seam:x``,
``dir:x:path``) reads well and stays. Each tool takes the readable form and the stored one alike (``stored``); the
structured result keeps the stored ids.

Agents guess argument names: Codex sent kg_impact ``files``, ``targets``, ``target``, ``changed`` and ``name``, and
kg_context ``budget`` and ``max_tokens`` (2026-09-20 to 2026-10-17). ``arguments`` takes the names of ALIASES as the
tool's own, and refuses any other unknown name with a message that lists the right ones.
"""

from __future__ import annotations

# The prefixes of the ids of other kinds: a source with one of these names keeps its stored ids, so a readable id
# never reads like the id of a plan, a repo or a symbol.
RESERVED = frozenset({"commit", "dir", "plan", "repo", "requirement", "seam", "source", "symbol", "usecase"})
_FILE = ":file:"
_SYMBOL = "symbol:"

# Per tool: an argument name agents use for one of the tool's own.
ALIASES = {
    "kg_search": {
        "q": "query",
        "text": "query",
        "term": "query",
        "kind": "kinds",
        "type": "kinds",
        "top_k": "limit",
        "max_results": "limit",
    },
    "kg_context": {
        "q": "query",
        "question": "query",
        "text": "query",
        "id": "ids",
        "node": "ids",
        "nodes": "ids",
        "budget": "budget_tokens",
        "max_tokens": "budget_tokens",
        "tokens": "budget_tokens",
        "token_budget": "budget_tokens",
        "depth": "hops",
    },
    "kg_node": {"node": "id", "node_id": "id", "ref": "id"},
    "kg_impact": {
        "node": "ids",
        "nodes": "ids",
        "node_ids": "ids",
        "symbol": "ids",
        "symbols": "ids",
        # A path or an id: kg_impact reads a path that names no file as an id, and an id that names no node as a path.
        "files": "paths",
        "file": "paths",
        "path": "paths",
        "target": "paths",
        "targets": "paths",
        "changed": "paths",
        "name": "paths",
        "patch": "diff",
        "hops": "depth",
    },
    "kg_path": {
        "source": "from",
        "start": "from",
        "src": "from",
        "target": "to",
        "end": "to",
        "dst": "to",
        "hops": "max_hops",
        "depth": "max_hops",
    },
    "kg_more": {"id": "handle", "cursor": "handle"},
}


class UnknownArgument(Exception):
    pass


def readable(node_id: str) -> str:
    """The id the text shows for ``node_id``: ``source:path``, ``source:path#section`` or ``source:path::symbol``
    for a file, a section and a symbol, else the id itself."""
    if node_id.startswith(_SYMBOL):
        rest = node_id[len(_SYMBOL) :]
        source, sep, path = rest.partition(":")
        return rest if sep and "::" in path and source not in RESERVED else node_id
    source, sep, path = node_id.partition(_FILE)
    if sep and path and source and ":" not in source and source not in RESERVED:
        return f"{source}:{path}"
    return node_id


def stored(ref: str) -> list[str]:
    """The stored ids ``ref`` may stand for, in the order to try them: ``ref`` itself first, then the symbol and
    the file (or section) a readable id names."""
    out = [ref]
    source, sep, rest = ref.partition(":")
    if not sep or not rest or source in RESERVED or rest.startswith("file:"):
        return out
    if "::" in rest:
        out.append(_SYMBOL + ref)
    out.append(f"{source}{_FILE}{rest}")
    return out


def path_like(ref: str) -> bool:
    """Whether ``ref``, which names no node, may be a path (``a/b.py``, ``repo:a/b.py``) rather than an id: it has a
    slash or a dot, no ``::`` and no prefix of another kind's id. An id is never read as a path, so a call that names
    a hidden node gets no answer that repeats it."""
    return ("/" in ref or "." in ref) and "::" not in ref and ref.partition(":")[0] not in RESERVED


def named_by(ref: str, name) -> bool:
    """Whether ``ref`` already ends with ``name`` after a separator, so a line need not print the name again."""
    name = name.rstrip("/") if isinstance(name, str) else ""
    if not name or not ref.endswith(name) or len(ref) == len(name):
        return False
    return ref[-len(name) - 1] in ":/#"


def arguments(tool: str, args: dict, known: set[str], lists: set[str]) -> dict:
    """``args`` with the names of ALIASES[tool] read as the tool's own. ``known`` are the tool's argument names,
    ``lists`` those that take a list: one string given for them becomes a list of one. An own name wins over an alias
    of it, except for a list, which takes both. Raises UnknownArgument for any other name, listing the known ones."""
    out = {key: value for key, value in args.items() if key in known}
    aliases = ALIASES.get(tool, {})
    for key, value in args.items():
        if key in known:
            continue
        own = aliases.get(key)
        if own is None:
            names = ", ".join(sorted(known)) or "no argument"
            raise UnknownArgument(f"{tool} has no argument {key!r}; it takes {names}")
        if own in lists:
            given = out.get(own)
            given = [given] if isinstance(given, str) else list(given or [])
            out[own] = given + ([value] if isinstance(value, str) else list(value or []))
        elif own not in out:
            out[own] = value
    for key in lists & set(out):
        if isinstance(out[key], str):
            out[key] = [out[key]]
    return out
