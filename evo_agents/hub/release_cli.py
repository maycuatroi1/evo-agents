"""``evo-agents hub release``: the daily release of a project (``evo_agents.hub.releases``).

``release status`` shows the schedule, whether it is paused, its next check, the latest checks and the cycle open now,
with its runs, the tags it follows and their CI, and for a stopped cycle why and the decision it waits for (GET
/v1/projects/{p}/releases). ``release set`` sets the schedule, which only an admin of the project may, and makes
that admin its owner (PUT .../releases/schedule): each option left out keeps what the schedule holds, or its default
the first time; ``--path`` repeats and replaces the packaging paths whole. ``release pause`` pauses it, cancels the
release runs still queued and closes an open cycle left idle; ``release resume`` lets it check again; ``release run``
checks now, through the same gates as the daily check, and prints the check. Each of these three needs the admin role
too. None of them touches the night shift (``hub curator``).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, when
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.curator import money
from evo_agents.hub.plan_cli import project_of_args
from evo_agents.hub.releases import DEFAULT_AT, DEFAULT_CYCLE_USD, DEFAULT_TIMEZONE, MAX_PATHS, short

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
STATUS_KEYS = ("project", "schedule", "paused", "next_check_at", "last_check", "checks", "cycle")
CHECK_KEYS = (
    "id",
    "trigger",
    "local_day",
    "checked_at",
    "requested_by",
    "outcome",
    "reason",
    "base_tag",
    "base_commit",
    "target_commit",
    "changed_paths",
    "cycle_id",
    "run_id",
)
STATUS = returns_object(*STATUS_KEYS, schema="ReleaseStatus")
CHECK = returns_object(*CHECK_KEYS, schema="ReleaseCheck")
SET_OPTIONS = {  # option, its key in the body
    "repo": "repo",
    "at": "at",
    "timezone": "timezone",
    "target_ref": "target_ref",
    "tag_pattern": "tag_pattern",
    "workflow": "workflow",
    "paths": "paths",
    "budget": "cycle_budget_usd",
}


def _path(project: str, *parts: str) -> str:
    return "/".join((f"/v1/projects/{quote(project, safe='')}/releases", *parts))


def _check_line(found: dict) -> str:
    what = found["outcome"]
    if found.get("cycle_id"):
        what += f", cycle #{found['cycle_id']} with run #{found['run_id']}"
    return (
        f"{found['local_day']} ({found['trigger']}, {when(found['checked_at'])} UTC): {what}: {found['reason'] or '-'}"
    )


def _print_status(status: dict) -> None:
    schedule = status["schedule"]
    if schedule is None:
        print(
            f"Project {status['project']} has no release schedule: an admin sets one with `evo-agents hub release set`"
        )
        return
    state = f"paused by {schedule['paused_by']} at {when(schedule['paused_at'])} UTC" if status["paused"] else "on"
    print(
        f"Release of {status['project']}: {state}, repo {schedule['repo']} ({schedule['github_repo'] or 'off GitHub'})"
    )
    print(f"  check     {schedule['at']} {schedule['timezone']}, next at {when(status['next_check_at'])} UTC")
    print(f"  compares  the newest tag {schedule['tag_pattern']} with {schedule['target_ref']}")
    print(f"  paths     {', '.join(schedule['paths'])}")
    print(f"  workflow  {schedule['workflow']}")
    print(f"  cycle cap {money(schedule['cycle_budget_usd'])}, runs dispatched as {schedule['owner']}")
    cycle = status["cycle"]
    if cycle is not None:
        print(
            f"  cycle #{cycle['id']} {cycle['state']} since {when(cycle['opened_at'])} UTC: {cycle['target_ref']} at "
            f"{short(cycle['target_commit'])} since {cycle['base_tag'] or '-'}, {money(cycle['cost_usd'])} of "
            f"{money(cycle['budget_usd'])}"
        )
        if cycle.get("stop_reason"):
            print(f"    stopped ({cycle.get('stop_cause') or '-'}): {cycle['stop_reason']}")
        if cycle["state"] == "stopped" and cycle.get("decision_id"):
            print(f"    waits for the owner's answer to decision #{cycle['decision_id']}")
        for run in cycle["runs"]:
            print(f"    run #{run['run_id']} {run['mode']}: {run['state']}, {money(run['cost_usd'])}{_landed(run)}")
        for tag in cycle.get("tags") or []:
            print(f"    tag {tag['tag']}: CI {tag['ci_state']}{_ci(tag)}")
    if status["last_check"] is not None:
        print(f"  last check {_check_line(status['last_check'])}")
    else:
        print("  no check yet")


def _ci(tag: dict) -> str:
    """The CI of a tag the cycle follows, for its line of `release status`."""
    said = f", {tag['workflow_url']}" if tag.get("workflow_url") else ""
    if tag.get("failed_tests"):
        shown = ", ".join(tag["failed_tests"][:3]) + (" and more" if len(tag["failed_tests"]) > 3 else "")
        said += f", failed: {shown}"
    if tag.get("reruns"):
        said += f", run again {tag['reruns']} time(s)"
    return said + (f"; {tag['problem']}" if tag.get("problem") else "")


def _landed(run: dict) -> str:
    """What a cycle's run pushed and how the hub landed it, for its line of `release status`."""
    if not run.get("version"):
        return ""
    said = f", pushed {run['branch']}"
    if run.get("tagged"):
        return f"{said}, tagged {run['tagged']} on {short(run.get('merge_commit'))}"
    if run.get("land_state"):
        return f"{said}, {run['land_state']}: {run.get('land_reason') or 'no reason given'}"
    return f"{said}, waiting to land"


@client_command
def cmd_status(args) -> int:
    hub, _ = signed_in()
    status = hub.call("GET", _path(project_of_args(args)))
    if args.json:
        print_json(status)
        return 0
    _print_status(status)
    for found in status["checks"][1:]:
        print(f"  earlier    {_check_line(found)}")
    return 0


@client_command
def cmd_set(args) -> int:
    body = {key: getattr(args, option) for option, key in SET_OPTIONS.items() if getattr(args, option) is not None}
    hub, _ = signed_in()
    project = project_of_args(args)
    status = hub.call("PUT", _path(project, "schedule"), body)
    if args.json:
        print_json(status)
        return 0
    print(f"Set the release schedule of {project}; its runs go as {status['schedule']['owner']}.")
    _print_status(status)
    return 0


def _switch(action: str):
    @client_command
    def run(args) -> int:
        hub, _ = signed_in()
        project = project_of_args(args)
        status = hub.call("POST", _path(project, action))
        if args.json:
            print_json(status)
            return 0
        if action == "pause":
            print(f"Paused the release of {project}: no check opens a cycle until `evo-agents hub release resume`.")
        else:
            print(f"The release of {project} checks again at its time.")
        _print_status(status)
        return 0

    return run


cmd_pause = _switch("pause")
cmd_resume = _switch("resume")


@client_command
def cmd_run(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", _path(project_of_args(args), "run"))
    if args.json:
        print_json(found)
        return 0
    print(f"Checked: {_check_line(found)}")
    if found["changed_paths"]:
        print(f"  changed since {found['base_tag']}: {', '.join(found['changed_paths'])}")
    return 0


def register_release(hsub) -> None:
    release = hsub.add_parser(
        "release", help="the daily release of a project: its schedule, status, pause, resume, and a check now"
    )
    rsub = release.add_subparsers(dest="release_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    status = rsub.add_parser("status", help="the schedule, its latest checks and the cycle open now")
    with_project(status)
    json_option(status, STATUS)
    status.set_defaults(func=cmd_status)

    write = rsub.add_parser("set", help="set the release schedule; an admin of the project, who becomes its owner")
    with_project(write)
    write.add_argument("--repo", help="a repo of the project on GitHub (default: its only one there)")
    write.add_argument("--at", metavar="HH:MM", help=f"the local time of the daily check (default: {DEFAULT_AT})")
    write.add_argument("--timezone", help=f"an IANA time zone (default: {DEFAULT_TIMEZONE})")
    write.add_argument("--target-ref", dest="target_ref", help="the ref a release ships (default: the tag nightly)")
    write.add_argument("--tag-pattern", dest="tag_pattern", help="the release tags, a prefix then * (default: v*)")
    write.add_argument("--workflow", help="the workflow file that publishes a tag (default: release.yml)")
    write.add_argument(
        "--path",
        dest="paths",
        action="append",
        metavar="PATH",
        help=f"a packaging path, repeated up to {MAX_PATHS} times; replaces them all (a directory ends in /)",
    )
    write.add_argument(
        "--budget", type=float, metavar="USD", help=f"what one cycle's runs may cost (default: {DEFAULT_CYCLE_USD:g})"
    )
    json_option(write, STATUS)
    write.set_defaults(func=cmd_set)

    pause = rsub.add_parser("pause", help="pause it, cancel its queued runs and close an idle open cycle")
    with_project(pause)
    json_option(pause, STATUS)
    pause.set_defaults(func=cmd_pause)

    resume = rsub.add_parser("resume", help="let the release schedule check again")
    with_project(resume)
    json_option(resume, STATUS)
    resume.set_defaults(func=cmd_resume)

    run = rsub.add_parser("run", help="check now, through the gates of the daily check; an admin of the project")
    with_project(run)
    json_option(run, CHECK)
    run.set_defaults(func=cmd_run)
