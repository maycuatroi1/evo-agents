"""A hub that answers the reads of the kg CLI over graphs built in the test: ``kg serve``, ``kg query`` and ``kg eval``
through its /mcp, ``kg status``, ``kg profile`` and ``kg semantic`` through POST tools/kg_status, and ``kg settings``
through its settings routes. The graph engine of the hub is ``evo_agents.kg.serve.Session``, so a Session over the
test's project answers here; the settings live where that Session reads them (``evo_agents.kg.settings``)."""

from __future__ import annotations

from urllib.parse import unquote

from evo_agents.hub.client import HubError
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.project import Project, load_project_at
from evo_agents.kg.serve import Session

URL = "https://hub.lumenops.example"


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

    def session(self, name: str | None, sink: str | None) -> Session:
        """The hub's engine for ``name`` as ``sink`` asks, by default the project's first agent-session sink."""
        project = self.project(name)
        sinks = (project.knowledge.get("policy") or {}).get("sinks") or []
        default = next((s["id"] for s in sinks if s.get("kind") == "agent-session"), "agent")
        return Session(project, sink or default)

    def call(self, method: str, path: str, body=None, *, timeout=None):
        self.asked.append((method, path))
        parts = path.split("?")[0].strip("/").split("/")
        if parts[:2] != ["v1", "kg"] or len(parts) < 4:
            raise HubError(f"HTTP 404: {method} {path}")
        name, rest = unquote(parts[2]), parts[3:]
        if rest == ["tools", "kg_status"] and method == "POST":
            return self.session(name, (body or {}).get("sink")).call("kg_status", {})
        if rest[0] == "settings":
            return self.settings(name, method, rest[1:], body or {})
        raise HubError(f"HTTP 404: {method} {path}")

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
