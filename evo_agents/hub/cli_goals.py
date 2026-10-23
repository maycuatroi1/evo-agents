"""``evo-agents hub goal list|show|set|close``: the goals of a project (``evo_agents.hub.goals``), over GET and PUT
/v1/projects/{p}/goals and the routes below it.

``list`` prints the project's goals by rank, those of one ``--status`` alone when given. ``show REF`` prints a goal, by
its code (G-<number>) or alias, at its newest revision or at ``--revision``, with its revisions. ``set`` writes a goal's
next revision: without REF it makes a new goal under the project's next code, which needs ``--statement``; with REF it
changes the options given and keeps the rest, at the revision the goal is at when it is read unless ``--if-revision``
says another. A metric is ``--metric KEY`` with ``--aggregate``, ``--comparator`` and ``--target``, and
``--window-days`` and ``--tenant`` optional; on a goal that has one, an option given replaces its part alone.
``--problem P-<number>`` names the problem of the project the goal answers (``evo-agents hub problem``), one that is
not dropped. ``--no-metric``, ``--no-deadline``, ``--no-canvas`` and ``--no-problem`` clear those. ``close REF
--reason`` closes a goal: achieved, dropped or expired.

Every member of the project reads its goals; writing one needs the admin role on the project. Every command finds its
project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub import goals
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.mirror import harness_project, harness_root

EXIT_USAGE = 2
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
SHOWN_STATEMENT_CHARS = 60  # of a goal's statement in a row of list
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
GOAL_KEYS = (
    "project",
    "code",
    "number",
    "alias",
    "revision",
    "statement",
    "metric",
    "deadline",
    "canvas",
    "problem",
    "rank",
    "owner",
    "status",
    "close_reason",
    "note",
    "created_by",
    "created_at",
)
METRIC_OPTIONS = {  # the part of a metric each option sets
    "metric": "key",
    "aggregate": "aggregate",
    "window_days": "window_days",
    "comparator": "comparator",
    "target": "target",
    "tenant": "tenant",
}
NEEDED = ("metric", "aggregate", "comparator", "target")  # the options a metric cannot go without
WRITTEN_FIELDS = ("statement", "metric", "deadline", "canvas", "problem", "rank", "owner", "status", "close_reason")
GIVEN_FIELDS = ("statement", "deadline", "canvas", "problem", "rank", "owner", "status")  # one field an option
CLEARED_FIELDS = ("deadline", "canvas", "problem")  # each with its --no-<field>


def _project(args) -> str:
    if args.project:
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/goals{rest}"


def _ref(ref: str) -> str:
    return "/" + quote(ref, safe="")


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


# Reading


def _clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


def _status(goal: dict) -> str:
    return goal["status"] + (f" ({goal['close_reason']})" if goal.get("close_reason") else "")


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    found = hub.call("GET", _path(project, f"?status={args.status}" if args.status else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no goals{' ' + args.status if args.status else ''}.")
        return 0
    rows = [
        (
            goal["code"],
            _status(goal),
            goal["rank"],
            goal["owner"],
            goal["deadline"] or "-",
            goals.metric_text(goal["metric"]),
            _clip(goal["statement"], SHOWN_STATEMENT_CHARS),
        )
        for goal in found
    ]
    table(("CODE", "STATUS", "RANK", "OWNER", "DEADLINE", "METRIC", "STATEMENT"), rows)
    return 0


def _show(goal: dict, revisions: list[dict]) -> None:
    alias = f" ({goal['alias']})" if goal.get("alias") else ""
    print(f"Goal {goal['code']}{alias} of project {goal['project']}, revision {goal['revision']}")
    print(f"  statement: {goal['statement']}")
    print(f"  metric: {goals.metric_text(goal['metric'])}")
    for name in ("deadline", "canvas", "problem", "rank", "owner"):
        value = goal[name]
        print(f"  {name}: {'-' if value is None else value}")
    print(f"  status: {_status(goal)}")
    written = f"  written by {goal['created_by']} at {when(goal['created_at'])} UTC"
    print(written + (f": {goal['note']}" if goal.get("note") else ""))
    if revisions:
        print("Revisions:")
        rows = [
            (item["revision"], when(item["created_at"]), item["created_by"], ", ".join(item["changed"]) or "-")
            for item in revisions
        ]
        table(("REVISION", "WRITTEN (UTC)", "BY", "CHANGED"), rows)


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    found = hub.call(
        "GET", _path(project, _ref(args.ref) + ("" if args.revision is None else f"?revision={args.revision}"))
    )
    if args.json:
        print_json(found)
        return 0
    _show(found, hub.call("GET", _path(project, _ref(args.ref) + "/revisions")))
    return 0


# Writing


def _metric(args, held: dict | None) -> tuple[dict | None, str | None]:
    """The metric the options make of ``held`` (the goal's now, if any); (None, why) when they make none."""
    given = {part: value for option, part in METRIC_OPTIONS.items() if (value := getattr(args, option)) is not None}
    if args.no_metric:
        return None, None
    if not given:
        return held, None
    metric = {**(held or {}), **given}
    missing = [f"--{option.replace('_', '-')}" for option in NEEDED if METRIC_OPTIONS[option] not in metric]
    return (None, f"a metric needs {', '.join(missing)}") if missing else (metric, None)


def _body(args, held: dict) -> tuple[dict | None, str | None]:
    """The goal to write: ``held`` (its fields now, {} for a new goal) with the options given; (None, why) when the
    options do not make one. A field left out of the body is empty, or kept for its rank and owner."""
    metric, why = _metric(args, held.get("metric"))
    if why:
        return None, why
    body = {name: held.get(name) for name in WRITTEN_FIELDS} | {"metric": metric}
    body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
    body |= {name: None for name in CLEARED_FIELDS if getattr(args, f"no_{name}")}
    if not body["statement"]:
        return None, "a new goal needs --statement"
    if args.status is not None:
        body["close_reason"] = None  # --status is active or paused: a closed goal opens again
    body["status"] = body["status"] or goals.ACTIVE
    body["note"] = args.note
    return {name: value for name, value in body.items() if value is not None}, None


def _conflict(args) -> str | None:
    """Why options given together say two things; None when they do not."""
    if any(getattr(args, f"no_{name}") and getattr(args, name) for name in CLEARED_FIELDS):
        return "--no-deadline, --no-canvas and --no-problem each go without the option they clear"
    if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
        return "--no-metric goes without the options of a metric"
    return None


def _next_code(hub, project: str) -> str:
    numbers = [goal["number"] for goal in hub.call("GET", _path(project))]
    return goals.code(max(numbers, default=0) + 1)


def _said(written: dict, project: str, made: str) -> None:
    if not written["changed"]:
        print(
            f"Goal {written['code']} of project {project} is this already, at revision {written['revision']}: "
            "nothing written."
        )
    else:
        print(f"{made} goal {written['code']} of project {project}: revision {written['revision']}.")


@client_command
def cmd_set(args) -> int:
    why = _conflict(args)
    if why:
        return _usage(why)
    hub, _ = signed_in()
    project = _project(args)
    held, revision, ref = {}, 0, args.ref
    if ref is not None:
        try:
            held = hub.call("GET", _path(project, _ref(ref)))
        except HubError as exc:
            if exc.status != 404:
                raise
        else:
            revision, ref = held["revision"], held["code"]
    body, why = _body(args, held)
    if why:
        return _usage(why)
    ref = ref or _next_code(hub, project)
    body["if_revision"] = revision if args.if_revision is None else args.if_revision
    written = hub.call("PUT", _path(project, _ref(ref)), body)
    if args.json:
        print_json(written)
        return 0
    _said(written, project, "Made" if written["revision"] == 1 else "Wrote")
    return 0


@client_command
def cmd_close(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    body = {"reason": args.reason}
    if args.note:
        body["note"] = args.note
    if args.if_revision is not None:
        body["if_revision"] = args.if_revision
    written = hub.call("POST", _path(project, _ref(args.ref) + "/close"), body)
    if args.json:
        print_json(written)
        return 0
    print(f"Closed goal {written['code']} of project {project} ({args.reason}): revision {written['revision']}.")
    return 0


# Registering


def _write_options(parser) -> None:
    parser.add_argument("--statement", help="what the project is for, in one statement (at most 2000 characters)")
    parser.add_argument("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals")
    parser.add_argument("--aggregate", choices=goals.AGGREGATES, help="how the key's signals are summed up")
    parser.add_argument("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)")
    parser.add_argument("--comparator", choices=goals.COMPARATORS, help="how the value compares with the target")
    parser.add_argument("--target", type=float, metavar="X", help="the value the goal reaches")
    parser.add_argument("--tenant", help="the signals of this tenant alone")
    parser.add_argument("--no-metric", action="store_true", help="clear the metric")
    parser.add_argument("--deadline", metavar="YYYY-MM-DD", help="when the goal is due")
    parser.add_argument("--no-deadline", action="store_true", help="clear the deadline")
    parser.add_argument("--canvas", metavar="SLUG", help="the canvas whose product the goal drives")
    parser.add_argument("--no-canvas", action="store_true", help="clear the canvas")
    parser.add_argument("--problem", metavar="P-N", help="the problem of the project the goal answers, not dropped")
    parser.add_argument("--no-problem", action="store_true", help="clear the problem")
    parser.add_argument("--rank", type=int, metavar="N", help="its place among the goals, 1 first")
    parser.add_argument("--owner", metavar="LOGIN", help="a member with the writer role or above")
    parser.add_argument("--status", choices=(goals.ACTIVE, goals.PAUSED), help="active or paused")


def register_goals(hsub) -> None:
    goal = hsub.add_parser("goal", help="the goals of a project: a statement, a metric and a target")
    gsub = goal.add_subparsers(dest="goal_command", required=True)

    listed = gsub.add_parser("list", help="the project's goals by rank")
    listed.add_argument("--status", choices=goals.STATUSES, help="those of this status alone")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*GOAL_KEYS, schema="ProjectGoal"))
    listed.set_defaults(func=cmd_list)

    shown = gsub.add_parser("show", help="a goal, with its revisions")
    shown.add_argument("ref", metavar="REF", help="its code (G-<number>) or alias")
    shown.add_argument("--revision", type=int, metavar="N", help="an older revision (default: the newest)")
    shown.add_argument("--project", help=PROJECT_HELP)
    json_option(shown, returns_object(*GOAL_KEYS, schema="ProjectGoal"))
    shown.set_defaults(func=cmd_show)

    written = gsub.add_parser(
        "set", help="make a goal, or write a goal's next revision (the admin role on the project)"
    )
    written.add_argument("ref", metavar="REF", nargs="?", help="the goal's code or alias; none: a new goal")
    _write_options(written)
    written.add_argument("--note", help="why, for the history (at most 1000 characters)")
    written.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the goal is at revision N")
    written.add_argument("--project", help=PROJECT_HELP)
    json_option(written, returns_object(*GOAL_KEYS, "changed", schema="ProjectGoalWritten"))
    written.set_defaults(func=cmd_set)

    closed = gsub.add_parser("close", help="close a goal for a reason (the admin role on the project)")
    closed.add_argument("ref", metavar="REF", help="its code (G-<number>) or alias")
    closed.add_argument("--reason", required=True, choices=goals.CLOSE_REASONS, help="why it is closed")
    closed.add_argument("--note", help="why, for the history (at most 1000 characters)")
    closed.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the goal is at revision N")
    closed.add_argument("--project", help=PROJECT_HELP)
    json_option(closed, returns_object(*GOAL_KEYS, "changed", schema="ProjectGoalWritten"))
    closed.set_defaults(func=cmd_close)
