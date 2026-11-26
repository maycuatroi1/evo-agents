"""``evo-agents hub curator status|pause|resume``: where the night shift of a project stands, and its switch.

``status`` shows the charter's revision, the schedules, whether they are paused, and the night now with its runs and
cost; ``pause`` pauses every schedule of the project and cancels the runs they queued that are still queued, ``resume``
lets them run again.
"""

from __future__ import annotations

from evo_agents.clikit import Result, Text
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.curator.charter import period_of
from evo_agents.hub.cli.curator.resources import STATUS, NightShift
from evo_agents.hub.cli_client import when
from evo_agents.hub.curator import money, zone_of


def _where(charter: dict, inside: bool) -> str:
    """Where the local time stands: inside or outside the window, or with pacing in or outside its quiet hours."""
    pacing = charter.get("pacing")
    if not pacing:
        return f", {'inside' if inside else 'outside'} the window"
    if not pacing.get("quiet_hours"):
        return ""
    return ", outside quiet hours" if inside else ", in quiet hours"


def _schedule_lines(status: dict) -> list[str]:
    lines = []
    for schedule in status["schedules"]:
        by = schedule["paused_by"] or "the hub"
        paused = f", paused by {by} at {when(schedule['paused_at'])} UTC" if schedule["paused_at"] else ""
        lines.append(f"  {schedule['kind']}: runs dispatched as {schedule['owner']}{paused}")
        if schedule.get("pause_reason"):
            lines.append(f"    {schedule['pause_reason']}")
    return lines


def _night_lines(status: dict, charter: dict) -> list[str]:
    """The night now, the last review run and the last brief."""
    lines = []
    night, period = status["night"], period_of(charter)
    if night is not None:
        active = f", run #{night['active_run_id']} active" if night["active_run_id"] else ""
        lines.append(
            f"  {period} of {night['night']} ({night['local_time']} local{_where(charter, night['in_window'])}): "
            f"{night['runs']} of {night['max_runs']} runs, {money(night['cost_usd'])} of "
            f"{money(night['budget_usd'])}{active}"
        )
    last = status.get("last_review_run")
    if last is not None:
        lines.append(
            f"  last review: run #{last['id']} of the {period} of {last['night']}, {last['state']}: {last['findings']} "
            f"findings, {last['proposals']} proposals (lenses {', '.join(last['lenses']) or '-'})"
        )
    brief = status.get("last_brief")
    if brief is not None:
        lines.append(
            f"  last brief: {brief['day']} ({period} of {brief['night']}) to {brief['to']}, at {when(brief['sent_at'])} "
            f"UTC: {brief['title']}"
        )
    else:
        lines.append(f"  no morning brief yet: the first goes out at {charter['brief_at']} in {zone_of(charter)}")
    return lines


def status_lines(status: dict) -> list[str]:
    """The night shift's status as ``curator status`` prints it."""
    charter = status["charter"]
    if charter is None:
        return [
            f"Project {status['project']} has no charter: an admin writes one with `evo-agents hub curator charter set`"
        ]
    state = "paused" if status["paused"] else "on"
    head = f"Night shift of {status['project']}: {state}, charter revision {charter['revision']}"
    return [head, *_schedule_lines(status), *_night_lines(status, charter)]


class CuratorStatus(HubCommand):
    name = "status"
    help = "the charter, the schedules and the night now"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        status = NightShift.of(ctx).status()
        return Result(status, Text(*status_lines(status)))


def switched(ctx: HubContext, action: str) -> Result:
    """Pause or resume the night shift of the context's project, and say what that did."""
    status = NightShift.of(ctx).switch(action)
    project = ctx.project
    if action == "pause":
        said = f"Paused the night shift of {project}: it queues no run until `evo-agents hub curator resume`."
    else:
        again = "through its period" if (status.get("charter") or {}).get("pacing") else "inside its window"
        said = f"The night shift of {project} runs again {again}."
    return Result(status, Text(said, *status_lines(status)))


class CuratorPause(HubCommand):
    name = "pause"
    help = "pause every schedule of the project and cancel the runs they still queue"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        return switched(ctx, "pause")


class CuratorResume(HubCommand):
    name = "resume"
    help = "let the schedules of the project run again"
    arguments = (PROJECT,)
    output = STATUS

    def run(self, ctx: HubContext) -> Result:
        return switched(ctx, "resume")
