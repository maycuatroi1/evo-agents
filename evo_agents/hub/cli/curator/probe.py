"""``evo-agents hub curator probe list|show|add|edit|retire|requeue|report``: the probes of the Curator's proposals and
changes (``evo_agents.hub.probes``; GET, POST .../curator/probes, PATCH .../curator/probes/{id}, POST
.../curator/probes/{id}/retire, POST .../curator/probes/{id}/requeue and GET .../curator/probe-report).

``probe list`` lists the probes the caller may read, newest first, by ``--change``, ``--proposal`` and ``--state``,
a core plugin's probe with its plugin and rule key (RULE, ``plugin:ref``);
``probe show`` prints one. Their verify is the answer key: the hub sends it to the project's admins alone, and prints
``(hidden)`` for the others. ``probe add`` (admins) adds one to ``--change`` or ``--proposal``: the ``--repo``, the base
``--commit`` (none: the tip of the default branch), the task (``--task``, or ``--task-file``, ``-`` for stdin) and the
one-line ``--verify``. ``probe edit`` (admins) changes the ``--repo``, ``--commit``, task or ``--verify`` of an active
one, at least one of them. ``probe retire`` (admins) retires one, with ``--note``. ``probe requeue`` (admins) puts the
rows of the ISO week in progress of an active probe that are stale, failed or unmeasured back to queued with no attempt,
so that the next night measures them again, and prints the rows it requeued and those it kept. ``probe report`` prints the weekly report
of ``--weeks`` ISO weeks, the newest first (the week of ``--week``, default the one in progress): each week's cost
against ``probes.budget_usd``, each probe on each runtime and model, arm on and arm off (failed of sessions), its
status and the week before's, and where its arms ran (ARMS AT: ``tip``, the tip of the default branch, or ``change``,
the change's own commits when its diff no longer comes off the tip; ``--json`` adds each arm's 1 - pass^k,
``recurrence``); then the merged changes with no active probe
whose proposal changes a file a probe arm places, or born of a correction, with why, the fix proposals of recurring probes with their state, and the probes
written in place of probes not reproduced or stale.

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Probes` is
the gateway to the routes, :func:`probe_body` and :func:`probe_edits` turn the options of ``add`` and ``edit`` into
their bodies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.client import HubError
from evo_agents.hub.probes import MAX_TASK_BYTES, STATES

PROBE_KEYS = (
    "id",
    "project",
    "proposal_id",
    "change_id",
    "repo",
    "commit",
    "task",
    "verify",
    "state",
    "source",
    "created_by",
    "created_run_id",
    "retired_at",
    "retired_note",
    "created_at",
    "updated_at",
    "replaces",
    "plugin",
    "ref",
)
PROBE_OUTPUT = returns_object(*PROBE_KEYS, schema="Probe")


REQUEUE = returns_object("probe_id", "week", "requeued", "kept", schema="ProbeRequeue")
EDITED = ("repo", "commit", "verify")  # with the task, what ``probe edit`` may change


REPORT_KEYS = ("project", "k", "budget_usd", "weeks", "unprobed", "fixes", "replacements")
DEFAULT_WEEKS = 2
MAX_WEEKS = 52
PROBE_ID = Arg("id", type=int)
TASK_HELP = "the task text the agent is given"
TASK_FILE_HELP = "a file holding the task text; - for stdin"
VERIFY_HELP = "one shell line that exits 0 when the agent does not repeat the mistake"
REPORT_COLUMNS = ("PROBE", "CHANGE", "RULE", "MODEL", "ON FAILED", "OFF FAILED", "STATUS", "WEEK BEFORE", "ARMS AT")


class Probes(ProjectResource):
    """The probes of a project: .../curator/probes and the routes below it, and GET .../curator/probe-report."""

    collection = "curator"

    def list(self, **query) -> dict:
        return self.call("GET", "probes", **query)

    def get(self, probe: int) -> dict:
        return self.call("GET", "probes", probe)

    def add(self, body: dict) -> dict:
        return self.call("POST", "probes", body=body)

    def edit(self, probe: int, body: dict) -> dict:
        return self.call("PATCH", "probes", probe, body=body)

    def act(self, probe: int, action: str, body: dict) -> dict:
        """POST ``action`` (retire or requeue) of one probe."""
        return self.call("POST", "probes", probe, action, body=body)

    def report(self, weeks: int, week: str | None) -> dict:
        return self.call("GET", "probe-report", weeks=weeks, week=week)


def task_of(args: argparse.Namespace) -> str:
    """The task of ``add`` or ``edit``: ``--task``, else the text of ``--task-file`` (``-``: stdin), at most
    MAX_TASK_BYTES; HubError when it cannot be read or is too long."""
    if args.task is not None:
        return args.task
    source = args.task_file
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read the task {source}: {exc}") from None
    if len(text.encode()) > MAX_TASK_BYTES:
        raise HubError(f"the task {source} is over {MAX_TASK_BYTES} bytes")
    return text


def probe_body(args: argparse.Namespace) -> dict:
    """The body of ``probe add``."""
    body = {"repo": args.repo, "task": task_of(args), "verify": args.verify}
    if args.commit:
        body["commit"] = args.commit
    body["change_id" if args.change else "proposal_id"] = args.change or args.proposal
    return body


def probe_edits(args: argparse.Namespace) -> dict:
    """The body of ``probe edit``: the options given; HubError when none is."""
    body = {key: getattr(args, key) for key in EDITED if getattr(args, key) is not None}
    if args.task is not None or args.task_file is not None:
        body["task"] = task_of(args)
    if not body:
        raise HubError("give at least one of --repo, --commit, --task, --task-file or --verify")
    return body


def rule_of(item: dict) -> str:
    """The plugin and rule key of a plugin's probe (source plugin), else ``-``."""
    return f"{item.get('plugin') or 'removed'}:{item['ref']}" if item.get("ref") else "-"


def probe_lines(probe: dict) -> list[str]:
    """One probe for a person: what it probes, its repo and verify, then its task."""
    target = f"change #{probe['change_id']}" if probe["change_id"] else f"proposal #{probe['proposal_id']}"
    if probe.get("ref"):
        target = f"plugin {probe.get('plugin') or 'removed'} rule {probe['ref']}"
    verify = probe["verify"] if probe["verify"] is not None else "(hidden: the admins of the project see it)"
    lines = [
        f"probe #{probe['id']} of {target}, {probe['state']} ({probe['source']})",
        f"repo: {probe['repo']} at {probe['commit'] or 'the tip of the default branch when it was made'}",
        f"verify: {verify}",
    ]
    if probe["retired_note"]:
        lines.append(f"retired: {probe['retired_note']}")
    if probe.get("replaces"):
        lines.append(f"replaces: probe #{probe['replaces']}")
    return [*lines, "", probe["task"]]


class ProbeList(HubCommand):
    name = "list"
    help = "the probes of the project, newest first"
    arguments = (
        PROJECT,
        Arg("--change", type=int, metavar="N", help="the probes of this change"),
        Arg("--proposal", type=int, metavar="N", help="the probes of this proposal"),
        Arg("--state", choices=STATES),
        Arg("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200"),
    )
    output = returns_object("probes", "total", schema="ProbeList")
    columns = ("PROBE", "CHANGE", "PROPOSAL", "RULE", "REPO", "STATE", "SOURCE", "TASK")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Probes.of(ctx).list(change=args.change, proposal=args.proposal, state=args.state, limit=args.limit)
        rows = [self.row(probe) for probe in found["probes"]]
        return Result(found, Views(Table(self.columns, rows), Text(f"{found['total']} probe(s) in {ctx.project}")))

    @staticmethod
    def row(probe: dict) -> tuple:
        return (
            f"#{probe['id']}",
            f"#{probe['change_id']}" if probe["change_id"] else "-",
            f"#{probe['proposal_id']}" if probe["proposal_id"] else "-",
            rule_of(probe),
            probe["repo"],
            probe["state"],
            probe["source"],
            probe["task"].strip().splitlines()[0][:60] if probe["task"].strip() else "",
        )


class ProbeShow(HubCommand):
    name = "show"
    help = "one probe: its task, and its verify for the project's admins"
    arguments = (PROJECT, PROBE_ID)
    output = PROBE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Probes.of(ctx).get(ctx.args.id)
        return Result(found, Text(*probe_lines(found)))


class ProbeAdd(HubCommand):
    name = "add"
    help = "add a probe to a change or a proposal (the project's admins)"
    output = PROBE_OUTPUT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        PROJECT.add_to(parser)
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--change", type=int, metavar="N", help="the change it probes")
        target.add_argument("--proposal", type=int, metavar="N", help="the proposal whose change it probes")
        parser.add_argument("--repo", required=True, help="the repo of the project the task runs in")
        parser.add_argument("--commit", help="the base commit; none: the tip of the default branch")
        task = parser.add_mutually_exclusive_group(required=True)
        task.add_argument("--task", help=TASK_HELP)
        task.add_argument("--task-file", metavar="PATH", help=TASK_FILE_HELP)
        parser.add_argument("--verify", required=True, help=VERIFY_HELP)

    def run(self, ctx: HubContext) -> Result:
        found = Probes.of(ctx).add(probe_body(ctx.args))
        return Result(found, Text(f"added probe #{found['id']}"))


class ProbeEdit(HubCommand):
    name = "edit"
    help = "change an active probe's repo, commit, task or verify (the project's admins)"
    output = PROBE_OUTPUT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        PROJECT.add_to(parser)
        PROBE_ID.add_to(parser)
        parser.add_argument("--repo", help="the repo of the project the task runs in")
        parser.add_argument("--commit", help="the base commit")
        task = parser.add_mutually_exclusive_group()
        task.add_argument("--task", help=TASK_HELP)
        task.add_argument("--task-file", metavar="PATH", help=TASK_FILE_HELP)
        parser.add_argument("--verify", help=VERIFY_HELP)

    def run(self, ctx: HubContext) -> Result:
        body = probe_edits(ctx.args)  # before the hub is asked: a usage mistake signs in nowhere
        found = Probes.of(ctx).edit(ctx.args.id, body)
        return Result(found, Text(f"edited probe #{found['id']}: {', '.join(sorted(body))}"))


class ProbeRetire(HubCommand):
    name = "retire"
    help = "retire a probe (the project's admins)"
    arguments = (PROJECT, PROBE_ID, Arg("--note", help="why it is retired"))
    output = PROBE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        body = {"note": ctx.args.note} if ctx.args.note else {}
        found = Probes.of(ctx).act(ctx.args.id, "retire", body)
        return Result(found, Text(f"retired probe #{found['id']}"))


def run_words(item: dict) -> str:
    model = f" {item['model']}" if item.get("model") else ""
    return f"  {item['runtime']}{model}: {item['state']}"


class ProbeRequeue(HubCommand):
    name = "requeue"
    help = "measure an active probe again this week: its stale, failed or unmeasured rows (admins)"
    arguments = (PROJECT, PROBE_ID)
    output = REQUEUE

    def run(self, ctx: HubContext) -> Result:
        found = Probes.of(ctx).act(ctx.args.id, "requeue", {})
        lines = [f"probe #{found['probe_id']}, the week of {found['week']}: {len(found['requeued'])} row(s) requeued"]
        lines += [run_words(item) for item in found["requeued"]]
        if found["kept"]:
            lines.append(f"kept as they are: {len(found['kept'])}")
            lines += [run_words(item) for item in found["kept"]]
        return Result(found, Text(*lines))


def report_row(item: dict) -> tuple:
    return (
        f"#{item['probe_id']}",
        f"#{item['change_id']}" if item["change_id"] else "-",
        rule_of(item),
        f"{item['runtime']}{' ' + item['model'] if item['model'] else ''}",
        f"{item['on']['failed']}/{item['on']['runs']}",
        f"{item['off']['failed']}/{item['off']['runs']}",
        item["status"],
        item["previous"] or "-",
        item.get("arms_at") or "-",
    )


def week_view(week: dict) -> Views:
    """One ISO week of the report: its totals and cost, then its probes."""
    totals = week["totals"]
    state = "" if week["complete"] else " (in progress)"
    head = (
        f"week of {week['week']}{state}: {totals['holds']} holds, {totals['recurring']} recurring, "
        f"{totals['not_reproduced']} not reproduced, {totals['stale']} stale, {totals['unmeasured']} unmeasured; "
        f"cost {week['cost_usd']:.2f} USD"
    )
    arms = (
        ["ARMS AT change: the change's diff no longer comes off the tip, so its arms ran at its own commits"]
        if any(item.get("arms_at") == "change" for item in week["probes"])
        else []
    )
    return Views(Text("", head), Table(REPORT_COLUMNS, [report_row(item) for item in week["probes"]]), Text(*arms))


def report_tail(found: dict) -> list[str]:
    """Under the weeks: the changes no probe measures, the fix proposals, the probes written in place of others."""
    lines = []
    if found["unprobed"]:
        lines += [
            "",
            f"changes without a probe a probe could measure, or born of a correction: {len(found['unprobed'])}",
        ]
        lines += [f"  #{item['change_id']} {item['title']}: {item['reason']}" for item in found["unprobed"]]
    lines += [
        f"fix proposal #{fix['proposal_id']} of change #{fix['change_id']} ({fix['state']}): {fix['title']}"
        for fix in found["fixes"]
    ]
    lines += [
        f"probe #{item['probe_id']} of change #{item['change_id']} in place of probe #{item['replaces']}"
        for item in found.get("replacements") or []
    ]
    return lines


class ProbeReport(HubCommand):
    name = "report"
    help = "the weekly report of the probes: arm on and off, status, cost"
    arguments = (
        PROJECT,
        Arg(
            "--weeks",
            type=int,
            default=DEFAULT_WEEKS,
            choices=range(1, MAX_WEEKS + 1),
            metavar="N",
            help=f"the newest weeks, the one in progress first; 1 to {MAX_WEEKS}, default {DEFAULT_WEEKS}",
        ),
        Arg("--week", metavar="YYYY-MM-DD", help="a day of the newest week to show; default today"),
    )
    output = returns_object(*REPORT_KEYS, schema="ProbeWeekly")

    def run(self, ctx: HubContext) -> Result:
        found = Probes.of(ctx).report(ctx.args.weeks, ctx.args.week or None)
        cap = (
            f"{found['budget_usd']:.2f} USD a week"
            if found["budget_usd"] is not None
            else "none (probes.budget_usd is null)"
        )
        head = Text(f"probes of {ctx.project}: k {found['k']}, budget {cap}")
        weeks = [week_view(week) for week in found["weeks"]]
        return Result(found, Views(head, *weeks, Text(*report_tail(found))))


class ProbeGroup(Group):
    """``curator probe``, whose commands its usage names COMMAND."""

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser(self.name, help=self.help, **self.options)
        below = parser.add_subparsers(dest=self.dest, required=True, metavar="COMMAND")
        for child in self.children:
            child.attach(below)


PROBE = ProbeGroup(
    "probe",
    "the probes of the Curator's proposals and changes",
    (ProbeList(), ProbeShow(), ProbeAdd(), ProbeEdit(), ProbeRetire(), ProbeRequeue(), ProbeReport()),
)
