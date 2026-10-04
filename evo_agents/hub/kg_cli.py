"""``evo-agents hub kg push|builds|build``, and the hub backend of ``kg serve`` and ``kg query``.

``push`` sends the runs a project's corpus holds and the hub lacks (``evo_agents.hub.kg_push``): ``--project P`` one
project of this machine, ``--all`` every project of projects.json the hub takes a push for. ``builds`` lists a
project's builds and the jobs still queued; ``build`` (the writer role) queues one and with ``--wait`` waits until it
has finished, exiting 1 when it failed.

``open_session`` picks where kg_* are answered. ``local`` reads the store on this machine, as before the hub.
``hub`` sends every call to POST /v1/kg/{project}/tools/{tool} (``RemoteSession``) with the same tool names and
results, so an MCP client sees no difference. ``auto`` takes the hub when this machine is signed in and the hub has
a successful build of the project it can show the caller, and the local store otherwise, including when the hub does
not answer within PROBE_TIMEOUT. Standard library only.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.client import Hub, HubError, load_credentials
from evo_agents.hub.kg_push import push_all, push_project

BACKENDS = ("auto", "local", "hub")
PROBE_TIMEOUT = 5.0  # seconds auto waits for the hub before answering locally
WAIT_INTERVAL = 2.0
DEFAULT_WAIT = 1800.0
FINISHED = ("succeeded", "failed")


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}"


# Pushing


def local_projects(project: str | None, everything: bool) -> list[tuple[str, object]]:
    """(name, project or the error it failed to load with) for ``--project`` or ``--all``."""
    from evo_agents.kg.project import ProjectError, load_project_at, resolve_project
    from evo_agents.kg.sync import indexed_projects

    if everything:
        found = []
        for name, root in indexed_projects():
            try:
                found.append((name, load_project_at(root)))
            except Exception as exc:  # a broken harness is reported on its project, the others go on
                found.append((name, exc if isinstance(exc, ProjectError) else ProjectError(f"{type(exc).__name__}")))
        return found
    loaded = resolve_project(project)
    return [(loaded.name, loaded)]


def push(hub: Hub, project: str | None, everything: bool) -> list:
    """The push reports of ``--project`` or ``--all``. Raises ProjectError when the one project does not load."""
    projects = local_projects(project, everything)
    if everything:
        return push_all(hub, projects)
    return [push_project(hub, loaded) for _, loaded in projects]


def print_reports(reports: list, out=None) -> None:
    out = out or sys.stdout
    for report in reports:
        print(report.summary_line(), file=out)
        for error in report.errors:
            print(f"  error: {error}", file=out)


@_client_command
def cmd_push(args) -> int:
    from evo_agents.kg.project import ProjectError

    if args.all == bool(args.project):
        raise HubError("pass --project P or --all")
    hub, _ = _signed_in()
    try:
        reports = push(hub, args.project, args.all)
    except ProjectError as exc:
        raise HubError(str(exc)) from None
    if args.json:
        _print_json({"ok": all(r.ok for r in reports), "projects": [r.to_json() for r in reports]})
    else:
        print_reports(reports)
    return 0 if all(r.ok for r in reports) else 1


# Builds


def _build_row(build: dict) -> tuple:
    outcome = build.get("error") or (
        f"{build['nodes']} nodes, {build['edges']} edges" if build["status"] == "succeeded" else ""
    )
    return (
        build["id"],
        build["status"],
        _when(build.get("queued_at")),
        _when(build.get("finished_at")),
        build.get("runs") if build.get("runs") is not None else "-",
        (build.get("content_hash") or "-")[:19],
        outcome[:80],
    )


@_client_command
def cmd_builds(args) -> int:
    hub, _ = _signed_in()
    found = hub.call("GET", f"{_base(args.project)}/builds?limit={args.limit}")
    if args.json:
        _print_json(found)
        return 0
    rows = [_build_row(build) for build in found["builds"]]
    if rows:
        _table(("BUILD", "STATUS", "QUEUED (UTC)", "FINISHED (UTC)", "RUNS", "CONTENT HASH", "OUTCOME"), rows)
    else:
        print(f"project {args.project} has no build on the hub yet")
    for job in found["jobs"]:
        state = "running" if job["status"] == "doing" else "waiting"
        print(f"job {job['job_id']} {state}" + (f" (build {job['build_id']})" if job["build_id"] else ""))
    return 0


def wait_for(hub: Hub, project: str, build_id: int, timeout: float, sleep=time.sleep, clock=time.monotonic) -> dict:
    deadline = clock() + timeout
    while True:
        build = hub.call("GET", f"{_base(project)}/builds/{build_id}")
        if build["status"] in FINISHED:
            return build
        if clock() >= deadline:
            raise HubError(f"build {build_id} of project {project} has not finished after {timeout:g}s")
        sleep(WAIT_INTERVAL)


@_client_command
def cmd_build(args) -> int:
    hub, _ = _signed_in()
    queued = hub.call("POST", f"{_base(args.project)}/builds")
    build = queued.get("build")
    if build is None:
        raise HubError(f"a build of project {args.project} is waiting already, but the hub names none")
    if args.wait:
        build = wait_for(hub, args.project, build["id"], args.timeout)
    if args.json:
        _print_json({"queued": queued["queued"], "build": build})
    else:
        verb = "Queued" if queued["queued"] else "A build was waiting already:"
        print(f"{verb} build {build['id']} of project {args.project}: {build['status']}.")
        if build["status"] == "succeeded":
            print(f"{build['nodes']} nodes, {build['edges']} edges, content {build['content_hash']}")
        elif build["status"] == "failed":
            print(f"error: {build['error']}", file=sys.stderr)
    return 1 if build["status"] == "failed" else 0


def register_kg(hsub) -> None:
    kg = hsub.add_parser("kg", help="knowledge graphs on the hub: push runs, list and queue builds")
    ksub = kg.add_subparsers(dest="kg_command", required=True)

    push_parser = ksub.add_parser("push", help="send the runs of a project's corpus that the hub does not have")
    push_parser.add_argument("--project", help="project name or harness path")
    push_parser.add_argument(
        "--all", action="store_true", help="every project in projects.json the hub takes a push for from you"
    )
    push_parser.add_argument("--json", action="store_true", help="machine-readable output")
    push_parser.set_defaults(func=cmd_push)

    builds = ksub.add_parser("builds", help="a project's builds on the hub, newest first, and the queued jobs")
    builds.add_argument("--project", required=True, help="project name on the hub")
    builds.add_argument("--limit", type=int, default=20, help="builds to show (default: 20, at most 100)")
    builds.add_argument("--json", action="store_true", help="machine-readable output")
    builds.set_defaults(func=cmd_builds)

    build = ksub.add_parser("build", help="queue a build of a project's graph (needs the writer role)")
    build.add_argument("--project", required=True, help="project name on the hub")
    build.add_argument("--wait", action="store_true", help="wait until the build has finished; exit 1 if it failed")
    build.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT, help="seconds --wait waits at most (default: 1800)"
    )
    build.add_argument("--json", action="store_true", help="machine-readable output")
    build.set_defaults(func=cmd_build)


# The hub backend of kg serve and kg query


class RemoteSession:
    """``Session.call`` answered by the hub: POST /v1/kg/{project}/tools/{tool}. A hub that cannot be reached or
    refuses the call gives a tool error naming the hub."""

    def __init__(self, hub: Hub, project: str, sink: str):
        self.hub = hub
        self.project = project
        self.sink = sink

    def call(self, name: str, args: dict) -> dict:
        from evo_agents.kg.serve import TOOLS, _error

        if name not in {tool["name"] for tool in TOOLS}:
            return _error(f"unknown tool {name!r}")
        try:
            result = self.hub.call(
                "POST", f"{_base(self.project)}/tools/{name}", {"arguments": args or {}, "sink": self.sink}
            )
        except HubError as exc:
            return _error(f"{exc} (hub {self.hub.url})")
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            return _error(f"the hub at {self.hub.url} did not answer with a tool result")
        return result


def hub_has_graph(hub: Hub, project: str) -> bool:
    """Whether the hub has a successful build of ``project`` it shows the caller."""
    found = hub.call("GET", f"{_base(project)}/builds?status=succeeded&limit=1")
    return bool(found.get("builds"))


def open_session(backend: str, project: str | None, sink: str):
    """What answers kg_* for ``kg serve`` and ``kg query``: a local Session or a RemoteSession (see the module)."""
    from evo_agents.kg.project import ProjectError, resolve_project
    from evo_agents.kg.serve import Session, make_session

    if backend == "local":
        return make_session(project, sink)
    try:
        name = resolve_project(project).name
    except ProjectError as exc:
        name = project if project and "/" not in project and project not in (".", "..") else None
        if name is None:
            return make_session(project, sink) if backend == "auto" else Session(None, sink, error=str(exc))
    try:
        credentials = load_credentials()
        hub = Hub(credentials.url, credentials.token)
        if backend == "auto" and not hub_has_graph(Hub(hub.url, hub.token, timeout=PROBE_TIMEOUT), name):
            return make_session(project, sink)
    except HubError as exc:
        return make_session(project, sink) if backend == "auto" else Session(None, sink, error=str(exc))
    return RemoteSession(hub, name, sink)
