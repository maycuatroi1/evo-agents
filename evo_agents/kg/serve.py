"""``evo-agents kg serve``: an MCP server over stdio, bound to one project for its whole life.

Every tool reads the latest ready build and only the visible set V_o: elements whose label flows to the
session's sink. Results carry ids, labels, status and evidence; free text from sources (integrity U) is
never returned by these tools. Each result stays under about 9,000 tokens; the rest waits behind a
handle for ``kg_more``. The structured result of a call that reveals elements names the project and the
join of their labels, which the PostToolUse hook adds to the session label (sessions.py).
"""

from __future__ import annotations

import json
import re
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
IMPACT_RELS = ("calls", "imports", "member_of", "defines", "depends_on", "implements", "verifies")
IMPACT_MIN_RANK = STATUS_RANK["resolved"]  # impact follows declared, parsed, introspected, resolved: never proposed
CHAIN_RELS = ("implements", "schedules", "verifies", "has_step", "touches")
PATH_SKIP = ("in_source",)  # every item hangs off its source: a path through one says nothing
SEED_EDGES = 500  # edges read around a seed or a path's start, which are expanded even when they are hubs
_SPAN = re.compile(r"lines? (\d+)(?:-(\d+))?")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

INSTRUCTIONS = (
    "Knowledge graph of the bound project: repos, files, symbols, documents and sections, plans and steps, "
    "seams, and the explicit links between them, each with provenance and a sensitivity label. Use kg_search "
    "or kg_context before grepping for a named requirement, plan, document, seam or feature; use grep for exact "
    "strings and code you just edited. Before editing, renaming or changing the signature of a symbol or file, call "
    "kg_impact (ids, repo:path or a diff) and check every dependent it lists. kg_path traces a requirement to its "
    "steps, code and tests, or shows how two nodes connect. Status 'resolved' and 'proposed' are weaker than "
    "'parsed' and 'declared'. The graph can lag the sources: read the live source (uri) before quoting or editing it."
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
        "name": "kg_impact",
        "description": (
            "What a change reaches: give node ids, repo:path paths or a unified diff. Walks back over callers, "
            "importers, members, definers, dependants, implementations and tests (never proposed edges); "
            "documents that mention a reached node come back as a separate group to re-check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "repo:path, or a bare path"},
                "diff": {"type": "string", "description": "a unified diff, e.g. the output of git diff"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
            },
        },
    },
    {
        "name": "kg_path",
        "description": (
            "How two nodes connect: the shortest path from one to the other, preferring stronger status. Without "
            "'to', the chains from one node along implements, schedules, verifies, has_step and touches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"},
                "max_hops": {"type": "integer", "minimum": 1, "maximum": 6, "default": 6},
            },
            "required": ["from"],
        },
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
        size = max(CAP_CHARS - 100, 1)  # room for kg_more's own note, or each chunk would be cut again
        chunks = deque(rest[i : i + size] for i in range(0, len(rest), size))
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

    def _lookup(self, store: Store, node_id: str, b: int) -> dict | None:
        """A visible node by id or alias; None alike for a missing node and a hidden one."""
        n = store.node(node_id, b)
        if n is None:
            alias = store.db.execute(
                "SELECT node_id FROM nodes WHERE aliases LIKE ? AND tx_to IS NULL LIMIT 1", (f'%"{node_id}"%',)
            ).fetchone()
            n = store.node(alias[0], b) if alias else None
        return n if n is not None and self.visible(n["label"]) else None

    def tool_kg_node(self, id: str) -> tuple[str, dict]:
        store, b = self._store()
        n = self._lookup(store, id, b)
        if n is None:
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

    def _changed_symbols(self, store: Store, b: int, symbols: list[dict], lines: set[int]) -> list[dict]:
        """The visible symbols a set of changed lines falls in: for each line, the symbol that starts last
        at or before it (the innermost one), within its unit's line span when the unit has one."""
        extents = []
        for s in symbols:
            if not self.visible(s["label"]):
                continue
            start, end = s["props"].get("line"), None
            for w in store.where(s["id"], b):
                m = _SPAN.fullmatch(w["span"] or "")
                if m:
                    start = int(m[1])
                unit = store.unit(w["unit"], b)
                m = _SPAN.fullmatch((unit or {}).get("span") or "")
                if m and m[2]:
                    end = int(m[2])
            if isinstance(start, int):
                extents.append((start, end, s))
        hit: dict[str, dict] = {}
        for line in sorted(lines):
            best = None
            for start, end, s in extents:
                if start <= line and (end is None or line <= end) and (best is None or start > best[0]):
                    best = (start, s)
            if best:
                hit[best[1]["id"]] = best[1]
        return list(hit.values())

    def tool_kg_impact(
        self,
        id: str | None = None,
        ids: list | None = None,
        paths: list | None = None,
        diff: str | None = None,
        depth: int = 2,
    ) -> tuple[str, dict]:
        store, b = self._store()
        depth = max(1, min(int(depth), 3))
        wanted = ([id] if isinstance(id, str) and id else []) + [i for i in ids or [] if isinstance(i, str)]
        paths = [p for p in ([paths] if isinstance(paths, str) else paths or []) if isinstance(p, str) and p]
        changes = diff_lines(diff) if isinstance(diff, str) else {}
        if not (wanted or paths or changes):
            raise ToolError("give an id, ids, paths (repo:path) or a unified diff")

        seeds: dict[str, dict] = {}
        missing = 0
        for nid in wanted:
            n = self._lookup(store, nid, b)
            if n is None:
                missing += 1
            else:
                seeds[n["id"]] = n
        ambiguous: list[dict] = []
        unmatched: list[str] = []
        if paths or changes:
            # A diff does not say which repo it comes from: a path names a file only when exactly one
            # visible file matches it; otherwise the candidates go back to the caller.
            files = [f for f in store.path_nodes(b) if self.visible(f["label"])]
            changed: dict[str, set[int]] = {}
            for path, lines in [(p, None) for p in paths] + sorted(changes.items()):
                repo, sep, rest = path.partition(":")
                if lines is None and sep and rest:
                    found = [f for f in files if f["props"].get("repo") == repo and f["props"]["path"] == rest]
                else:
                    found = _path_candidates(path, files)
                if len(found) == 1:
                    seeds[found[0]["id"]] = found[0]
                    if lines:
                        changed.setdefault(found[0]["id"], set()).update(lines)
                elif found:
                    ambiguous.append({"path": path, "candidates": [f["id"] for f in found]})
                else:
                    unmatched.append(path)
            symbols = store.file_symbols(changed, b)
            for fid, lines in changed.items():
                for s in self._changed_symbols(store, b, symbols.get(fid, []), lines):
                    seeds[s["id"]] = s
        if not seeds and not (ambiguous or unmatched):
            raise ToolError("nothing visible matches; try kg_search")

        # Walk back from the seeds: whoever has an edge into a reached node depends on it.
        reached: dict[str, dict] = {}
        mentions: dict[str, list[dict]] = {}
        frontier = sorted(seeds)
        for hop in range(depth + 1):
            step: dict[str, tuple] = {}
            for nid in frontier:
                edges = store.edges_of(nid, b, limit=SEED_EDGES if hop == 0 else HUB_DEGREE + 1)
                if hop > 0 and len(edges) > HUB_DEGREE:
                    reached[nid]["hub"] = True  # do not expand hubs: half the graph depends on them
                    continue
                for e in edges:
                    if e["dst"] != nid or e["src"] == nid or not self.visible(e["label"]):
                        continue
                    if STATUS_RANK.get(e["status"], 0) < IMPACT_MIN_RANK:
                        continue
                    if e["rel"] == "mentions":
                        mentions.setdefault(e["src"], []).append(e)
                    elif e["rel"] in IMPACT_RELS and hop < depth and e["src"] not in seeds and e["src"] not in reached:
                        key = (-STATUS_RANK[e["status"]], e["rel"], e["dst"])
                        if e["src"] not in step or key < step[e["src"]][0]:
                            step[e["src"]] = (key, e)
            found = store.nodes(step, b)
            frontier = []
            for other in sorted(step):
                node = found.get(other)
                if node is not None and self.visible(node["label"]):
                    reached[other] = {"hop": hop + 1, "node": node, "edge": step[other][1], "hub": False}
                    frontier.append(other)
        docs = {
            d: n
            for d, n in store.nodes(mentions, b).items()
            if self.visible(n["label"]) and d not in seeds and d not in reached
        }

        lines = [
            f"impact of {len(seeds)} seed(s) within {depth} hop(s) (build {b}): {len(reached)} dependent(s),"
            f" {len(docs)} document(s) to re-check"
        ]
        if seeds:
            lines.append("seeds:")
            lines += [f"  {self._line(seeds[s])}" for s in sorted(seeds)]
        for hop in range(1, depth + 1):
            group = [r for r in reached.values() if r["hop"] == hop]
            if group:
                lines.append(f"hop {hop}:")
            for r in group:
                e = r["edge"]
                lines.append(
                    f"  {self._line(r['node'])} -{e['rel']}-> [{e['dst']}] ({e['status']})"
                    + (" (hub, not expanded)" if r["hub"] else "")
                )
        if docs:
            lines.append("documents to re-check:")
        for d in sorted(docs):
            targets = sorted(mentions[d], key=lambda e: e["dst"])
            lines.append(
                f"  {self._line(docs[d])} -mentions-> " + ", ".join(f"[{e['dst']}] ({e['status']})" for e in targets)
            )
        for a in ambiguous:
            candidates = ", ".join(f"[{c}]" for c in a["candidates"])
            lines.append(f"ambiguous path {a['path']}: {candidates}; pass repo:path or an id")
        if unmatched:
            lines.append("no visible file for: " + ", ".join(unmatched))
        if missing:
            lines.append(f"{missing} id(s) not found or not visible")
        data = {
            "build": b,
            "depth": depth,
            "seeds": [self._node_json(seeds[s]) for s in sorted(seeds)],
            "impacted": [
                {
                    "hop": r["hop"],
                    "node": self._node_json(r["node"]),
                    "rel": r["edge"]["rel"],
                    "status": r["edge"]["status"],
                    "to": r["edge"]["dst"],
                    "hub": r["hub"],
                }
                for r in reached.values()
            ],
            "documents": [
                {
                    "node": self._node_json(docs[d]),
                    "mentions": [
                        {"node": e["dst"], "status": e["status"]} for e in sorted(mentions[d], key=lambda e: e["dst"])
                    ],
                }
                for d in sorted(docs)
            ],
            "ambiguous": ambiguous,
            "unmatched": unmatched,
            "not_found": missing,
            "summary": f"{len(reached)} dependents, {len(docs)} documents",
        }
        return "\n".join(lines), data

    def _walk(
        self, store: Store, b: int, start: dict, max_hops: int, rels: tuple | None, goal: str | None
    ) -> dict[str, dict]:
        """Breadth-first over visible nodes and edges in both directions, as a tree of parents. A node keeps,
        among the parents one hop closer to the start, the one whose path has the highest total STATUS_RANK
        (ties: smaller parent id, then rel, then edge id), so the answer is deterministic. Hubs other than
        the start are reached but not expanded; a hidden node is never entered, so it breaks every path
        through it."""
        tree = {start["id"]: {"hop": 0, "score": 0, "parent": None, "edge": None, "node": start, "hub": False}}
        layer = [start["id"]]
        for hop in range(1, max_hops + 1):
            if goal in tree or not layer:
                break
            best: dict[str, tuple] = {}
            for nid in layer:
                edges = store.edges_of(nid, b, limit=SEED_EDGES if hop == 1 else HUB_DEGREE + 1)
                if hop > 1 and len(edges) > HUB_DEGREE:
                    tree[nid]["hub"] = True
                    continue
                for e in edges:
                    if e["rel"] in PATH_SKIP or (rels and e["rel"] not in rels) or not self.visible(e["label"]):
                        continue
                    other = e["dst"] if e["src"] == nid else e["src"]
                    if other in tree:
                        continue
                    key = (-(tree[nid]["score"] + STATUS_RANK.get(e["status"], 0)), nid, e["rel"], e["id"])
                    if other not in best or key < best[other][0]:
                        best[other] = (key, nid, e)
            found = store.nodes(best, b)
            layer = []
            for other in sorted(best):
                node = found.get(other)
                if node is None or not self.visible(node["label"]):
                    continue
                key, parent, e = best[other]
                tree[other] = {"hop": hop, "score": -key[0], "parent": parent, "edge": e, "node": node, "hub": False}
                layer.append(other)
        return tree

    @staticmethod
    def _step(parent: str, e: dict) -> str:
        arrow = f"-{e['rel']}->" if e["src"] == parent else f"<-{e['rel']}-"
        return f"{arrow} ({e['status']})"

    def tool_kg_path(self, to: str | None = None, max_hops: int = 6, **kwargs) -> tuple[str, dict]:
        start_id = kwargs.pop("from", None)  # a Python keyword, so it cannot be a parameter name
        if kwargs:
            raise TypeError(f"unexpected argument(s) {', '.join(sorted(kwargs))}")
        if not isinstance(start_id, str) or not start_id:
            raise ToolError("give from (a node id), and to for a path between two nodes")
        store, b = self._store()
        max_hops = max(1, min(int(max_hops), 6))
        start = self._lookup(store, start_id, b)
        goal = self._lookup(store, to, b) if isinstance(to, str) and to else None
        if start is None or (to and goal is None):
            raise ToolError("no visible node has that id; search with kg_search")
        tree = self._walk(store, b, start, max_hops, None if goal else CHAIN_RELS, goal["id"] if goal else None)

        def edge_json(t: dict) -> dict:
            e = t["edge"]
            return {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]}

        if goal is not None:
            chain = []
            nid = goal["id"] if goal["id"] in tree else None
            while nid is not None:
                chain.append(tree[nid])
                nid = tree[nid]["parent"]
            chain.reverse()
            if not chain:
                lines = [
                    f"no path within {max_hops} hop(s) from {start['id']} to {goal['id']} through visible nodes"
                    f" (build {b}; hubs are not expanded)"
                ]
            else:
                lines = [f"path from {start['id']} to {goal['id']} (build {b}): {len(chain) - 1} hop(s)"]
                lines.append(self._line(start))
                lines += [f"  {self._step(t['parent'], t['edge'])} {self._line(t['node'])}" for t in chain[1:]]
            data = {
                "build": b,
                "from": start["id"],
                "to": goal["id"],
                "found": bool(chain),
                "hops": len(chain) - 1 if chain else None,
                "nodes": [self._node_json(t["node"]) for t in chain],
                "edges": [edge_json(t) for t in chain[1:]],
                "summary": f"{len(chain) - 1} hops" if chain else "no path",
            }
            return "\n".join(lines), data

        children: dict[str, list[str]] = {}
        for nid, t in tree.items():
            if t["parent"] is not None:
                children.setdefault(t["parent"], []).append(nid)
        lines = [
            f"chains from {start['id']} along {', '.join(CHAIN_RELS)} (build {b}): {len(tree) - 1} node(s)"
            f" within {max_hops} hop(s)",
            self._line(start),
        ]
        stack = sorted(children.get(start["id"], []), reverse=True)
        while stack:
            nid = stack.pop()
            t = tree[nid]
            lines.append(
                "  " * t["hop"]
                + f"{self._step(t['parent'], t['edge'])} {self._line(t['node'])}"
                + (" (hub, not expanded)" if t["hub"] else "")
            )
            stack.extend(sorted(children.get(nid, []), reverse=True))
        data = {
            "build": b,
            "from": start["id"],
            "to": None,
            "nodes": [{**self._node_json(t["node"]), "hop": t["hop"], "hub": t["hub"]} for t in tree.values()],
            "edges": [edge_json(t) for t in tree.values() if t["parent"] is not None],
            "summary": f"{len(tree) - 1} nodes",
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


def _diff_path(header: str, prefix: str) -> str | None:
    path = header.split("\t", 1)[0].strip()
    if len(path) > 1 and path[0] == path[-1] == '"':
        path = path[1:-1]
    if path == "/dev/null":
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def diff_lines(diff: str) -> dict[str, set[int]]:
    """The files a unified diff touches and their changed lines. A removed line counts in the old numbering
    and an added one in the new, each with its neighbour in the other numbering, so the lines name the
    right symbols whether the graph was built before the change or after it."""
    files: dict[str, set[int]] = {}
    old_path = None
    current: set[int] | None = None
    old = new = old_left = new_left = 0
    for raw in diff.splitlines():
        if current is not None and (old_left > 0 or new_left > 0):
            tag = raw[:1]
            if tag == "-":
                current.update((old, max(new - 1, 1)))
                old, old_left = old + 1, old_left - 1
            elif tag == "+":
                current.update((new, max(old - 1, 1)))
                new, new_left = new + 1, new_left - 1
            elif tag != "\\":  # a context line, maybe with its leading space stripped
                old, new, old_left, new_left = old + 1, new + 1, old_left - 1, new_left - 1
            continue
        if raw.startswith("diff --git "):
            # Seen even when no hunk follows (a binary or mode-only change).
            rest = raw[len("diff --git ") :]
            current, old_path = None, None
            if " b/" in rest:
                files.setdefault(rest[rest.rfind(" b/") + 3 :], set())
        elif raw.startswith("--- "):
            old_path = _diff_path(raw[4:], "a/")
        elif raw.startswith("+++ "):
            path = _diff_path(raw[4:], "b/") or old_path
            current = files.setdefault(path, set()) if path else None
        elif current is not None and (m := _HUNK.match(raw)):
            old, new = int(m[1]), int(m[3])
            old_left = int(m[2]) if m[2] is not None else 1
            new_left = int(m[4]) if m[4] is not None else 1
    return files


def _path_candidates(path: str, files: list[dict]) -> list[dict]:
    """The files a bare path can name: those with exactly that path, else those sharing its longest
    suffix at a directory boundary (a diff may be taken from a parent or a child directory)."""
    exact = [f for f in files if f["props"]["path"] == path]
    if exact:
        return exact
    best, out = 0, []
    for f in files:
        p = f["props"]["path"]
        if p.endswith("/" + path) or path.endswith("/" + p):
            size = min(len(p), len(path))
            if size > best:
                best, out = size, [f]
            elif size == best:
                out.append(f)
    return out


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
