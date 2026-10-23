"""``evo-agents hub curator probe list|show|add|retire|report``: the probes of the Curator's proposals and changes
(``evo_agents.hub.probes``; GET, POST .../curator/probes, POST .../curator/probes/{id}/retire and GET
.../curator/probe-report).

``probe list`` lists the probes the caller may read, newest first, by ``--change``, ``--proposal`` and ``--state``;
``probe show`` prints one. Their verify is the answer key: the hub sends it to the project's admins alone, and prints
``(hidden)`` for the others. ``probe add`` (admins) adds one to ``--change`` or ``--proposal``: the ``--repo``, the base
``--commit`` (none: the tip of the default branch), the task (``--task``, or ``--task-file``, ``-`` for stdin) and the
one-line ``--verify``. ``probe retire`` (admins) retires one, with ``--note``. ``probe report`` prints the weekly report
of ``--weeks`` ISO weeks, the newest first (the week of ``--week``, default the one in progress): each week's cost
against ``probes.budget_usd``, each probe on each runtime and model, arm on and arm off (failed of sessions), its
status and the week before's (``--json`` adds each arm's 1 - pass^k, ``recurrence``); then the changes born of a
correction that have no probe, with why, the fix proposals of recurring probes with their state, and the probes
written in place of probes not reproduced.

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import HubError, client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import project_of_args
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
)
PROBE = returns_object(*PROBE_KEYS, schema="Probe")


REPORT_KEYS = ("project", "k", "budget_usd", "weeks", "unprobed", "fixes", "replacements")
DEFAULT_WEEKS = 2
MAX_WEEKS = 52


def _path(project: str, *parts: str, **params) -> str:
    asked = {key: value for key, value in params.items() if value is not None}
    query = f"?{urlencode(asked)}" if asked else ""
    return "/".join((f"/v1/projects/{quote(project, safe='')}/curator/probes", *parts)) + query


def _task(args) -> str:
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


def _print(probe: dict) -> None:
    target = f"change #{probe['change_id']}" if probe["change_id"] else f"proposal #{probe['proposal_id']}"
    print(f"probe #{probe['id']} of {target}, {probe['state']} ({probe['source']})")
    print(f"repo: {probe['repo']} at {probe['commit'] or 'the tip of the default branch when it was made'}")
    print(f"verify: {probe['verify'] if probe['verify'] is not None else '(hidden: the admins of the project see it)'}")
    if probe["retired_note"]:
        print(f"retired: {probe['retired_note']}")
    if probe.get("replaces"):
        print(f"replaces: probe #{probe['replaces']}")
    print()
    print(probe["task"])


@client_command
def cmd_probe_list(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call(
        "GET", _path(project, change=args.change, proposal=args.proposal, state=args.state, limit=args.limit)
    )
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            f"#{probe['id']}",
            f"#{probe['change_id']}" if probe["change_id"] else "-",
            f"#{probe['proposal_id']}" if probe["proposal_id"] else "-",
            probe["repo"],
            probe["state"],
            probe["source"],
            probe["task"].strip().splitlines()[0][:60] if probe["task"].strip() else "",
        )
        for probe in found["probes"]
    ]
    if rows:
        table(("PROBE", "CHANGE", "PROPOSAL", "REPO", "STATE", "SOURCE", "TASK"), rows)
    print(f"{found['total']} probe(s) in {project}")
    return 0


@client_command
def cmd_probe_show(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(project_of_args(args), str(args.id)))
    if args.json:
        print_json(found)
        return 0
    _print(found)
    return 0


@client_command
def cmd_probe_add(args) -> int:
    hub, _ = signed_in()
    body = {"repo": args.repo, "task": _task(args), "verify": args.verify}
    if args.commit:
        body["commit"] = args.commit
    body["change_id" if args.change else "proposal_id"] = args.change or args.proposal
    found = hub.call("POST", _path(project_of_args(args)), body)
    if args.json:
        print_json(found)
        return 0
    print(f"added probe #{found['id']}")
    return 0


@client_command
def cmd_probe_retire(args) -> int:
    hub, _ = signed_in()
    body = {"note": args.note} if args.note else {}
    found = hub.call("POST", _path(project_of_args(args), str(args.id), "retire"), body)
    if args.json:
        print_json(found)
        return 0
    print(f"retired probe #{found['id']}")
    return 0


@client_command
def cmd_probe_report(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    asked = {"weeks": args.weeks, **({"week": args.week} if args.week else {})}
    found = hub.call("GET", f"/v1/projects/{quote(project, safe='')}/curator/probe-report?{urlencode(asked)}")
    if args.json:
        print_json(found)
        return 0
    cap = (
        f"{found['budget_usd']:.2f} USD a week"
        if found["budget_usd"] is not None
        else "none (probes.budget_usd is null)"
    )
    print(f"probes of {project}: k {found['k']}, budget {cap}")
    for week in found["weeks"]:
        totals = week["totals"]
        state = "" if week["complete"] else " (in progress)"
        print()
        print(
            f"week of {week['week']}{state}: {totals['holds']} holds, {totals['recurring']} recurring, "
            f"{totals['not_reproduced']} not reproduced, {totals['stale']} stale, {totals['unmeasured']} unmeasured; "
            f"cost {week['cost_usd']:.2f} USD"
        )
        rows = [
            (
                f"#{item['probe_id']}",
                f"#{item['change_id']}",
                f"{item['runtime']}{' ' + item['model'] if item['model'] else ''}",
                f"{item['on']['failed']}/{item['on']['runs']}",
                f"{item['off']['failed']}/{item['off']['runs']}",
                item["status"],
                item["previous"] or "-",
            )
            for item in week["probes"]
        ]
        if rows:
            table(("PROBE", "CHANGE", "MODEL", "ON FAILED", "OFF FAILED", "STATUS", "WEEK BEFORE"), rows)
    if found["unprobed"]:
        print()
        print(f"changes born of a correction without a probe: {len(found['unprobed'])}")
        for item in found["unprobed"]:
            print(f"  #{item['change_id']} {item['title']}: {item['reason']}")
    for fix in found["fixes"]:
        print(f"fix proposal #{fix['proposal_id']} of change #{fix['change_id']} ({fix['state']}): {fix['title']}")
    for item in found.get("replacements") or []:
        print(f"probe #{item['probe_id']} of change #{item['change_id']} in place of probe #{item['replaces']}")
    return 0


def register_probe(csub, with_project) -> None:
    """``curator probe list|show|add|retire|report``."""
    probe = csub.add_parser("probe", help="the probes of the Curator's proposals and changes")
    psub = probe.add_subparsers(dest="probe_command", required=True, metavar="COMMAND")

    listed = psub.add_parser("list", help="the probes of the project, newest first")
    with_project(listed)
    listed.add_argument("--change", type=int, metavar="N", help="the probes of this change")
    listed.add_argument("--proposal", type=int, metavar="N", help="the probes of this proposal")
    listed.add_argument("--state", choices=STATES)
    listed.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200")
    json_option(listed, returns_object("probes", "total", schema="ProbeList"))
    listed.set_defaults(func=cmd_probe_list)

    shown = psub.add_parser("show", help="one probe: its task, and its verify for the project's admins")
    with_project(shown)
    shown.add_argument("id", type=int)
    json_option(shown, PROBE)
    shown.set_defaults(func=cmd_probe_show)

    added = psub.add_parser("add", help="add a probe to a change or a proposal (the project's admins)")
    with_project(added)
    target = added.add_mutually_exclusive_group(required=True)
    target.add_argument("--change", type=int, metavar="N", help="the change it probes")
    target.add_argument("--proposal", type=int, metavar="N", help="the proposal whose change it probes")
    added.add_argument("--repo", required=True, help="the repo of the project the task runs in")
    added.add_argument("--commit", help="the base commit; none: the tip of the default branch")
    task = added.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="the task text the agent is given")
    task.add_argument("--task-file", metavar="PATH", help="a file holding the task text; - for stdin")
    added.add_argument(
        "--verify", required=True, help="one shell line that exits 0 when the agent does not repeat the mistake"
    )
    json_option(added, PROBE)
    added.set_defaults(func=cmd_probe_add)

    retired = psub.add_parser("retire", help="retire a probe (the project's admins)")
    with_project(retired)
    retired.add_argument("id", type=int)
    retired.add_argument("--note", help="why it is retired")
    json_option(retired, PROBE)
    retired.set_defaults(func=cmd_probe_retire)

    report = psub.add_parser("report", help="the weekly report of the probes: arm on and off, status, cost")
    with_project(report)
    report.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_WEEKS,
        choices=range(1, MAX_WEEKS + 1),
        metavar="N",
        help=f"the newest weeks, the one in progress first; 1 to {MAX_WEEKS}, default {DEFAULT_WEEKS}",
    )
    report.add_argument("--week", metavar="YYYY-MM-DD", help="a day of the newest week to show; default today")
    json_option(report, returns_object(*REPORT_KEYS, schema="ProbeWeekly"))
    report.set_defaults(func=cmd_probe_report)
