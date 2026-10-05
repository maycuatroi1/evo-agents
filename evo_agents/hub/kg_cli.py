"""``evo-agents hub kg push|builds|build|prune``, and the hub backend of ``kg serve`` and ``kg query``.

``push`` sends the runs a project's corpus holds and the hub lacks (``evo_agents.hub.kg_push``): ``--project P`` one
project of this machine, ``--all`` every project of projects.json the hub takes a push for. It reports its progress on
stderr, a line per run and per batch of blobs, and its outcome on stdout. ``builds`` lists a project's builds and the
jobs still queued; ``build`` (the writer role) queues one and with ``--wait`` waits until it has finished, exiting 1
when it failed. ``prune`` (a hub admin) runs the retention of built graphs now (``evo_agents.hub.kg_prune``): the
artifacts of graphs older than each project's ``--keep`` newest leave the bucket, and ``--dry-run`` only says which.

``open_session`` picks where kg_* are answered. ``local`` reads the store on this machine, as before the hub.
``hub`` sends every call to the hub's /mcp as a tools/call (``RemoteSession``), the seven tools with the same names and
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
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.kg_push import push_all, push_project

BACKENDS = ("auto", "local", "hub")
PROBE_TIMEOUT = 5.0  # seconds auto waits for the hub before answering locally
WAIT_INTERVAL = 2.0
DEFAULT_WAIT = 1800.0
FINISHED = ("succeeded", "failed")
PRUNE_TIMEOUT = 600.0  # seconds a prune may take: it deletes objects in the blob store


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


def push(hub: Hub, project: str | None, everything: bool, progress=None) -> list:
    """The push reports of ``--project`` or ``--all``; ``progress`` receives a line per run and per batch of blobs.
    Raises ProjectError when the one project does not load."""
    projects = local_projects(project, everything)
    if everything:
        return push_all(hub, projects, progress)
    return [push_project(hub, loaded, progress) for _, loaded in projects]


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)  # stderr: stdout keeps the summary, or the JSON of --json


def push_json(reports: list) -> dict:
    """What ``hub kg push --json`` prints."""
    return {"ok": all(r.ok for r in reports), "projects": [r.to_json() for r in reports]}


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
        reports = push(hub, args.project, args.all, _progress)
    except ProjectError as exc:
        raise HubError(str(exc)) from None
    if args.json:
        _print_json(push_json(reports))
    else:
        print_reports(reports)
    return 0 if all(r.ok for r in reports) else 1


# Builds


def _artifact_note(build: dict) -> str:
    """What became of a succeeded build's artifact, when it is not simply its own."""
    if build.get("artifact_pruned_at"):
        return ", artifact pruned"
    if build.get("artifact_reused_from"):
        return f", artifact of build {build['artifact_reused_from']}"
    return ""


def _build_row(build: dict) -> tuple:
    succeeded = build["status"] == "succeeded"
    outcome = build.get("error") or (
        f"{build['nodes']} nodes, {build['edges']} edges{_artifact_note(build)}" if succeeded else ""
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
            counts = f"{build['nodes']} nodes, {build['edges']} edges"
            print(f"{counts}, content {build['content_hash']}{_artifact_note(build)}")
        elif build["status"] == "failed":
            print(f"error: {build['error']}", file=sys.stderr)
    return 1 if build["status"] == "failed" else 0


def _bytes(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


@_client_command
def cmd_prune(args) -> int:
    hub, _ = _signed_in()
    body = {"project": args.project, "keep": args.keep, "dry_run": args.dry_run}
    report = hub.call("POST", "/v1/admin/kg/prune", body, timeout=PRUNE_TIMEOUT)
    if args.json:
        _print_json(report)
        return 0
    rows = [
        (p["project"], p["artifacts"], p["kept"], p["pruned"], _bytes(p["pruned_bytes"]), p["builds"])
        for p in report["projects"]
    ]
    if rows:
        _table(("PROJECT", "ARTIFACTS", "KEPT", "PRUNED", "PRUNED SIZE", "BUILDS"), rows)
    else:
        print("no project has a built graph on the hub")
    verb = "would delete" if report["dry_run"] else "deleted"
    print(
        f"{'Dry run, keeping' if report['dry_run'] else 'Kept'} the artifacts of the {report['keep']} newest graph(s) "
        f"of each project: {verb} {report['deleted']} object(s), {_bytes(report['deleted_bytes'])}."
    )
    if report["pending"]:
        print(f"{report['pending']} object(s) still wait to be deleted; the next prune tries again.", file=sys.stderr)
    return 0


def register_kg(hsub) -> None:
    from evo_agents.hub.cli import _positive

    kg = hsub.add_parser("kg", help="knowledge graphs on the hub: push runs, list and queue builds, prune old graphs")
    ksub = kg.add_subparsers(dest="kg_command", required=True)

    push_parser = ksub.add_parser("push", help="send the runs of a project's corpus that the hub does not have")
    push_parser.add_argument("--project", help="project name or harness path")
    push_parser.add_argument(
        "--all", action="store_true", help="every project in projects.json the hub takes a push for from you"
    )
    json_option(push_parser, returns_object("ok", "projects"))
    push_parser.set_defaults(func=cmd_push)

    builds = ksub.add_parser("builds", help="a project's builds on the hub, newest first, and the queued jobs")
    builds.add_argument("--project", required=True, help="project name on the hub")
    builds.add_argument("--limit", type=int, default=20, help="builds to show (default: 20, at most 100)")
    json_option(builds, returns_object("builds", "jobs", schema="Builds"))
    builds.set_defaults(func=cmd_builds)

    build = ksub.add_parser("build", help="queue a build of a project's graph (needs the writer role)")
    build.add_argument("--project", required=True, help="project name on the hub")
    build.add_argument("--wait", action="store_true", help="wait until the build has finished; exit 1 if it failed")
    build.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT, help="seconds --wait waits at most (default: 1800)"
    )
    json_option(build, returns_object("queued", "build", schema="Queued"))
    build.set_defaults(func=cmd_build)

    prune = ksub.add_parser(
        "prune", help="delete the artifacts of graphs older than each project's newest few (needs a hub admin)"
    )
    prune.add_argument("--project", help="project name on the hub (default: every project)")
    prune.add_argument(
        "--keep",
        type=_positive,
        help="newest graphs of each project whose artifact stays (default: the hub's EVO_HUB_KG_KEEP_ARTIFACTS, 3)",
    )
    prune.add_argument("--dry-run", action="store_true", help="say what would be deleted, and delete nothing")
    json_option(
        prune,
        returns_object("dry_run", "keep", "projects", "deleted", "deleted_bytes", "pending", schema="KgPruned"),
    )
    prune.set_defaults(func=cmd_prune)


# The hub backend of kg serve and kg query


class RemoteSession:
    """``Session.call`` answered by the hub: a tools/call to its /mcp for ``project`` through ``sink``
    (``evo_agents.hub.mcp_proxy.McpClient``). A hub that cannot be reached or refuses the call gives a tool error naming
    the hub."""

    def __init__(self, hub: Hub, project: str, sink: str):
        self.hub = hub
        self.project = project
        self.sink = sink

    def call(self, name: str, args: dict) -> dict:
        from evo_agents.hub.mcp_proxy import McpClient
        from evo_agents.kg.serve import TOOLS, _error

        if name not in {tool["name"] for tool in TOOLS}:
            return _error(f"unknown tool {name!r}")
        try:
            return McpClient(self.hub.url, self.hub.token, self.project, self.sink).call_tool(name, args or {})
        except HubError as exc:
            return _error(f"{exc} (hub {self.hub.url})")


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
