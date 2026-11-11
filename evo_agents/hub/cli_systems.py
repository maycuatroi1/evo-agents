"""``evo-agents hub system list|show|put|export|validate|delete|metrics|default|ops``: the agent systems of a project
(``docs/hub.md``, Agent systems; ``evo_agents.hub.system_spec``), over /v1/projects/{p}/systems.

A system is a YAML file of spec ``evo-agent-system/1``: its entry, agents, named graphs, curator and orchestrator on a
canvas, and the typed edges between them. ``export`` writes a system as such a file, with the revision it is at in a
``# if_revision: N`` line; ``put`` stores a file as the system's next revision, taking that line (or
``--if-revision``) as the revision it replaces, so a change made meanwhile is a refusal rather than lost. ``validate``
asks the hub what is wrong in a file, against the project, and writes nothing. Every member of the project reads its
systems; ``put`` and ``delete`` need the admin role on it. ``metrics`` prints the spread of the traces of a system,
or of every system of the project, over the last N days (cost, runs, deepest hop, budget refusals, overrun of
limits.trace_usd, Orchestrator threads, off design per edge), and reads only. ``default`` reads the project's default
system, or makes another one the default (admin). ``ops show|pause|resume|batches`` reads the ops of a system (its
spec's ``ops``, its pause, the batches of the project's queue) and pauses or resumes them (admin).

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
SUMMARY_KEYS = ("slug", "name", "revision", "entry_agent", "nodes", "edges", "updated_by", "updated_at", "default")
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
DEFAULT_KEYS = ("project", "slug", "name", "ops", "limits", "can_change")
OPS_KEYS = (
    "project",
    "slug",
    "default",
    "ops",
    "ops_daily_usd",
    "paused",
    "paused_at",
    "paused_reason",
    "spent_usd",
    "batches",
)
BATCH_KEYS = (
    "id",
    "key",
    "kind",
    "cause",
    "subject",
    "state",
    "items",
    "pending",
    "opened_at",
    "closes_at",
    "closed_at",
    "run_id",
    "recurring",
    "thread",
)
BATCH_STATES = ("open", "ready", "empty", "running", "done")
DISTRIBUTION_KEYS = ("system", "days", "since", "withheld", "truncated", "summary", "traces")
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
            s["slug"] + (" *" if s.get("default") else ""),
            s["name"],
            str(s["revision"]),
            "@" + s["entry_agent"] if s["entry_agent"] else "-",
            f"{s['nodes']}/{s['edges']}",
            when(s["updated_at"]),
        )
        for s in found
    ]
    table(("SYSTEM", "NAME", "REV", "ENTRY AGENT", "NODES/EDGES", "CHANGED"), rows)
    if any(s.get("default") for s in found):
        print("* the project's default system")
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


def _figure(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _print_distribution(found: dict, traces: bool) -> None:
    summary = found["summary"]
    usd, runs, hop = summary["usd"], summary["runs"], summary["max_hop"]
    print(f"system {found['system']}: {summary['traces']} traces since {when(found['since'])} UTC", end="")
    print(f", {found['withheld']} withheld" if found["withheld"] else "", end="")
    print(", truncated to the newest" if found["truncated"] else "")
    if not summary["traces"]:
        return
    print(
        f"  usd p50 {_figure(usd['p50'])} p95 {_figure(usd['p95'])} max {_figure(usd['max'])}; runs p50 "
        f"{_figure(runs['p50'], 1)} p95 {_figure(runs['p95'], 1)} max {_figure(runs['max'], 0)}; hop p95 "
        f"{_figure(hop['p95'], 1)} max {_figure(hop['max'], 0)}"
    )
    print(
        f"  over trace_usd {summary['over_budget']} (max {_figure(summary['max_over_usd'])} USD); budget refusals "
        f"{summary['budget_refusals']}; threads loop {summary['loop_threads']}, idle {summary['idle_threads']}, "
        f"rounds {summary['rounds_threads']}"
    )
    for edge in summary["edges"]:
        print(
            f"  edge {edge['from_node']} -> {edge['to_node']}: {edge['count']} crossings, {edge['off_design']} off "
            f"design ({edge['off_design_ratio']:.0%})"
        )
    if traces:
        rows = [
            (
                str(t["id"]),
                when(t["started_at"]),
                _figure(t["usd"]),
                str(t["runs"]),
                "-" if t["max_hop"] is None else str(t["max_hop"]),
                _figure(t["over_usd"]),
                str(t["budget_refusals"]),
                f"{t['loop_threads']}/{t['idle_threads']}/{t['rounds_threads']}",
                str(sum(edge["off_design"] for edge in t["edges"])),
                "open" if t["open"] else "",
            )
            for t in found["traces"]
        ]
        header = ("TRACE", "STARTED", "USD", "RUNS", "HOP", "OVER", "REFUSED", "LOOP/IDLE/ROUNDS", "OFF", "")
        table(header, rows)


@client_command
def cmd_metrics(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    slugs = [args.slug] if args.slug else [s["slug"] for s in hub.call("GET", _path(project)) or []]
    found = [call_object(hub, "GET", f"{_path(project, slug)}/metrics/traces?days={args.days}") for slug in slugs]
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no agent system.")
    for each in found:
        _print_distribution(each, args.traces)
    return 0


def _ops_settings(ops: dict | None) -> str:
    if not ops:
        return "-"
    state = "on" if ops.get("enabled") else "off"
    return f"{state}, a batch closes after {ops.get('debounce_minutes')} min or at {ops.get('max_items')} items"


@client_command
def cmd_default(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    path = f"/v1/projects/{quote(project, safe='')}/default-system"
    found = call_object(hub, "PUT", path, {"slug": args.slug}) if args.slug else call_object(hub, "GET", path)
    if args.json:
        print_json(found)
        return 0
    if not found.get("slug"):
        print(f"Project {project} has no default system: set one with `evo-agents hub system default SLUG`.")
        return 0
    print(f"default system of project {project}: {found['slug']} ({found['name']})")
    print(f"  ops: {_ops_settings(found.get('ops'))}")
    print(f"  ops budget: {(found.get('limits') or {}).get('ops_daily_usd')} USD in 24 hours")
    return 0


def _print_ops(found: dict) -> None:
    role = "the default system" if found["default"] else "not the default system: its ops take no event"
    print(f"system {found['slug']} of project {found['project']}: {role}")
    print(f"  ops: {_ops_settings(found['ops'])}; budget {found['ops_daily_usd']} USD in 24 hours")
    if found["paused"]:
        print(f"  paused since {when(found['paused_at'])} UTC: {found['paused_reason'] or '-'}")
    if found["batches"]:
        print("  batches: " + ", ".join(f"{n} {state}" for state, n in found["batches"].items()))


def _print_batches(found: list[dict], slug: str) -> None:
    if not found:
        print(f"System {slug} has no batch.")
        return
    rows = [
        (str(b["id"]), b["state"], b["key"], f"{b['pending']}/{b['items']}", when(b["opened_at"]), when(b["closes_at"]))
        for b in found
    ]
    table(("BATCH", "STATE", "CAUSE KEY", "PENDING/ITEMS", "OPENED", "CLOSES"), rows)


@client_command
def cmd_ops(args) -> int:
    hub, _ = signed_in()
    path = _path(project_of(args), args.slug) + "/ops"
    if args.ops_command == "batches":
        query = f"?limit={args.limit}" + (f"&state={args.state}" if args.state else "")
        found = hub.call("GET", f"{path}/batches{query}") or []
        if args.json:
            print_json(found)
        else:
            _print_batches(found, args.slug)
        return 0
    if args.ops_command == "pause":
        found = call_object(hub, "POST", f"{path}/pause", {"reason": args.reason})
    elif args.ops_command == "resume":
        found = call_object(hub, "POST", f"{path}/resume", {})
    else:
        found = call_object(hub, "GET", path)
    if args.json:
        print_json(found)
        return 0
    _print_ops(found)
    return 0


def _register_ops(ssub) -> None:
    default = ssub.add_parser(
        "default",
        help="the project's default system, or make SLUG the default (admin)",
        description="A project has at most one default system: its Orchestrator handles the project's operations "
        "events when its spec turns ops on. With no SLUG it prints the default; with one it makes that system the "
        "default (the admin role on the project).",
    )
    default.add_argument("slug", metavar="SLUG", nargs="?", help="the system to make the default")
    _project(default)
    json_option(default, returns_object(*DEFAULT_KEYS, schema="DefaultSystem"))
    default.set_defaults(func=cmd_default)

    ops = ssub.add_parser(
        "ops",
        help="show, pause or resume the ops of a system, or list the batches of its queue",
        description="show: the ops of a system's spec, its budget, its pause and its batches by state; pause and "
        "resume (admin): a paused system's queue still gathers, and a closed batch waits; batches: the batches of "
        "the project's queue, the newest first.",
    )
    osub = ops.add_subparsers(dest="ops_command", required=True)
    for name, text in (
        ("show", "the ops of a system"),
        ("pause", "pause the ops of a system (admin): closed batches wait"),
        ("resume", "resume the ops of a system (admin)"),
    ):
        each = osub.add_parser(name, help=text)
        _slug(each)
        _project(each)
        if name == "pause":
            each.add_argument("--reason", help="why, shown with the pause")
        json_option(each, returns_object(*OPS_KEYS, schema="SystemOps"))
        each.set_defaults(func=cmd_ops)
    batches = osub.add_parser("batches", help="the batches of the project's queue, the newest first")
    _slug(batches)
    _project(batches)
    batches.add_argument("--state", choices=BATCH_STATES, help="only the batches in this state")
    batches.add_argument("--limit", type=int, default=50, metavar="N", help="at most N batches (50)")
    json_option(batches, returns_array(*BATCH_KEYS, schema="OpsBatch"))
    batches.set_defaults(func=cmd_ops)


def _project(parser) -> None:
    parser.add_argument("--project", help=PROJECT_HELP)


def _slug(parser) -> None:
    parser.add_argument("slug", metavar="SLUG", help="the system's slug")


def register(hsub) -> None:
    """``hub system``, under the subparsers ``hsub`` of ``hub``."""
    system = hsub.add_parser(
        "system",
        help="list, show, put, export, validate, delete or measure the agent systems of a project, and their ops",
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

    metrics = ssub.add_parser(
        "metrics",
        help="the spread of the traces of a system, or of every system of the project, over the last N days",
        description="Per system: p50, p95 and max of the cost, the runs and the deepest hop of its traces, the traces "
        "past limits.trace_usd, the budget refusals, the loop, idle and rounds threads and the off-design ratio per "
        "edge, over the traces you may read. It reads only.",
    )
    metrics.add_argument("slug", metavar="SLUG", nargs="?", help="the system's slug (default: every system)")
    _project(metrics)
    metrics.add_argument("--days", type=int, default=30, metavar="N", help="the traces of the last N days (30)")
    metrics.add_argument("--traces", action="store_true", help="also print a line for each trace")
    json_option(metrics, returns_array(*DISTRIBUTION_KEYS, schema="TraceDistribution"))
    metrics.set_defaults(func=cmd_metrics)

    _register_ops(ssub)
