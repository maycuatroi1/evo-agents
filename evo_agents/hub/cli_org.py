"""``evo-agents hub org show|unit|seat|worker`` and ``evo-agents hub autonomy ceiling set``: the org chart of the hub (plan
org-chart, criterion a4), over the routes under /v1/org (``evo_agents.hub.server.org``).

``org show`` prints the tree of units, indented below the root, each with its kind, its ceiling and the effective
level with the unit that sets it, and below each unit its seats: the person (or the agent profile and its project),
the role, whether the seat is primary, reports_to, sponsor, the end date and the effective level with its source.
Archived units and ended seats are left out unless ``--all``. ``--json`` prints GET /v1/org as the hub answered it.

``org unit create|edit|archive`` change a unit, ``org seat add|edit|end`` a seat, and ``autonomy ceiling set`` writes
the next ceiling revision of a unit, of a person's primary seat or of one seat. A seat is named by its id
(``--seat``), by a login, or by ``--agent HANDLE --project P``; with a login, ``--unit`` picks the live seat in that
unit, else the person's live primary seat is meant. What a member may change is the hub's to decide: a refusal
(403, 404, 409, 422) prints the hub's reason and exits 1. ``hub autonomy`` is a group the autonomy levels grow into.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from datetime import date
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.cli_org_workers import register_workers
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object

EXIT_USAGE = 2
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
CEILING_KEYS = ("scope", "target", "revision", "level", "previous", "note")


class Usage(Exception):
    """A command line the hub is not asked about; exit 2 with this sentence."""


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _day(value: str | None, flag: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise Usage(f"{flag} takes a day as YYYY-MM-DD, not {value!r}") from None


def _print(answer: dict, args, sentence: str) -> int:
    if args.json:
        print_json(answer)
    else:
        print(sentence)
    return 0


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


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    chart = hub.call("GET", "/v1/org")
    if args.json:
        print_json(chart)
        return 0
    print("\n".join(tree_lines(chart, args.all)))
    viewer = chart["viewer"]
    role = "a hub admin" if viewer["admin"] else ("lead of " + ", ".join(viewer["leads"]) if viewer["leads"] else "")
    print(
        f"\nYou are {viewer['login']}{', ' + role if role else ''}: effective {viewer['effective']} "
        f"from {viewer['source']}."
    )
    return 0


# Units


def _unit_path(slug: str, rest: str = "") -> str:
    return f"/v1/org/units/{quote(slug, safe='')}{rest}"


@client_command
def cmd_unit_create(args) -> int:
    try:
        body = {"slug": args.slug, "parent": args.parent, "kind": args.kind}
        for key, value in (("name", args.name), ("ends_on", _day(args.ends_on, "--ends-on"))):
            if value is not None:
                body[key] = value
        if args.level:
            body["level"] = args.level
        if args.note:
            body["note"] = args.note
    except Usage as exc:
        return _usage(str(exc))
    hub, _ = signed_in()
    unit = hub.call("POST", "/v1/org/units", body)
    return _print(unit, args, f"Created {unit['kind']} {unit['slug']} below {unit['parent']}: {_unit_line(unit)}")


@client_command
def cmd_unit_edit(args) -> int:
    try:
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
            raise Usage("nothing to change: pass --name, --parent, --ends-on or --clear-ends-on")
        if args.if_revision is not None:
            body["if_revision"] = args.if_revision
    except Usage as exc:
        return _usage(str(exc))
    hub, _ = signed_in()
    unit = hub.call("PATCH", _unit_path(args.slug), body)
    return _print(unit, args, f"Changed unit {unit['slug']}, now at revision {unit['revision']}: {_unit_line(unit)}")


@client_command
def cmd_unit_archive(args) -> int:
    hub, _ = signed_in()
    unit = hub.call("POST", _unit_path(args.slug, "/archive"))
    return _print(unit, args, f"Archived unit {unit['slug']}.")


# Seats


def _person_or_agent(args) -> dict:
    if args.agent is not None:
        if args.login is not None:
            raise Usage("name a person (LOGIN) or an agent (--agent), not both")
        if args.project is None:
            raise Usage("--agent needs --project, the project of the agent profile")
        return {"agent": args.agent, "project": args.project}
    if args.project is not None:
        raise Usage("--project goes with --agent")
    if args.login is None:
        raise Usage("name a person (LOGIN) or an agent (--agent HANDLE --project P)")
    return {"login": args.login}


def find_seat(chart: dict, args) -> dict:
    """The seat the command line names: ``--seat``, or the live seat of a login or an agent profile, in ``--unit``
    when given, else the primary one."""
    if args.seat is not None:
        if args.login is not None or args.agent is not None:
            raise Usage("name the seat by --seat or by its holder, not both")
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


@client_command
def cmd_seat_add(args) -> int:
    try:
        body = {**_person_or_agent(args), "unit": args.unit, "role": args.role}
        dates = (("starts_on", _day(args.starts_on, "--starts-on")), ("ends_on", _day(args.ends_on, "--ends-on")))
        for key, value in (("reports_to", args.reports_to), ("sponsor", args.sponsor), *dates):
            if value is not None:
                body[key] = value
        if args.primary:
            body["primary"] = True
    except Usage as exc:
        return _usage(str(exc))
    hub, _ = signed_in()
    seat = hub.call("POST", "/v1/org/seats", body)
    today = date.today().isoformat()
    return _print(seat, args, f"Added to unit {seat['unit']} {_seat_line(seat, today)}")


def _seat_changes(args) -> dict:
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
        raise Usage(
            "nothing to change: pass --role, --primary, --reports-to, --clear-reports-to, --sponsor, --starts-on, "
            "--ends-on or --clear-ends-on"
        )
    return body


@client_command
def cmd_seat_edit(args) -> int:
    try:
        body = _seat_changes(args)
        hub, _ = signed_in()
        seat = find_seat(hub.call("GET", "/v1/org"), args)
    except Usage as exc:
        return _usage(str(exc))
    changed = hub.call("PATCH", f"/v1/org/seats/{seat['id']}", body)
    today = date.today().isoformat()
    return _print(changed, args, f"Changed {_seat_line(changed, today)}")


@client_command
def cmd_seat_end(args) -> int:
    try:
        hub, _ = signed_in()
        seat = find_seat(hub.call("GET", "/v1/org"), args)
    except Usage as exc:
        return _usage(str(exc))
    ended = hub.call("POST", f"/v1/org/seats/{seat['id']}/end")
    return _print(ended, args, f"Ended seat {ended['id']} of {_holder(ended)} in unit {ended['unit']}.")


# Ceilings


@client_command
def cmd_ceiling_set(args) -> int:
    body: dict = {"level": args.level, "note": args.note}
    if args.unit is not None:
        body["unit"] = args.unit
    elif args.login is not None:
        body["login"] = args.login
    else:
        body["seat"] = args.seat
    hub, _ = signed_in()
    written = hub.call("PUT", "/v1/org/ceilings", body)
    before = written["previous"] or "none"
    sentence = f"Ceiling of {written['target']} is {written['level']} (was {before}), revision {written['revision']}."
    return _print(written, args, sentence)


# Registering


def _holder_options(parser, required_unit: bool) -> None:
    parser.add_argument("login", nargs="?", metavar="LOGIN", help="the person holding the seat")
    parser.add_argument("--agent", metavar="HANDLE", help="the agent profile holding the seat, instead of a person")
    parser.add_argument("--project", help="the project of the agent profile (with --agent)")
    help_unit = "the unit of the seat" if required_unit else "the unit of the live seat meant (default: the primary)"
    parser.add_argument("--unit", required=required_unit, metavar="SLUG", help=help_unit)


def _seat_fields(parser, editing: bool) -> None:
    parser.add_argument("--role", choices=ROLES, required=not editing, help="the role of the seat")
    parser.add_argument("--reports-to", metavar="LOGIN", help="the person the seat reports to (an intern needs one)")
    parser.add_argument("--sponsor", metavar="LOGIN", help="the person answering for an agent's seat")
    parser.add_argument("--primary", action="store_true", help="make it the seat that decides the person's ceiling")
    parser.add_argument("--starts-on", metavar="YYYY-MM-DD", help="its first day (default: today)")
    parser.add_argument("--ends-on", metavar="YYYY-MM-DD", help="its last day")
    if editing:
        parser.add_argument("--clear-reports-to", action="store_true", help="report to nobody")
        parser.add_argument("--clear-ends-on", action="store_true", help="no last day")


def _register_units(osub) -> None:
    unit = osub.add_parser("unit", help="create, change and archive the units of the org chart")
    usub = unit.add_subparsers(dest="org_unit_command", required=True)

    create = usub.add_parser("create", help="a team or a cohort below a unit (admin, or a lead of a unit above)")
    create.add_argument("slug", metavar="SLUG", help="its name in commands: lowercase letters, digits and dashes")
    create.add_argument("--parent", required=True, metavar="SLUG", help="the unit above it")
    create.add_argument("--kind", required=True, choices=("team", "cohort"), help="a team, or a cohort with an end")
    create.add_argument("--name", help="its name as people read it (default: the slug)")
    create.add_argument("--ends-on", metavar="YYYY-MM-DD", help="a cohort's last day")
    create.add_argument("--level", choices=LEVELS, help="its ceiling (default: A1 for a cohort, none for a team)")
    create.add_argument("--note", help="why that ceiling, for its history")
    json_option(create, returns_object(*UNIT_KEYS, schema="OrgUnitView"))
    create.set_defaults(func=cmd_unit_create)

    edit = usub.add_parser("edit", help="change a unit's name, parent or end date")
    edit.add_argument("slug", metavar="SLUG", help="the unit")
    edit.add_argument("--name", help="its new name")
    edit.add_argument("--parent", metavar="SLUG", help="move it below this unit")
    ends = edit.add_mutually_exclusive_group()
    ends.add_argument("--ends-on", metavar="YYYY-MM-DD", help="a cohort's new last day")
    ends.add_argument("--clear-ends-on", action="store_true", help="no last day")
    edit.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the unit is at revision N")
    json_option(edit, returns_object(*UNIT_KEYS, schema="OrgUnitView"))
    edit.set_defaults(func=cmd_unit_edit)

    archive = usub.add_parser("archive", help="archive a unit with no unit below it and no live seat left")
    archive.add_argument("slug", metavar="SLUG", help="the unit")
    json_option(archive, returns_object(*UNIT_KEYS, schema="OrgUnitView"))
    archive.set_defaults(func=cmd_unit_archive)


def _register_seats(osub) -> None:
    seat = osub.add_parser("seat", help="add, change and end the seats of people and agents in the org chart")
    ssub = seat.add_subparsers(dest="org_seat_command", required=True)

    add = ssub.add_parser("add", help="a seat for a person (LOGIN) or an agent profile (--agent, --project)")
    _holder_options(add, required_unit=True)
    _seat_fields(add, editing=False)
    json_option(add, returns_object(*SEAT_KEYS, schema="OrgSeatView"))
    add.set_defaults(func=cmd_seat_add)

    edit = ssub.add_parser("edit", help="change a live seat's role, reports_to, sponsor or dates, or make it primary")
    edit.add_argument("--seat", type=int, metavar="ID", help="the seat by its id, instead of by its holder")
    _holder_options(edit, required_unit=False)
    _seat_fields(edit, editing=True)
    json_option(edit, returns_object(*SEAT_KEYS, schema="OrgSeatView"))
    edit.set_defaults(func=cmd_seat_edit)

    end = ssub.add_parser("end", help="end a seat now")
    end.add_argument("--seat", type=int, metavar="ID", help="the seat by its id, instead of by its holder")
    _holder_options(end, required_unit=False)
    json_option(end, returns_object(*SEAT_KEYS, schema="OrgSeatView"))
    end.set_defaults(func=cmd_seat_end)


def register_org(hsub) -> None:
    group = hsub.add_parser("org", help="the org chart: units, the seats of people and agents, and their ceilings")
    osub = group.add_subparsers(dest="org_command", required=True)

    show = osub.add_parser("show", help="the tree of units and seats, with ceilings and effective levels")
    show.add_argument("--all", action="store_true", help="archived units and ended seats too")
    json_option(show, returns_object(*CHART_KEYS, schema="OrgChartView"))
    show.set_defaults(func=cmd_show)

    _register_units(osub)
    _register_seats(osub)
    register_workers(osub)


def register_autonomy(hsub) -> None:
    group = hsub.add_parser("autonomy", help="the autonomy levels A0 to A5 of people and agents")
    asub = group.add_subparsers(dest="autonomy_command", required=True)
    ceiling = asub.add_parser("ceiling", help="the ceilings the org chart sets on autonomy levels")
    csub = ceiling.add_subparsers(dest="autonomy_ceiling_command", required=True)

    written = csub.add_parser("set", help="the next ceiling revision of a unit, a person's primary seat or a seat")
    scope = written.add_mutually_exclusive_group(required=True)
    scope.add_argument("--unit", metavar="SLUG", help="the ceiling of a unit, which every unit below it keeps to")
    scope.add_argument("--login", help="the ceiling of a person's primary seat")
    scope.add_argument("--seat", type=int, metavar="ID", help="the ceiling of one seat, such as an agent's")
    written.add_argument("--level", required=True, choices=LEVELS, help="the highest level allowed")
    written.add_argument("--note", required=True, help="why, for the history (at most 1000 characters)")
    json_option(written, returns_object(*CEILING_KEYS, schema="CeilingWritten"))
    written.set_defaults(func=cmd_ceiling_set)
