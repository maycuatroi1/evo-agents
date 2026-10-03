"""``evo-agents kg serve``: an MCP server over stdio, bound to one project for its whole life.

Every tool reads the latest ready build and only the visible set V_o: elements whose label flows to the
session's sink. Results carry ids, labels, status and evidence; free text from sources (integrity U) is
never returned by these tools. Each result stays under about 9,000 tokens; the rest waits behind a
handle for ``kg_more``. The structured result of a call that reveals elements names the project and the
join of their labels, which the PostToolUse hook adds to the session label (sessions.py).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections import deque

from evo_agents import __version__
from evo_agents.kg.policy import Label
from evo_agents.kg.project import Project, ProjectError, resolve_project
from evo_agents.kg.sessions import current_session_id, read_session
from evo_agents.kg.sessions import describe as describe_session
from evo_agents.kg.store import StaleSchema, Store

SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
CAP_CHARS = 25_000  # about 9,000 tokens even for Vietnamese text, under Claude Code's 10,000 warning
HANDLE_TTL = 1800
STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "proposed": 1}
HUB_DEGREE = 150

INSTRUCTIONS = (
    "Knowledge graph of the bound project: repos, files, symbols, documents and sections, plans and steps, "
    "seams, and the explicit links between them, each with provenance and a sensitivity label. Use kg_search "
    "or kg_context before grepping for a named requirement, plan, document, seam or feature; use grep for exact "
    "strings and code you just edited. Status 'resolved' and 'proposed' are weaker than 'parsed' and "
    "'declared'. The graph can lag the sources: read the live source (uri) before quoting or editing it."
)

TOOLS = [
    {
        "name": "kg_search",
        "description": "Find nodes by name, id, path, code or words. Returns ids to pass to kg_node or kg_context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "name, id, path, code or words"},
                "kinds": {"type": "array", "items": {"type": "string"}, "description": "e.g. Plan, Symbol, Section"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kg_context",
        "description": "A connected subgraph around a question or a set of ids, within a token budget.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "budget_tokens": {"type": "integer", "minimum": 500, "maximum": 8000, "default": 3000},
                "hops": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
            },
        },
    },
    {
        "name": "kg_node",
        "description": "One node: properties, status, label, evidence (source, revision, span, uri) and its edges.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "kg_status",
        "description": "Bound project, sink profile, snapshot age, and coverage per source.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "kg_more",
        "description": "Continue a truncated result.",
        "inputSchema": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"]},
    },
]


class ToolError(Exception):
    pass


class Session:
    def __init__(self, project: Project | None, sink: str, *, error: str | None = None):
        self.project = project
        self.sink = sink
        self.error = error
        self.handles: dict[str, tuple[float, deque]] = {}
        self.policy = project.policy if project else None
        self.clearance = None
        self.read: Label | None = None  # join of the labels the current call reveals
        if project is not None and error is None:
            if sink == "cli" and "cli" not in self.policy.sinks:
                # The local operator already holds the corpus; the cli sink sees everything unless declared.
                self.clearance = (len(self.policy.levels) - 1, len(self.policy.locations) - 1)
            else:
                try:
                    clr = self.policy.clearance(sink)
                    self.clearance = (clr.level, clr.location)
                except KeyError as exc:
                    self.error = str(exc.args[0])

    # -- visibility -----------------------------------------------------------------------------

    def visible(self, label: tuple) -> bool:
        level, location, _integrity = label
        return level <= self.clearance[0] and location <= self.clearance[1]

    def describe(self, label: tuple) -> str:
        lvl = self.policy.level_name(label[0])
        loc = self.policy.location_name(label[1])
        return f"{lvl}{'' if loc == 'any' else '/' + loc},{label[2]}"

    def _saw(self, label: tuple) -> None:
        seen = Label(label[0], label[1], label[2], frozenset({self.project.name}))
        self.read = seen if self.read is None else self.read.join(seen)

    def _store(self) -> tuple[Store, int]:
        try:
            store = Store.open_existing(self.project)
        except StaleSchema as exc:
            raise ToolError(str(exc)) from None
        b = store.latest_ready() if store else None
        if b is None:
            raise ToolError(f"project {self.project.name!r} has no graph yet: run `evo-agents kg sync --build`")
        return store, b

    # -- dispatch -------------------------------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        if self.error:
            return _error(self.error)
        self.read = None
        try:
            handler = getattr(self, f"tool_{name}", None)
            if handler is None:
                raise ToolError(f"unknown tool {name!r}")
            text, data = handler(**(args or {}))
        except ToolError as exc:
            return _error(str(exc))
        except TypeError as exc:
            return _error(f"bad arguments for {name}: {exc}")
        return self._envelope(text, data)

    def _envelope(self, text: str, data: dict) -> dict:
        if self.read is not None:
            label = {**self.policy.describe(self.read), "projects": sorted(self.read.projects)}
            data = {**data, "project": self.project.name, "label": label}
        if len(text) <= CAP_CHARS:
            return {"content": [{"type": "text", "text": text}], "structuredContent": data}
        head, rest = text[:CAP_CHARS], text[CAP_CHARS:]
        cut = head.rfind("\n")
        if cut > CAP_CHARS // 2:
            head, rest = head[:cut], head[cut + 1 :] + rest
        handle = uuid.uuid4().hex[:12]
        chunks = deque(rest[i : i + CAP_CHARS] for i in range(0, len(rest), CAP_CHARS))
        self.handles[handle] = (time.monotonic(), chunks)
        note = f"\n[truncated: {len(rest)} more characters; call kg_more with handle {handle}]"
        return {
            "content": [{"type": "text", "text": head + note}],
            "structuredContent": {
                "truncated": True,
                "handle": handle,
                "summary": data.get("summary"),
                **{k: data[k] for k in ("project", "label") if k in data},
            },
        }

    # -- tools ----------------------------------------------------------------------------------

    def _line(self, n: dict) -> str:
        props = n["props"]
        extra = []
        for key in ("path", "uri", "status", "number", "line"):
            if props.get(key) not in (None, ""):
                extra.append(f"{key}={props[key]}")
        return f"[{n['id']}] {n['kind']} {n['name']!r} ({n['status']}, {self.describe(n['label'])})" + (
            f" {' '.join(extra)}" if extra else ""
        )

    def _node_json(self, n: dict) -> dict:
        self._saw(n["label"])  # every node in a structured result is revealed
        label = {
            "level": self.policy.level_name(n["label"][0]),
            "location": self.policy.location_name(n["label"][1]),
            "integrity": n["label"][2],
        }
        return {
            "id": n["id"],
            "kind": n["kind"],
            "name": n["name"],
            "status": n["status"],
            "conf": n["conf"],
            "label": label,
            "props": n["props"],
            "aliases": n["aliases"],
        }

    def tool_kg_search(self, query: str, kinds: list | None = None, limit: int = 10) -> tuple[str, dict]:
        store, b = self._store()
        limit = max(1, min(int(limit), 25))
        ids = store.search(query, b, limit=limit * 4)
        found = store.nodes(ids, b)
        results = []
        for nid in ids:
            n = found.get(nid)
            if n is None or not self.visible(n["label"]):
                continue
            if kinds and n["kind"] not in kinds:
                continue
            results.append(n)
            if len(results) >= limit:
                break
        lines = [f"{len(results)} result(s) for {query!r} in project {self.project.name} (build {b})"]
        lines += [self._line(n) for n in results]
        return "\n".join(lines), {
            "build": b,
            "results": [self._node_json(n) for n in results],
            "summary": f"{len(results)} results",
        }

    def _evidence(self, store: Store, elem: str, b: int) -> list[dict]:
        out = []
        for w in store.where(elem, b)[:10]:
            unit = store.unit(w["unit"], b)
            if not unit:
                continue
            item = store.node(unit["item"], b)
            if item is None or not self.visible(item["label"]):
                continue
            self._saw(item["label"])
            out.append(
                {
                    "item": unit["item"],
                    "anchor": unit["anchor"],
                    "rev": unit["rev"],
                    "span": w["span"] or unit["span"],
                    "uri": item["props"].get("uri"),
                    "source": unit["source"],
                }
            )
        if not out:
            for d in store.derivations(elem, b)[:3]:
                for u in d["units"][:3]:
                    unit = store.unit(u, b)
                    if unit:
                        item = store.node(unit["item"], b)
                        if item and self.visible(item["label"]):
                            self._saw(item["label"])
                            out.append(
                                {
                                    "item": unit["item"],
                                    "anchor": unit["anchor"],
                                    "rev": unit["rev"],
                                    "span": unit["span"],
                                    "uri": item["props"].get("uri"),
                                    "source": unit["source"],
                                }
                            )
        return out

    def tool_kg_node(self, id: str) -> tuple[str, dict]:
        store, b = self._store()
        n = store.node(id, b)
        if n is None:
            alias = store.db.execute(
                "SELECT node_id FROM nodes WHERE aliases LIKE ? AND tx_to IS NULL LIMIT 1", (f'%"{id}"%',)
            ).fetchone()
            n = store.node(alias[0], b) if alias else None
        if n is None or not self.visible(n["label"]):
            raise ToolError("no visible node has that id; search with kg_search")
        edges = [e for e in store.edges_of(n["id"], b, limit=400) if self.visible(e["label"])]
        others = store.nodes({e["dst"] if e["src"] == n["id"] else e["src"] for e in edges}, b)
        lines = [self._line(n)]
        if n["aliases"]:
            lines.append(f"aliases: {', '.join(n['aliases'])}")
        props = {k: v for k, v in n["props"].items() if k not in ("span",)}
        if props:
            lines.append("props: " + json.dumps(props, ensure_ascii=False, sort_keys=True))
        evidence = self._evidence(store, n["id"], b)
        for ev in evidence:
            lines.append(
                f"evidence: {ev['item']}{'#' + ev['anchor'] if ev['anchor'] else ''} rev {ev['rev']}"
                f"{' ' + ev['span'] if ev['span'] else ''}{' ' + ev['uri'] if ev['uri'] else ''}"
            )
        out_edges, in_edges = [], []
        for e in edges:
            other_id = e["dst"] if e["src"] == n["id"] else e["src"]
            other = others.get(other_id)
            if other is None or not self.visible(other["label"]):
                continue
            self._saw(e["label"])
            self._saw(other["label"])
            entry = {
                "rel": e["rel"],
                "node": other_id,
                "kind": other["kind"],
                "name": other["name"],
                "status": e["status"],
                "conf": e["conf"],
            }
            (out_edges if e["src"] == n["id"] else in_edges).append(entry)
        for title, group in (("out", out_edges), ("in", in_edges)):
            if group:
                lines.append(f"{title} ({len(group)}):")
                for e in group[:60]:
                    arrow = "->" if title == "out" else "<-"
                    lines.append(f"  {arrow} {e['rel']} [{e['node']}] {e['kind']} {e['name']!r} ({e['status']})")
                if len(group) > 60:
                    lines.append(f"  ... {len(group) - 60} more")
        data = {
            "build": b,
            "node": self._node_json(n),
            "evidence": evidence,
            "out": out_edges,
            "in": in_edges,
            "summary": n["id"],
        }
        return "\n".join(lines), data

    def tool_kg_context(
        self, query: str | None = None, ids: list | None = None, budget_tokens: int = 3000, hops: int = 2
    ) -> tuple[str, dict]:
        store, b = self._store()
        budget_chars = max(500, min(int(budget_tokens), 8000)) * 3
        hops = max(1, min(int(hops), 3))
        seeds: list[str] = []
        if ids:
            seeds = [i for i in ids if isinstance(i, str)]
        elif query:
            seeds = store.search(query, b, limit=5)[:5]
        else:
            raise ToolError("give a query or ids")
        seed_nodes = store.nodes(seeds, b)
        seeds = [s for s in seeds if s in seed_nodes and self.visible(seed_nodes[s]["label"])]
        if not seeds:
            raise ToolError("nothing visible matches; try kg_search")

        included: dict[str, dict] = {s: seed_nodes[s] for s in seeds}
        used = sum(len(self._line(n)) for n in included.values())
        edges_out: dict[str, dict] = {}
        frontier = deque((s, 0) for s in seeds)
        while frontier and used < budget_chars:
            current, depth = frontier.popleft()
            if depth >= hops or (depth > 0 and store.degree(current, b) > HUB_DEGREE):
                continue  # do not expand hubs: they would fill the budget with neighbours of everything
            edges = [e for e in store.edges_of(current, b, limit=300) if self.visible(e["label"])]
            edges.sort(
                key=lambda e: (
                    -STATUS_RANK.get(e["status"], 0),
                    e["rel"] in ("in_source", "part_of"),
                    e["rel"],
                    e["src"],
                    e["dst"],
                )
            )
            neighbours = store.nodes({e["dst"] if e["src"] == current else e["src"] for e in edges}, b)
            for e in edges:
                other = e["dst"] if e["src"] == current else e["src"]
                node = neighbours.get(other)
                if node is None or not self.visible(node["label"]):
                    continue
                if other not in included:
                    cost = len(self._line(node)) + 60
                    if used + cost > budget_chars:
                        break
                    included[other] = node
                    used += cost
                    frontier.append((other, depth + 1))
                edges_out[e["id"]] = e
        lines = [
            f"context for {query or ', '.join(seeds)}: {len(included)} nodes, {len(edges_out)} edges"
            f" (build {b}, connected through the seeds)"
        ]
        lines += [self._line(n) for n in included.values()]
        lines.append("edges:")
        for e in edges_out.values():
            if e["src"] in included and e["dst"] in included:
                self._saw(e["label"])
                lines.append(f"  [{e['src']}] -{e['rel']}-> [{e['dst']}] ({e['status']})")
        data = {
            "build": b,
            "seeds": seeds,
            "nodes": [self._node_json(n) for n in included.values()],
            "edges": [
                {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]}
                for e in edges_out.values()
                if e["src"] in included and e["dst"] in included
            ],
            "summary": f"{len(included)} nodes",
        }
        return "\n".join(lines), data

    def tool_kg_status(self) -> tuple[str, dict]:
        from evo_agents.kg.status import project_status, render_status

        status = project_status(self.project)
        clr = (self.policy.level_name(self.clearance[0]), self.policy.location_name(self.clearance[1]))
        text = f"sink {self.sink}: clearance {clr[0]}/{clr[1]}\n" + render_status(status)
        status["sink"] = {"id": self.sink, "clearance": {"level": clr[0], "location": clr[1]}}
        session = read_session(current_session_id())
        if session is not None:
            text += "\n" + describe_session(session)
            status["session"] = {k: v for k, v in session.items() if k != "v"}
        status["summary"] = status["project"]
        return text, status

    def tool_kg_more(self, handle: str) -> tuple[str, dict]:
        now = time.monotonic()
        for h in [h for h, (t, _) in self.handles.items() if now - t > HANDLE_TTL]:
            del self.handles[h]
        if handle not in self.handles:
            raise ToolError(f"unknown or expired handle {handle!r}")
        _, chunks = self.handles[handle]
        text = chunks.popleft()
        if chunks:
            text += f"\n[more: call kg_more with handle {handle} again]"
        else:
            del self.handles[handle]
        return text, {"handle": handle, "remaining": len(chunks), "summary": "continued"}


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def make_session(project: str | None, sink: str) -> Session:
    try:
        bound = resolve_project(project)
    except ProjectError as exc:
        return Session(None, sink, error=f"{exc}. Start the server with --project or set EVO_KG_PROJECT.")
    return Session(bound, sink)


def handle(session: Session, message: dict) -> dict | None:
    method = message.get("method")
    mid = message.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1]
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "evo-kg", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = session.call(params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_stdio(project: str | None = None, sink: str = "claude-code@anthropic") -> int:
    session = make_session(project, sink)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(session, message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
