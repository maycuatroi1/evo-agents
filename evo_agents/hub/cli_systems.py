"""``evo-agents hub system list|show|put|export|validate|delete``: the agent systems of a project (``docs/hub.md``,
Agent systems; ``evo_agents.hub.system_spec``), over /v1/projects/{p}/systems.

A system is a YAML file of spec ``evo-agent-system/1``: its entry, agents, named graphs, curator and orchestrator on a
canvas, and the typed edges between them. ``export`` writes a system as such a file, with the revision it is at in a
``# if_revision: N`` line; ``put`` stores a file as the system's next revision, taking that line (or
``--if-revision``) as the revision it replaces, so a change made meanwhile is a refusal rather than lost. ``validate``
asks the hub what is wrong in a file, against the project, and writes nothing. Every member of the project reads its
systems; ``put`` and ``delete`` need the admin role on it.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library and PyYAML only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub import system_spec
from evo_agents.hub.cli_agents import PROJECT_HELP, project_of
from evo_agents.hub.cli_client import call_object, client_command, print_json, signed_in, table, when
from evo_agents.hub.client import EXIT_FAILED, EXIT_USAGE, HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

# The keys of the hub's answers in --json (the contract, `evo-agents hub contract print`).
SUMMARY_KEYS = ("slug", "name", "revision", "entry_agent", "nodes", "edges", "updated_by", "updated_at")
SYSTEM_KEYS = (
    "project",
    "slug",
    "name",
    "revision",
    "latest_revision",
    "spec",
    "pins",
    "problems",
    "entry_agent",
    "can_change",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)
CHECKED_KEYS = ("valid", "spec", "problems")
IF_REVISION = re.compile(r"^#\s*if_revision:\s*(\d+)\s*$", re.M)


def _path(project: str, slug: str | None = None) -> str:
    path = f"/v1/projects/{quote(project, safe='')}/systems"
    return path if slug is None else f"{path}/{quote(slug, safe='')}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _read(path: str) -> tuple[dict, str]:
    """The spec in the YAML file ``path`` (``-`` for stdin), and its text."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return system_spec.from_yaml(text), text


def _problem_line(problem: dict) -> str:
    where = problem.get("path") or "spec"
    if problem.get("node"):
        where += f" (node {problem['node']})"
    elif problem.get("edge") is not None:
        where += f" (edge {problem['edge']})"
    return f"  {problem.get('level', 'error')}: {where}: {problem.get('message', '')}"


def _print_problems(problems: list[dict], stream=sys.stdout) -> None:
    for problem in problems:
        print(_problem_line(problem), file=stream)


def _show(system: dict) -> None:
    latest = "" if system["revision"] == system["latest_revision"] else f" (latest {system['latest_revision']})"
    print(
        f"system {system['slug']} ({system['name']}) of project {system['project']}, revision {system['revision']}"
        f"{latest}"
    )
    spec = system["spec"]
    if spec.get("purpose"):
        print(f"  purpose: {spec['purpose']}")
    print(f"  entry agent: {'@' + system['entry_agent'] if system['entry_agent'] else 'none'}")
    limits = spec.get("limits") or {}
    print(
        f"  limits: {limits.get('trace_usd')} USD and {limits.get('trace_runs')} runs a trace, "
        f"{limits.get('daily_usd')} USD a day"
    )
    for node in spec.get("nodes") or ():
        ref = f" @{node['profile']} v{node.get('version')}" if node.get("profile") else ""
        ref += f" graph {node['graph']}" if node.get("graph") else ""
        print(f"  node {node['id']}: {node['kind']}{ref}")
    for edge in spec.get("edges") or ():
        about = f": {edge['description']}" if edge.get("description") else ""
        print(f"  edge {edge['from']} -{edge['kind']}-> {edge['to']}{about}")
    for pin in system["pins"]:
        if pin["outdated"] or pin["deleted"]:
            state = "deleted" if pin["deleted"] else f"version {pin['latest_version']} is out"
            print(f"  pin {pin['node']}: @{pin['profile']} v{pin['version']}, {state}")
    _print_problems(system["problems"])
    print(f"  changed by {system['updated_by'] or '-'} at {when(system['updated_at'])} UTC")


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no agent system.")
        return 0
    rows = [
        (
            s["slug"],
            s["name"],
            str(s["revision"]),
            "@" + s["entry_agent"] if s["entry_agent"] else "-",
            f"{s['nodes']}/{s['edges']}",
            when(s["updated_at"]),
        )
        for s in found
    ]
    table(("SYSTEM", "NAME", "REV", "ENTRY AGENT", "NODES/EDGES", "CHANGED"), rows)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    path = _path(project_of(args), args.slug) + (f"?revision={args.revision}" if args.revision else "")
    found = call_object(hub, "GET", path)
    if args.json:
        print_json(found)
        return 0
    _show(found)
    return 0


@client_command
def cmd_export(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    path = _path(project, args.slug) + (f"?revision={args.revision}" if args.revision else "")
    found = call_object(hub, "GET", path)
    head = (
        f"# agent system {found['slug']} of project {project}, revision {found['revision']}\n"
        f"# if_revision: {found['latest_revision']}\n"
    )
    text = head + system_spec.to_yaml(found["slug"], found["spec"])
    if args.output in (None, "-"):
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote system {found['slug']} at revision {found['revision']} to {args.output}", file=sys.stderr)
    return 0


def _slug_of(args, spec: dict) -> str | None:
    named = spec.get("slug")
    return args.slug or (named if isinstance(named, str) else None)


@client_command
def cmd_put(args) -> int:
    try:
        spec, text = _read(args.file)
    except (OSError, ValueError) as exc:
        return _usage(str(exc))
    slug = _slug_of(args, spec)
    if not slug:
        return _usage("which system? give --slug, or slug: in the file")
    if (why := system_spec.check_slug(slug)) is not None:
        return _usage(why)
    if args.slug and isinstance(spec.get("slug"), str) and spec["slug"] != args.slug:
        return _usage(f"the file names system {spec['slug']}, --slug {args.slug}")
    marked = IF_REVISION.search(text)
    if_revision = args.if_revision if args.if_revision is not None else int(marked[1]) if marked else None
    body = {"spec": {k: v for k, v in spec.items() if k != "slug"}, "if_revision": if_revision}
    hub, _ = signed_in()
    try:
        found = call_object(hub, "PUT", _path(project_of(args), slug), body)
    except HubError as exc:
        if exc.status == 422 and isinstance(exc.payload, dict) and exc.payload.get("detail"):
            print(f"error: {exc}", file=sys.stderr)
            _print_problems(exc.payload["detail"], sys.stderr)
            return EXIT_FAILED
        raise
    if args.json:
        print_json(found)
        return 0
    if not found["changed"]:
        print(f"system {slug} is at revision {found['revision']} already: nothing changed")
    else:
        print(f"{'created' if found['created'] else 'stored'} system {slug} at revision {found['revision']}")
    _print_problems(found["problems"])
    return 0


@client_command
def cmd_validate(args) -> int:
    try:
        spec, _ = _read(args.file)
    except (OSError, ValueError) as exc:
        return _usage(str(exc))
    hub, _ = signed_in()
    body = {"spec": {k: v for k, v in spec.items() if k != "slug"}, "slug": _slug_of(args, spec)}
    found = call_object(hub, "POST", _path(project_of(args)) + "/validate", body)
    if args.json:
        print_json(found)
    else:
        print("valid: put would store it" if found["valid"] else "not valid: put would refuse it")
        _print_problems(found["problems"])
    return 0 if found["valid"] else EXIT_FAILED


@client_command
def cmd_delete(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "DELETE", _path(project_of(args), args.slug))
    if args.json:
        print_json(found)
        return 0
    print(f"deleted system {found['deleted']} of project {found['project']}; its agents, graphs and runs stay")
    return 0


def _project(parser) -> None:
    parser.add_argument("--project", help=PROJECT_HELP)


def _slug(parser) -> None:
    parser.add_argument("slug", metavar="SLUG", help="the system's slug")


def register(hsub) -> None:
    """``hub system``, under the subparsers ``hsub`` of ``hub``."""
    system = hsub.add_parser(
        "system",
        help="list, show, put, export, validate or delete the agent systems of a project",
        description="An agent system is a project's agents, named graphs, curator and orchestrator on a canvas, with "
        "typed edges between them (evo-agent-system/1, a YAML file). Members read them; admins write them.",
    )
    ssub = system.add_subparsers(dest="system_command", required=True)

    listed = ssub.add_parser("list", help="the agent systems of a project")
    _project(listed)
    json_option(listed, returns_array(*SUMMARY_KEYS, schema="SystemSummary"))
    listed.set_defaults(func=cmd_list)

    show = ssub.add_parser("show", help="one agent system: its nodes, edges, pins and what is wrong in it")
    _slug(show)
    _project(show)
    show.add_argument("--revision", type=int, metavar="N", help="a revision of it (default: the latest)")
    json_option(show, returns_object(*SYSTEM_KEYS, schema="SystemOut"))
    show.set_defaults(func=cmd_show)

    put = ssub.add_parser("put", help="create an agent system from a YAML file, or store it as its next revision")
    put.add_argument("file", metavar="FILE", help="the YAML file, - for stdin")
    put.add_argument("--slug", help="the system (default: slug: in the file)")
    _project(put)
    put.add_argument(
        "--if-revision",
        type=int,
        metavar="N",
        help="the revision it replaces (default: the file's '# if_revision: N' line; none creates it)",
    )
    written = (*SYSTEM_KEYS, "created", "changed")
    json_option(put, returns_object(*written, schema="SystemWritten"))
    put.set_defaults(func=cmd_put)

    export = ssub.add_parser("export", help="write an agent system as a YAML file that put reads back")
    _slug(export)
    _project(export)
    export.add_argument("--revision", type=int, metavar="N", help="a revision of it (default: the latest)")
    export.add_argument("-o", "--output", metavar="FILE", help="the file to write (default: stdout)")
    export.set_defaults(func=cmd_export)

    validate = ssub.add_parser("validate", help="what is wrong in a YAML file of a system, against the project")
    validate.add_argument("file", metavar="FILE", help="the YAML file, - for stdin")
    validate.add_argument("--slug", help="the system it would replace (default: slug: in the file)")
    _project(validate)
    json_option(validate, returns_object(*CHECKED_KEYS, schema="SystemChecked"))
    validate.set_defaults(func=cmd_validate)

    delete = ssub.add_parser("delete", help="delete an agent system with its revisions; its agents and graphs stay")
    _slug(delete)
    _project(delete)
    json_option(delete, returns_object("project", "deleted", schema="SystemDeleted"))
    delete.set_defaults(func=cmd_delete)
