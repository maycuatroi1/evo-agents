"""This machine's side of the hub's knowledge graphs: what answers kg_* for ``kg serve``, ``kg query`` and ``kg status``,
and the projects of this machine a push sends.

``open_session`` picks where kg_* are answered: the hub, always, since graphs are built and read on the hub only. It
sends every call to the hub's /mcp as a tools/call (``RemoteSession``), the seven tools with the same names and
results, so an MCP client sees no difference. ``auto`` is the same as ``hub``, kept for the .mcp.json files that name
it; a graph store left on this machine is never read. Not signed in, or a project it cannot name, gives a session
whose every tool answers the error. Inside a run of this machine's worker (``mcp_proxy.run_credentials``), it asks the
hub as the agent of that run, with the worker's token and X-Evo-Run, as ``evo-agents hub mcp`` does there: the hub
binds the session to the run's project, so a run whose worktree names no project of this machine, or whose machine is
not signed in, still reads its graph.

``local_projects`` and ``push`` are what ``hub kg push`` and ``kg sync --all`` send from: the project of ``--project``,
or every project of projects.json (``evo_agents.hub.kg_push``). Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.kg_push import PushReport, push_all, push_project

if TYPE_CHECKING:
    from evo_agents.kg.project import Project

BACKENDS = ("auto", "hub")
PROBE_TIMEOUT = 5.0  # seconds the SessionStart hook waits for the hub before it says nothing


# Pushing


def local_projects(project: str | None, everything: bool) -> list[tuple[str, Project | Exception]]:
    """(name, project or the error it failed to load with) for ``--project`` or ``--all``."""
    from evo_agents.kg.project import ProjectError, load_project_at, resolve_project
    from evo_agents.kg.sync import indexed_projects

    if everything:
        found: list[tuple[str, Project | Exception]] = []
        for name, root in indexed_projects():
            try:
                found.append((name, load_project_at(root)))
            except Exception as exc:  # a broken harness is reported on its project, the others go on
                found.append((name, exc if isinstance(exc, ProjectError) else ProjectError(f"{type(exc).__name__}")))
        return found
    loaded = resolve_project(project)
    return [(loaded.name, loaded)]


def push(
    hub: Hub, project: str | None, everything: bool, progress: Callable[[str], None] | None = None
) -> list[PushReport]:
    """The push reports of ``--project`` or ``--all``; ``progress`` receives a line per run and per batch of blobs.
    Raises ProjectError when the one project does not load."""
    projects = local_projects(project, everything)
    if everything:
        return push_all(hub, projects, progress)
    return [push_project(hub, loaded, progress) for _, loaded in projects if not isinstance(loaded, Exception)]


def push_json(reports: list[PushReport]) -> dict:
    """What ``hub kg push --json`` prints."""
    return {"ok": all(r.ok for r in reports), "projects": [r.to_json() for r in reports]}


# The hub backend of kg serve and kg query


class RemoteSession:
    """``Session.call`` answered by the hub: a tools/call to its /mcp for ``project`` through ``sink``
    (``evo_agents.hub.mcp_proxy.McpClient``) that asks for the structured result too, as a local Session gives it. A
    hub that cannot be reached or refuses the call gives a tool error naming the hub."""

    def __init__(self, hub: Hub, project: str | None, sink: str, run: int | None = None, timeout: float | None = None):
        self.hub = hub
        self.project = project
        self.sink = sink
        self.run = run  # the run whose agent asks, with a worker's token: the hub binds the session to its project
        self.timeout = timeout  # seconds one call may take; None: McpClient's own

    def call(self, name: str, args: dict) -> dict:
        from evo_agents.hub.mcp_proxy import McpClient
        from evo_agents.kg.serve import DATA_META, TOOLS, error_result

        if name not in {tool["name"] for tool in TOOLS}:
            return error_result(f"unknown tool {name!r}")
        try:
            client = McpClient(self.hub.url, self.hub.token or "", self.project, self.sink, run=self.run)
            if self.timeout is not None:
                client.timeout = self.timeout
            return client.call_tool(name, args or {}, {DATA_META: True})
        except HubError as exc:
            return error_result(f"{exc} (hub {self.hub.url})")


def open_session(backend: str, project: str | None, sink: str, timeout: float | None = None):
    """What answers kg_* for ``kg serve``, ``kg query`` and ``kg status``: a RemoteSession, or a Session that answers
    every tool with why the hub cannot be asked (see the module). ``backend`` is ``hub`` or ``auto``, the same."""
    from evo_agents.hub.mcp_proxy import run_credentials, session_credentials
    from evo_agents.kg.project import ProjectError, resolve_project
    from evo_agents.kg.serve import Session

    run = run_credentials()
    if run is not None:  # the hub knows the run's project
        return RemoteSession(Hub(run.url, run.token), project, sink, run=run.run, timeout=timeout)
    try:
        name = resolve_project(project).name
    except ProjectError as exc:
        name = project if project and "/" not in project and project not in (".", "..") else None
        if name is None:
            return Session(None, sink, error=f"{exc}. Start the server with --project or set EVO_KG_PROJECT.")
    try:
        credentials = session_credentials()  # never a run's token of the hub, which /mcp does not take
    except HubError as exc:
        return Session(None, sink, error=f"the hub: {exc}")
    return RemoteSession(Hub(credentials.url, credentials.token), name, sink, timeout=timeout)
