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

The tools live by seam: what a session may see (``serve_view``), how its answers name nodes (``serve_text``),
kg_search (``serve_search``), kg_context and kg_path (``serve_walk``) and kg_impact (``serve_impact``); ``Session``
holds them all, with kg_node, kg_status, kg_more, the envelope of every result and the MCP server around them.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from collections import deque

from evo_agents import __version__
from evo_agents.kg.freshness import Links
from evo_agents.kg.policy import audience_below, audience_of
from evo_agents.kg.project import ProjectError, resolve_project
from evo_agents.kg.refs import UnknownArgument, arguments, named_by, readable
from evo_agents.kg.serve_impact import ImpactTools
from evo_agents.kg.serve_search import SearchTools
from evo_agents.kg.serve_view import ToolError
from evo_agents.kg.serve_walk import WalkTools
from evo_agents.kg.sessions import READ_META, current_session_id, labels_in, read_session
from evo_agents.kg.sessions import describe as describe_session
from evo_agents.kg.store import Store
from evo_agents.kg.tools import INSTRUCTIONS, SIGNATURES, TOOLS

SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
CAP_CHARS = 25_000  # about 9,000 tokens even for Vietnamese text, under Claude Code's 10,000 warning
DATA_META = "evo-agents/data"  # true in a tools/call's params._meta: the result keeps its structuredContent
HANDLE_TTL = 1800


class Session(SearchTools, WalkTools, ImpactTools):
    """A session of ``kg serve``: the tools, each answering through ``call`` with what the session may see."""

    # -- dispatch -------------------------------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        if self.error:
            return error_result(self.error)
        self.read = None
        self.sees_all = False
        self._opened = []
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
            return error_result(str(exc))
        except (TypeError, ValueError, OverflowError) as exc:
            return error_result(f"bad arguments for {name}: {exc}")
        except sqlite3.Error as exc:
            return error_result(f"the graph could not be read: {type(exc).__name__}: {exc}")
        finally:
            for store in self._opened:
                store.close()
            self._opened = None
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

    def tool_kg_status(self) -> tuple[str, dict]:
        from evo_agents.kg.status import project_status, render_status

        status = project_status(self.project)
        self._clear_status(status)
        clr = (self.policy.level_name(self.clearance[0]), self.policy.location_name(self.clearance[1]))
        text = f"sink {self.sink}: clearance {clr[0]}/{clr[1]}\n" + render_status(status)
        status["sink"] = {"id": self.sink, "clearance": {"level": clr[0], "location": clr[1]}}
        session = read_session(current_session_id())
        if session is not None:
            text += "\n" + describe_session(session)
            status["session"] = {k: v for k, v in session.items() if k != "v"}
        status["summary"] = status["project"]
        return text, status

    def _clear_status(self, status: dict) -> None:
        """Keep in ``status`` what the sink's clearance reads, as the other tools do, and raise the session's label by
        it: a source above the clearance shows its id alone; a build whose highest label is above it shows its counts,
        not its coverage, warnings or reads, which name items."""
        for entry in status["sources"]:
            label = self.policy.source_label(entry["id"])
            shown = (label.level, label.location, label.integrity)
            if self.visible(shown):
                self._saw(shown)
            else:
                sid = entry["id"]
                entry.clear()
                entry.update(id=sid, withheld=True)
        graph = status.get("graph") or {}
        stats = graph.get("stats") or {}
        top = stats.get("top_label")
        if not graph.get("ready") or not isinstance(top, list) or len(top) != 2:
            return
        shown = (top[0], top[1], "U", audience_of(stats.get("top_audience") or []))
        if self.visible(shown):
            self._saw(shown)
            return
        for key in ("coverage", "stats"):
            graph.pop(key, None)
        status.update(warnings=[], multiple_definitions=None, reads=[])

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


def error_result(message: str) -> dict:
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


def _rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(session: Session, message) -> dict | None:
    if not isinstance(message, dict):  # a batch or a bare value: JSON-RPC's invalid request
        return _rpc_error(None, -32600, "invalid request: a message is one JSON object")
    method = message.get("method")
    mid = message.get("id")
    if mid is None:
        return None  # notification
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(mid, -32602, "invalid params: params is an object")
    if method == "initialize":
        requested = params.get("protocolVersion")
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
        name, args = params.get("name", ""), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(mid, -32602, "invalid params: name is a string and arguments an object")
        result = session.call(name, args)
        if not wants_data(params.get("_meta")):
            result = for_model(result)
    else:
        return _rpc_error(mid, -32601, f"method not found: {method}")
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
        except (ValueError, RecursionError):  # not JSON, an integer past Python's digit limit, nesting too deep
            reply = _rpc_error(None, -32700, "parse error")
        else:
            try:
                reply = handle(session, message)
            except Exception as exc:  # one message that breaks a tool does not stop the server
                mid = message.get("id") if isinstance(message, dict) else None
                reply = _rpc_error(mid, -32603, f"internal error: {type(exc).__name__}: {exc}"[:500])
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
