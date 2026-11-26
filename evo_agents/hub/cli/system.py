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

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Systems` is
the gateway to the systems of a project and :class:`DefaultSystem` to its default one; :func:`system_lines`,
:func:`distribution_lines` and :func:`ops_lines` say what the hub answered to a person.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from evo_agents.clikit import (
    Arg,
    Group,
    Result,
    Stderr,
    Table,
    Text,
    UsageError,
    Verbatim,
    View,
    Views,
    returns_array,
    returns_object,
)
from evo_agents.hub import system_spec
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, call_object, when
from evo_agents.hub.client import EXIT_FAILED, HubError

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
NO_SYSTEM = "Project {project} has no agent system."


class Systems(ProjectResource):
    """The agent systems of a project: /v1/projects/{p}/systems and the routes below it (Gateway)."""

    collection = "systems"

    def object(self, method: str, *parts: Any, body: dict | None = None, **query: Any) -> dict:
        """The hub's answer, for a route that answers a JSON object; HubError when it did not."""
        return call_object(self.hub, method, self.path(*parts, **query), body)

    def list(self) -> list[dict]:
        return self.call("GET")

    def get(self, slug: str, revision: int | None) -> dict:
        return self.object("GET", slug, revision=revision or None)

    def put(self, slug: str, body: dict) -> dict:
        return self.object("PUT", slug, body=body)

    def validate(self, body: dict) -> dict:
        return self.object("POST", "validate", body=body)

    def delete(self, slug: str) -> dict:
        return self.object("DELETE", slug)

    def metrics(self, slug: str, days: int) -> dict:
        return self.object("GET", slug, "metrics", "traces", days=days)

    def ops(self, slug: str) -> dict:
        return self.object("GET", slug, "ops")

    def pause(self, slug: str, reason: str | None) -> dict:
        return self.object("POST", slug, "ops", "pause", body={"reason": reason})

    def resume(self, slug: str) -> dict:
        return self.object("POST", slug, "ops", "resume", body={})

    def batches(self, slug: str, limit: int, state: str | None) -> list[dict]:
        return self.call("GET", slug, "ops", "batches", limit=limit, state=state) or []


class DefaultSystem(ProjectResource):
    """The default system of a project: GET and PUT /v1/projects/{p}/default-system (Gateway)."""

    collection = "default-system"

    def get(self) -> dict:
        return call_object(self.hub, "GET", self.path())

    def put(self, slug: str) -> dict:
        return call_object(self.hub, "PUT", self.path(), {"slug": slug})


def read_spec(path: str) -> tuple[dict, str]:
    """The spec in the YAML file ``path`` (``-`` for stdin), and its text."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return system_spec.from_yaml(text), text


def problem_line(problem: dict) -> str:
    where = problem.get("path") or "spec"
    if problem.get("node"):
        where += f" (node {problem['node']})"
    elif problem.get("edge") is not None:
        where += f" (edge {problem['edge']})"
    return f"  {problem.get('level', 'error')}: {where}: {problem.get('message', '')}"


def runs_limit(count) -> str:
    """limits.trace_runs as ``show`` prints it: "N runs", or "any number of runs" for UNLIMITED_RUNS."""
    return "any number of runs" if count == system_spec.UNLIMITED_RUNS else f"{count} runs"


def usd_limit(usd) -> str:
    """A limit in USD as ``show`` prints it: "N USD", or "no limit" for UNLIMITED_USD and a spec that sets none."""
    return "no limit" if usd in (None, system_spec.UNLIMITED_USD) else f"{usd} USD"


def system_lines(system: dict) -> list[str]:
    """One system as ``show`` prints it: its nodes, edges, outdated pins and problems."""
    latest = "" if system["revision"] == system["latest_revision"] else f" (latest {system['latest_revision']})"
    lines = [
        f"system {system['slug']} ({system['name']}) of project {system['project']}, revision {system['revision']}"
        f"{latest}"
    ]
    spec = system["spec"]
    if spec.get("purpose"):
        lines.append(f"  purpose: {spec['purpose']}")
    lines.append(f"  entry agent: {'@' + system['entry_agent'] if system['entry_agent'] else 'none'}")
    limits = spec.get("limits") or {}
    lines.append(
        f"  limits: {usd_limit(limits.get('trace_usd'))} and {runs_limit(limits.get('trace_runs'))} a trace, "
        f"{usd_limit(limits.get('daily_usd'))} a day"
    )
    for node in spec.get("nodes") or ():
        ref = f" @{node['profile']} v{node.get('version')}" if node.get("profile") else ""
        ref += f" graph {node['graph']}" if node.get("graph") else ""
        lines.append(f"  node {node['id']}: {node['kind']}{ref}")
    for edge in spec.get("edges") or ():
        about = f": {edge['description']}" if edge.get("description") else ""
        lines.append(f"  edge {edge['from']} -{edge['kind']}-> {edge['to']}{about}")
    for pin in system["pins"]:
        if pin["outdated"] or pin["deleted"]:
            state = "deleted" if pin["deleted"] else f"version {pin['latest_version']} is out"
            lines.append(f"  pin {pin['node']}: @{pin['profile']} v{pin['version']}, {state}")
    lines.extend(problem_line(problem) for problem in system["problems"])
    lines.append(f"  changed by {system['updated_by'] or '-'} at {when(system['updated_at'])} UTC")
    return lines


SLUG = Arg("slug", metavar="SLUG", help="the system's slug")
REVISION = Arg("--revision", type=int, metavar="N", help="a revision of it (default: the latest)")
FILE = Arg("file", metavar="FILE", help="the YAML file, - for stdin")


class SystemList(HubCommand):
    name = "list"
    help = "the agent systems of a project"
    arguments = (PROJECT,)
    output = returns_array(*SUMMARY_KEYS, schema="SystemSummary")
    columns = ("SYSTEM", "NAME", "REV", "ENTRY AGENT", "NODES/EDGES", "CHANGED")

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).list()
        if not found:
            return Result(found, Text(NO_SYSTEM.format(project=ctx.project)))
        view: View = Table(self.columns, [self.row(s) for s in found])
        if any(s.get("default") for s in found):
            view = Views(view, Text("* the project's default system"))
        return Result(found, view)

    @staticmethod
    def row(s: dict) -> tuple:
        return (
            s["slug"] + (" *" if s.get("default") else ""),
            s["name"],
            str(s["revision"]),
            "@" + s["entry_agent"] if s["entry_agent"] else "-",
            f"{s['nodes']}/{s['edges']}",
            when(s["updated_at"]),
        )


class SystemShow(HubCommand):
    name = "show"
    help = "one agent system: its nodes, edges, pins and what is wrong in it"
    arguments = (SLUG, PROJECT, REVISION)
    output = returns_object(*SYSTEM_KEYS, schema="SystemOut")

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).get(ctx.args.slug, ctx.args.revision)
        return Result(found, Text(*system_lines(found)))


def slug_of(args: argparse.Namespace, spec: dict) -> str | None:
    named = spec.get("slug")
    return args.slug or (named if isinstance(named, str) else None)


def spec_body(spec: dict) -> dict:
    """The spec as the hub takes it: without ``slug``, which names the route."""
    return {key: value for key, value in spec.items() if key != "slug"}


def read_file(args: argparse.Namespace) -> tuple[dict, str]:
    """The spec and text of ``args.file``; UsageError when it does not read."""
    try:
        return read_spec(args.file)
    except (OSError, ValueError) as exc:
        raise UsageError(str(exc)) from None


class Refusal(Exception):
    """The hub refused a spec with the problems it found (422 with a detail): each problem goes to stderr."""

    def __init__(self, error: HubError, problems: list[dict]):
        super().__init__(str(error))
        self.error, self.problems = error, problems


class SystemPut(HubCommand):
    name = "put"
    help = "create an agent system from a YAML file, or store it as its next revision"
    arguments = (
        FILE,
        Arg("--slug", help="the system (default: slug: in the file)"),
        PROJECT,
        Arg(
            "--if-revision",
            type=int,
            metavar="N",
            help="the revision it replaces (default: the file's '# if_revision: N' line; none creates it)",
        ),
    )
    output = returns_object(*SYSTEM_KEYS, "created", "changed", schema="SystemWritten")

    @staticmethod
    def body(args: argparse.Namespace) -> tuple[str, dict]:
        """The slug and the body of PUT, read from the file before the hub is asked; UsageError when they do not."""
        spec, text = read_file(args)
        slug = slug_of(args, spec)
        if not slug:
            raise UsageError("which system? give --slug, or slug: in the file")
        if (why := system_spec.check_slug(slug)) is not None:
            raise UsageError(why)
        if args.slug and isinstance(spec.get("slug"), str) and spec["slug"] != args.slug:
            raise UsageError(f"the file names system {spec['slug']}, --slug {args.slug}")
        marked = IF_REVISION.search(text)
        if_revision = args.if_revision if args.if_revision is not None else int(marked[1]) if marked else None
        return slug, {"spec": spec_body(spec), "if_revision": if_revision}

    def run(self, ctx: HubContext) -> Result:
        slug, body = self.body(ctx.args)
        try:
            found = Systems.of(ctx).put(slug, body)
        except HubError as exc:
            if exc.status == 422 and isinstance(exc.payload, dict) and exc.payload.get("detail"):
                raise Refusal(exc, exc.payload["detail"]) from exc
            raise
        if not found["changed"]:
            said = f"system {slug} is at revision {found['revision']} already: nothing changed"
        else:
            said = f"{'created' if found['created'] else 'stored'} system {slug} at revision {found['revision']}"
        return Result(found, Text(said, *(problem_line(problem) for problem in found["problems"])))

    def failed(self, exc: Exception) -> int | None:
        if not isinstance(exc, Refusal):
            return super().failed(exc)
        print(f"error: {exc.error}", file=sys.stderr)
        for problem in exc.problems:
            print(problem_line(problem), file=sys.stderr)
        return EXIT_FAILED


class SystemExport(HubCommand):
    name = "export"
    help = "write an agent system as a YAML file that put reads back"
    arguments = (
        SLUG,
        PROJECT,
        REVISION,
        Arg("-o", "--output", metavar="FILE", help="the file to write (default: stdout)"),
    )

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Systems.of(ctx).get(args.slug, args.revision)
        head = (
            f"# agent system {found['slug']} of project {ctx.project}, revision {found['revision']}\n"
            f"# if_revision: {found['latest_revision']}\n"
        )
        text = head + system_spec.to_yaml(found["slug"], found["spec"])
        if args.output in (None, "-"):
            return Result(found, Verbatim(text))
        Path(args.output).write_text(text, encoding="utf-8")
        said = f"wrote system {found['slug']} at revision {found['revision']} to {args.output}"
        return Result(found, Stderr(Text(said)))


class SystemValidate(HubCommand):
    name = "validate"
    help = "what is wrong in a YAML file of a system, against the project"
    arguments = (FILE, Arg("--slug", help="the system it would replace (default: slug: in the file)"), PROJECT)
    output = returns_object(*CHECKED_KEYS, schema="SystemChecked")

    def run(self, ctx: HubContext) -> Result:
        spec, _ = read_file(ctx.args)
        systems = Systems.of(ctx)
        found = systems.validate({"spec": spec_body(spec), "slug": slug_of(ctx.args, spec)})
        said = "valid: put would store it" if found["valid"] else "not valid: put would refuse it"
        view = Text(said, *(problem_line(problem) for problem in found["problems"]))
        return Result(found, view, 0 if found["valid"] else EXIT_FAILED)


class SystemDelete(HubCommand):
    name = "delete"
    help = "delete an agent system with its revisions; its agents and graphs stay"
    arguments = (SLUG, PROJECT)
    output = returns_object("project", "deleted", schema="SystemDeleted")

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).delete(ctx.args.slug)
        said = f"deleted system {found['deleted']} of project {found['project']}; its agents, graphs and runs stay"
        return Result(found, Text(said))


def figure(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


TRACE_COLUMNS = ("TRACE", "STARTED", "USD", "RUNS", "HOP", "OVER", "REFUSED", "LOOP/IDLE/ROUNDS", "OFF", "")


def trace_row(t: dict) -> tuple:
    return (
        str(t["id"]),
        when(t["started_at"]),
        figure(t["usd"]),
        str(t["runs"]),
        "-" if t["max_hop"] is None else str(t["max_hop"]),
        figure(t["over_usd"]),
        str(t["budget_refusals"]),
        f"{t['loop_threads']}/{t['idle_threads']}/{t['rounds_threads']}",
        str(sum(edge["off_design"] for edge in t["edges"])),
        "open" if t["open"] else "",
    )


def distribution_lines(found: dict) -> list[str]:
    """The spread of one system's traces, as ``metrics`` prints it above the table of traces."""
    summary = found["summary"]
    usd, runs, hop = summary["usd"], summary["runs"], summary["max_hop"]
    lines = [
        f"system {found['system']}: {summary['traces']} traces since {when(found['since'])} UTC"
        + (f", {found['withheld']} withheld" if found["withheld"] else "")
        + (", truncated to the newest" if found["truncated"] else "")
    ]
    if not summary["traces"]:
        return lines
    lines.append(
        f"  usd p50 {figure(usd['p50'])} p95 {figure(usd['p95'])} max {figure(usd['max'])}; runs p50 "
        f"{figure(runs['p50'], 1)} p95 {figure(runs['p95'], 1)} max {figure(runs['max'], 0)}; hop p95 "
        f"{figure(hop['p95'], 1)} max {figure(hop['max'], 0)}"
    )
    lines.append(
        f"  over trace_usd {summary['over_budget']} (max {figure(summary['max_over_usd'])} USD); budget refusals "
        f"{summary['budget_refusals']}; threads loop {summary['loop_threads']}, idle {summary['idle_threads']}, "
        f"rounds {summary['rounds_threads']}"
    )
    lines.extend(
        f"  edge {edge['from_node']} -> {edge['to_node']}: {edge['count']} crossings, {edge['off_design']} off "
        f"design ({edge['off_design_ratio']:.0%})"
        for edge in summary["edges"]
    )
    return lines


def distribution_view(found: dict, traces: bool) -> View:
    head = Text(*distribution_lines(found))
    if not traces or not found["summary"]["traces"]:
        return head
    return Views(head, Table(TRACE_COLUMNS, [trace_row(t) for t in found["traces"]], bare=True))


class SystemMetrics(HubCommand):
    name = "metrics"
    help = "the spread of the traces of a system, or of every system of the project, over the last N days"
    parser_options = {
        "description": "Per system: p50, p95 and max of the cost, the runs and the deepest hop of its traces, the "
        "traces past limits.trace_usd, the budget refusals, the loop, idle and rounds threads and the off-design ratio "
        "per edge, over the traces you may read. It reads only."
    }
    arguments = (
        Arg("slug", metavar="SLUG", nargs="?", help="the system's slug (default: every system)"),
        PROJECT,
        Arg("--days", type=int, default=30, metavar="N", help="the traces of the last N days (30)"),
        Arg("--traces", action="store_true", help="also print a line for each trace"),
    )
    output = returns_array(*DISTRIBUTION_KEYS, schema="TraceDistribution")

    def run(self, ctx: HubContext) -> Result:
        args, systems = ctx.args, Systems.of(ctx)
        slugs = [args.slug] if args.slug else [s["slug"] for s in systems.list() or []]
        found = [systems.metrics(slug, args.days) for slug in slugs]
        if not found:
            return Result(found, Text(NO_SYSTEM.format(project=ctx.project)))
        return Result(found, Views(*(distribution_view(each, args.traces) for each in found)))


def ops_settings(ops: dict | None) -> str:
    if not ops:
        return "-"
    state = "on" if ops.get("enabled") else "off"
    quiet = ops.get("quiet_workers")
    muted = f", run.queued_long muted for workers {', '.join(map(str, quiet))}" if quiet else ""
    return f"{state}, a batch closes after {ops.get('debounce_minutes')} min or at {ops.get('max_items')} items{muted}"


class SystemDefault(HubCommand):
    name = "default"
    help = "the project's default system, or make SLUG the default (admin)"
    parser_options = {
        "description": "A project has at most one default system: its Orchestrator handles the project's operations "
        "events when its spec turns ops on. With no SLUG it prints the default; with one it makes that system the "
        "default (the admin role on the project)."
    }
    arguments = (Arg("slug", metavar="SLUG", nargs="?", help="the system to make the default"), PROJECT)
    output = returns_object(*DEFAULT_KEYS, schema="DefaultSystem")

    def run(self, ctx: HubContext) -> Result:
        default = DefaultSystem.of(ctx)
        found = default.put(ctx.args.slug) if ctx.args.slug else default.get()
        project = ctx.project
        if not found.get("slug"):
            said = f"Project {project} has no default system: set one with `evo-agents hub system default SLUG`."
            return Result(found, Text(said))
        return Result(
            found,
            Text(
                f"default system of project {project}: {found['slug']} ({found['name']})",
                f"  ops: {ops_settings(found.get('ops'))}",
                f"  ops budget: {usd_limit((found.get('limits') or {}).get('ops_daily_usd'))} in 24 hours",
            ),
        )


def ops_lines(found: dict) -> list[str]:
    role = "the default system" if found["default"] else "not the default system: its ops take no event"
    lines = [
        f"system {found['slug']} of project {found['project']}: {role}",
        f"  ops: {ops_settings(found['ops'])}; budget {usd_limit(found['ops_daily_usd'])} in 24 hours",
    ]
    if found["paused"]:
        lines.append(f"  paused since {when(found['paused_at'])} UTC: {found['paused_reason'] or '-'}")
    if found["batches"]:
        lines.append("  batches: " + ", ".join(f"{n} {state}" for state, n in found["batches"].items()))
    return lines


def ops_result(ctx: HubContext, found: dict) -> Result:
    """The ops of a system; with --json the answer alone, its lines not even made, as the command always did."""
    return Result(found) if ctx.args.json else Result(found, Text(*ops_lines(found)))


OPS_OUTPUT = returns_object(*OPS_KEYS, schema="SystemOps")


class OpsShow(HubCommand):
    name = "show"
    help = "the ops of a system"
    arguments = (SLUG, PROJECT)
    output = OPS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).ops(ctx.args.slug)
        return ops_result(ctx, found)


class OpsPause(HubCommand):
    name = "pause"
    help = "pause the ops of a system (admin): closed batches wait"
    arguments = (SLUG, PROJECT, Arg("--reason", help="why, shown with the pause"))
    output = OPS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).pause(ctx.args.slug, ctx.args.reason)
        return ops_result(ctx, found)


class OpsResume(HubCommand):
    name = "resume"
    help = "resume the ops of a system (admin)"
    arguments = (SLUG, PROJECT)
    output = OPS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Systems.of(ctx).resume(ctx.args.slug)
        return ops_result(ctx, found)


class OpsBatches(HubCommand):
    name = "batches"
    help = "the batches of the project's queue, the newest first"
    arguments = (
        SLUG,
        PROJECT,
        Arg("--state", choices=BATCH_STATES, help="only the batches in this state"),
        Arg("--limit", type=int, default=50, metavar="N", help="at most N batches (50)"),
    )
    output = returns_array(*BATCH_KEYS, schema="OpsBatch")
    columns = ("BATCH", "STATE", "CAUSE KEY", "PENDING/ITEMS", "OPENED", "CLOSES")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Systems.of(ctx).batches(args.slug, args.limit, args.state)
        return Result(found, Table(self.columns, [self.row(b) for b in found], f"System {args.slug} has no batch."))

    @staticmethod
    def row(b: dict) -> tuple:
        return (
            str(b["id"]),
            b["state"],
            b["key"],
            f"{b['pending']}/{b['items']}",
            when(b["opened_at"]),
            when(b["closes_at"]),
        )


OPS = Group(
    "ops",
    "show, pause or resume the ops of a system, or list the batches of its queue",
    [OpsShow(), OpsPause(), OpsResume(), OpsBatches()],
    description="show: the ops of a system's spec, its budget, its pause and its batches by state; pause and "
    "resume (admin): a paused system's queue still gathers, and a closed batch waits; batches: the batches of "
    "the project's queue, the newest first.",
)
SYSTEM = Group(  # hub system
    "system",
    "list, show, put, export, validate, delete or measure the agent systems of a project, and their ops",
    [
        SystemList(),
        SystemShow(),
        SystemPut(),
        SystemExport(),
        SystemValidate(),
        SystemDelete(),
        SystemMetrics(),
        SystemDefault(),
        OPS,
    ],
    description="An agent system is a project's agents, named graphs, curator and orchestrator on a canvas, with "
    "typed edges between them (evo-agent-system/1, a YAML file). Members read them; admins write them.",
)
