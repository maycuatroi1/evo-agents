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

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Releases` is
the gateway to the routes, :func:`schedule_body` turns the options of ``set`` into its body.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Text, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.curator import money
from evo_agents.hub.releases import DEFAULT_AT, DEFAULT_TIMEZONE, MAX_PATHS, short

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
}


class Releases(ProjectResource):
    """The release of a project: GET /v1/projects/{p}/releases, PUT .../releases/schedule and POST
    .../releases/{pause,resume,run}."""

    collection = "releases"

    def status(self) -> dict:
        return self.call("GET")

    def schedule(self, body: dict) -> dict:
        return self.call("PUT", "schedule", body=body)

    def act(self, action: str) -> dict:
        """POST ``action`` (pause, resume or run) with no body."""
        return self.call("POST", action)


def schedule_body(args: argparse.Namespace) -> dict:
    """The body of ``release set``: each option given, under its key."""
    return {key: getattr(args, option) for option, key in SET_OPTIONS.items() if getattr(args, option) is not None}


def check_line(found: dict) -> str:
    what = found["outcome"]
    if found.get("cycle_id"):
        what += f", cycle #{found['cycle_id']} with run #{found['run_id']}"
    return (
        f"{found['local_day']} ({found['trigger']}, {when(found['checked_at'])} UTC): {what}: {found['reason'] or '-'}"
    )


def ci_text(tag: dict) -> str:
    """The CI of a tag the cycle follows, for its line of `release status`."""
    said = f", {tag['workflow_url']}" if tag.get("workflow_url") else ""
    if tag.get("failed_tests"):
        shown = ", ".join(tag["failed_tests"][:3]) + (" and more" if len(tag["failed_tests"]) > 3 else "")
        said += f", failed: {shown}"
    if tag.get("reruns"):
        said += f", run again {tag['reruns']} time(s)"
    return said + (f"; {tag['problem']}" if tag.get("problem") else "")


def landed_text(run: dict) -> str:
    """What a cycle's run pushed and how the hub landed it, for its line of `release status`."""
    if not run.get("version"):
        return ""
    said = f", pushed {run['branch']}"
    if run.get("tagged"):
        return f"{said}, tagged {run['tagged']} on {short(run.get('merge_commit'))}"
    if run.get("land_state"):
        return f"{said}, {run['land_state']}: {run.get('land_reason') or 'no reason given'}"
    return f"{said}, waiting to land"


def cycle_lines(cycle: dict) -> list[str]:
    """The cycle open now, for `release status`: its state, why it stopped, its runs and its tags."""
    lines = [
        f"  cycle #{cycle['id']} {cycle['state']} since {when(cycle['opened_at'])} UTC: {cycle['target_ref']} at "
        f"{short(cycle['target_commit'])} since {cycle['base_tag'] or '-'}, {money(cycle['cost_usd'])}"
    ]
    if cycle.get("stop_reason"):
        lines.append(f"    stopped ({cycle.get('stop_cause') or '-'}): {cycle['stop_reason']}")
    if cycle["state"] == "stopped" and cycle.get("decision_id"):
        lines.append(f"    waits for the owner's answer to decision #{cycle['decision_id']}")
    lines += [
        f"    run #{run['run_id']} {run['mode']}: {run['state']}, {money(run['cost_usd'])}{landed_text(run)}"
        for run in cycle["runs"]
    ]
    lines += [f"    tag {tag['tag']}: CI {tag['ci_state']}{ci_text(tag)}" for tag in cycle.get("tags") or []]
    return lines


def status_lines(status: dict) -> list[str]:
    """The release schedule of a project, its cycle open now and its last check."""
    schedule = status["schedule"]
    if schedule is None:
        return [
            f"Project {status['project']} has no release schedule: an admin sets one with `evo-agents hub release set`"
        ]
    state = f"paused by {schedule['paused_by']} at {when(schedule['paused_at'])} UTC" if status["paused"] else "on"
    lines = [
        f"Release of {status['project']}: {state}, repo {schedule['repo']} ({schedule['github_repo'] or 'off GitHub'})",
        f"  check     {schedule['at']} {schedule['timezone']}, next at {when(status['next_check_at'])} UTC",
        f"  compares  the newest tag {schedule['tag_pattern']} with {schedule['target_ref']}",
        f"  paths     {', '.join(schedule['paths'])}",
        f"  workflow  {schedule['workflow']}",
        f"  runs      dispatched as {schedule['owner']}, no cost cap",
    ]
    if status["cycle"] is not None:
        lines += cycle_lines(status["cycle"])
    if status["last_check"] is not None:
        lines.append(f"  last check {check_line(status['last_check'])}")
    else:
        lines.append("  no check yet")
    return lines


class ReleaseStatus(HubCommand):
    name = "status"
    help = "the schedule, its latest checks and the cycle open now"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        status = Releases.of(ctx).status()
        earlier = [f"  earlier    {check_line(found)}" for found in status["checks"][1:]]
        return Result(status, Text(*status_lines(status), *earlier))


class ReleaseSet(HubCommand):
    name = "set"
    help = "set the release schedule; an admin of the project, who becomes its owner"
    arguments = (
        PROJECT,
        Arg("--repo", help="a repo of the project on GitHub (default: its only one there)"),
        Arg("--at", metavar="HH:MM", help=f"the local time of the daily check (default: {DEFAULT_AT})"),
        Arg("--timezone", help=f"an IANA time zone (default: {DEFAULT_TIMEZONE})"),
        Arg("--target-ref", dest="target_ref", help="the ref a release ships (default: the tag nightly)"),
        Arg("--tag-pattern", dest="tag_pattern", help="the release tags, a prefix then * (default: v*)"),
        Arg("--workflow", help="the workflow file that publishes a tag (default: release.yml)"),
        Arg(
            "--path",
            dest="paths",
            action="append",
            metavar="PATH",
            help=f"a packaging path, repeated up to {MAX_PATHS} times; replaces them all (a directory ends in /)",
        ),
    )
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        body = schedule_body(ctx.args)
        status = Releases.of(ctx).schedule(body)
        said = f"Set the release schedule of {ctx.project}; its runs go as {status['schedule']['owner']}."
        return Result(status, Text(said, *status_lines(status)))


def switched(ctx: HubContext, action: str, said: str) -> Result:
    """``release pause`` or ``release resume``: what the hub answered, after what was done."""
    status = Releases.of(ctx).act(action)
    return Result(status, Text(said, *status_lines(status)))


class ReleasePause(HubCommand):
    name = "pause"
    help = "pause it, cancel its queued runs and close an idle open cycle"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        said = f"Paused the release of {ctx.project}: no check opens a cycle until `evo-agents hub release resume`."
        return switched(ctx, self.name, said)


class ReleaseResume(HubCommand):
    name = "resume"
    help = "let the release schedule check again"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        return switched(ctx, self.name, f"The release of {ctx.project} checks again at its time.")


class ReleaseRun(HubCommand):
    name = "run"
    help = "check now, through the gates of the daily check; an admin of the project"
    arguments = (PROJECT,)
    output = CHECK

    def run(self, ctx: HubContext) -> Result:
        found = Releases.of(ctx).act("run")
        lines = [f"Checked: {check_line(found)}"]
        if found["changed_paths"]:
            lines.append(f"  changed since {found['base_tag']}: {', '.join(found['changed_paths'])}")
        return Result(found, Text(*lines))


RELEASE = Group(
    "release",
    "the daily release of a project: its schedule, status, pause, resume, and a check now",
    (ReleaseStatus(), ReleaseSet(), ReleasePause(), ReleaseResume(), ReleaseRun()),
)
