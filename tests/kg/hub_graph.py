"""A hub that answers the reads of the kg CLI over graphs built in the test: ``kg serve``, ``kg query`` and ``kg eval``
through its /mcp, ``kg status``, ``kg profile`` and ``kg semantic`` through POST tools/kg_status, and ``kg settings``
through its settings routes. The hub answers with its own engine, ``evo_agents.hub.kg_graph.HubSession``, over a
snapshot of the graph the test built in memory (``tests.kg.built``), as the hub reads one of its tables; the settings
live in the project's directory (``evo_agents.kg.settings``)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote

from evo_agents.hub.client import HubError
from evo_agents.hub.kg_graph import BuiltGraph, HubSession
from evo_agents.kg import grep_links
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.policy import Label
from evo_agents.kg.project import Project, load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.snapshot import Snapshot, SnapshotView
from tests.kg.built import snapshot_of

URL = "https://hub.lumenops.example"


class NoGraph:
    """What the hub answers every kg_* tool of a project it holds no graph of (``hub.server.kg.tool_result``)."""

    def __init__(self, project: str):
        self.project = project

    def call(self, name: str, args: dict) -> dict:
        text = f"error: project {self.project} has no graph on the hub yet: push its runs with `evo-agents hub kg push`"
        return {"content": [{"type": "text", "text": text + " (a build follows)"}], "isError": True}


class GraphHub:
    """``projects``: the projects of the hub by name. ``asked``: (method, path) of every REST call, then of every tool
    called through /mcp as ("MCP", name)."""

    url = URL
    token = "evo_test"

    def __init__(self, projects: dict[str, Project]):
        self.projects = projects
        self.asked: list[tuple[str, str]] = []

    def project(self, name: str | None) -> Project:
        if name not in self.projects:
            raise HubError(f"HTTP 404: no project {name}")
        project = self.projects[name]
        return load_project_at(project.harness.root, project.home)  # read again: a test may change its files

    def session(self, name: str | None, sink: str | None) -> Session | NoGraph:
        """The hub's engine for ``name`` as ``sink`` asks, by default the project's first agent-session sink; a
        session that says what the hub says of a project without a graph (``NoGraph``)."""
        project = self.project(name)
        sinks = (project.knowledge.get("policy") or {}).get("sinks") or []
        sink = sink or next((s["id"] for s in sinks if s.get("kind") == "agent-session"), "agent")
        found = graph_of(project)
        if found is None:
            return NoGraph(project.name)
        snap, view, summary = found
        b = snap.build
        policy = project.policy
        ceiling = None  # a sink knowledge.yaml does not declare sees nothing, as on the hub
        if sink in policy.sinks:
            clearance = policy.clearance(sink)
            ceiling = Label(clearance.level, clearance.location, "U", frozenset({project.name}))
        elif sink == "cli":  # the tests' operator, who holds the corpus, who reads every label
            ceiling = Label(len(policy.levels) - 1, len(policy.locations) - 1, "U", frozenset({project.name}))
        vectors = (summary.get("coverage") or {}).get("vectors")
        built = BuiltGraph(b, None, None, snap.content_hash, 0, 0, datetime.now(UTC), vectors=vectors)
        return HubSession(project.name, policy, ceiling, sink, view, built, built)

    def call(self, method: str, path: str, body=None, *, timeout=None):
        self.asked.append((method, path))
        parts = path.split("?")[0].strip("/").split("/")
        if parts[:2] != ["v1", "kg"] or len(parts) < 4:
            raise HubError(f"HTTP 404: {method} {path}")
        name, rest = unquote(parts[2]), parts[3:]
        if rest == ["tools", "kg_status"] and method == "POST":
            return self.session(name, (body or {}).get("sink")).call("kg_status", {})
        if rest == ["grep-links"] and method == "POST":
            return {"lines": self.grep_links(name, body or {})}
        if rest[0] == "settings":
            return self.settings(name, method, rest[1:], body or {})
        raise HubError(f"HTTP 404: {method} {path}")

    def grep_links(self, name: str, body: dict) -> list[str]:
        """POST grep-links as the hub answers it (``evo_agents.hub.server.kg_grep``), with the session's (level,
        location) clearance, none for a sink knowledge.yaml does not declare; 409 without a graph."""
        session = self.session(name, body.get("sink"))
        if isinstance(session, NoGraph):
            raise HubError("HTTP 409: no graph yet", 409)
        files = [(f["repo"], f["path"]) for f in body.get("files") or []]
        hits = [(files[h["file"]], h.get("line"), h.get("text") or "") for h in body.get("hits") or []]
        clearance = None if session.error else session.clearance
        return grep_links.link_lines(session.graph_store, clearance, hits)

    def settings(self, name: str, method: str, rest: list[str], body: dict) -> dict:
        root = self.project(name).root
        before = kg_settings.read(root)
        after = before
        if rest == ["undo"]:
            after = kg_settings.undo(root)
            if after is None:
                raise HubError("HTTP 409: nothing to undo")
        elif method == "PUT":
            try:
                after = kg_settings.write(
                    root, kg_settings.of(body.get("values") or {}, before), reason=body.get("reason")
                )
            except kg_settings.SettingsError as exc:
                raise HubError(f"HTTP 422: {exc}") from None
        changes = [
            {"id": i, "kind": "set", "changed_by": "alice", "run_id": None, "reason": entry.get("reason")}
            for i, entry in reversed(list(enumerate(kg_settings.history(root), 1)))
        ]
        return {
            "values": after.to_json(),
            "overrides": after.overrides(),
            "needs_rebuild": kg_settings.rebuild_keys(before, after),
            "changes": changes,
        }


def search_tuning(project: Project) -> kg_settings.Tuning:
    """What the project's settings tune search by: the override a test asked for, else its settings.json, else the
    defaults."""
    asked = kg_settings.override_of()
    if asked is not None:
        return asked
    try:
        return kg_settings.read(project.root)
    except kg_settings.SettingsError:
        return kg_settings.DEFAULT


def graph_of(project: Project) -> tuple[Snapshot, SnapshotView, dict] | None:
    """The snapshot of the project's latest build, its view as the project's settings tune search, and its summary:
    the build ``tests.kg.built`` keeps; None before one."""
    snap = snapshot_of(project)
    if snap is None:
        return None
    return snap, snap.view(tuning=search_tuning(project)), dict(snap.summary)


def sign_in(monkeypatch, *projects: Project) -> GraphHub:
    """This machine signed in to a GraphHub of ``projects``, for the REST calls and for /mcp."""
    from evo_agents.hub import cli_client, client, mcp_proxy

    hub = GraphHub({project.name: project for project in projects})
    credentials = client.Credentials(hub.url, "alice", hub.token)

    def call_tool(self, name: str, arguments: dict, meta: dict | None = None) -> dict:
        hub.asked.append(("MCP", name))
        return hub.session(self.project, self.sink).call(name, arguments)

    monkeypatch.setattr(client, "load_credentials", lambda: credentials)
    monkeypatch.setattr(client, "Hub", lambda url, token, **kwargs: hub)
    monkeypatch.setattr(cli_client, "load_credentials", lambda: credentials)  # signed_in of the hub kg commands
    monkeypatch.setattr(cli_client, "Hub", lambda url, token, **kwargs: hub)
    monkeypatch.setattr(mcp_proxy, "session_credentials", lambda: credentials)
    monkeypatch.setattr(mcp_proxy.McpClient, "call_tool", call_tool)
    return hub


def hub_session(project: Project, sink: str | None = None) -> Session | NoGraph:
    """The hub's engine for ``project`` as ``sink`` asks, as GraphHub answers it."""
    return GraphHub({project.name: project}).session(project.name, sink)


def hub_status(project: Project, sink: str | None = None) -> dict:
    """The hub's kg_status of ``project`` as ``sink`` asks: its text and its data."""
    return hub_session(project, sink).call("kg_status", {})
