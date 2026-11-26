"""``evo-agents hub org show|unit|seat|worker``: the org chart of the hub (plan org-chart, criterion a4), over the
routes under /v1/org (``evo_agents.hub.server.org``).

``org show`` prints the tree of units, indented below the root, each with its kind, its ceiling and the effective
level with the unit that sets it, and below each unit its seats: the person (or the agent profile and its project),
the role, whether the seat is primary, reports_to, sponsor, the end date and the effective level with its source.
Archived units and ended seats are left out unless ``--all``. ``--json`` prints GET /v1/org as the hub answered it.

``org unit create|edit|archive`` change a unit and ``org seat add|edit|end`` a seat; ``hub autonomy ceiling set``
(``evo_agents.hub.cli.autonomy``) writes the ceilings. A seat is named by its id (``--seat``), by a login, or by
``--agent HANDLE --project P``; with a login, ``--unit`` picks the live seat in that unit, else the person's live
primary seat is meant. What a member may change is the hub's to decide: a refusal
(403, 404, 409, 422) prints the hub's reason and exits 1.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes) and ask the hub through
:class:`~evo_agents.hub.cli.org.resources.OrgChart`; ``org worker`` is the group of ``evo_agents.hub.cli.org.workers``.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
from datetime import date

from evo_agents.clikit import Arg, Group, Result, Text, UsageError
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.org.resources import OrgChart
from evo_agents.hub.cli.org.workers import WORKER
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import returns_object

LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")
ROLES = ("lead", "member", "intern", "agent")
# The keys of the hub's answers these commands print with --json (``evo_agents.hub.server.org_models``).
CHART_KEYS = ("units", "seats", "viewer")
UNIT_KEYS = ("slug", "name", "kind", "parent", "ends_on", "archived_at", "revision", "ceiling", "effective", "source")
SEAT_KEYS = (
    "id",
    "unit",
    "login",
    "agent",
    "project",
    "role",
    "primary",
    "reports_to",
    "reports_to_seat",
    "sponsor",
    "starts_on",
    "ends_on",
    "ended_at",
    "ceiling",
    "effective",
    "source",
)
UNIT_OUTPUT = returns_object(*UNIT_KEYS, schema="OrgUnitView")
SEAT_OUTPUT = returns_object(*SEAT_KEYS, schema="OrgSeatView")


def _day(value: str | None, flag: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise UsageError(f"{flag} takes a day as YYYY-MM-DD, not {value!r}") from None


# Reading


def _seat_ended(seat: dict, today: str) -> bool:
    return seat["ended_at"] is not None or (seat["ends_on"] is not None and seat["ends_on"] < today)


def _holder(seat: dict) -> str:
    return seat["login"] if seat["login"] is not None else f"agent {seat['agent']} ({seat['project']})"


def _seat_line(seat: dict, today: str) -> str:
    parts = [_holder(seat), seat["role"]]
    if seat["primary"]:
        parts.append("primary")
    if seat["reports_to"]:
        parts.append(f"reports to {seat['reports_to']}")
    if seat["sponsor"]:
        parts.append(f"sponsor {seat['sponsor']}")
    if seat["ends_on"]:
        parts.append(f"ends {seat['ends_on']}")
    if seat["ceiling"]:
        parts.append(f"ceiling {seat['ceiling']}")
    parts.append(f"effective {seat['effective']} from {seat['source']}")
    if _seat_ended(seat, today):
        parts.append(f"ended {(seat['ended_at'] or seat['ends_on'])[:10]}")
    return f"seat {seat['id']}: " + ", ".join(parts)


def _unit_line(unit: dict) -> str:
    parts = [unit["kind"]]
    if unit["name"] != unit["slug"]:
        parts.append(unit["name"])
    if unit["ends_on"]:
        parts.append(f"ends {unit['ends_on']}")
    parts.append(f"ceiling {unit['ceiling'] or '-'}")
    parts.append(f"effective {unit['effective']} from {unit['source']}")
    if unit["archived_at"]:
        parts.append(f"archived {unit['archived_at'][:10]}")
    return f"{unit['slug']} ({', '.join(parts)})"


def tree_lines(chart: dict, every: bool = False, today: str | None = None) -> list[str]:
    """The chart as indented lines: each unit below its parent, its seats below it; archived units and ended seats
    only with ``every``."""
    today = today or date.today().isoformat()
    below: dict[str | None, list[dict]] = {}
    for unit in chart["units"]:
        if every or unit["archived_at"] is None:
            below.setdefault(unit["parent"], []).append(unit)
    seats: dict[str, list[dict]] = {}
    for seat in chart["seats"]:
        if every or not _seat_ended(seat, today):
            seats.setdefault(seat["unit"], []).append(seat)
    lines: list[str] = []

    def walk(unit: dict, depth: int) -> None:
        lines.append("  " * depth + _unit_line(unit))
        lines.extend("  " * (depth + 1) + "- " + _seat_line(seat, today) for seat in seats.get(unit["slug"], []))
        for child in below.get(unit["slug"], []):
            walk(child, depth + 1)

    for root in below.get(None, []):
        walk(root, 0)
    return lines


def holder_line(chart: dict) -> str:
    """The last line of ``org show``: who the caller is and the level they act at."""
    viewer = chart["viewer"]
    role = "a hub admin" if viewer["admin"] else ("lead of " + ", ".join(viewer["leads"]) if viewer["leads"] else "")
    return (
        f"\nYou are {viewer['login']}{', ' + role if role else ''}: effective {viewer['effective']} "
        f"from {viewer['source']}."
    )


class OrgShow(HubCommand):
    name = "show"
    help = "the tree of units and seats, with ceilings and effective levels"
    arguments = (Arg("--all", action="store_true", help="archived units and ended seats too"),)
    output = returns_object(*CHART_KEYS, schema="OrgChartView")

    def run(self, ctx: HubContext) -> Result:
        chart = OrgChart(ctx.hub).chart()
        return Result(chart, Text("\n".join(tree_lines(chart, ctx.args.all)), holder_line(chart)))


# Units


def unit_body(args) -> dict:
    """The body of POST /v1/org/units for the command line ``args``."""
    body = {"slug": args.slug, "parent": args.parent, "kind": args.kind}
    for key, value in (("name", args.name), ("ends_on", _day(args.ends_on, "--ends-on"))):
        if value is not None:
            body[key] = value
    if args.level:
        body["level"] = args.level
    if args.note:
        body["note"] = args.note
    return body


def unit_changes(args) -> dict:
    """The body of PATCH /v1/org/units/{slug}: what the command line changes; UsageError when it changes nothing."""
    body: dict = {}
    if args.name is not None:
        body["name"] = args.name
    if args.parent is not None:
        body["parent"] = args.parent
    if args.clear_ends_on:
        body["ends_on"] = None
    elif args.ends_on is not None:
        body["ends_on"] = _day(args.ends_on, "--ends-on")
    if not body:
        raise UsageError("nothing to change: pass --name, --parent, --ends-on or --clear-ends-on")
    if args.if_revision is not None:
        body["if_revision"] = args.if_revision
    return body


class UnitCreate(HubCommand):
    name = "create"
    help = "a team or a cohort below a unit (admin, or a lead of a unit above)"
    arguments = (
        Arg("slug", metavar="SLUG", help="its name in commands: lowercase letters, digits and dashes"),
        Arg("--parent", required=True, metavar="SLUG", help="the unit above it"),
        Arg("--kind", required=True, choices=("team", "cohort"), help="a team, or a cohort with an end"),
        Arg("--name", help="its name as people read it (default: the slug)"),
        Arg("--ends-on", metavar="YYYY-MM-DD", help="a cohort's last day"),
        Arg("--level", choices=LEVELS, help="its ceiling (default: A1 for a cohort, none for a team)"),
        Arg("--note", help="why that ceiling, for its history"),
    )
    output = UNIT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        body = unit_body(ctx.args)  # a wrong day is refused before the hub is asked
        unit = OrgChart(ctx.hub).create_unit(body)
        return Result(unit, Text(f"Created {unit['kind']} {unit['slug']} below {unit['parent']}: {_unit_line(unit)}"))


class UnitEdit(HubCommand):
    name = "edit"
    help = "change a unit's name, parent or end date"
    arguments = (
        Arg("slug", metavar="SLUG", help="the unit"),
        Arg("--name", help="its new name"),
        Arg("--parent", metavar="SLUG", help="move it below this unit"),
    )
    output = UNIT_OUTPUT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        ends = parser.add_mutually_exclusive_group()
        ends.add_argument("--ends-on", metavar="YYYY-MM-DD", help="a cohort's new last day")
        ends.add_argument("--clear-ends-on", action="store_true", help="no last day")
        parser.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the unit is at revision N")

    def run(self, ctx: HubContext) -> Result:
        body = unit_changes(ctx.args)
        unit = OrgChart(ctx.hub).edit_unit(ctx.args.slug, body)
        said = f"Changed unit {unit['slug']}, now at revision {unit['revision']}: {_unit_line(unit)}"
        return Result(unit, Text(said))


class UnitArchive(HubCommand):
    name = "archive"
    help = "archive a unit with no unit below it and no live seat left"
    arguments = (Arg("slug", metavar="SLUG", help="the unit"),)
    output = UNIT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        unit = OrgChart(ctx.hub).archive_unit(ctx.args.slug)
        return Result(unit, Text(f"Archived unit {unit['slug']}."))


# Seats


def _person_or_agent(args) -> dict:
    if args.agent is not None:
        if args.login is not None:
            raise UsageError("name a person (LOGIN) or an agent (--agent), not both")
        if args.project is None:
            raise UsageError("--agent needs --project, the project of the agent profile")
        return {"agent": args.agent, "project": args.project}
    if args.project is not None:
        raise UsageError("--project goes with --agent")
    if args.login is None:
        raise UsageError("name a person (LOGIN) or an agent (--agent HANDLE --project P)")
    return {"login": args.login}


def find_seat(chart: dict, args) -> dict:
    """The seat the command line names: ``--seat``, or the live seat of a login or an agent profile, in ``--unit``
    when given, else the primary one."""
    if args.seat is not None:
        if args.login is not None or args.agent is not None:
            raise UsageError("name the seat by --seat or by its holder, not both")
        for seat in chart["seats"]:
            if seat["id"] == args.seat:
                return seat
        raise HubError(f"the org chart has no seat {args.seat}", 404)
    who = _person_or_agent(args)
    live = [
        seat
        for seat in chart["seats"]
        if seat["ended_at"] is None and all(seat[key] == value for key, value in who.items())
    ]
    holder = who.get("login") or f"agent {who.get('agent')} of project {who.get('project')}"
    if args.unit is not None:
        live = [seat for seat in live if seat["unit"] == args.unit]
        where = f" in unit {args.unit}"
    else:
        live = [seat for seat in live if seat["primary"]] or live
        where = ""
    if not live:
        raise HubError(f"{holder} holds no live seat{where}", 404)
    if len(live) > 1:
        ids = ", ".join(str(seat["id"]) for seat in live)
        raise HubError(f"{holder} holds live seats {ids}{where}; pass --unit or --seat", 409)
    return live[0]


def seat_body(args) -> dict:
    """The body of POST /v1/org/seats for the command line ``args``."""
    body = {**_person_or_agent(args), "unit": args.unit, "role": args.role}
    dates = (("starts_on", _day(args.starts_on, "--starts-on")), ("ends_on", _day(args.ends_on, "--ends-on")))
    for key, value in (("reports_to", args.reports_to), ("sponsor", args.sponsor), *dates):
        if value is not None:
            body[key] = value
    if args.primary:
        body["primary"] = True
    return body


def seat_changes(args) -> dict:
    """The body of PATCH /v1/org/seats/{id}: what the command line changes; UsageError when it changes nothing."""
    body: dict = {}
    for key, value in (("role", args.role), ("sponsor", args.sponsor)):
        if value is not None:
            body[key] = value
    if args.primary:
        body["primary"] = True
    if args.clear_reports_to:
        body["reports_to"] = None
    elif args.reports_to is not None:
        body["reports_to"] = args.reports_to
    if args.starts_on is not None:
        body["starts_on"] = _day(args.starts_on, "--starts-on")
    if args.clear_ends_on:
        body["ends_on"] = None
    elif args.ends_on is not None:
        body["ends_on"] = _day(args.ends_on, "--ends-on")
    if not body:
        raise UsageError(
            "nothing to change: pass --role, --primary, --reports-to, --clear-reports-to, --sponsor, --starts-on, "
            "--ends-on or --clear-ends-on"
        )
    return body


def holder_args(required_unit: bool) -> tuple[Arg, ...]:
    """The options that name the holder of a seat."""
    help_unit = "the unit of the seat" if required_unit else "the unit of the live seat meant (default: the primary)"
    return (
        Arg("login", nargs="?", metavar="LOGIN", help="the person holding the seat"),
        Arg("--agent", metavar="HANDLE", help="the agent profile holding the seat, instead of a person"),
        Arg("--project", help="the project of the agent profile (with --agent)"),
        Arg("--unit", required=required_unit, metavar="SLUG", help=help_unit),
    )


def seat_field_args(editing: bool) -> tuple[Arg, ...]:
    """The options that set the fields of a seat; ``editing`` adds those that clear one."""
    fields = (
        Arg("--role", choices=ROLES, required=not editing, help="the role of the seat"),
        Arg("--reports-to", metavar="LOGIN", help="the person the seat reports to (an intern needs one)"),
        Arg("--sponsor", metavar="LOGIN", help="the person answering for an agent's seat"),
        Arg("--primary", action="store_true", help="make it the seat that decides the person's ceiling"),
        Arg("--starts-on", metavar="YYYY-MM-DD", help="its first day (default: today)"),
        Arg("--ends-on", metavar="YYYY-MM-DD", help="its last day"),
    )
    if not editing:
        return fields
    return (
        *fields,
        Arg("--clear-reports-to", action="store_true", help="report to nobody"),
        Arg("--clear-ends-on", action="store_true", help="no last day"),
    )


SEAT_ID = Arg("--seat", type=int, metavar="ID", help="the seat by its id, instead of by its holder")


class SeatAdd(HubCommand):
    name = "add"
    help = "a seat for a person (LOGIN) or an agent profile (--agent, --project)"
    arguments = (*holder_args(required_unit=True), *seat_field_args(editing=False))
    output = SEAT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        body = seat_body(ctx.args)
        seat = OrgChart(ctx.hub).add_seat(body)
        today = date.today().isoformat()
        return Result(seat, Text(f"Added to unit {seat['unit']} {_seat_line(seat, today)}"))


class SeatEdit(HubCommand):
    name = "edit"
    help = "change a live seat's role, reports_to, sponsor or dates, or make it primary"
    arguments = (SEAT_ID, *holder_args(required_unit=False), *seat_field_args(editing=True))
    output = SEAT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        body = seat_changes(ctx.args)
        chart = OrgChart(ctx.hub)
        changed = chart.edit_seat(find_seat(chart.chart(), ctx.args)["id"], body)
        today = date.today().isoformat()
        return Result(changed, Text(f"Changed {_seat_line(changed, today)}"))


class SeatEnd(HubCommand):
    name = "end"
    help = "end a seat now"
    arguments = (SEAT_ID, *holder_args(required_unit=False))
    output = SEAT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        chart = OrgChart(ctx.hub)
        ended = chart.end_seat(find_seat(chart.chart(), ctx.args)["id"])
        return Result(ended, Text(f"Ended seat {ended['id']} of {_holder(ended)} in unit {ended['unit']}."))


UNIT = Group(
    "unit",
    "create, change and archive the units of the org chart",
    [UnitCreate(), UnitEdit(), UnitArchive()],
    dest="org_unit_command",
)
SEAT = Group(
    "seat",
    "add, change and end the seats of people and agents in the org chart",
    [SeatAdd(), SeatEdit(), SeatEnd()],
    dest="org_seat_command",
)
ORG = Group(
    "org",
    "the org chart: units, the seats of people and agents, and their ceilings",
    [OrgShow(), UNIT, SEAT, WORKER],
)
