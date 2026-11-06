"""The spec of an agent system, ``evo-agent-system/1`` (``docs/hub.md``, Agent systems): what it holds, how it is
checked, and its YAML form.

An agent system is an object of a project: nodes on a canvas and typed edges between them. A node is the ``entry``
(whoever writes to the system), an ``agent`` (a profile of the project, pinned at one of its versions), a ``kg`` (a
named graph of the project, ``default`` for every source), the ``curator`` or the ``orchestrator`` (a profile of the
project, pinned like an agent). Each node has a place on the canvas (``position``). An edge has a kind, which fixes
the kinds of its two ends (``EDGE_ENDS``), and a description:

- ``interacts``: the entry or an agent to an agent;
- ``uses_kg``: an agent to a kg;
- ``optimizes``: the curator to an agent or a kg;
- ``orchestrates``: the orchestrator to an agent;
- ``escalates``: an agent to the orchestrator.

``check`` reads a spec as a client sent it and gives it back whole (defaults filled in) with what is wrong in it, each
problem naming the node or the edge it is about, so the canvas can show it there. ``check_refs`` adds what needs the
hub: a profile or a named graph the project does not have, a deleted profile, a version it never had, and a profile
that is already the entry agent of another system. ``pin`` fills in the version of a node that names none: its
profile's latest. An error stops a write; a warning does not.

The entry agent of a system is the agent its entry interacts with: at most one, and a profile is the entry agent of
at most one system, so a message to it in any chat belongs to one system.

Standard library and PyYAML only: the command line imports this module on a core install.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

import yaml

from evo_agents.hub.profiles import HANDLE_PATTERN

SCHEMA = "evo-agent-system/1"
NODE_KINDS = ("entry", "agent", "kg", "curator", "orchestrator")
PINNED_KINDS = ("agent", "orchestrator")  # the nodes that name a profile and pin one of its versions
SINGLE_KINDS = ("entry", "curator", "orchestrator")  # a system has one entry, at most one curator and orchestrator
EDGE_ENDS = {  # kind: (the kinds it may start from, the kinds it may end at)
    "interacts": (("entry", "agent"), ("agent",)),
    "uses_kg": (("agent",), ("kg",)),
    "optimizes": (("curator",), ("agent", "kg")),
    "orchestrates": (("orchestrator",), ("agent",)),
    "escalates": (("agent",), ("orchestrator",)),
}
EDGE_KINDS = tuple(EDGE_ENDS)
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"  # of a system, in its URL
NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"
GRAPH_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"  # a named graph's (tables_kg.GRAPH_NAME_PATTERN), or default
RESERVED_SLUGS = ("validate",)  # POST .../systems/validate
MAX_NODES = 100
MAX_EDGES = 400
MAX_NAME_CHARS = 80
MAX_PURPOSE_CHARS = 2000
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 500
MAX_POSITION = 1_000_000.0
LIMITS = {  # name: (default, lowest, highest); the budgets the hub holds a trace and a day of the system to
    "trace_usd": (2.0, 0.01, 100.0),  # the cost of one trace, worker runs of its tree included
    "trace_runs": (12, 1, 200),  # the runs of one trace
    "daily_usd": (5.0, 0.01, 1000.0),  # what the hub runtime of the system spends in a day
}
TOP_KEYS = ("schema", "slug", "name", "purpose", "limits", "nodes", "edges")
NODE_KEYS = ("id", "kind", "label", "position", "profile", "version", "graph")
EDGE_KEYS = ("from", "to", "kind", "description")
ERROR, WARNING = "error", "warning"

_SLUG = re.compile(SLUG_PATTERN)
_NODE_ID = re.compile(NODE_ID_PATTERN)
_HANDLE = re.compile(HANDLE_PATTERN)
_GRAPH = re.compile(GRAPH_PATTERN)


@dataclass(frozen=True)
class Problem:
    """What is wrong in a spec: ``level`` (error or warning), ``path`` into the spec, the ``node`` id or the ``edge``
    index it is about when there is one, and a sentence."""

    level: str
    path: str
    message: str
    node: str | None = None
    edge: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProfileRef:
    """A profile of the project as ``check_refs`` needs it: its latest version, and whether it was deleted."""

    latest: int
    deleted: bool = False


def check_slug(slug: str) -> str | None:
    """Why ``slug`` cannot name a system, or None."""
    if not isinstance(slug, str) or not _SLUG.match(slug):
        return f"a system's slug is 1 to 63 of a-z, 0-9 and '-', starting with a letter or a digit: {slug!r}"
    if slug in RESERVED_SLUGS:
        return f"{slug!r} is a word of the API: pick another slug"
    return None


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == ERROR]


class _Checker:
    def __init__(self) -> None:
        self.problems: list[Problem] = []

    def add(self, path: str, message: str, *, node=None, edge=None, level: str = ERROR) -> None:
        self.problems.append(Problem(level, path, message, node, edge))

    def text(self, value, path: str, limit: int, *, required: bool = False, **where) -> str:
        if value is None:
            if required:
                self.add(path, "is required", **where)
            return ""
        if not isinstance(value, str):
            self.add(path, "is not text", **where)
            return ""
        value = value.strip()
        if required and not value:
            self.add(path, "is empty", **where)
        if len(value) > limit:
            self.add(path, f"has {len(value)} characters, over the {limit} it takes", **where)
        return value

    def unknown(self, found: dict, known: tuple[str, ...], path: str, **where) -> None:
        for key in found:
            if key not in known:
                self.add(f"{path}.{key}" if path else str(key), f"is not a key of {SCHEMA}", **where)


def _limits(c: _Checker, given) -> dict:
    found = {name: default for name, (default, _, _) in LIMITS.items()}
    if given is None:
        return found
    if not isinstance(given, dict):
        c.add("limits", "is not a mapping")
        return found
    c.unknown(given, tuple(LIMITS), "limits")
    for name, (default, low, high) in LIMITS.items():
        value = given.get(name, default)
        whole = isinstance(default, int)
        number = isinstance(value, int) if whole else isinstance(value, int | float)
        if isinstance(value, bool) or not number or not low <= value <= high:
            kind = "a whole number" if whole else "a number"
            c.add(f"limits.{name}", f"is {kind} from {low} to {high}")
            continue
        found[name] = int(value) if whole else float(value)
    return found


def _position(c: _Checker, given, path: str, node: str | None) -> dict:
    if given is None:
        return {"x": 0.0, "y": 0.0}
    if not isinstance(given, dict) or set(given) - {"x", "y"}:
        c.add(path, "is {x, y}", node=node)
        return {"x": 0.0, "y": 0.0}
    found = {}
    for axis in ("x", "y"):
        value = given.get(axis, 0)
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            c.add(f"{path}.{axis}", "is a number", node=node)
            value = 0
        elif abs(value) > MAX_POSITION:
            c.add(f"{path}.{axis}", f"is from -{MAX_POSITION:g} to {MAX_POSITION:g}", node=node)
            value = 0
        found[axis] = float(value)
    return found


def _ref_fields(c: _Checker, given: dict, kind: str, path: str, node: str | None) -> dict:
    """The profile and version of a pinned node, the graph of a kg node; nothing for the others."""
    found: dict = {}
    if kind in PINNED_KINDS:
        handle = given.get("profile")
        if isinstance(handle, str):
            handle = handle.strip().removeprefix("@")
        if not isinstance(handle, str) or not _HANDLE.match(handle):
            c.add(f"{path}.profile", "names the handle of a profile of the project", node=node)
        found["profile"] = handle if isinstance(handle, str) else ""
        version = given.get("version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            c.add(f"{path}.version", "is a version of the profile, 1 or more", node=node)
            version = None
        found["version"] = version
    elif kind == "kg":
        graph = given.get("graph", "default")
        if not isinstance(graph, str) or not _GRAPH.match(graph):
            c.add(f"{path}.graph", "names a named graph of the project, or default", node=node)
        found["graph"] = graph if isinstance(graph, str) else ""
    for key in ("profile", "version", "graph"):
        if key in given and key not in found:
            c.add(f"{path}.{key}", f"is not a field of a {kind} node", node=node)
    return found


def _node(c: _Checker, given, index: int) -> dict | None:
    path = f"nodes[{index}]"
    if not isinstance(given, dict):
        c.add(path, "is not a mapping")
        return None
    node_id = given.get("id")
    where = node_id if isinstance(node_id, str) else None
    if not isinstance(node_id, str) or not _NODE_ID.match(node_id):
        c.add(f"{path}.id", "is 1 to 63 of letters, digits, '.', '_' and '-'", node=where)
        return None
    c.unknown(given, NODE_KEYS, path, node=node_id)
    kind = given.get("kind")
    if kind not in NODE_KINDS:
        c.add(f"{path}.kind", f"is one of {', '.join(NODE_KINDS)}", node=node_id)
        return None
    found = {"id": node_id, "kind": kind}
    label = c.text(given.get("label"), f"{path}.label", MAX_LABEL_CHARS, node=node_id)
    if label:
        found["label"] = label
    found.update(_ref_fields(c, given, kind, path, node_id))
    found["position"] = _position(c, given.get("position"), f"{path}.position", node_id)
    return found


def _nodes(c: _Checker, given) -> list[dict]:
    if not isinstance(given, list):
        c.add("nodes", "is a list of nodes")
        return []
    if len(given) > MAX_NODES:
        c.add("nodes", f"holds {len(given)} nodes, over the {MAX_NODES} a system takes")
    nodes: list[dict] = []
    seen: dict[str, str] = {}  # what a node names (a profile, a graph): the node that named it first
    for index, item in enumerate(given[:MAX_NODES]):
        node = _node(c, item, index)
        if node is None:
            continue
        if any(n["id"] == node["id"] for n in nodes):
            c.add(f"nodes[{index}].id", f"{node['id']} names two nodes", node=node["id"])
            continue
        named = f"profile @{node['profile']}" if "profile" in node else f"graph {node.get('graph')}"
        if ("profile" in node and node["profile"]) or node["kind"] == "kg":
            if named in seen:
                c.add(f"nodes[{index}]", f"{named} is on node {seen[named]} already", node=node["id"])
            seen.setdefault(named, node["id"])
        nodes.append(node)
    for kind in SINGLE_KINDS:
        held = [n["id"] for n in nodes if n["kind"] == kind]
        for extra in held[1:]:
            c.add("nodes", f"a system has one {kind} node, and {held[0]} is it", node=extra)
    if not any(n["kind"] == "entry" for n in nodes):
        c.add("nodes", "a system has exactly one entry node: whoever writes to it")
    return nodes


def _edge(c: _Checker, given, index: int, kinds: dict[str, str]) -> dict | None:
    path = f"edges[{index}]"
    if not isinstance(given, dict):
        c.add(path, "is not a mapping", edge=index)
        return None
    c.unknown(given, EDGE_KEYS, path, edge=index)
    kind = given.get("kind")
    if kind not in EDGE_ENDS:
        c.add(f"{path}.kind", f"is one of {', '.join(EDGE_KINDS)}", edge=index)
        return None
    ends = []
    for side in ("from", "to"):
        end = given.get(side)
        if not isinstance(end, str) or end not in kinds:
            c.add(f"{path}.{side}", f"names no node of the system: {end!r}", edge=index)
        ends.append(end)
    description = c.text(given.get("description"), f"{path}.description", MAX_DESCRIPTION_CHARS, edge=index)
    if not all(isinstance(end, str) and end in kinds for end in ends):
        return None
    source, target = ends
    starts, finishes = EDGE_ENDS[kind]
    if source == target:
        c.add(path, "joins a node to itself", edge=index)
    elif kinds[source] not in starts or kinds[target] not in finishes:
        c.add(
            path,
            f"{kind} goes from {' or '.join(starts)} to {' or '.join(finishes)}, not from {kinds[source]} to "
            f"{kinds[target]}",
            edge=index,
        )
    return {"from": source, "to": target, "kind": kind, "description": description}


def _edges(c: _Checker, given, nodes: list[dict]) -> list[dict]:
    if given is None:
        return []
    if not isinstance(given, list):
        c.add("edges", "is a list of edges")
        return []
    if len(given) > MAX_EDGES:
        c.add("edges", f"holds {len(given)} edges, over the {MAX_EDGES} a system takes")
    kinds = {n["id"]: n["kind"] for n in nodes}
    edges: list[dict] = []
    for index, item in enumerate(given[:MAX_EDGES]):
        edge = _edge(c, item, index, kinds)
        if edge is None:
            continue
        key = (edge["from"], edge["to"], edge["kind"])
        if any((e["from"], e["to"], e["kind"]) == key for e in edges):
            c.add(f"edges[{index}]", f"{edge['kind']} from {edge['from']} to {edge['to']} is drawn twice", edge=index)
            continue
        edges.append(edge)
    entry = next((n["id"] for n in nodes if n["kind"] == "entry"), None)
    out = [i for i, e in enumerate(edges) if e["from"] == entry and e["kind"] == "interacts"]
    if entry is not None and not out:
        c.add("edges", "the entry interacts with no agent: nothing answers it yet", node=entry, level=WARNING)
    for index in out[1:]:
        c.add(f"edges[{index}]", "the entry interacts with one agent, its entry agent", edge=index)
    return edges


def check(spec) -> tuple[dict, list[Problem]]:
    """``spec`` whole, as the hub keeps it (defaults filled in, text stripped, ``slug`` left out), and what is wrong
    in it. Nothing here needs the hub; ``check_refs`` adds what does."""
    c = _Checker()
    if not isinstance(spec, dict):
        c.add("", "a system's spec is a mapping")
        return {}, c.problems
    c.unknown(spec, TOP_KEYS, "")
    if spec.get("schema") != SCHEMA:
        c.add("schema", f"is {SCHEMA}")
    found = {
        "schema": SCHEMA,
        "name": c.text(spec.get("name"), "name", MAX_NAME_CHARS, required=True),
        "purpose": c.text(spec.get("purpose"), "purpose", MAX_PURPOSE_CHARS),
        "limits": _limits(c, spec.get("limits")),
    }
    found["nodes"] = _nodes(c, spec.get("nodes"))
    found["edges"] = _edges(c, spec.get("edges"), found["nodes"])
    return found, c.problems


def entry_agent(spec: dict) -> str | None:
    """The handle of the entry agent of a checked spec: the agent its entry interacts with; None for none."""
    nodes = {n["id"]: n for n in spec.get("nodes") or ()}
    entry = next((n["id"] for n in nodes.values() if n["kind"] == "entry"), None)
    for edge in spec.get("edges") or ():
        if edge["from"] == entry and edge["kind"] == "interacts" and edge["to"] in nodes:
            return nodes[edge["to"]].get("profile") or None
    return None


def pins(spec: dict) -> list[dict]:
    """The pinned nodes of a checked spec: ``{node, kind, profile, version}`` each, in the order of its nodes."""
    return [
        {"node": n["id"], "kind": n["kind"], "profile": n["profile"], "version": n.get("version")}
        for n in spec.get("nodes") or ()
        if n["kind"] in PINNED_KINDS
    ]


def pin(spec: dict, profiles: dict[str, ProfileRef]) -> dict:
    """``spec`` with every pinned node that names no version pinned at its profile's latest (one the project has)."""
    nodes = []
    for node in spec.get("nodes") or ():
        ref = profiles.get(node.get("profile") or "")
        if node["kind"] in PINNED_KINDS and node.get("version") is None and ref is not None:
            node = {**node, "version": ref.latest}
        nodes.append(node)
    return {**spec, "nodes": nodes}


def check_refs(
    spec: dict, profiles: dict[str, ProfileRef], graphs: set[str], entries: dict[str, str] | None = None
) -> list[Problem]:
    """What is wrong in a checked spec against the project: ``profiles`` its profiles by handle, ``graphs`` the names
    of its named graphs, ``entries`` the profiles that are the entry agent of another system, with its slug."""
    c = _Checker()
    for index, node in enumerate(spec.get("nodes") or ()):
        path, node_id = f"nodes[{index}]", node["id"]
        if node["kind"] == "kg" and node.get("graph") not in graphs | {"default"}:
            c.add(f"{path}.graph", f"the project has no named graph {node.get('graph')}", node=node_id)
        if node["kind"] not in PINNED_KINDS or not node.get("profile"):
            continue
        ref = profiles.get(node["profile"])
        if ref is None:
            c.add(f"{path}.profile", f"the project has no agent @{node['profile']}", node=node_id)
            continue
        if ref.deleted:
            c.add(f"{path}.profile", f"@{node['profile']} was deleted: it takes no new run", node=node_id)
        version = node.get("version")
        if version is not None and version > ref.latest:
            c.add(f"{path}.version", f"@{node['profile']} is at version {ref.latest}, not {version}", node=node_id)
        elif version is not None and version < ref.latest:
            message = f"@{node['profile']} has version {ref.latest}; this node pins {version}"
            c.add(f"{path}.version", message, node=node_id, level=WARNING)
    handle = entry_agent(spec)
    other = (entries or {}).get(handle or "")
    if other is not None:
        node = next(n["id"] for n in spec["nodes"] if n.get("profile") == handle)
        c.add("edges", f"@{handle} is the entry agent of system {other} already", node=node)
    return c.problems


def to_yaml(slug: str, spec: dict) -> str:
    """The YAML of a system, its slug first, as ``hub system export`` writes it and ``put`` reads it back."""
    ordered = {"schema": spec.get("schema", SCHEMA), "slug": slug}
    ordered.update({k: spec[k] for k in TOP_KEYS if k in spec and k not in ordered})
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=120)


def from_yaml(text: str) -> dict:
    """The spec a YAML text holds; ValueError when it is not YAML of a mapping."""
    try:
        found = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not YAML: {exc}") from None
    if not isinstance(found, dict):
        raise ValueError("a system's YAML is a mapping: schema, name, nodes, edges")
    return found
