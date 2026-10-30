"""``evo-agents kg serve``: an MCP server over stdio, bound to one project for its whole life.

Every tool reads the latest ready build and only the visible set V_o: elements whose label flows to the
session's sink and whose audience is within the session's (``Session.audience``: none for kg serve, so an element with
an audience never shows here). The store is opened read-only and reads as that audience (``Store.reader``), so hidden
elements fill no limit, count or page. When the highest label of the build (``Store.top_label``) flows to the sink and
its audiences (``Store.top_audience``) are within the session's, V_o is the whole build and no label is compared.
Results carry ids, labels, status and evidence; free
text from sources (integrity U) is never returned by these tools. Each result stays under about 9,000 tokens; the
rest waits behind a handle for ``kg_more``, for a reader whose audience still holds what the result revealed. The
structured result of a call that reveals elements names the project and the join of their labels, and so does the
``_meta`` of its text block (READ_META), which the PostToolUse hook adds to the session label (sessions.py).

The text is written to be read: a node is named by its readable id (``refs.readable``: ``repo:path``,
``repo:path::Symbol``, ``repo:path#section``) and its name only when the id does not already end with it;
its URI is left to kg_node, which links to the file at the commit its source read (``freshness.Links``); a
label shared by every node of a result is printed once, under the first line. kg_context keeps its whole
text, nodes and edges, within ``budget_tokens`` (UTF-8 bytes / 3), and kg_impact lists the first ``limit``
dependents of each hop and counts the rest. Each tool takes readable and stored ids alike, and the argument
names agents guess (``refs.ALIASES``).

An MCP client gets a result's text alone (``for_model``), which is what its model reads: Claude Code 2.1.296
shows the model a result's structuredContent instead of its text when it has one, and the JSON of the data
runs two to three times longer than the text, past the bounds above. A client that wants the data, such as
``kg query --json`` through the hub, asks for it with DATA_META in the call's ``_meta``.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections import Counter, deque

from evo_agents import __version__
from evo_agents.kg import semantic
from evo_agents.kg.budget import (
    CONTEXT_LABEL_BYTES,
    CONTEXT_MIN_LINE,
    CONTEXT_NOTE_BYTES,
    CONTEXT_QUERY_SHOWN,
    Room,
    line_size,
)
from evo_agents.kg.explore import MARK, POOL, Explorer
from evo_agents.kg.freshness import Links
from evo_agents.kg.impact import Impact, coverage_line, diff_lines, path_candidates, seed_coverage
from evo_agents.kg.policy import Label, audience_below
from evo_agents.kg.project import Project, ProjectError, resolve_project
from evo_agents.kg.refs import UnknownArgument, arguments, named_by, path_like, readable, stored
from evo_agents.kg.search import SNIPPET_KINDS, query_words, with_mentions
from evo_agents.kg.sessions import READ_META, current_session_id, labels_in, read_session
from evo_agents.kg.sessions import describe as describe_session
from evo_agents.kg.store import StaleSchema, Store
from evo_agents.kg.text import slugify
from evo_agents.kg.tools import IMPACT_LIMIT, IMPACT_MAX_LIMIT, INSTRUCTIONS, SIGNATURES, TOOLS

SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
CAP_CHARS = 25_000  # about 9,000 tokens even for Vietnamese text, under Claude Code's 10,000 warning
DATA_META = "evo-agents/data"  # true in a tools/call's params._meta: the result keeps its structuredContent
HANDLE_TTL = 1800
WEAK_LIMIT = 5  # kg_search returns at most this many results when none holds every word of the query
FULL_LINES = 5  # the results kg_search shows whole; those past them show their id alone (kg_node reads one)
KIND_PROPS = {"ChatMessage": ("chat", "thread")}  # the props a line of that kind names too; never a text
STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "proposed": 1}
HUB_DEGREE = 150
IMPACT_RELS = (
    "calls",
    "imports",
    "member_of",
    "defines",
    "depends_on",
    "implements",
    "verifies",
    "inherits",
    "overrides",
)
IMPACT_MIN_RANK = STATUS_RANK["resolved"]  # impact follows declared, parsed, introspected, resolved: never proposed
IMPACT_STATUSES = tuple(status for status, rank in STATUS_RANK.items() if rank >= IMPACT_MIN_RANK)
WALKED_RELS = (*IMPACT_RELS, "mentions")  # what kg_impact reads into a node: mentions name documents to re-check
CHAIN_RELS = ("implements", "schedules", "verifies", "has_step", "touches")
PATH_SKIP = ("in_source",)  # every item hangs off its source: a path through one says nothing
SEED_EDGES = 500  # edges read around a seed or a path's start, which are expanded even when they are hubs
IMPACT_SEED_EDGES = 2_000  # edges kg_impact reads into a seed, SEED_EDGES at a time; it reports a seed with more
IMPACT_MENTIONS_SHOWN = 3  # reached nodes a document's line names; the data has them all
_SPAN = re.compile(r"lines? (\d+)(?:-(\d+))?")


class ToolError(Exception):
    pass


class Session:
    def __init__(self, project: Project | None, sink: str, *, error: str | None = None):
        self.project = project
        self.sink = sink
        self.error = error
        self.handles: dict[str, tuple] = {}  # handle: (made, chunks left, audience its result revealed)
        self.policy = project.policy if project else None
        self.clearance = None
        self.sees_all = False  # this call's build holds no label above the clearance (``_store``)
        self.read: Label | None = None  # join of the labels the current call reveals
        self.audience: frozenset[str] = frozenset()  # principals the reader is (evo_agents.kg.policy)
        self.deprecated = False  # this call shows the deprecated nodes and edges too (``seen``)
        # kg_search ranks by meaning too (evo_agents.kg.semantic): None as the graph's mode says, True or False always
        self.semantic: bool | None = None
        self.plain = False  # kg_search ranks by plain BM25 alone, the public benches' baseline (Store.plain_search)
        self.plain_files: tuple | None = None  # (clone, commit): with ``plain``, rank whole files (bench.filebm25)
        self.explorer: Explorer | None = None  # kg_search explores (``evo_agents.kg.explore``): agents on the hub only
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
        """Whether an element labelled ``label`` (level, location, integrity and, from the store, audience) shows."""
        if self.sees_all:
            return True
        level, location, *_rest = label
        audience = label[3] if len(label) > 3 else frozenset()
        return level <= self.clearance[0] and location <= self.clearance[1] and audience_below(audience, self.audience)

    def seen(self, x: dict) -> bool:
        """``visible``, and not deprecated by a decision (``evo_agents.kg.pipeline.curation``) unless asked for."""
        return self.visible(x["label"]) and (self.deprecated or "deprecated" not in (x.get("props") or {}))

    def describe(self, label: tuple) -> str:
        lvl = self.policy.level_name(label[0])
        loc = self.policy.location_name(label[1])
        audience = " audience " + " ".join(sorted(label[3])) if len(label) > 3 and label[3] else ""
        return f"{lvl}{'' if loc == 'any' else '/' + loc},{label[2]}{audience}"

    def _saw(self, label: tuple) -> None:
        seen = Label.of(label, {self.project.name})
        self.read = seen if self.read is None else self.read.join(seen)

    def _store(self) -> tuple[Store, int]:
        """The graph this call reads, as the session's audience reads it, and its latest ready build. When the highest
        label of that build is within the clearance and its audiences within the session's, every element is
        visible, and ``visible`` answers so without comparing labels."""
        store, b = self._graph()
        store.reader = self.audience
        store.asked_clearance = self.clearance  # what agents asked before reading a node, under this clearance only
        top = store.top_label(b)
        within = top is not None and top[0] <= self.clearance[0] and top[1] <= self.clearance[1]
        self.sees_all = within and audience_below(store.top_audience(b), self.audience)
        return store, b

    def _graph(self) -> tuple[Store, int]:
        """The project's store, opened read-only: serving never writes, so it never waits on a build."""
        try:
            store = Store.open_readonly(self.project)
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
        self.sees_all = False
        try:
            handler = getattr(self, f"tool_{name}", None)
            if handler is None:
                raise ToolError(f"unknown tool {name!r}")
            args = args or {}
            if name in SIGNATURES:
                args = arguments(name, args, *SIGNATURES[name])
            self.deprecated = bool(args.pop("deprecated", False))
            text, data = handler(**args)
        except (ToolError, UnknownArgument) as exc:
            return _error(str(exc))
        except (TypeError, ValueError) as exc:
            return _error(f"bad arguments for {name}: {exc}")
        return self._envelope(text, data)

    def _envelope(self, text: str, data: dict) -> dict:
        read = {}
        if self.read is not None:
            label = {**self.policy.describe(self.read), "projects": sorted(self.read.projects)}
            data = {**data, "project": self.project.name, "label": label}
            elements = labels_in(data)[1]
            read = {"_meta": {READ_META: {"project": self.project.name, "label": label, "results_read": elements}}}
        if len(text) <= CAP_CHARS:
            return {"content": [{"type": "text", "text": text, **read}], "structuredContent": data}
        head, rest = text[:CAP_CHARS], text[CAP_CHARS:]
        cut = head.rfind("\n")
        if cut > CAP_CHARS // 2:
            head, rest = head[:cut], head[cut + 1 :] + rest
        handle = uuid.uuid4().hex[:12]
        size = max(CAP_CHARS - 100, 1)  # room for kg_more's own note, or each chunk would be cut again
        chunks = deque(rest[i : i + size] for i in range(0, len(rest), size))
        self.handles[handle] = (time.monotonic(), chunks, self.read.audience if self.read else frozenset())
        note = f"\n[truncated: {len(rest)} more characters; call kg_more with handle {handle}]"
        return {
            "content": [{"type": "text", "text": head + note, **read}],
            "structuredContent": {
                "truncated": True,
                "handle": handle,
                "summary": data.get("summary"),
                **{k: data[k] for k in ("project", "label") if k in data},
            },
        }

    # -- tools ----------------------------------------------------------------------------------

    def _line(self, n: dict, shared: str | None = None, passage: str | None = None, status: str | None = None) -> str:
        """One node as the text shows it: its readable id, its kind, its name unless the id ends with it, its status,
        its label unless it is ``shared`` (the one the result prints once), and the path when the id does not hold
        it, the status and number of a plan step, the chat and thread of a chat message (KIND_PROPS), and where in
        its file it is (``lines``): the lines of a section, the first and last line of a symbol, or ``passage``, the
        lines a search matched best (``Store.matched``), with the node's own after ``of``. Never its URI: kg_node
        gives that. With ``status``, the status the result prints once too (kg_search), the line is briefer: its
        node's status only when it is another, no name of a section its anchor spells, no number of a step its id
        holds, and no parentheses when they would hold nothing."""
        ref, props = readable(n["id"]), n["props"]
        brief = status is not None
        named = named_by(ref, n["name"]) or (
            brief and n["kind"] == "Section" and bool(n["name"]) and slugify(n["name"]) == props.get("anchor")
        )
        head = f"[{ref}] {n['kind']}" + ("" if named else f" {n['name']!r}")
        extra = [
            f"{key}={props[key]}"
            for key in ("path", "status", "number", *KIND_PROPS.get(n["kind"], ()))
            if props.get(key) not in (None, "")
            and not (key == "path" and str(props[key]) in ref)
            and not (brief and key == "number" and ref.endswith(f"step:{props[key]}"))
        ]
        where = lines(props)
        if passage:
            where = f"{passage} of {where.split(' ', 1)[1]}" if where.startswith("lines ") else passage
        if where:
            extra.append(where)
        label = self.describe(n["label"])
        if brief:
            told = [part for part, same in ((n["status"], n["status"] == status), (label, label == shared)) if not same]
            state = f" ({', '.join(told)})" if told else ""
        else:
            state = f" ({n['status']})" if label == shared else f" ({n['status']}, {label})"
        return f"{head}{state}" + (f" {' '.join(extra)}" if extra else "")

    def _labels(self, nodes) -> tuple[str | None, list[str]]:
        """The label a result prints once, under its first line: the label of most of ``nodes`` (the first seen of
        those as common). A line names its node's label only when it is another. None and no line without nodes."""
        counts = Counter(self.describe(n["label"]) for n in nodes)
        if not counts:
            return None, []
        shared = counts.most_common(1)[0][0]
        unless = "" if len(counts) == 1 else " unless its line names another"
        return shared, [f"label of every node{unless}: {shared}"]

    def _node_json(self, n: dict) -> dict:
        self._saw(n["label"])  # every node in a structured result is revealed
        label = self.policy.describe(Label.of(n["label"]))  # level, location, integrity; audience when it has one
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
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if self.plain:
            return self._plain_search(store, b, query, kinds, limit)
        vector = semantic.query_vector(store, query, self.semantic)
        ids = store.search(query, b, limit=limit * 4, kinds=kinds, deprecated=self.deprecated, vector=vector)
        named = self._lookup(store, query, b) if ":" in query else None  # an id the text showed comes first
        if named is not None:
            ids = [named["id"], *(i for i in ids if i != named["id"])]
        found = store.nodes(ids, b)
        hits = [found[i] for i in ids if i in found and self.seen(found[i])]
        results, via = with_mentions(
            store, b, [n for n in hits if not kinds or n["kind"] in kinds], limit, self.seen, kinds
        )
        words = len(query_words(query))
        direct = [n for n in results if n["id"] not in via]  # a node a hit mentions was not found by its words
        matched = store.matched(
            [n["id"] for n in direct], query, {n["id"] for n in direct if n["kind"] in SNIPPET_KINDS}
        )
        partial = words > 1 and bool(matched) and all(m["words"] < words for m in matched.values())
        if partial and len(results) > WEAK_LIMIT:  # no result holds the whole query: the few best say as much
            results = results[:WEAK_LIMIT]
            via = {nid: reached for nid, reached in via.items() if any(n["id"] == nid for n in results)}
        explored = self._explore(store, results, hits, via)
        for reached in via.values():
            self._saw(reached["label"])
        shared, label_line = self._labels(results)
        statuses = Counter(n["status"] for n in results)
        status = statuses.most_common(1)[0][0] if statuses else ""
        head = f"{len(results)} result(s) in project {self.project.name} (build {b})"
        if statuses:
            head += f", status {status}" + ("" if len(statuses) == 1 else " unless its line names another")
        lines = [head, *label_line]
        if partial:
            lines.append(f"no result holds all {words} words of the query")
        for rank, n in enumerate(results, 1):
            mark = "  mentions " if n["id"] in via else ""
            if rank > FULL_LINES:
                lines.append(f"{mark}[{readable(n['id'])}]")
                continue
            m = matched.get(n["id"])
            line = mark + self._line(n, shared, (m or {}).get("lines"), status)
            lines.append(line + (f" words={m['words']}/{words}" if m and m["words"] < words else ""))
            if m and m.get("snippet"):
                lines.append(f"    > {m['snippet']}")
        if explored is not None:
            lines.append(MARK.format(rank=explored[0], p=explored[1], ref=readable(results[explored[0] - 1]["id"])))
        return "\n".join(lines), {
            "build": b,
            "results": [self._node_json(n) for n in results],
            "mentioned_by": {nid: reached["from"] for nid, reached in via.items()},
            "matched": matched,
            "summary": f"{len(results)} results",
        }

    def _plain_search(self, store: Store, b: int, query: str, kinds: list | None, limit: int) -> tuple[str, dict]:
        """kg_search by plain BM25: the nodes in its order, no mention, cut or exploring; the passages matched."""
        if self.plain_files:
            from evo_agents.kg.bench import filebm25

            clone, commit = self.plain_files
            files = {
                i.split(":file:", 1)[1]: i
                for (i,) in store.db.execute("SELECT node_id FROM nodes WHERE kind = 'File' AND tx_to IS NULL")
                if ":file:" in i
            }
            ids = [files[p] for p in filebm25.rank_files(clone, commit, set(files), query, limit * 4)]
        else:
            ids = store.plain_search(query, limit * 4, kinds)
        found = store.nodes(ids, b)
        results = [found[i] for i in ids if i in found and self.seen(found[i])][:limit]
        matched = store.matched(
            [n["id"] for n in results], query, {n["id"] for n in results if n["kind"] in SNIPPET_KINDS}
        )
        shared, label_line = self._labels(results)
        lines = [f"{len(results)} result(s) in project {self.project.name} (build {b}), plain BM25", *label_line]
        lines += [self._line(n, shared, (matched.get(n["id"]) or {}).get("lines"), n["status"]) for n in results]
        return "\n".join(lines), {
            "build": b,
            "results": [self._node_json(n) for n in results],
            "mentioned_by": {},
            "matched": matched,
            "summary": f"{len(results)} results",
        }

    def _explore(self, store: Store, results: list, hits: list, via: dict) -> tuple[int, float] | None:
        """With an explorer, maybe put a hit past ``results`` that no agent read after a search yet at rank 4 or 5 of
        them, in place, the last result dropped when that makes one more: (its rank, the chance it had), else None."""
        if self.explorer is None:
            return None
        shown = {n["id"] for n in results}
        past = [n for n in hits if n["id"] not in shown and n["id"] not in via][: 2 * POOL]
        read = store.asked_nodes(n["id"] for n in past)
        candidates = [n for n in past if n["id"] not in read]
        picked = self.explorer.pick(results, candidates)
        if picked is None:
            return None
        index, rank, chance = picked
        count = len(results)
        results.insert(rank - 1, candidates[index])
        del results[max(count, rank) :]
        kept = {n["id"] for n in results}
        for nid in [nid for nid in via if nid not in kept]:
            del via[nid]
        return rank, chance

    def _evidence(self, store: Store, elem: str, b: int, links: Links) -> list[dict]:
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
                    "uri": links.at_commit(item["props"].get("uri"), unit["source"]),
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
                                    "uri": links.at_commit(item["props"].get("uri"), unit["source"]),
                                    "source": unit["source"],
                                }
                            )
        return out

    def _lookup(self, store: Store, node_id: str, b: int) -> dict | None:
        """A visible node by its stored id, its readable id (``refs.stored``) or an alias; None alike for a missing
        node and a hidden one."""
        if not isinstance(node_id, str) or not node_id:
            return None
        n = None
        for candidate in stored(node_id):
            n = store.node(candidate, b)
            if n is not None:
                break
        if n is None:
            alias = store.alias_of(node_id, b)
            n = store.node(alias, b) if alias else None
        return n if n is not None and self.visible(n["label"]) else None

    def tool_kg_node(self, id: str) -> tuple[str, dict]:
        store, b = self._store()
        n = self._lookup(store, id, b)
        if n is None:
            raise ToolError("no visible node has that id; search with kg_search")
        links = Links.of(store, b)
        n = links.node(n)
        edges = [e for e in store.edges_of(n["id"], b, limit=400) if self.seen(e)]
        others = store.nodes({e["dst"] if e["src"] == n["id"] else e["src"] for e in edges}, b)
        lines = [self._line(n)]
        if n["aliases"]:
            lines.append(f"aliases: {', '.join(n['aliases'])}")
        if n["props"].get("deprecated"):
            lines.append("deprecated: {reason} (decision {decision})".format(**n["props"]["deprecated"]))
        props = {k: v for k, v in n["props"].items() if k not in ("span", "deprecated")}
        if props:
            lines.append("props: " + json.dumps(props, ensure_ascii=False, sort_keys=True))
        evidence = self._evidence(store, n["id"], b, links)
        for ev in evidence:
            lines.append(
                f"evidence: {readable(ev['item'])}{'#' + ev['anchor'] if ev['anchor'] else ''} rev {ev['rev']}"
                f"{' ' + ev['span'] if ev['span'] else ''}{' ' + ev['uri'] if ev['uri'] else ''}"
            )
        out_edges, in_edges = [], []
        for e in edges:
            other_id = e["dst"] if e["src"] == n["id"] else e["src"]
            other = others.get(other_id)
            if other is None or not self.seen(other):
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
                    arrow, ref = "->" if title == "out" else "<-", readable(e["node"])
                    name = "" if named_by(ref, e["name"]) else f" {e['name']!r}"
                    lines.append(f"  {arrow} {e['rel']} [{ref}] {e['kind']}{name} ({e['status']})")
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
        size = max(500, min(int(budget_tokens), 8000)) * 3
        hops = max(1, min(int(hops), 3))
        seeds = self._context_seeds(store, b, query, ids)
        if query:
            what = str(query)[:CONTEXT_QUERY_SHOWN]
        else:
            what = ", ".join(readable(s) for s in list(seeds)[:3]) + (f" and {len(seeds) - 3} more" * (len(seeds) > 3))

        def header(nodes: int, edges: int) -> str:
            return f"context for {what}: {nodes} nodes, {edges} edges (build {b}, connected through the seeds)"

        # The header with the widest counts, the label line, "edges:" and the note on what was left out come out of
        # the budget first; a node line is costed with its label, which the text drops when the label line has it.
        reserved = line_size(header(99_999, 99_999)) + CONTEXT_LABEL_BYTES + line_size("edges:") + CONTEXT_NOTE_BYTES
        room = Room(size - reserved)
        for sid in seeds:
            if not room.take_node(sid, seeds[sid], line_size(self._line(seeds[sid]))):
                room.left_nodes.add(sid)
        if not room.nodes:
            raise ToolError("budget_tokens holds none of the seeds: raise it, or pass fewer ids")
        frontier = deque((s, 0) for s in room.nodes)
        while frontier:
            if room.size - room.used < CONTEXT_MIN_LINE:
                room.beyond = any(depth < hops for _, depth in frontier)  # full: what lies past them is not read
                break
            current, depth = frontier.popleft()
            if depth >= hops or (depth > 0 and store.degree(current, b) > HUB_DEGREE):
                continue  # do not expand hubs: they would fill the budget with neighbours of everything
            self._context_from(store, b, current, depth, room, frontier)
        shared, label_line = self._labels(room.nodes.values())
        lines = [header(len(room.nodes), len(room.edges)), *label_line]
        lines += [self._line(n, shared) for n in room.nodes.values()]
        lines.append("edges:")
        for e in room.edges.values():
            self._saw(e["label"])
            lines.append(self._edge_line(e))
        lines += room.note()
        data = {
            "build": b,
            "seeds": list(seeds),
            "nodes": [self._node_json(n) for n in room.nodes.values()],
            "edges": [
                {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]} for e in room.edges.values()
            ],
            "left_out": {"nodes": room.left()[0], "edges": room.left()[1], "beyond": room.beyond},
            "summary": f"{len(room.nodes)} nodes",
        }
        return "\n".join(lines), data

    def _context_seeds(self, store: Store, b: int, query, ids) -> dict[str, dict]:
        """The visible seeds of kg_context: the nodes ``ids`` name (stored or readable ids), else the first five
        that match ``query``."""
        if ids:
            found = [self._lookup(store, i, b) for i in ids if isinstance(i, str)]
            seeds = {n["id"]: n for n in found if n is not None}
        elif query:
            hits = store.search(query, b, limit=5, deprecated=self.deprecated)[:5]
            nodes = store.nodes(hits, b)
            seeds = {h: nodes[h] for h in hits if h in nodes and self.seen(nodes[h])}
        else:
            raise ToolError("give a query or ids")
        if not seeds:
            raise ToolError("nothing visible matches; try kg_search")
        return seeds

    def _edge_line(self, e: dict) -> str:
        return f"  [{readable(e['src'])}] -{e['rel']}-> [{readable(e['dst'])}] ({e['status']})"

    def _context_from(self, store: Store, b: int, current: str, depth: int, room: Room, frontier: deque) -> None:
        """Put the visible edges of ``current`` into ``room``, strongest first, each with the neighbour it brings
        when that one is new; what does not fit is counted as left out, and a new neighbour joins the frontier."""
        edges = [e for e in store.edges_of(current, b, limit=300) if self.seen(e)]
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
            if node is None or not self.seen(node) or e["id"] in room.edges:
                continue
            cost = line_size(self._edge_line(e))
            if other in room.nodes:
                if room.take(cost):
                    room.edges[e["id"]] = e
                else:
                    room.left_edges.add(e["id"])
            elif room.take_node(other, node, cost + line_size(self._line(node))):
                room.edges[e["id"]] = e
                frontier.append((other, depth + 1))
            else:
                room.left_nodes.add(other)
                room.left_edges.add(e["id"])

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
        limit: int = IMPACT_LIMIT,
    ) -> tuple[str, dict]:
        store, b = self._store()
        found = Impact(b, max(1, min(int(depth), 3)), max(1, min(int(limit), IMPACT_MAX_LIMIT)))
        wanted = ([id] if isinstance(id, str) and id else []) + [i for i in ids or [] if isinstance(i, str)]
        paths = [p for p in ([paths] if isinstance(paths, str) else paths or []) if isinstance(p, str) and p]
        changes = diff_lines(diff) if isinstance(diff, str) else {}
        if not (wanted or paths or changes):
            raise ToolError("give an id, ids, paths (repo:path) or a unified diff")
        for nid in wanted:
            n = self._lookup(store, nid, b)
            if n is not None:
                found.seeds[n["id"]] = n
            elif path_like(nid):
                paths.append(nid)  # an id that names no node may be a path
            else:
                found.missing += 1
        if paths or changes:
            self._seeds_by_path(store, found, paths, changes)
        unmatched, found.unmatched = found.unmatched, []
        for path in unmatched:  # a path that names no file may be an id, such as one the text showed
            n = self._lookup(store, path, b)
            if n is not None:
                found.seeds[n["id"]] = n
            else:
                found.unmatched.append(path)
        if not found.seeds and not (found.ambiguous or found.unmatched):
            raise ToolError("nothing visible matches; try kg_search")
        self._walk_back(store, found)
        found.docs = {
            d: n
            for d, n in store.nodes(found.mentions, b).items()
            if self.visible(n["label"]) and d not in found.seeds and d not in found.reached
        }
        found.coverage = seed_coverage(found.seeds.values())
        return "\n".join(self._impact_lines(found)), self._impact_data(found)

    def _seeds_by_path(self, store: Store, found: Impact, paths: list[str], changes: dict[str, set[int]]) -> None:
        """Seeds from repo:path paths and the files of a diff, and the symbols its changed lines fall in. A diff
        does not say which repo it comes from: a path names a file only when exactly one visible file matches it;
        otherwise the candidates go back to the caller."""
        b = found.build
        files = [f for f in store.path_nodes(b) if self.visible(f["label"])]
        changed: dict[str, set[int]] = {}
        for path, lines in [(p, None) for p in paths] + sorted(changes.items()):
            repo, sep, rest = path.partition(":")
            if lines is None and sep and rest:
                matches = [f for f in files if f["props"].get("repo") == repo and f["props"]["path"] == rest]
            else:
                matches = path_candidates(path, files)
            if len(matches) == 1:
                found.seeds[matches[0]["id"]] = matches[0]
                if lines:
                    changed.setdefault(matches[0]["id"], set()).update(lines)
            elif matches:
                found.ambiguous.append({"path": path, "candidates": [f["id"] for f in matches]})
            else:
                found.unmatched.append(path)
        symbols = store.file_symbols(changed, b)
        for fid, lines in changed.items():
            for s in self._changed_symbols(store, b, symbols.get(fid, []), lines):
                found.seeds[s["id"]] = s

    def _walk_back(self, store: Store, found: Impact) -> None:
        """Walk back from the seeds: whoever has an edge into a reached node depends on it. A node is reached once,
        through its strongest edge (then the first rel by name); ``rels`` lists every relation it has to that
        edge's target, so a method that both overrides and calls its parent's shows both."""
        frontier = sorted(found.seeds)
        for hop in range(found.depth + 1):
            step: dict[str, tuple] = {}
            links: dict[tuple[str, str], set[str]] = {}
            for nid in frontier:
                for e in self._edges_back(store, found, nid, hop):
                    if e["rel"] == "mentions":
                        found.mentions.setdefault(e["src"], []).append(e)
                    elif hop < found.depth and e["src"] not in found.seeds and e["src"] not in found.reached:
                        links.setdefault((e["src"], e["dst"]), set()).add(e["rel"])
                        key = (-STATUS_RANK[e["status"]], e["rel"], e["dst"])
                        if e["src"] not in step or key < step[e["src"]][0]:
                            step[e["src"]] = (key, e)
            nodes = store.nodes(step, found.build)
            frontier = []
            for other in sorted(step):
                node = nodes.get(other)
                if node is not None and self.visible(node["label"]):
                    e = step[other][1]
                    rels = sorted(links[(other, e["dst"])])
                    found.reached[other] = {"hop": hop + 1, "node": node, "edge": e, "rels": rels, "hub": False}
                    frontier.append(other)

    def _edges_back(self, store: Store, found: Impact, nid: str, hop: int) -> list[dict]:
        """The visible edges into ``nid`` that kg_impact follows, mentions included, never weaker than resolved.
        A seed's are all read, a page at a time (``_seed_edges``); a reached node with more than HUB_DEGREE edges
        is a hub: it is marked and not expanded, since half the graph depends on it."""
        if hop == 0:
            edges = self._seed_edges(store, found, nid)
        else:
            edges = store.edges_of(nid, found.build, limit=HUB_DEGREE + 1)
            if len(edges) > HUB_DEGREE:
                found.reached[nid]["hub"] = True
                return []
        return [
            e
            for e in edges
            if e["dst"] == nid
            and e["src"] != nid
            and e["rel"] in WALKED_RELS
            and STATUS_RANK.get(e["status"], 0) >= IMPACT_MIN_RANK
            and self.seen(e)
        ]

    def _seed_edges(self, store: Store, found: Impact, nid: str) -> list[dict]:
        """The edges into a seed, SEED_EDGES at a time, up to IMPACT_SEED_EDGES; a seed with more is added to
        ``found.cut`` with the number of edges it has, so no caller is dropped without a word."""
        query = (nid, found.build, WALKED_RELS, IMPACT_STATUSES, self.clearance)
        edges: list[dict] = []
        while len(edges) < IMPACT_SEED_EDGES:
            want = min(SEED_EDGES, IMPACT_SEED_EDGES - len(edges))
            page = store.edges_into(*query, limit=want, offset=len(edges))
            edges += page
            if len(page) < want:
                return edges
        total = store.count_into(*query)
        if total > len(edges):
            found.cut.append({"seed": nid, "read": len(edges), "total": total})
        return edges

    @staticmethod
    def _impact_shown(found: Impact) -> tuple[dict[int, list[dict]], list[str]]:
        """What the text of kg_impact lists: per hop, the first ``found.limit`` dependents, strongest edge first,
        then by id; and the first ``found.doc_limit`` documents, those that mention the most reached nodes first."""
        hops: dict[int, list[dict]] = {}
        for r in found.reached.values():
            hops.setdefault(r["hop"], []).append(r)
        for group in hops.values():
            group.sort(key=lambda r: (-STATUS_RANK.get(r["edge"]["status"], 0), r["node"]["id"]))
        docs = sorted(found.docs, key=lambda d: (-len(found.mentions[d]), d))
        return {hop: group[: found.limit] for hop, group in hops.items()}, docs[: found.doc_limit]

    def _impact_lines(self, found: Impact) -> list[str]:
        hops, docs = self._impact_shown(found)
        shown = [*found.seeds.values(), *(r["node"] for g in hops.values() for r in g), *(found.docs[d] for d in docs)]
        shared, label_line = self._labels(shown)
        lines = [
            f"impact of {len(found.seeds)} seed(s) within {found.depth} hop(s) (build {found.build}):"
            f" {len(found.reached)} dependent(s), {len(found.docs)} document(s) to re-check"
        ]
        lines += label_line
        if found.seeds:
            lines.append("seeds:")
            lines += [f"  {self._line(found.seeds[s], shared)}" for s in sorted(found.seeds)]
        lines += [coverage_line(row) for row in found.coverage]
        lines += [
            f"cut: {c['total']} edges lead into [{readable(c['seed'])}] and the first {c['read']} were read; what"
            " depends on it through the others is not listed"
            for c in found.cut
        ]
        for hop in sorted(hops):
            total = sum(r["hop"] == hop for r in found.reached.values())
            lines.append(f"hop {hop}:" + (f" first {found.limit} of {total}" if total > found.limit else ""))
            for r in hops[hop]:
                e, also = r["edge"], [rel for rel in r["rels"] if rel != r["edge"]["rel"]]
                lines.append(
                    f"  {self._line(r['node'], shared)} -{e['rel']}-> [{readable(e['dst'])}] ({e['status']})"
                    + (f" (also {', '.join(also)})" if also else "")
                    + (" (hub, not expanded)" if r["hub"] else "")
                )
            if total > found.limit:
                lines.append(
                    f"  ... {total - found.limit} more at hop {hop}: raise limit (up to {IMPACT_MAX_LIMIT}), or pass "
                    "the symbols you change instead of their file"
                )
        lines += self._impact_doc_lines(found, docs, shared)
        for a in found.ambiguous:
            candidates = ", ".join(f"[{readable(c)}]" for c in a["candidates"])
            lines.append(f"ambiguous path {a['path']}: {candidates}; pass repo:path or an id")
        if found.unmatched:
            lines.append("no visible file for: " + ", ".join(found.unmatched))
        if found.missing:
            lines.append(f"{found.missing} id(s) not found or not visible")
        return lines

    def _impact_doc_lines(self, found: Impact, docs: list[str], shared: str | None) -> list[str]:
        if not found.docs:
            return []
        total = len(found.docs)
        lines = ["documents to re-check:" + (f" first {len(docs)} of {total}" if total > len(docs) else "")]
        for d in docs:
            targets = sorted(found.mentions[d], key=lambda e: e["dst"])
            more = f", and {len(targets) - IMPACT_MENTIONS_SHOWN} more" if len(targets) > IMPACT_MENTIONS_SHOWN else ""
            lines.append(
                f"  {self._line(found.docs[d], shared)} -mentions-> "
                + ", ".join(f"[{readable(e['dst'])}] ({e['status']})" for e in targets[:IMPACT_MENTIONS_SHOWN])
                + more
            )
        if total > len(docs):
            lines.append(f"  ... {total - len(docs)} more documents: raise limit (up to {IMPACT_MAX_LIMIT})")
        return lines

    def _impact_data(self, found: Impact) -> dict:
        """The structured result: every dependent and document, whatever the text lists, since callers such as the
        Curator's tier check read them all; ``left_out`` says what the text did not list."""
        cut = f"; {len(found.cut)} seed(s) cut" if found.cut else ""
        per_hop: dict[int, int] = {}
        for r in found.reached.values():
            per_hop[r["hop"]] = per_hop.get(r["hop"], 0) + 1
        return {
            "build": found.build,
            "depth": found.depth,
            "seeds": [self._node_json(found.seeds[s]) for s in sorted(found.seeds)],
            "coverage": found.coverage,
            "impacted": [
                {
                    "hop": r["hop"],
                    "node": self._node_json(r["node"]),
                    "rel": r["edge"]["rel"],
                    "rels": r["rels"],
                    "status": r["edge"]["status"],
                    "to": r["edge"]["dst"],
                    "hub": r["hub"],
                }
                for r in found.reached.values()
            ],
            "documents": [
                {
                    "node": self._node_json(found.docs[d]),
                    "mentions": [
                        {"node": e["dst"], "status": e["status"]}
                        for e in sorted(found.mentions[d], key=lambda e: e["dst"])
                    ],
                }
                for d in sorted(found.docs)
            ],
            "cut": found.cut,
            "ambiguous": found.ambiguous,
            "unmatched": found.unmatched,
            "not_found": found.missing,
            "limit": found.limit,
            "left_out": {
                "hops": {str(hop): n - found.limit for hop, n in sorted(per_hop.items()) if n > found.limit},
                "documents": max(len(found.docs) - found.doc_limit, 0),
            },
            "summary": f"{len(found.reached)} dependents, {len(found.docs)} documents{cut}",
        }

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
                    if e["rel"] in PATH_SKIP or (rels and e["rel"] not in rels) or not self.seen(e):
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
                if node is None or not self.seen(node):
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
        if goal is not None:
            return self._path_between(b, start, goal, tree, max_hops)
        children: dict[str, list[str]] = {}
        for nid, t in tree.items():
            if t["parent"] is not None:
                children.setdefault(t["parent"], []).append(nid)
        shared, label_line = self._labels(t["node"] for t in tree.values())
        lines = [
            f"chains from {readable(start['id'])} along {', '.join(CHAIN_RELS)} (build {b}): {len(tree) - 1} node(s)"
            f" within {max_hops} hop(s)",
            *label_line,
            self._line(start, shared),
        ]
        stack = sorted(children.get(start["id"], []), reverse=True)
        while stack:
            nid = stack.pop()
            t = tree[nid]
            lines.append(
                "  " * t["hop"]
                + f"{self._step(t['parent'], t['edge'])} {self._line(t['node'], shared)}"
                + (" (hub, not expanded)" if t["hub"] else "")
            )
            stack.extend(sorted(children.get(nid, []), reverse=True))
        data = {
            "build": b,
            "from": start["id"],
            "to": None,
            "nodes": [{**self._node_json(t["node"]), "hop": t["hop"], "hub": t["hub"]} for t in tree.values()],
            "edges": [_edge_json(t) for t in tree.values() if t["parent"] is not None],
            "summary": f"{len(tree) - 1} nodes",
        }
        return "\n".join(lines), data

    def _path_between(self, b: int, start: dict, goal: dict, tree: dict, max_hops: int) -> tuple[str, dict]:
        chain = []
        nid = goal["id"] if goal["id"] in tree else None
        while nid is not None:
            chain.append(tree[nid])
            nid = tree[nid]["parent"]
        chain.reverse()
        ends = f"{readable(start['id'])} to {readable(goal['id'])}"
        if not chain:
            lines = [
                f"no path within {max_hops} hop(s) from {ends} through visible nodes (build {b}; hubs are not expanded)"
            ]
        else:
            shared, label_line = self._labels(t["node"] for t in chain)
            lines = [f"path from {ends} (build {b}): {len(chain) - 1} hop(s)", *label_line, self._line(start, shared)]
            lines += [f"  {self._step(t['parent'], t['edge'])} {self._line(t['node'], shared)}" for t in chain[1:]]
        data = {
            "build": b,
            "from": start["id"],
            "to": goal["id"],
            "found": bool(chain),
            "hops": len(chain) - 1 if chain else None,
            "nodes": [self._node_json(t["node"]) for t in chain],
            "edges": [_edge_json(t) for t in chain[1:]],
            "summary": f"{len(chain) - 1} hops" if chain else "no path",
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
        for h in [h for h, (t, *_) in self.handles.items() if now - t > HANDLE_TTL]:
            del self.handles[h]
        if handle not in self.handles:
            raise ToolError(f"unknown or expired handle {handle!r}")
        _, chunks, audience = self.handles[handle]
        if not audience_below(audience, self.audience):  # the reader left a chat the result named since
            del self.handles[handle]
            raise ToolError(f"handle {handle!r} continues a result you may no longer read: call its tool again")
        text = chunks.popleft()
        if chunks:
            text += f"\n[more: call kg_more with handle {handle} again]"
        else:
            del self.handles[handle]
        return text, {"handle": handle, "remaining": len(chunks), "summary": "continued"}


def _edge_json(t: dict) -> dict:
    e = t["edge"]
    return {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]}


def lines(props: dict) -> str:
    """Where a node is in its file, as its text shows it: ``lines A-B`` of a section (its ``span``) or of a symbol (its
    ``line`` and ``end_line``), ``line A`` of a symbol read before symbols had an end; "" for any other node."""
    span = props.get("span")
    if isinstance(span, str) and _SPAN.fullmatch(span):
        return span
    first, last = props.get("line"), props.get("end_line")
    if not isinstance(first, int) or isinstance(first, bool) or first < 1:
        return ""
    if isinstance(last, int) and not isinstance(last, bool) and last > first:
        return f"lines {first}-{last}"
    return f"line {first}"


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def wants_data(meta) -> bool:
    """Whether the ``_meta`` of a tools/call's params asks for the structured result: DATA_META true."""
    return isinstance(meta, dict) and meta.get(DATA_META) is True


def for_model(result: dict) -> dict:
    """``result`` as an MCP client gets it unless it asks for the data: the text blocks alone, so the model reads the
    text the tool wrote. Claude Code (2.1.296) shows the model a result's structuredContent in place of its text
    when there is one, and Codex (0.153) hands the model's script the whole result; opencode (1.18) shows the text
    either way. The labels stay in the text block's ``_meta``, which the hook gets and the model does not."""
    return {key: value for key, value in result.items() if key != "structuredContent"}


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
        if not wants_data(params.get("_meta")):
            result = for_model(result)
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_stdio(project: str | None = None, sink: str = "claude-code@anthropic", session=None) -> int:
    """Answer MCP over stdin and stdout. ``session`` is anything with ``Session.call``, such as the hub backend of
    ``evo_agents.hub.kg_cli``; without one, the local store of ``project`` answers."""
    session = session or make_session(project, sink)
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
